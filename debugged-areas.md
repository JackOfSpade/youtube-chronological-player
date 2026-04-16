# Debugged Areas & Edge Cases Audit

This document logs the areas of the `youtube-chronological-player` application audited for edge cases, potential failures, and subtle bugs. 

## 1. Config Manager Missing File Initialization (`config_manager.py`)
- **Bug/Edge Case**: The previous version silently ignored attempts to save channel lists if `config.yaml` did not already exist locally, expecting users to copy the example. This would result in an unexplained failure on fresh installations.
- **Fix**: Updated `save_channels()` to automatically instantiate a baseline config dictionary (inheriting defaults like `lookback_hours: 36` and placeholder `api_key`) and persist it immediately if `config.yaml` does not exist. 
- **Additional hardening**: Added a strict `try/except` block catching `Exception` to `load_config()` to ensure corrupted YAML syntax doesn't crash the entire API service and gracefully falls back to `None`. 

## 2. API Concurrent Connection Exhaustion (`sync_service.py`)
- **Bug/Edge Case**: A single global `requests.Session()` object was passed through an entire `concurrent.futures.ThreadPoolExecutor` context. Since standard connection pools in `requests` have extremely low `pool_maxsize` limits compared to cross-thread usage, concurrent API pagination calls would silently starve or trigger underlying thread un-safety bounds with urllib3 state.
- **Fix**: Removed the shared session. Pushed `requests.Session()` orchestration directly into the inner closure thread `_process_channel`. Each thread worker now scopes its own completely isolated connection pool, providing genuinely safe `Session` pooling strictly for paging within that single channel's fetch operations.

## 3. Empty/Null JSON Payload Guard (`app.py`)
- **Bug/Edge Case**: A raw UI post for saving channels without explicitly supplying the `'channels'` key properly within a dictionary, or omitting `Content-Type: application/json` altogether, would cause `request.json` to evaluate safely as `None`. However, the route handled `not data` by returning `400`, but neglected that if the client passed a raw `List` (`[...]`), `request.json` interprets as `list` rather than `dict`, leading to an unhandled `AttributeError` on `data.get('channels')`.
- **Fix**: Strictly enforced `isinstance(data, dict)` in the `POST /api/channels` route to prevent a 500 error if weird data arrays were pushed instead of JSON dictionaries.

## 4. Deleted & Private Video Aggregation (`youtube_api.py`)
- **Bug/Edge Case**: The `uploads` playlist of a channel includes items the creator has made private or deleted. These lack true valid video contexts but their generic `snippet` blocks pass all previous validity checks and bubble up to the UI/AI filters showing up with "Private video" tiles.
- **Fix**: Enforced an explicit filter within the pagination block of `fetch_videos_from_playlist()` that drops items displaying `'Private video'` or `'Deleted video'` directly by title name, eliminating them from the entire pipeline from the very bottom API layer. 

## 5. Storage Access IO Gracefulness (`storage_manager.py`)
- **Bug/Edge Case**: Checking `os.path.exists()` on `history.json` before performing an unlocked file open leaves a narrow race window where a file handle becomes unreadable (i.e. permissions edge-case) or missing precisely at execution time. The specific `try...except (json.JSONDecodeError, ValueError):` handling was too narrow and let `OSError` bypass it entirely, crashing the API.
- **Fix**: Elevated the specific decoder exceptions to capture all generic `Exception` types during the history file loading phase. Safe defaults are aggressively ensured without bringing down backend reads.
