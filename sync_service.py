"""
sync_service.py — Orchestration layer for queue management and SSE streaming.

Coordinates config_manager (data access) and youtube_api (API calls) to
build and cache the video queue.  This is the module that app.py routes call.
"""

import json
import logging
import concurrent.futures
import threading
import time

import config_manager as cfg
import youtube_api
import ai_filter
import storage_manager

logger = logging.getLogger(__name__)

# ── Sync State Management ──────────────────────────────────────────────────

class SyncStateManager:
    """Manages the global state of the synchronization process."""
    def __init__(self):
        self._lock = threading.Lock()
        self.is_running = False
        self.start_time = 0
        self.last_activity = 0
        self.current_step = "Idle"
        self.last_error = None
        self.last_videos_count = 0

    def start(self):
        with self._lock:
            # Stale lock detection: if running for more than 20 mins without activity, override
            now = time.time()
            if self.is_running and (now - self.last_activity > 1200):
                logger.warning("Sync detected as stale (no activity for 20m). Overriding.")
                self.is_running = False
            
            if self.is_running:
                return False
            
            self.is_running = True
            self.start_time = now
            self.last_activity = now
            self.current_step = "Starting..."
            self.last_error = None
            return True

    def update(self, step_msg, videos_count=None):
        with self._lock:
            self.current_step = step_msg
            self.last_activity = time.time()
            if videos_count is not None:
                self.last_videos_count = videos_count

    def stop(self, error=None):
        with self._lock:
            self.is_running = False
            self.last_error = error
            self.current_step = "Idle" if not error else f"Error: {error}"

    def reset(self):
        with self._lock:
            self.is_running = False
            self.current_step = "Idle"
            self.last_error = None
            self.start_time = 0
            self.last_activity = 0
            self.last_videos_count = 0
            logger.info("Sync state has been manually reset.")

    def get_status(self):
        with self._lock:
            return {
                "is_running": self.is_running,
                "current_step": self.current_step,
                "last_error": self.last_error,
                "elapsed": (time.time() - self.start_time) if self.is_running else 0,
                "last_videos_count": self.last_videos_count
            }

_sync_manager = SyncStateManager()


# ── Queue (non-streaming) ──────────────────────────────────────────────────

def get_queue(force_sync=False):
    """Return the cached queue, or fetch fresh if expired / forced."""
    if not force_sync and cfg.is_cache_valid():
        return cfg.load_cache()

    # Consume the streaming generator to get the final video list.
    # This reuses the same parallel-fetch, dedup, and AI-filter pipeline.
    videos = []
    for event_str in _fetch_all_videos_stream(force_sync=force_sync):
        try:
            data = json.loads(event_str.removeprefix("data: ").strip())
            if data.get('type') == 'videos':
                videos = data['videos']
            elif data.get('type') == 'error':
                raise RuntimeError(data.get('message', 'Sync failed'))
        except json.JSONDecodeError:
            pass
    return videos


# ── Queue (SSE streaming) ──────────────────────────────────────────────────

def _sse_event(data_dict):
    """Format a dict as a Server-Sent Event data line."""
    return f"data: {json.dumps(data_dict)}\n\n"


def _sse_heartbeat():
    """Format an SSE comment as a heartbeat to keep connections alive."""
    return ":heartbeat\n\n"


def _deduplicate_by_id(videos):
    """Deduplicate a list of videos by their `id` property, preserving order."""
    deduped = []
    seen = set()
    for v in videos:
        if not isinstance(v, dict):
            continue
        vid = v.get('id')
        if not isinstance(vid, str):
            continue
        vid = vid.strip()
        if not vid or vid.lower() in ('none', 'undefined', 'null'):
            continue
        if vid not in seen:
            seen.add(vid)
            deduped.append(v)
    return deduped


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

    yield from _fetch_all_videos_stream(force_sync=force_sync)


def _fetch_all_videos_stream(force_sync=False):
    """Parallel-fetch all channels, yielding SSE progress events."""
    if not _sync_manager.start():
        status = _sync_manager.get_status()
        yield _sse_event({
            'type': 'error', 
            'error_code': 'LOCKED',
            'message': f'Sync already running: {status["current_step"]}',
            'status': status
        })
        return

    try:
        yield from _fetch_all_videos_stream_locked(force_sync=force_sync)
    except (GeneratorExit, Exception) as e:
        if isinstance(e, GeneratorExit):
            logger.info("SSE client disconnected from sync stream.")
            _sync_manager.stop()
        else:
            logger.exception("Global sync failure")
            err_msg = str(e)
            _sync_manager.stop(error=err_msg)
            yield _sse_event({'type': 'error', 'message': err_msg})
        return
    finally:
        # Stop manager if not already stopped by except block
        if _sync_manager.is_running:
            _sync_manager.stop()


def _fetch_all_videos_stream_locked(force_sync=False):
    config, api_key, start_date, raw_channels_list = cfg.get_sync_params()

    if not config:
        yield _sse_event({'type': 'error', 'message': 'No config'})
        return
    if not api_key:
        yield _sse_event({'type': 'error', 'message': 'No API Key'})
        return

    if force_sync:
        # Explicitly invalidate cache at start of forced sync to prevent
        # serving stale data if the process halts mid-way.
        cfg.save_cache([])

    channels = cfg.deduplicate_channels(raw_channels_list)
    if not channels:
        cfg.save_cache([])
        yield _sse_event({'type': 'videos', 'videos': []})
        yield _sse_event({'type': 'done', 'message': 'No channels configured'})
        return

    all_videos = []

    def _process_channel(channel):
        ch_id = cfg.normalize_channel_id(channel)
        ch_name = cfg.normalize_channel_name(channel)
        # Note: We do not pass `session=...` so it falls back to youtube_api._global_session,
        # which utilizes a central connection pool (max 20) and avoids TLS handshake overhead.
        playlist_id = youtube_api.get_uploads_playlist_id(ch_id, api_key)
        if isinstance(playlist_id, dict) and 'error' in playlist_id:
            return ch_name, playlist_id
        videos = []
        if isinstance(playlist_id, str):
            videos = youtube_api.fetch_videos_from_playlist(playlist_id, start_date, api_key)
        return ch_name, videos

    yield _sse_event({'type': 'progress', 'message': f'Syncing {len(channels)} channels in parallel ...'})

    last_heartbeat = time.time()
    
    max_workers = min(cfg.MAX_PARALLEL_CHANNELS, len(channels))
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    futures = {executor.submit(_process_channel, ch): ch for ch in channels}
    
    try:
        # We use a loop with wait() + timeout to allow heartbeats while waiting for the first/next future
        while futures:
            # Check if we should still be running (e.g. manual override)
            if not _sync_manager.is_running:
                logger.info("Sync aborted by manager while processing channels.")
                for f in futures:
                    f.cancel()
                break

            # wait for any future to complete, but timeout every 5s to check heartbeat
            done, not_done = concurrent.futures.wait(futures, timeout=5, return_when=concurrent.futures.FIRST_COMPLETED)
            
            now = time.time()
            if now - last_heartbeat > 15:
                yield _sse_heartbeat()
                last_heartbeat = now
            
            for future in done:
                ch = futures.pop(future)
                try:
                    ch_name, result = future.result()
                    if isinstance(result, dict) and 'error' in result:
                        if result['error'] == 'QUOTA_EXCEEDED':
                            err = 'YouTube API Quota Exceeded. Please try again tomorrow.'
                            yield _sse_event({'type': 'error', 'message': err})
                            _sync_manager.update(f"Error: {err}")
                            executor.shutdown(wait=False, cancel_futures=True)
                            return
                        else:
                            msg = f'Failed to sync {ch_name}: {result.get("message", "Unknown error")}'
                            yield _sse_event({'type': 'progress', 'message': msg})
                            _sync_manager.update(msg)
                    elif result:
                        all_videos.extend(result)
                        msg = f'Downloaded {len(result)} videos from {ch_name} ...'
                        yield _sse_event({'type': 'progress', 'message': msg})
                        # Don't update manager too aggressively in the loop to avoid lock contention,
                        # but we want some granularity.
                        _sync_manager.update(msg, videos_count=len(all_videos))
                except Exception as exc:
                    logger.exception("Channel sync failed for %s", futures.get(future, "unknown"))
                    msg = f'Warning: A channel failed to sync completely.'
                    yield _sse_event({'type': 'progress', 'message': msg})
                    _sync_manager.update(msg)
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False)

    # Ensure we deduplicate all videos by their `id` across overlaps to avoid hitting AI with multiple variants of the same object
    all_videos = _deduplicate_by_id(all_videos)

    youtube_api.sort_videos_newest_first(all_videos)
    
    # AI Context limit protection. Limit to the absolute newest 500 records mathematically shielding global Gemini text-token ceilings spanning unbounded config lookback loops natively.
    if len(all_videos) > 500:
        all_videos = all_videos[:500]

    filtered_videos = all_videos
    for event in ai_filter.filter_videos(all_videos, check_abortion=lambda: _sync_manager.is_running):
        now = time.time()
        if now - last_heartbeat > 15:
            yield _sse_heartbeat()
            last_heartbeat = now
            
        if event.get('type') == 'progress':
            yield _sse_event(event)
            _sync_manager.update(event['message'])
        elif event.get('type') == 'result':
            filtered_videos = event.get('videos', all_videos)

    # ── Already-watched check ────────────────────────────────────────────────
    # The UI uses the global history to render the "watched" badge. We simply
    # return the filtered videos (which are already sorted newest-first).
    truly_new = filtered_videos
    # ────────────────────────────────────────────────────────────────────────

    try:
        cfg.save_cache(truly_new)
    except Exception as exc:
        logger.warning("Failed to save cache: %s", exc)

    yield _sse_event({'type': 'videos', 'videos': truly_new})
    yield _sse_event({'type': 'done', 'message': 'Sync complete!'})
