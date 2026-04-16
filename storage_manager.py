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
        if video_id not in watched:
            watched.append(video_id)
        # Enforce cap — keep the most-recent MAX_WATCHED_HISTORY entries
        if len(watched) > MAX_WATCHED_HISTORY:
            history['watched_video_ids'] = watched[-MAX_WATCHED_HISTORY:]
        history['last_watched_video_id'] = video_id
        _save_unlocked(history)


def get_watched_ids():
    """Thread-safe: return just the list of watched video IDs."""
    with _history_lock:
        return _load_unlocked().get('watched_video_ids', [])


# ── Internal ────────────────────────────────────────────────────────────────

def _load_unlocked():
    if not os.path.exists(HISTORY_FILE):
        return _make_default()
    try:
        with open(HISTORY_FILE, 'r') as f:
            data = json.load(f)
            if not isinstance(data.get('watched_video_ids'), list):
                return _make_default()
            return data
    except Exception:
        return _make_default()


def _save_unlocked(history):
    cfg.atomic_write_json(history, HISTORY_FILE)
