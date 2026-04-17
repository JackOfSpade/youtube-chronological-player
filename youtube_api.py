"""
youtube_api.py — Pure YouTube Data API v3 calls.

Every function here takes an api_key (and optionally a requests.Session)
and returns parsed data.  No config loading, no caching, no orchestration.
"""

import datetime
import logging
import requests
from dateutil import parser as dateutil_parser

import config_manager as cfg

logger = logging.getLogger(__name__)


# ── Low-level request wrapper ───────────────────────────────────────────────

def _safe_dict_get(data, *keys):
    """Safely traverse nested dictionaries, guaranteeing a dict return."""
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return {}
    return current if isinstance(current, dict) else {}


def _parse_iso_datetime(date_str, default_min=False):
    """Parse an ISO date string into a UTC-aware datetime."""
    if not date_str:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc) if default_min else None
    if isinstance(date_str, str) and len(date_str) > 100:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc) if default_min else None
    try:
        dt = dateutil_parser.parse(str(date_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except (ValueError, TypeError, OverflowError):
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc) if default_min else None


def _api_get(url, params=None, session=None):
    """Perform a GET with timeout.  Returns parsed JSON or {} on failure."""
    requester = session or requests
    try:
        resp = requester.get(url, params=params, timeout=cfg.API_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.warning("YouTube API request failed: %s — %s", url, exc)
        return {}


# ── Channels ────────────────────────────────────────────────────────────────

def get_uploads_playlist_id(channel_id, api_key, session=None):
    """Return the 'uploads' playlist ID for a channel, or None."""
    data = _api_get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={'part': 'contentDetails', 'id': channel_id, 'key': api_key},
        session=session,
    )
    items = data.get('items')
    if isinstance(items, list) and items and isinstance(items[0], dict):
        upl = _safe_dict_get(items[0], 'contentDetails', 'relatedPlaylists').get('uploads')
        return str(upl) if isinstance(upl, (str, int)) else None
    return None


def search_channels(query, api_key):
    """Search for YouTube channels by name.  Returns a list of dicts."""
    if not query or not cfg.is_api_key_valid(api_key):
        return []

    data = _api_get(
        "https://www.googleapis.com/youtube/v3/search",
        params={'part': 'snippet', 'type': 'channel', 'q': query, 'maxResults': 5, 'key': api_key},
    )

    items = data.get('items')
    if not isinstance(items, list):
        items = []

    return [
        {
            'channelId': str(item['snippet']['channelId']),
            'title': str(item['snippet'].get('title') or ''),
            'thumbnail': str(_get_thumbnail_url(item['snippet'])),
        }
        for item in items
        if isinstance(item, dict) and isinstance(item.get('snippet'), dict) and isinstance(item['snippet'].get('channelId'), (str, int)) and item['snippet'].get('channelId')
    ]


# ── Videos / Playlist ──────────────────────────────────────────────────────

def fetch_videos_from_playlist(playlist_id, start_date_iso, api_key, session=None):
    """
    Fetch all videos from a playlist published on or after *start_date_iso*.
    Stops paginating once the entire batch is older than the window.
    """
    videos = []
    next_page_token = ""
    start_dt = _parse_iso_datetime(start_date_iso, default_min=True)

    page_count = 0
    while page_count < 10:
        page_count += 1
        params = {
            'part': 'snippet',
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
        items = data.get('items')
        if not items or not isinstance(items, list):
            break

        oldest_in_batch = None
        for item in items:
            if not isinstance(item, dict):
                continue
            snippet = item.get('snippet')
            if not isinstance(snippet, dict) or 'publishedAt' not in snippet:
                continue
            
            pub_dt = _parse_iso_datetime(snippet['publishedAt'])
            if pub_dt is None:
                continue

            if pub_dt >= start_dt:
                title = snippet.get('title') or ''
                video_id = _safe_dict_get(snippet, 'resourceId').get('videoId', '')
                if not isinstance(video_id, (str, int)) or not video_id:
                    continue
                if title not in ('Private video', 'Deleted video'):
                    videos.append({
                        'id': str(video_id),
                        'title': str(title),
                        'channelTitle': str(snippet.get('channelTitle') or ''),
                        'channelId': str(snippet.get('channelId') or ''),
                        'publishedAt': str(snippet['publishedAt']),
                        'thumbnail': str(_get_thumbnail_url(snippet)),
                    })
            oldest_in_batch = pub_dt

        next_page_token = data.get('nextPageToken')
        if not next_page_token:
            break

        # Playlist returns newest-first; stop once entire batch is outside window
        if oldest_in_batch and oldest_in_batch < start_dt:
            break

    return videos


# ── Comments ────────────────────────────────────────────────────────────────

def fetch_video_comments(video_id, api_key, page_token=None):
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

    data = _api_get("https://www.googleapis.com/youtube/v3/commentThreads", params=params)

    items = data.get('items')
    if not isinstance(items, list):
        items = []

    comments_list = []
    for item in items:
        if not isinstance(item, dict): continue
        top_snippet = _safe_dict_get(item, 'snippet', 'topLevelComment', 'snippet')
        comment_obj = _parse_comment(top_snippet, item.get('id') if isinstance(item.get('id'), (str, int)) else '')
        raw_comments = _safe_dict_get(item, 'replies').get('comments')
        comment_obj['replies'] = [
            _parse_comment(_safe_dict_get(r, 'snippet'), r.get('id') if isinstance(r.get('id'), (str, int)) else '')
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
    """Sort a video list in-place by publishedAt, newest first."""
    if not isinstance(videos, list):
        return
    videos.sort(key=lambda v: _parse_iso_datetime(v.get('publishedAt', '') if isinstance(v, dict) else '', default_min=True), reverse=True)
