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

def _atomic_write(data, target_path, directory, mode, writer_func):
    """Write data atomically: tmp-file → fsync → rename."""
    os.makedirs(directory, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode, dir=directory, delete=False, suffix='.tmp', encoding='utf-8') as f:
            temp_name = f.name
            writer_func(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, target_path)
    except Exception:
        if temp_name and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except Exception:
                pass
        raise


def atomic_write_json(data, target_path, directory=DATA_DIR):
    """Write JSON atomically."""
    _atomic_write(data, target_path, directory, 'w', lambda d, f: json.dump(d, f, indent=2))


def atomic_write_yaml(data, target_path, directory='.'):
    """Write YAML atomically."""
    _atomic_write(data, target_path, directory, 'w', lambda d, f: yaml.safe_dump(d, f, sort_keys=False))


# ── Config ──────────────────────────────────────────────────────────────────

def load_config():
    """Load config.yaml. Returns None if file doesn't exist or is corrupt."""
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            content = f.read(1024 * 1024)
            data = yaml.safe_load(content)
            return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.error("Failed to parse config.yaml: %s", exc)
        return None


def save_channels(channels_list):
    """Persist the channels list into config.yaml (preserves other keys)."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                content = f.read(1024 * 1024)
                config = yaml.safe_load(content)
            if config is not None and not isinstance(config, dict):
                raise RuntimeError("config.yaml contains invalid structure (not a dictionary).")
            if not isinstance(config, dict):
                config = {'api_key': _PLACEHOLDER_KEY, 'lookback_hours': DEFAULT_LOOKBACK_HOURS}
        except Exception as exc:
            logger.error("Refusing to overwrite corrupted config.yaml: %s", exc)
            raise RuntimeError(f"config.yaml is corrupted or invalid. Please fix it manually before saving. ({exc})")
    else:
        config = {'api_key': _PLACEHOLDER_KEY, 'lookback_hours': DEFAULT_LOOKBACK_HOURS}

    config['channels'] = channels_list or []
    atomic_write_yaml(config, CONFIG_FILE)


def is_api_key_valid(api_key):
    """Check whether the API key is set and not the placeholder."""
    return isinstance(api_key, str) and bool(api_key.strip()) and api_key.strip() != _PLACEHOLDER_KEY


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
    api_key = api_key.strip()

    # Compute the start-date window
    lookback = config.get('lookback_hours')
    if lookback is not None:
        try:
            start_date = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=float(lookback))
            ).isoformat()
        except (ValueError, TypeError, OverflowError):
            start_date = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=DEFAULT_LOOKBACK_HOURS)
            ).isoformat()
    else:
        start_val = config.get('start_date')
        if isinstance(start_val, (datetime.datetime, datetime.date)):
            start_date = start_val.isoformat()
        elif start_val:
            start_date = str(start_val)
        else:
            start_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=DEFAULT_LOOKBACK_HOURS)).isoformat()

    raw_channels = config.get('channels')
    if not isinstance(raw_channels, list):
        raw_channels = []
    if len(raw_channels) > 500:
        raw_channels = raw_channels[:500]
    return config, api_key, start_date, raw_channels


# ── Cache ───────────────────────────────────────────────────────────────────

def is_cache_valid():
    """True if the cache file exists and was written within CACHE_EXPIRY_HOURS."""
    try:
        if not os.path.exists(CACHE_FILE):
            return False
        modified = datetime.datetime.fromtimestamp(os.path.getmtime(CACHE_FILE), tz=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - modified) <= datetime.timedelta(hours=CACHE_EXPIRY_HOURS)
    except Exception:
        return False


def load_cache():
    """Load cached video list. Returns [] on missing or corrupt file."""
    if not os.path.exists(CACHE_FILE):
        return []
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            content = f.read(10 * 1024 * 1024)
            data = json.loads(content)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_cache(videos):
    """Atomically persist the video queue cache."""
    atomic_write_json(videos, CACHE_FILE)


# ── Channel helpers ─────────────────────────────────────────────────────────

def normalize_channel_id(channel):
    """Extract channel ID from either a plain string or a {id, name} dict."""
    if isinstance(channel, dict):
        return str(channel.get('id') or channel.get('name') or '')
    return str(channel)


def normalize_channel_name(channel):
    """Extract display name from either a plain string or a {id, name} dict."""
    if isinstance(channel, dict):
        return str(channel.get('name') or channel.get('id') or '')
    return str(channel)


def deduplicate_channels(raw_channels):
    """Deduplicate a channel list by ID, preserving insertion order."""
    seen = {}
    for ch in raw_channels:
        if not isinstance(ch, (str, dict)):
            continue
        ch_id = normalize_channel_id(ch)
        if not ch_id or ch_id == 'None':
            continue
        if ch_id not in seen:
            seen[ch_id] = ch
    return list(seen.values())
