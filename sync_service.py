"""
sync_service.py — Orchestration layer for queue management and SSE streaming.

Coordinates config_manager (data access) and youtube_api (API calls) to
build and cache the video queue.  This is the module that app.py routes call.
"""

import json
import logging
import concurrent.futures
import requests

import config_manager as cfg
import youtube_api
import ai_filter
import storage_manager

logger = logging.getLogger(__name__)


# ── Queue (non-streaming) ──────────────────────────────────────────────────

def get_queue(force_sync=False):
    """Return the cached queue, or fetch fresh if expired / forced."""
    if not force_sync and cfg.is_cache_valid():
        return cfg.load_cache()

    # Consume the streaming generator to get the final video list.
    # This reuses the same parallel-fetch, dedup, and AI-filter pipeline.
    videos = []
    for event_str in _fetch_all_videos_stream():
        data = json.loads(event_str.removeprefix("data: ").strip())
        if data.get('type') == 'videos':
            videos = data['videos']
    return videos


# ── Queue (SSE streaming) ──────────────────────────────────────────────────

def _sse_event(data_dict):
    """Format a dict as a Server-Sent Event data line."""
    return f"data: {json.dumps(data_dict)}\n\n"


def get_queue_stream(force_sync=False):
    """
    Generator that yields SSE events.  Uses cache if valid, otherwise
    fetches in parallel and streams progress events.
    """
    if not force_sync and cfg.is_cache_valid():
        videos = cfg.load_cache()
        yield _sse_event({'type': 'videos', 'videos': videos})
        yield _sse_event({'type': 'done', 'message': 'Loaded from cache'})
        return

    yield from _fetch_all_videos_stream()


def _fetch_all_videos_stream():
    """Parallel-fetch all channels, yielding SSE progress events."""
    config, api_key, start_date, raw_channels_list = cfg.get_sync_params()

    if not config:
        yield _sse_event({'type': 'error', 'message': 'No config'})
        return
    if not api_key:
        yield _sse_event({'type': 'error', 'message': 'No API Key'})
        return

    channels = cfg.deduplicate_channels(raw_channels_list)
    if not channels:
        yield _sse_event({'type': 'done', 'message': 'No channels configured'})
        return

    all_videos = []

    def _process_channel(channel):
        with requests.Session() as session:
            ch_id = cfg.normalize_channel_id(channel)
            ch_name = cfg.normalize_channel_name(channel)
            playlist_id = youtube_api.get_uploads_playlist_id(ch_id, api_key, session=session)
            videos = []
            if playlist_id:
                videos = youtube_api.fetch_videos_from_playlist(playlist_id, start_date, api_key, session=session)
            return ch_name, videos

    yield _sse_event({'type': 'progress', 'message': f'Syncing {len(channels)} channels in parallel ...'})

    max_workers = min(cfg.MAX_PARALLEL_CHANNELS, len(channels))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_process_channel, ch): ch for ch in channels}
        for future in concurrent.futures.as_completed(futures):
            try:
                ch_name, videos = future.result()
                if videos:
                    all_videos.extend(videos)
                    yield _sse_event({'type': 'progress', 'message': f'Finished {ch_name} ...'})
            except Exception as exc:
                logger.exception("Channel sync failed: %s", exc)

    youtube_api.sort_videos_newest_first(all_videos)

    yield _sse_event({'type': 'progress', 'message': 'Analyzing videos with AI ...'})
    filtered_videos = ai_filter.filter_videos(all_videos)

    # ── Already-watched check ────────────────────────────────────────────────
    # Split into IDs we already know about locally vs. candidates.
    watched_ids = storage_manager.get_watched_ids()
    watched_set = set(watched_ids)

    known_watched = [v for v in filtered_videos if v['id'] in watched_set]
    candidates    = [v for v in filtered_videos if v['id'] not in watched_set]

    if candidates and watched_ids:
        yield _sse_event({'type': 'progress', 'message': 'Checking watched history with Gemini ...'})
        candidates = ai_filter.check_already_watched(candidates, watched_ids)

    # Recombine: already-known-watched ones are kept in the list so the UI
    # can still render them with the "watched" badge; only the Gemini-confirmed
    # new videos are surfaced as unwatched.
    truly_new = known_watched + candidates
    # ────────────────────────────────────────────────────────────────────────

    cfg.save_cache(truly_new)
    yield _sse_event({'type': 'videos', 'videos': truly_new})
    yield _sse_event({'type': 'done', 'message': 'Sync complete!'})
