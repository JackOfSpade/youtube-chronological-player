"""
config_manager.py — Configuration, cache, and shared I/O infrastructure.

Centralizes all file-system operations so that other modules only deal with
data, never with paths, locks, or serialization details.
"""

import os
import json
import yaml
import datetime
import tempfile
import logging

logger = logging.getLogger(__name__)

# ── Path Constants ──────────────────────────────────────────────────────────

CONFIG_FILE = 'config.yaml'
CACHE_FILE = 'data/cache.json'
DATA_DIR = 'data'

# ── Tuning Constants ────────────────────────────────────────────────────────

CACHE_EXPIRY_HOURS = 12
API_TIMEOUT = 15  # seconds per HTTP request
DEFAULT_LOOKBACK_HOURS = 36
MAX_PARALLEL_CHANNELS = 10

_PLACEHOLDER_KEY = "YOUR_YOUTUBE_API_KEY_HERE"


# ── Atomic I/O ──────────────────────────────────────────────────────────────

def atomic_write_json(data, target_path, directory=DATA_DIR):
    """Write JSON atomically: tmp-file → fsync → rename."""
    os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=directory, delete=False, suffix='.tmp') as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
        temp_name = f.name
    os.replace(temp_name, target_path)


# ── Config ──────────────────────────────────────────────────────────────────

def load_config():
    """Load config.yaml. Returns None if file doesn't exist."""
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, 'r') as f:
        return yaml.safe_load(f)


def save_channels(channels_list):
    """Persist the channels list into config.yaml (preserves other keys)."""
    config = load_config()
    if config is not None:
        config['channels'] = channels_list
        with open(CONFIG_FILE, 'w') as f:
            yaml.safe_dump(config, f, sort_keys=False)


def is_api_key_valid(api_key):
    """Check whether the API key is set and not the placeholder."""
    return bool(api_key) and api_key != _PLACEHOLDER_KEY


def is_api_configured():
    """Quick check: does the config have a usable API key?"""
    config = load_config()
    return config is not None and is_api_key_valid(config.get('api_key'))


def get_sync_params():
    """
    Resolve sync parameters from config.

    Returns:
        (config, api_key, start_date_iso, raw_channels_list)
        Any of these may be None/empty if unconfigured.
    """
    config = load_config()
    if not config:
        return None, None, None, []

    api_key = config.get('api_key')
    if not is_api_key_valid(api_key):
        return config, None, None, []

    # Compute the start-date window
    lookback = config.get('lookback_hours')
    if lookback is not None:
        start_date = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=int(lookback))
        ).isoformat()
    else:
        start_date = config.get(
            'start_date',
            (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=DEFAULT_LOOKBACK_HOURS)).isoformat(),
        )

    raw_channels = config.get('channels', [])
    return config, api_key, start_date, raw_channels


# ── Cache ───────────────────────────────────────────────────────────────────

def is_cache_valid():
    """True if the cache file exists and was written within CACHE_EXPIRY_HOURS."""
    if not os.path.exists(CACHE_FILE):
        return False
    modified = datetime.datetime.fromtimestamp(os.path.getmtime(CACHE_FILE))
    return (datetime.datetime.now() - modified) <= datetime.timedelta(hours=CACHE_EXPIRY_HOURS)


def load_cache():
    """Load cached video list. Returns [] on missing or corrupt file."""
    if not os.path.exists(CACHE_FILE):
        return []
    with open(CACHE_FILE, 'r') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def save_cache(videos):
    """Atomically persist the video queue cache."""
    atomic_write_json(videos, CACHE_FILE)


# ── Channel helpers ─────────────────────────────────────────────────────────

def normalize_channel_id(channel):
    """Extract channel ID from either a plain string or a {id, name} dict."""
    return channel.get('id', channel) if isinstance(channel, dict) else channel


def normalize_channel_name(channel):
    """Extract display name from either a plain string or a {id, name} dict."""
    if isinstance(channel, dict):
        return channel.get('name', channel.get('id', ''))
    return channel


def deduplicate_channels(raw_channels):
    """Deduplicate a channel list by ID, preserving insertion order."""
    seen = {}
    for ch in raw_channels:
        ch_id = normalize_channel_id(ch)
        if ch_id not in seen:
            seen[ch_id] = ch
    return list(seen.values())
