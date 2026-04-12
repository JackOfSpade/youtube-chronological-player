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
_history_lock = threading.Lock()

_DEFAULT_HISTORY = {"watched_video_ids": [], "last_watched_video_id": None}


def _make_default():
    return dict(_DEFAULT_HISTORY, watched_video_ids=[])


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
    """Thread-safe: append a video to the watched list and set it as last-played."""
    with _history_lock:
        history = _load_unlocked()
        watched = history['watched_video_ids']
        if video_id not in watched:
            watched.append(video_id)
        history['last_watched_video_id'] = video_id
        _save_unlocked(history)


# ── Internal ────────────────────────────────────────────────────────────────

def _load_unlocked():
    if not os.path.exists(HISTORY_FILE):
        return _make_default()
    with open(HISTORY_FILE, 'r') as f:
        try:
            data = json.load(f)
            if not isinstance(data.get('watched_video_ids'), list):
                return _make_default()
            return data
        except (json.JSONDecodeError, ValueError):
            return _make_default()


def _save_unlocked(history):
    cfg.atomic_write_json(history, HISTORY_FILE)
