"""
storage_manager.py — Watch history persistence.

Thread-safe read/write of the user's watch history (which videos have been
seen, which was last played).
"""

import json
import os
import threading

import config_manager as cfg

HISTORY_FILE = 'data/history.json'
MAX_WATCHED_HISTORY = 500   # oldest entries evicted when limit is exceeded
# 500 entries ≈ 2–5 months of history at typical watch rates.
# Each YouTube ID is 11 chars, so 500 IDs ≈ 2,000 tokens — negligible
# against Gemini 2.5 Pro's 1M-token context window.
_history_lock = threading.Lock()
_history_cache = None


def _make_default():
    return {"watched_video_ids": [], "last_watched_video_id": None}


# ── Public API ──────────────────────────────────────────────────────────────

def load_history():
    """Thread-safe: load and return the watch history dict."""
    with _history_lock:
        return _load_unlocked()


def save_history(history):
    """Thread-safe: persist the entire history dict."""
    with _history_lock:
        _save_unlocked(history)


def mark_watched(video_id):
    """Thread-safe: append a video to the watched list and set it as last-played.

    The list is capped at MAX_WATCHED_HISTORY entries; the oldest IDs are
    evicted first when the cap is exceeded.
    """
    with _history_lock:
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

def _load_unlocked():
    global _history_cache
    if _history_cache is not None:
        return _history_cache

    if not os.path.exists(HISTORY_FILE):
        _history_cache = _make_default()
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
            if not isinstance(last_id, (str, int, type(None))):
                last_id = None
                
            sanitized_data = {
                'watched_video_ids': [str(v) for v in raw_ids if isinstance(v, (str, int, float, bool))],
                'last_watched_video_id': str(last_id) if last_id is not None else None
            }
            
            if len(sanitized_data['watched_video_ids']) > MAX_WATCHED_HISTORY:
                sanitized_data['watched_video_ids'] = sanitized_data['watched_video_ids'][-MAX_WATCHED_HISTORY:]
                
            _history_cache = sanitized_data
            return _history_cache
    except Exception:
        _history_cache = _make_default()
        return _history_cache


def _save_unlocked(history):
    global _history_cache
    _history_cache = history
    cfg.atomic_write_json(history, HISTORY_FILE)
