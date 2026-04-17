"""
ai_filter.py — Gemini-powered video deduplication.

Uses transcript analysis and web-grounded bias assessment to select the best
representative video when multiple channels cover the same news story.
"""

import os
import re
import json
import logging
import concurrent.futures
import functools

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

try:
    client = genai.Client(vertexai=True, project=_PROJECT_ID, location=_LOCATION)
except Exception:
    logger.exception("Failed to initialize Gemini client")
    client = None


@functools.lru_cache(maxsize=2000)
def get_video_transcript(video_id):
    """Fetch the first ~1000 chars of an English transcript, or a placeholder."""
    if not video_id or video_id == 'unknown' or not isinstance(video_id, (str, int, float)):
        return "<No transcript available>"
    video_id = str(video_id)
    if len(video_id) > 100:
        return "<No transcript available>"
    
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        try:
            transcript = transcript_list.find_transcript(['en', 'en-US', 'en-GB'])
        except Exception:
            transcript = next(iter(transcript_list))
        data = transcript.fetch()
        text = " ".join([t.get('text', '') for t in data if isinstance(t, dict)])
        return text[:1000]
    except Exception:
        logger.debug("Transcript fetch failed for %s", video_id)
        return "<No transcript available>"


def _extract_json_from_text(text):
    """Safely extract JSON from Gemini markdown responses."""
    if "```json" in text.lower():
        match = re.search(r'```json\s*(.*?)\s*```', text, flags=re.DOTALL | re.IGNORECASE)
        if match: return match.group(1).strip()
    elif "```" in text:
        match = re.search(r'```\s*(.*?)\s*```', text, flags=re.DOTALL)
        if match: return match.group(1).strip()
    else:
        match = re.search(r'\{.*\}', text, flags=re.DOTALL)
        if match: return match.group(0).strip()
    return text


def filter_videos(videos):
    """
    Deduplicate *videos* using Gemini with search grounding.

    Returns the filtered list, or the original list unchanged if the AI
    filter is unavailable or encounters an error.
    """
    if not client:
        logger.warning("No Gemini API key or invalid client, skipping AI filter.")
        return videos

    if not videos:
        return []
        
    if len(videos) <= 1:
        return videos

    prompt = """
    You are given a list of recently published YouTube videos from various channels.
    Your task is to identify videos that cover the EXACT SAME news story (duplicates) and filter them down to a single video per story.
    
    Rules for selecting which video to keep for a given story:
    1. PRIORITY 1 (Narration): Read the provided 'transcript_snippet' for each video. You MUST prefer videos that contain structured narration/commentary over videos that are just unstructured background noise (raw footage). Keep raw footage only if it is the absolute ONLY option.
    2. PRIORITY 2 (Unbiasedness as Tie-Breaker): If multiple videos for the same story have structured narration, use Google Search to research those channels and determine their current media bias and reputation. Break the tie by keeping the video from the most neutral/unbiased channel.
    3. If videos do not cover the same story as another, keep them.
    
    Return a JSON object with a single key "video_ids" whose value is an array of
    the video ID strings that should be kept. Include every ID that survives dedup.
    """

    video_metadata = []

    # Limit max workers to 15 to avoid YouTube throwing aggressive rate limits on transcript API
    max_workers = min(15, len(videos))

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    future_to_video = {}
    try:
        future_to_video = {executor.submit(get_video_transcript, v.get('id') or 'unknown'): v for v in videos if isinstance(v, dict) and v.get('id')}
        processed_vids = set()
        try:
            for future in concurrent.futures.as_completed(future_to_video, timeout=25):
                v = future_to_video[future]
                processed_vids.add(v.get('id', ''))
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

    try:
        response = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=f"{prompt}\n\nVideos list:\n{json.dumps(video_metadata, indent=2)}",
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}],
                temperature=0.1,
            ),
        )

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

        kept_ids = parsed.get("video_ids", [])
        if not isinstance(kept_ids, list):
            kept_ids = []

        kept_set = set(str(v) for v in kept_ids if isinstance(v, (str, int, float, bool)))

        filtered = [v for v in videos if isinstance(v, dict) and str(v.get('id')) in kept_set]

        removed = len(videos) - len(filtered)
        logger.info("AI filter: %d input → %d kept (%d duplicates removed)", len(videos), len(filtered), removed)

        if not filtered and videos:
            logger.warning("AI filter removed all videos, falling back to original list.")
            return videos

        return filtered
    except Exception:
        logger.exception("Gemini API error or schema parse error")
        return videos


