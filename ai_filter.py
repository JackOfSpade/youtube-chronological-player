"""
ai_filter.py — Gemini-powered video relevance filtering and deduplication.

Uses transcript analysis and web-grounded bias assessment to select the best
representative video when multiple channels cover the same news story, and discards all videos unrelated to stock trading and financial markets.
"""

import os
import json
import re
import logging
import concurrent.futures
import functools
import time
import random

from dotenv import load_dotenv
from google import genai
from google.genai import types
from youtube_transcript_api import YouTubeTranscriptApi

logger = logging.getLogger(__name__)

load_dotenv()

# Vertex AI auth via service account key
_SA_KEY_PATH = os.path.join(os.path.dirname(__file__), 'service-account.json')
if os.path.exists(_SA_KEY_PATH):
    os.environ.setdefault('GOOGLE_APPLICATION_CREDENTIALS', _SA_KEY_PATH)

_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "video-gen-492111")
_LOCATION = os.getenv("GCP_LOCATION", "us-central1")

# Resolve GEMINI_API_KEY from .env
gemini_api_key = os.getenv("GEMINI_API_KEY")

try:
    if gemini_api_key:
        logger.info("Using standard Google AI API with GEMINI_API_KEY")
        client = genai.Client(api_key=gemini_api_key)
        _USE_VERTEX = False
    else:
        logger.info("Using Vertex AI with service account: %s", _PROJECT_ID)
        client = genai.Client(vertexai=True, project=_PROJECT_ID, location=_LOCATION)
        _USE_VERTEX = True
except Exception:
    logger.exception("Failed to initialize Gemini client")
    client = None
    _USE_VERTEX = False


# Default model names to try if 404 occurs
_MODELS_TO_TRY = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.5-flash-001", "gemini-1.5-flash-002"]
if os.getenv("GEMINI_MODEL"):
    _MODELS_TO_TRY.insert(0, os.getenv("GEMINI_MODEL"))


@functools.lru_cache(maxsize=2000)
def get_video_transcript(video_id):
    """Fetch the first ~1000 chars of an English transcript, or a placeholder."""
    if not video_id or video_id == 'unknown' or not isinstance(video_id, (str, int, float)):
        return "<No transcript available>"
    video_id = str(video_id)
    if len(video_id) > 100:
        return "<No transcript available>"
    
    try:
        # Add a small jittered sleep to avoid aggressive rate limiting on the transcript API
        time.sleep(random.uniform(0.1, 0.5))
        
        # Retry with exponential backoff on TooManyRequests or similar transient errors
        max_retries = 3
        for attempt in range(max_retries):
            try:
                transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
                try:
                    transcript = transcript_list.find_transcript(['en', 'en-US', 'en-GB'])
                except Exception:
                    transcript = next(iter(transcript_list))
                data = transcript.fetch()
                break # Success
            except Exception as e:
                err_type = type(e).__name__
                if "TooManyRequests" in err_type or "TranscriptsDisabled" not in err_type:
                    if attempt < max_retries - 1:
                        sleep_time = (2 ** attempt) + random.uniform(0.1, 1.0)
                        logger.debug("Transcript API transient error for %s (%s). Retrying in %.2fs", video_id, err_type, sleep_time)
                        time.sleep(sleep_time)
                        continue
                raise e # Re-raise if retries exhausted or it's a fatal error
                
        try:
            text = " ".join([str(t.get('text', '')) for t in data if isinstance(t, dict)])
        except (TypeError, ValueError):
            text = ""
            
        # We cap the returned transcript text to 1000 chars to avoid massive context sizes.
        return text[:1000] if text else "<No transcript available (empty)>"
    except Exception as e:
        err_type = type(e).__name__
        if "YouTubeTranscriptApi" in err_type or "TranscriptsDisabled" in err_type or "NoTranscriptFound" in err_type:
             logger.debug("Transcript API error for %s: %s", video_id, err_type)
        else:
             logger.debug("Transcript fetch failed for %s: %s", video_id, e)
        return f"<No transcript available: {err_type}>"


def _extract_json_from_text(text):
    """Safely extract JSON from Gemini responses."""
    if not text:
        return "{}"
        
    text = text.strip()
    
    # Try to extract from a markdown code block first
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        text = match.group(1).strip()
        
    # Find the bounds of the outermost JSON object
    start_idx = text.find('{')
    end_idx = text.rfind('}')
    
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        parsed_str = text[start_idx:end_idx+1]
        try:
            # Check if it's actually parseable before returning
            json.loads(parsed_str)
            return parsed_str
        except json.JSONDecodeError:
            # If not parseable, try to fix common issues like missing commas or trailing commas
            # (Very basic fix for common LLM hiccups)
            try:
                # Remove trailing commas before a closing brace/bracket
                fixed_str = re.sub(r',\s*([\]}])', r'\1', parsed_str)
                json.loads(fixed_str)
                return fixed_str
            except Exception:
                pass
            
    return text



def filter_videos(videos, check_abortion=None):
    """
    Filter videos for market relevance and deduplicate using Gemini with search grounding.

    Returns a generator yielding progress dicts and a final 'result' dict, e.g.:
    {"type": "progress", "message": "..."}
    {"type": "result", "videos": [...]}
    """
    if check_abortion and not check_abortion():
        return
        
    yield {"type": "progress", "message": "AI Initialization..."}

    if not client:
        logger.warning("No Gemini API key or invalid client, skipping AI filter.")
        yield {"type": "result", "videos": videos}
        return

    if not videos:
        yield {"type": "result", "videos": []}
        return

    prompt = """
    You are given a list of recently published YouTube videos from various channels.
    Your task is to analyze these videos and filter them according to TWO main criteria:
    
    CRITERION A: STOCK MARKET RELEVANCE (Removal)
    Think like a stock trader. Remove any video that would NOT plausibly move any stock,
    sector, commodity, currency, or financial market. Use your own broad judgment —
    this includes direct company news, but also geopolitics, macroeconomics, technology
    breakthroughs, policy changes, or anything else a trader would care about.
    Remove things like local crime, car crashes, celebrity gossip, sports scores, or
    human interest stories that have no economic angle.
    
    If a video's transcript_snippet is unavailable, judge relevance from its TITLE
    and CHANNEL NAME alone, giving news channels the benefit of the doubt.
    
    CRITERION B: DEDUPLICATION (Selection)
    For the videos that pass Criterion A, identify those covering the EXACT SAME story
    and keep only one per story:
    1. Prefer videos with structured narration over raw footage.
    2. Break ties by keeping the most neutral/unbiased channel (use Google Search to verify).
    3. Keep all unique stories.
    
    Return a JSON object: {"video_ids": ["id1", "id2", ...]}.
    Include every ID that survives both filtering and dedup.
    If NONE are market-relevant, return {"video_ids": []}.
    The array MUST contain literal strings, not numbers or booleans.
    """

    video_metadata = []

    # Limit max workers to 15 to avoid YouTube throwing aggressive rate limits on transcript API
    max_workers = min(15, len(videos))

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    future_to_video = {}
    try:
        future_to_video = {executor.submit(get_video_transcript, v.get('id') or 'unknown'): v for v in videos if isinstance(v, dict) and v.get('id')}
        processed_vids = set()
        count = 0
        total = len(future_to_video)
        try:
            for future in concurrent.futures.as_completed(future_to_video, timeout=60):
                if check_abortion and not check_abortion():
                    logger.info("AI Filter: Transcript fetch aborted by user.")
                    break
                
                v = future_to_video[future]
                processed_vids.add(v.get('id', ''))
                count += 1
                if count % 5 == 0 or count == 1 or count == total:
                    yield {"type": "progress", "message": f"Fetching transcripts: {count}/{total}..."}
                
                try:
                    snippet = future.result()
                except Exception:
                    logger.exception("Transcript parallel fetch failed for video %s", v['id'])
                    snippet = "<No transcript available>"

                video_metadata.append({
                    "id": v.get('id', ''),
                    "title": v.get("title", ""),
                    "channelTitle": v.get("channelTitle", ""),
                    "transcript_snippet": snippet,
                })
        except concurrent.futures.TimeoutError:
            logger.warning("Transcript fetching timed out globally, proceeding with acquired data.")
            for f, v in future_to_video.items():
                if v.get('id', '') not in processed_vids:
                    video_metadata.append({
                        "id": v.get('id', ''),
                        "title": v.get("title", ""),
                        "channelTitle": v.get("channelTitle", ""),
                        "transcript_snippet": "<No transcript available (timeout)>",
                    })
    finally:
        for f in future_to_video:
            f.cancel()
        executor.shutdown(wait=False)

    if check_abortion and not check_abortion():
        return

    yield {"type": "progress", "message": "Filtering by market relevance & deduplicating stories with Gemini..."}

    # ── Payload Capping ──────────────────────────────────────────────────────
    # Estimate token/char count to avoid reaching Gemini limits or long wait times.
    # 1.5 Flash has 1M context, but generating a list of 500 items is slow.
    # We cap the payload at ~400,000 chars total for safety.
    serialized_metadata = json.dumps(video_metadata)
    if len(serialized_metadata) > 400000:
        logger.warning("AI filter payload too large (%d chars), truncating transcripts.", len(serialized_metadata))
        for m in video_metadata:
            if len(m.get('transcript_snippet', '')) > 200:
                m['transcript_snippet'] = m['transcript_snippet'][:200] + "..."
        serialized_metadata = json.dumps(video_metadata)
    # ─────────────────────────────────────────────────────────────────────────

    try:
        # Added a per-request timeout to prevent the sync from hanging indefinitely
        # on network issues or Gemini infrastructure stalls.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ext_executor:
            response = None
            last_err = None
            for model_name in _MODELS_TO_TRY:
                logger.info("Sync: AI Filter attempting model '%s'...", model_name)
                future = ext_executor.submit(
                    client.models.generate_content,
                    model=model_name,
                    contents=f"{prompt}\n\nVideos list:\n{serialized_metadata}",
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearchRetrieval())] if _USE_VERTEX else None,
                        temperature=0.1,
                    )
                )
                try:
                    response = future.result(timeout=120)
                    logger.info("Sync: AI Filter successfully used model '%s'", model_name)
                    break
                except Exception as e:
                    last_err = e
                    err_msg = str(e)
                    if "404" in err_msg or "NOT_FOUND" in err_msg:
                        logger.warning("Sync: Model '%s' not found, trying next...", model_name)
                        continue
                    else:
                        logger.error("Sync: AI Filter failed with non-404 error: %s", err_msg)
                        break

            if not response:
                logger.error("Gemini API failed after trying all models. Last error: %s", last_err)
                yield {'type': 'progress', 'message': f"AI Filter: Failed ({type(last_err).__name__}). Returning unfiltered."}
                yield {"type": "result", "videos": videos}
                return

        try:
            text = response.text.strip()
            text = _extract_json_from_text(text)
        except ValueError:
            logger.warning("Gemini Safety Filter blocked prompt payload correctly dropping to empty responses.")
            text = "{}"

        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                parsed = {}
        except Exception:
            parsed = {}

        kept_ids = parsed.get("video_ids")

        # Distinguish between parse failure (key missing entirely) and
        # Gemini intentionally returning an empty list (no relevant videos).
        if kept_ids is None:
            # Parse failure: 'video_ids' key was not in the response at all.
            logger.warning("AI filter: Gemini response missing 'video_ids' key. Falling back to original list.")
            yield {"type": "result", "videos": videos}
            return

        if not isinstance(kept_ids, list):
            kept_ids = []

        kept_set = set(str(v) for v in kept_ids if isinstance(v, (str, int, float)))

        filtered = [v for v in videos if isinstance(v, dict) and str(v.get('id')) in kept_set]

        removed = len(videos) - len(filtered)
        logger.info("AI filter: %d input → %d kept (%d irrelevant/duplicate removed)", len(videos), len(filtered), removed)

        yield {"type": "result", "videos": filtered}
        return
    except Exception:
        logger.exception("Gemini API error or schema parse error")
        yield {"type": "result", "videos": videos}
        return


