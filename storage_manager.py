"""
storage_manager.py — Watch history persistence.

Thread-safe read/write of the user's watch history (which videos have been
seen, which was last played).
"""

import json
import os
import threading
import logging

import config_manager as cfg

logger = logging.getLogger(__name__)

HISTORY_FILE = os.path.join(cfg.DATA_DIR, 'history.json')
MAX_WATCHED_HISTORY = 500   # oldest entries evicted when limit is exceeded
# 500 entries ≈ 2–5 months of history at typical watch rates.
# Each YouTube ID is 11 chars, so 500 IDs ≈ 2,000 tokens — negligible
# against Gemini 2.5 Pro's 1M-token context window.
_history_lock = threading.Lock()
_history_cache = None
_history_mtime = 0


def _make_default():
    return {"watched_video_ids": [], "last_watched_video_id": None}


# ── Public API ──────────────────────────────────────────────────────────────

def load_history():
    """Thread-safe: load and return the watch history dict."""
    with _history_lock:
        hist = _load_unlocked()
        return {
            'watched_video_ids': list(hist.get('watched_video_ids', [])),
            'last_watched_video_id': hist.get('last_watched_video_id')
        }


def save_history(history):
    """Thread-safe: persist the entire history dict."""
    with _history_lock:
        _save_unlocked({
            'watched_video_ids': list(history.get('watched_video_ids', [])),
            'last_watched_video_id': history.get('last_watched_video_id')
        })


def mark_watched(video_id):
    """Thread-safe: append a video to the watched list and set it as last-played.

    The list is capped at MAX_WATCHED_HISTORY entries; the oldest IDs are
    evicted first when the cap is exceeded.
    """
    with _history_lock:
        if not isinstance(video_id, str):
            return
        video_id = video_id.strip()[:100]
        if not video_id or video_id.lower() in ('none', 'undefined', 'null'):
            return
        history = _load_unlocked()
        watched = history['watched_video_ids']
        watched = [vid for vid in watched if vid != video_id]
        watched.append(video_id)
        # Enforce cap — keep the most-recent MAX_WATCHED_HISTORY entries
        if len(watched) > MAX_WATCHED_HISTORY:
            watched = watched[-MAX_WATCHED_HISTORY:]
        history['watched_video_ids'] = watched
        history['last_watched_video_id'] = video_id
        _save_unlocked(history)


def get_watched_ids():
    """Thread-safe: return just the list of watched video IDs."""
    with _history_lock:
        return list(_load_unlocked().get('watched_video_ids', []))


# ── Internal ────────────────────────────────────────────────────────────────

def _sanitize_id(vid):
    if not isinstance(vid, str):
        return None
    s = vid.strip()[:100]
    if not s or s.lower() in ('none', 'undefined', 'null'):
        return None
    return s


def _load_unlocked():
    global _history_cache, _history_mtime
    
    # Check modification time to see if we should invalidate cache
    mtime = 0
    if os.path.exists(HISTORY_FILE):
        try:
            mtime = os.path.getmtime(HISTORY_FILE)
        except OSError:
            pass

    if _history_cache is not None and mtime <= _history_mtime:
        return _history_cache

    if mtime == 0:
        _history_cache = _make_default()
        _history_mtime = 0
        return _history_cache
        
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            content = f.read(5 * 1024 * 1024)
            data = json.loads(content)
            if not isinstance(data, dict):
                return _make_default()
                
            raw_ids = data.get('watched_video_ids')
            if not isinstance(raw_ids, list):
                raw_ids = []
                
            last_id = data.get('last_watched_video_id')
                
            sanitized_ids = []
            for v in raw_ids:
                sid = _sanitize_id(v)
                if sid:
                    sanitized_ids.append(sid)

            sanitized_data = {
                'watched_video_ids': sanitized_ids,
                'last_watched_video_id': _sanitize_id(last_id)
            }
            
            if len(sanitized_data['watched_video_ids']) > MAX_WATCHED_HISTORY:
                sanitized_data['watched_video_ids'] = sanitized_data['watched_video_ids'][-MAX_WATCHED_HISTORY:]
                
            _history_cache = sanitized_data
            _history_mtime = mtime
            return _history_cache
    except Exception as e:
        logger.warning("Failed to load history, falling back to default: %s", e)
        _history_cache = _make_default()
        _history_mtime = 0 # force reload attempt next time
        return _history_cache


def _save_unlocked(history):
    global _history_cache, _history_mtime
    _history_cache = history
    try:
        cfg.atomic_write_json(history, HISTORY_FILE)
        # Update mtime after successful write to avoid immediately reloading our own write
        if os.path.exists(HISTORY_FILE):
             _history_mtime = os.path.getmtime(HISTORY_FILE)
    except Exception as exc:
        logger.error("Failed to persist watch history: %s", exc)
