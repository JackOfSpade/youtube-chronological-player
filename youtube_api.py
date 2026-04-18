"""
youtube_api.py — Pure YouTube Data API v3 calls.

Every function here takes an api_key (and optionally a requests.Session)
and returns parsed data.  No config loading, no caching, no orchestration.
"""

import datetime
import logging
import functools
import requests
from dateutil import parser as dateutil_parser

from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

import config_manager as cfg

logger = logging.getLogger(__name__)

# ── Low-level request wrapper ───────────────────────────────────────────────

_global_session = requests.Session()

# Implement retries for transient HTTP errors (502, 503, 504)
_retries = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[502, 503, 504],
    allowed_methods=["GET"]
)
_adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=_retries)
_global_session.mount('https://', _adapter)
_global_session.mount('http://', _adapter)

def _safe_dict_get(data, *keys):
    """Safely traverse nested dictionaries, guaranteeing a dict return."""
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return {}
    return current if isinstance(current, dict) else {}


@functools.lru_cache(maxsize=2000)
def _parse_iso_datetime(date_str, default_min=False):
    """Parse an ISO date string into a UTC-aware datetime."""
    if not date_str:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc) if default_min else None
    
    try:
        date_str = str(date_str)
        if len(date_str) > 100:
            return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc) if default_min else None
            
        try:
            if date_str.endswith('Z'):
                dt = datetime.datetime.fromisoformat(date_str[:-1] + '+00:00')
            else:
                dt = datetime.datetime.fromisoformat(date_str)
        except ValueError:
            dt = dateutil_parser.parse(date_str)
            
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except (ValueError, TypeError, OverflowError):
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc) if default_min else None


def _api_get(url, params=None, session=None):
    """Perform a GET with timeout and retries. Returns parsed JSON or an error dict on failure."""
    requester = session or _global_session
    try:
        resp = requester.get(url, params=params, timeout=cfg.API_TIMEOUT)
        
        # Explicitly handle quota and rate limits
        if resp.status_code == 403:
            if 'quotaExceeded' in resp.text:
                logger.error("YouTube API Quota Exceeded (403).")
                return {"error": "QUOTA_EXCEEDED", "status_code": 403, "message": "YouTube API Quota Exceeded. Please try again tomorrow."}
            resp.raise_for_status()
        if resp.status_code == 429:
            logger.error("YouTube API Rate Limit Hit (429).")
            return {"error": "RATE_LIMIT_EXCEEDED", "status_code": 429, "message": "Too many requests. Please wait a moment."}
            
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except (requests.RequestException, ValueError, TypeError) as exc:
        err_msg = str(exc)
        if params and 'key' in params:
            err_msg = err_msg.replace(params['key'], '***MASKED***')
        logger.warning("YouTube API request failed: %s — %s", url, err_msg)
        return {"error": "REQUEST_FAILED", "message": err_msg}


# ── Channels ────────────────────────────────────────────────────────────────

def get_uploads_playlist_id(channel_id, api_key, session=None):
    """Return the 'uploads' playlist ID for a channel, or None."""
    if not cfg.is_api_key_valid(api_key):
        return None

    params = {'part': 'contentDetails', 'key': api_key}
    is_handle = channel_id.startswith('@')
    
    if is_handle:
        params['forHandle'] = channel_id
    else:
        params['id'] = channel_id

    data = _api_get(
        "https://www.googleapis.com/youtube/v3/channels",
        params=params,
        session=session,
    )
    if 'error' in data: return data
    items = data.get('items')
    
    # Fallback for handle if not found directly (sometimes handles change or API is finicky)
    if is_handle and (not items or not isinstance(items, list)):
        fallback_data = _api_get(
            "https://www.googleapis.com/youtube/v3/search",
            params={'part': 'snippet', 'type': 'channel', 'q': channel_id, 'maxResults': 1, 'key': api_key},
            session=session
        )
        fb_items = fallback_data.get('items')
        if isinstance(fb_items, list) and fb_items and isinstance(fb_items[0], dict):
            snippet = _safe_dict_get(fb_items[0], 'snippet')
            ch_id = snippet.get('channelId')
            if isinstance(ch_id, str) and ch_id.strip():
                # Now fetch the uploads playlist using the resolved ID
                return get_uploads_playlist_id(ch_id, api_key, session)

    if isinstance(items, list) and items and isinstance(items[0], dict):
        related = _safe_dict_get(items[0], 'contentDetails', 'relatedPlaylists')
        upl = related.get('uploads')
        return str(upl) if isinstance(upl, str) and upl.strip() else None
    return None


def search_channels(query, api_key, session=None):
    """Search for YouTube channels by name.  Returns a list of dicts."""
    if not query or not cfg.is_api_key_valid(api_key):
        return []

    data = _api_get(
        "https://www.googleapis.com/youtube/v3/search",
        params={'part': 'snippet', 'type': 'channel', 'q': query, 'maxResults': 5, 'key': api_key},
        session=session,
    )
    if 'error' in data: return data

    items = data.get('items')
    if not isinstance(items, list):
        items = []

    resolved = []
    for item in items:
        if not isinstance(item, dict): continue
        snippet = item.get('snippet')
        res_id = item.get('id')
        if not isinstance(snippet, dict) or not isinstance(res_id, dict): continue
        
        ch_id = res_id.get('channelId') or snippet.get('channelId')
        if not ch_id or not isinstance(ch_id, str): continue
        
        resolved.append({
            'channelId': str(ch_id),
            'title': str(snippet.get('title') or 'Unknown'),
            'thumbnail': str(_get_thumbnail_url(snippet)),
        })
    return resolved


# ── Videos / Playlist ──────────────────────────────────────────────────────

def fetch_videos_from_playlist(playlist_id, start_date_iso, api_key, session=None):
    """
    Fetch all videos from a playlist published on or after *start_date_iso*.
    Stops paginating once the entire batch is older than the window.
    """
    if not cfg.is_api_key_valid(api_key):
        return []

    videos = []
    next_page_token = ""
    start_dt = _parse_iso_datetime(start_date_iso, default_min=True)

    page_count = 0
    while page_count < 10:
        page_count += 1
        params = {
            'part': 'snippet,status',
            'maxResults': 50,
            'playlistId': playlist_id,
            'key': api_key,
        }
        if next_page_token:
            params['pageToken'] = next_page_token

        data = _api_get(
            "https://www.googleapis.com/youtube/v3/playlistItems",
            params=params,
            session=session,
        )
        if 'error' in data: return data
        items = data.get('items')
        if not items or not isinstance(items, list):
            break

        oldest_in_batch = None
        for item in items:
            if not isinstance(item, dict):
                continue
            snippet = item.get('snippet')
            status = item.get('status')
            if not isinstance(snippet, dict) or 'publishedAt' not in snippet:
                continue
            
            if isinstance(status, dict) and status.get('privacyStatus') != 'public':
                continue
            
            pub_dt = _parse_iso_datetime(snippet['publishedAt'])
            if pub_dt is None:
                continue

            if pub_dt >= start_dt:
                title = snippet.get('title') or ''
                video_id = _safe_dict_get(snippet, 'resourceId').get('videoId', '')
                if not isinstance(video_id, str) or not video_id.strip():
                    continue
                videos.append({
                        'id': str(video_id),
                        'title': str(title),
                        'channelTitle': str(snippet.get('channelTitle') or ''),
                        'channelId': str(snippet.get('channelId') or ''),
                        'publishedAt': str(snippet['publishedAt']),
                        'thumbnail': str(_get_thumbnail_url(snippet)),
                    })
            if oldest_in_batch is None or pub_dt < oldest_in_batch:
                oldest_in_batch = pub_dt

        next_page_token = data.get('nextPageToken')
        if not next_page_token:
            break

        # Playlist returns newest-first; stop once entire batch is outside window
        if oldest_in_batch and oldest_in_batch < start_dt:
            break

    return videos


# ── Comments ────────────────────────────────────────────────────────────────

def fetch_video_comments(video_id, api_key, page_token=None, session=None):
    """Fetch a page of top-level comment threads with replies."""
    if not video_id or not cfg.is_api_key_valid(api_key):
        return {"comments": [], "nextPageToken": None}

    params = {
        'part': 'snippet,replies',
        'videoId': video_id,
        'maxResults': 25,
        'order': 'relevance',
        'key': api_key,
    }
    if page_token:
        params['pageToken'] = page_token

    data = _api_get("https://www.googleapis.com/youtube/v3/commentThreads", params=params, session=session)
    if 'error' in data: return data

    items = data.get('items')
    if not isinstance(items, list):
        items = []

    comments_list = []
    for item in items:
        if not isinstance(item, dict): continue
        top_snippet = _safe_dict_get(item, 'snippet', 'topLevelComment', 'snippet')
        comment_obj = _parse_comment(top_snippet, item.get('id') if isinstance(item.get('id'), str) else '')
        raw_comments = _safe_dict_get(item, 'replies').get('comments')
        comment_obj['replies'] = [
            _parse_comment(_safe_dict_get(r, 'snippet'), r.get('id') if isinstance(r.get('id'), str) else '')
            for r in (raw_comments if isinstance(raw_comments, list) else [])
            if isinstance(r, dict)
        ]
        comments_list.append(comment_obj)

    return {"comments": comments_list, "nextPageToken": data.get('nextPageToken')}


# ── Private helpers ─────────────────────────────────────────────────────────

def _get_thumbnail_url(snippet):
    if not snippet: return ''
    thumbnails = _safe_dict_get(snippet, 'thumbnails')
    url = _safe_dict_get(thumbnails, 'medium').get('url') or _safe_dict_get(thumbnails, 'default').get('url', '')
    if url and not isinstance(url, str): return ''
    if url and not (url.startswith('http://') or url.startswith('https://')): return ''
    return url or ''


def _parse_comment(snippet, comment_id):
    avatar = snippet.get('authorProfileImageUrl') or ''
    if avatar and not isinstance(avatar, str): avatar = ''
    if avatar and not (avatar.startswith('http://') or avatar.startswith('https://')): avatar = ''
    
    try:
        like_count = int(snippet.get('likeCount') or 0)
    except (ValueError, TypeError):
        like_count = 0

    return {
        'id': str(comment_id),
        'author': str(snippet.get('authorDisplayName') or 'Unknown'),
        'avatar': str(avatar),
        'text': str(snippet.get('textDisplay') or ''),
        'publishedAt': str(snippet.get('publishedAt') or ''),
        'likeCount': like_count,
    }


def sort_videos_newest_first(videos):
    """Sort a video list in-place by publishedAt, newest first. Uses fast lexical sort of ISO strings."""
    if not isinstance(videos, list):
        return
    videos.sort(key=lambda v: str(v.get('publishedAt', '')) if isinstance(v, dict) else '', reverse=True)
