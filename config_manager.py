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
import threading
import copy
import time
import errno
import shutil

logger = logging.getLogger(__name__)

# ── Locks ───────────────────────────────────────────────────────────────────

_config_lock = threading.Lock()

# ── Path Constants ──────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.yaml')
CACHE_FILE = os.path.join(BASE_DIR, 'data', 'cache.json')
DATA_DIR = os.path.join(BASE_DIR, 'data')

# Ensure data directory exists on load
os.makedirs(DATA_DIR, exist_ok=True)

# ── Tuning Constants ────────────────────────────────────────────────────────

CACHE_EXPIRY_HOURS = 12
API_TIMEOUT = 15  # seconds per HTTP request
DEFAULT_LOOKBACK_HOURS = 36
MAX_PARALLEL_CHANNELS = 10

_PLACEHOLDER_KEY = "YOUR_YOUTUBE_API_KEY_HERE"


# ── Atomic I/O ──────────────────────────────────────────────────────────────

def _atomic_write(data, target_path, directory, mode, writer_func):
    """Write data atomically: tmp-file → fsync → rename. Protected by a cross-process lock."""
    os.makedirs(directory, exist_ok=True)
    lock_path = target_path + '.lock'
    
    start = time.time()
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.time() - start > 5:
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
            time.sleep(0.1)
        except OSError as e:
            if e.errno == errno.EACCES or e.errno == errno.EROFS:
                logger.error("Permission denied or read-only filesystem while locking %s", target_path)
                raise
            break

    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode, dir=directory, delete=False, suffix='.tmp', encoding='utf-8') as f:
            temp_name = f.name
            writer_func(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, target_path)
    except OSError as e:
        if e.errno == errno.ENOSPC:
            logger.error("Disk full (ENOSPC) when writing %s", target_path)
        elif e.errno == errno.EACCES or e.errno == errno.EROFS:
            logger.error("Permission error (EACCES) or read-only error (EROFS) when writing %s", target_path)
        elif e.errno == errno.EXDEV:
            # Cross-device link; NamedTemporaryFile with dir=directory should prevent this,
            # but we'll catch it as part of exhausting all stability possibilities.
            shutil.move(temp_name, target_path)
            temp_name = None # Mark as handled
            return
        if temp_name and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except Exception:
                pass
        raise
    except Exception:
        if temp_name and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except Exception:
                pass
        raise
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass


def atomic_write_json(data, target_path, directory=None):
    """Write JSON atomically."""
    if directory is None:
        directory = DATA_DIR
    _atomic_write(data, target_path, directory, 'w', lambda d, f: json.dump(d, f, indent=2))


def atomic_write_yaml(data, target_path, directory=None):
    """Write YAML atomically."""
    if directory is None:
        directory = BASE_DIR
    _atomic_write(data, target_path, directory, 'w', lambda d, f: yaml.safe_dump(d, f, sort_keys=False))


# ── Config ──────────────────────────────────────────────────────────────────

_config_cache = None
_config_mtime = 0

def load_config():
    """Load config.yaml. Returns None if file doesn't exist or is corrupt."""
    global _config_cache, _config_mtime
    
    # Fast path: check mtime outside the lock
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        current_mtime = os.path.getmtime(CONFIG_FILE)
        if _config_cache is not None and _config_mtime == current_mtime:
            return copy.deepcopy(_config_cache)
    except Exception:
        return None
        
    with _config_lock:
        # Re-check under lock to avoid race conditions
        try:
            current_mtime = os.path.getmtime(CONFIG_FILE)
            if _config_cache is not None and _config_mtime == current_mtime:
                return copy.deepcopy(_config_cache)
                
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                content = f.read(1024 * 1024)
                data = yaml.safe_load(content)
                if isinstance(data, dict):
                    _config_cache = data
                    _config_mtime = current_mtime
                    return copy.deepcopy(data)
                return None
        except Exception as exc:
            logger.error("Failed to parse config.yaml: %s", exc)
            return None


def save_channels(channels_list):
    """Persist the channels list into config.yaml (preserves other keys)."""
    if channels_list is None:
        channels_list = []
    if len(channels_list) > 100:
        raise ValueError('Too many channels')
        
    sanitized = []
    for c in channels_list:
        if not isinstance(c, (str, dict)):
            raise ValueError('Invalid channel format')
        if isinstance(c, str):
            c_str = c.strip()
            if len(c_str) > 200:
                raise ValueError('Channel string too large')
            sanitized.append(c_str)
        else:
            cid = str(c.get('id') or '').strip()
            cname = str(c.get('name') or '').strip()
            if len(cid) > 200 or len(cname) > 200:
                raise ValueError('Channel properties too large')
            sanitized.append({'id': cid, 'name': cname})

    with _config_lock:
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

        config['channels'] = sanitized
        atomic_write_yaml(config, CONFIG_FILE)


def is_api_key_valid(api_key):
    """Check whether the API key is set and not the placeholder."""
    return isinstance(api_key, str) and len(api_key) <= 500 and bool(api_key.strip()) and api_key.strip() != _PLACEHOLDER_KEY


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
            val = abs(float(lookback))
            if val == 0:
                val = DEFAULT_LOOKBACK_HOURS
            if val > 8760:
                val = 8760
            start_date = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=val)
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
    
    # Filter and normalize channels list to ensure no non-dict/non-string items leak through
    sanitized_channels = []
    for ch in raw_channels:
        if isinstance(ch, (str, dict)):
            # If it's a dict, ensure it has at least an 'id' or 'name'
            if isinstance(ch, dict) and not (ch.get('id') or ch.get('name')):
                continue
            sanitized_channels.append(ch)
            
    if len(sanitized_channels) > 500:
        sanitized_channels = sanitized_channels[:500]
        
    return config, api_key, start_date, sanitized_channels


# ── Cache ───────────────────────────────────────────────────────────────────

_queue_cache = None
_queue_mtime = 0
_queue_lock = threading.Lock()

def is_cache_stale():
    """True if the cache file does not exist or is older than CACHE_EXPIRY_HOURS."""
    try:
        if not os.path.exists(CACHE_FILE):
            return True
        modified = datetime.datetime.fromtimestamp(os.path.getmtime(CACHE_FILE), tz=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - modified) > datetime.timedelta(hours=CACHE_EXPIRY_HOURS)
    except Exception:
        return True


def load_cache():
    """Load cached video list. Returns [] on missing or corrupt file."""
    global _queue_cache, _queue_mtime
    
    if not os.path.exists(CACHE_FILE):
        return []
        
    try:
        # Prevent OOM if CACHE_FILE is unexpectedly massive (e.g. corruption or malicious injection)
        fsize = os.path.getsize(CACHE_FILE)
        if fsize > 20 * 1024 * 1024: # 20MB limit for JSON cache
            logger.error("Cache file is too large (%d bytes), skipping load.", fsize)
            return []
        current_mtime = os.path.getmtime(CACHE_FILE)
    except Exception:
        return []
        
    # Lockless quick read check
    if _queue_cache is not None and _queue_mtime == current_mtime:
        return copy.deepcopy(_queue_cache)
        
    with _queue_lock:
        try:
            current_mtime = os.path.getmtime(CACHE_FILE)
            if _queue_cache is not None and _queue_mtime == current_mtime:
                return copy.deepcopy(_queue_cache)
                
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                content = f.read(10 * 1024 * 1024)
                data = json.loads(content)
                if isinstance(data, list):
                    # Final sanity check on items
                    valid_data = [v for v in data if isinstance(v, dict) and v.get('id')]
                    _queue_cache = valid_data
                    _queue_mtime = current_mtime
                    return copy.deepcopy(valid_data)
                return []
        except Exception:
            return []


def save_cache(videos):
    """Atomically persist the video queue cache."""
    global _queue_cache, _queue_mtime
    atomic_write_json(videos, CACHE_FILE)
    with _queue_lock:
        try:
            _queue_cache = copy.deepcopy(videos)
            _queue_mtime = os.path.getmtime(CACHE_FILE)
        except Exception:
            pass



# ── Channel helpers ─────────────────────────────────────────────────────────

def normalize_channel_id(channel):
    """Extract channel ID from either a plain string or a {id, name} dict. Handles full YouTube URLs."""
    if isinstance(channel, dict):
        val = channel.get('id') or channel.get('name')
    else:
        val = channel
        
    if not isinstance(val, str):
        return ''
    
    val = val.strip()
    
    if val.startswith('http://') or val.startswith('https://'):
        if '@' in val:
            val = '@' + val.split('@', 1)[1].split('/')[0].split('?')[0]
        elif '/channel/' in val:
            val = val.split('/channel/', 1)[1].split('/')[0].split('?')[0]
        elif '/c/' in val:
            val = val.split('/c/', 1)[1].split('/')[0].split('?')[0]
        elif '/user/' in val:
            val = val.split('/user/', 1)[1].split('/')[0].split('?')[0]

    val = val[:200]
    return val if val.lower() not in ('none', 'null', 'undefined') else ''


def normalize_channel_name(channel):
    """Extract display name from either a plain string or a {id, name} dict."""
    if isinstance(channel, dict):
        val = channel.get('name') or channel.get('id')
    else:
        val = channel
        
    if not isinstance(val, str):
        return ''
    
    val = val.strip()[:200]
    return val if val.lower() not in ('none', 'null', 'undefined') else ''


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
