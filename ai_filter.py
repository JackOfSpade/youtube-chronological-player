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


def check_already_watched(candidate_videos, watched_ids):
    """
    Ask Gemini whether any candidate video was effectively already watched.

    A video is considered "already watched" if it is semantically equivalent to
    something the user has previously seen — e.g. the same story re-uploaded
    with a minor title change, a duplicate upload, etc.

    Parameters
    ----------
    candidate_videos : list[dict]
        Videos whose IDs are not in the local watched history.
    watched_ids : list[str]
        The capped (≤ 100) list of previously watched video IDs.

    Returns
    -------
    list[dict]
        The subset of *candidate_videos* that are genuinely new.
        Returns *candidate_videos* unchanged on any error.
    """
    if not client:
        logger.warning("No Gemini client; skipping already-watched check.")
        return candidate_videos

    if not candidate_videos:
        return []

    # If there's no watch history there's nothing to compare against
    if not watched_ids:
        return candidate_videos

    prompt = """
You are a video deduplication assistant for a YouTube chronological player.

The user has previously watched the YouTube videos whose IDs are listed under
"Watched Video IDs". You are given a list of candidate videos (with metadata)
that are about to be added to the user's unwatched queue.

Your task: identify which candidates are TRULY NEW content that the user has
NOT watched before. A candidate is NOT truly new if:
- It has the same YouTube video ID as a watched video (but these should already
  be filtered — double-check anyway).
- It is a re-upload, mirror, or nearly identical copy of a watched video.
- It covers the exact same narrow news event/story as a watched video and adds
  no new information.

Use Google Search to look up any video ID you need more context on.

Return ONLY a JSON array of video IDs from the candidate list that are truly new.
No markdown, no explanation — just the JSON array.
"""

    payload = {
        "watched_video_ids": watched_ids,
        "candidate_videos": [
            {
                "id": v["id"],
                "title": v.get("title", ""),
                "channelTitle": v.get("channelTitle", ""),
                "publishedAt": v.get("publishedAt", ""),
            }
            for v in candidate_videos
        ],
    }

    try:
        response = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=f"{prompt}\n\nData:\n{json.dumps(payload, indent=2)}",
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}],
                temperature=0.1,
            ),
        )

        raw = _CODEBLOCK_RE.sub('', response.text).strip()
        truly_new_ids = set(json.loads(raw))

        result = [v for v in candidate_videos if v['id'] in truly_new_ids]

        removed = len(candidate_videos) - len(result)
        logger.info(
            "Already-watched check: %d candidates → %d truly new (%d suppressed)",
            len(candidate_videos), len(result), removed,
        )

        # Safety: if Gemini nukes everything, fall back to all candidates
        if not result and candidate_videos:
            logger.warning("Already-watched check removed all candidates; falling back.")
            return candidate_videos

        return result

    except Exception:
        logger.exception("check_already_watched: Gemini error or JSON parse error")
        return candidate_videos
