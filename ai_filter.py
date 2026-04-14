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


_CODEBLOCK_RE = re.compile(r'^```\w*\n?|```$', re.MULTILINE)


def get_video_transcript(video_id):
    """Fetch the first ~1000 chars of an English transcript, or a placeholder."""
    try:
        ytt_api = YouTubeTranscriptApi()
        transcript_list = ytt_api.list(video_id)
        transcript = transcript_list.find_transcript(['en'])
        data = transcript.fetch()
        text = " ".join([t['text'] for t in data])
        return text[:1000]
    except Exception:
        logger.debug("Transcript fetch failed for %s", video_id, exc_info=True)
        return "<No transcript available>"


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

    prompt = """
    You are given a list of recently published YouTube videos from various channels.
    Your task is to identify videos that cover the EXACT SAME news story (duplicates) and filter them down to a single video per story.
    
    Rules for selecting which video to keep for a given story:
    1. PRIORITY 1 (Narration): Read the provided 'transcript_snippet' for each video. You MUST prefer videos that contain structured narration/commentary over videos that are just unstructured background noise (raw footage). Keep raw footage only if it is the absolute ONLY option.
    2. PRIORITY 2 (Unbiasedness as Tie-Breaker): If multiple videos for the same story have structured narration, use Google Search to research those channels and determine their current media bias and reputation. Break the tie by keeping the video from the most neutral/unbiased channel.
    3. If videos do not cover the same story as another, keep them.
    4. Keep the output strictly as a JSON array.
    
    Input data format:
    JSON array of objects with keys: id, title, channelTitle, transcript_snippet
    
    Output format:
    A pure JSON array of strings containing ONLY the video IDs that should be kept.
    Do not include markdown codeblocks or any explanations. Just the JSON array.
    """

    video_metadata = []

    # Limit max workers to 15 to avoid YouTube throwing aggressive rate limits on transcript API
    max_workers = min(15, len(videos))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_video = {executor.submit(get_video_transcript, v['id']): v for v in videos}
        for future in concurrent.futures.as_completed(future_to_video):
            v = future_to_video[future]
            try:
                snippet = future.result()
            except Exception:
                logger.exception("Transcript parallel fetch failed for video %s", v['id'])
                snippet = "<No transcript available>"

            video_metadata.append({
                "id": v['id'],
                "title": v["title"],
                "channelTitle": v["channelTitle"],
                "transcript_snippet": snippet,
            })

    try:
        response = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=f"{prompt}\n\nVideos list:\n{json.dumps(video_metadata, indent=2)}",
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}],
                temperature=0.1,
            ),
        )

        kept_ids_text = _CODEBLOCK_RE.sub('', response.text).strip()
        kept_ids = json.loads(kept_ids_text)
        kept_set = set(kept_ids)

        filtered = [v for v in videos if v['id'] in kept_set]

        removed = len(videos) - len(filtered)
        logger.info("AI filter: %d input → %d kept (%d duplicates removed)", len(videos), len(filtered), removed)

        if not filtered and videos:
            logger.warning("AI filter removed all videos, falling back to original list.")
            return videos

        return filtered
    except Exception:
        logger.exception("Gemini API error or JSON parse error")
        return videos
