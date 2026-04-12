"""
youtube_api.py — Pure YouTube Data API v3 calls.

Every function here takes an api_key (and optionally a requests.Session)
and returns parsed data.  No config loading, no caching, no orchestration.
"""

import logging
import requests
from dateutil import parser as dateutil_parser

import config_manager as cfg

logger = logging.getLogger(__name__)


# ── Low-level request wrapper ───────────────────────────────────────────────

def _api_get(url, params=None, session=None):
    """Perform a GET with timeout.  Returns parsed JSON or {} on failure."""
    requester = session or requests
    try:
        resp = requester.get(url, params=params, timeout=cfg.API_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
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
    if items:
        return items[0]['contentDetails']['relatedPlaylists']['uploads']
    return None


def search_channels(query, api_key):
    """Search for YouTube channels by name.  Returns a list of dicts."""
    if not query or not cfg.is_api_key_valid(api_key):
        return []

    data = _api_get(
        "https://www.googleapis.com/youtube/v3/search",
        params={'part': 'snippet', 'type': 'channel', 'q': query, 'maxResults': 5, 'key': api_key},
    )

    return [
        {
            'channelId': item['snippet']['channelId'],
            'title': item['snippet']['title'],
            'thumbnail': _get_thumbnail_url(item['snippet']),
        }
        for item in data.get('items', [])
    ]


# ── Videos / Playlist ──────────────────────────────────────────────────────

def fetch_videos_from_playlist(playlist_id, start_date_iso, api_key, session=None):
    """
    Fetch all videos from a playlist published on or after *start_date_iso*.
    Stops paginating once the entire batch is older than the window.
    """
    videos = []
    next_page_token = ""
    start_dt = dateutil_parser.parse(start_date_iso)
    if start_dt.tzinfo is None:
        import datetime
        start_dt = start_dt.replace(tzinfo=datetime.timezone.utc)

    while True:
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
        if not items:
            break

        oldest_in_batch = None
        for item in items:
            snippet = item['snippet']
            pub_dt = dateutil_parser.parse(snippet['publishedAt'])

            if pub_dt >= start_dt:
                videos.append({
                    'id': snippet['resourceId']['videoId'],
                    'title': snippet['title'],
                    'channelTitle': snippet['channelTitle'],
                    'channelId': snippet['channelId'],
                    'publishedAt': snippet['publishedAt'],
                    'thumbnail': _get_thumbnail_url(snippet),
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

    comments_list = []
    for item in data.get('items', []):
        top_snippet = item['snippet']['topLevelComment']['snippet']
        comment_obj = _parse_comment(top_snippet, item['id'])
        comment_obj['replies'] = [
            _parse_comment(r['snippet'], r['id'])
            for r in item.get('replies', {}).get('comments', [])
        ]
        comments_list.append(comment_obj)

    return {"comments": comments_list, "nextPageToken": data.get('nextPageToken')}


# ── Private helpers ─────────────────────────────────────────────────────────

def _get_thumbnail_url(snippet):
    thumbnails = snippet.get('thumbnails', {})
    return thumbnails.get('medium', thumbnails.get('default', {})).get('url', '')


def _parse_comment(snippet, comment_id):
    return {
        'id': comment_id,
        'author': snippet.get('authorDisplayName', 'Unknown'),
        'avatar': snippet.get('authorProfileImageUrl', ''),
        'text': snippet.get('textDisplay', ''),
        'publishedAt': snippet.get('publishedAt', ''),
        'likeCount': snippet.get('likeCount', 0),
    }


def sort_videos_newest_first(videos):
    """Sort a video list in-place by publishedAt, newest first."""
    videos.sort(key=lambda v: dateutil_parser.parse(v['publishedAt']), reverse=True)
