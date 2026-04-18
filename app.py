"""
app.py — Flask route definitions.

Thin routing layer: each handler validates input, delegates to the appropriate
service module, and serializes the response.  No business logic lives here.
"""

import json
import logging
from flask import Flask, jsonify, request, render_template, Response
from werkzeug.exceptions import HTTPException

import config_manager as cfg
import youtube_api
import sync_service
import storage_manager
import time

import threading
import uuid

_last_request_times = {} # IP -> last_time
_rate_limit_lock = threading.Lock()

def _get_remote_addr():
    """Identify the original client IP, correctly handling common proxy headers."""
    # check X-Forwarded-For if behind a reverse proxy like Nginx or Cloudflare
    forwarded_for = request.headers.get('X-Forwarded-For')
    if forwarded_for:
        # get the first IP in the list, stripping any port/whitespace
        return str(forwarded_for.split(',')[0]).strip().split(':')[0]
    return request.remote_addr or '127.0.0.1'

def _check_rate_limit(req_type, limit_seconds=1.0):
    """Simple per-IP rate limit for sensitive operations."""
    ip = _get_remote_addr()
    now = time.time()
    key = f"{ip}:{req_type}"
    
    with _rate_limit_lock:
        # Prune old entries to prevent memory growth
        if len(_last_request_times) > 1000:
            # remove anything older than 60 seconds in place
            t_threshold = now - 60
            stale_keys = [k for k, t in _last_request_times.items() if t < t_threshold]
            for k in stale_keys:
                del _last_request_times[k]
                
            # If still too large, keep only the 500 most recent
            if len(_last_request_times) > 1000:
                recent = sorted(_last_request_times.items(), key=lambda x: x[1], reverse=True)[:500]
                _last_request_times.clear()
                _last_request_times.update(recent)

        last = _last_request_times.get(key, 0)
        if now - last < limit_seconds:
            return False
        _last_request_times[key] = now
        return True

def rate_limit(req_type, limit_seconds=1.0):
    """Decorator to enforce rate limiting on a route."""
    import functools
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            if not _check_rate_limit(req_type, limit_seconds):
                return jsonify({"error": "RATE_LIMIT", "message": "Too many requests."}), 429
            return f(*args, **kwargs)
        return wrapped
    return decorator

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB limit for JSON payloads


# ── Global Error Handlers ───────────────────────────────────────────────────

@app.errorhandler(Exception)
def handle_exception(e):
    # Pass through HTTP errors as JSON if they affect API endpoints
    if request.path.startswith('/api/'):
        if isinstance(e, HTTPException):
            return jsonify({'status': 'error', 'message': e.description}), e.code
        logging.exception("Unhandled Exception in API route")
        return jsonify({'status': 'error', 'message': 'Internal Server Error'}), 500
    
    # For non-API routes (e.g. index.html), default behavior
    if isinstance(e, HTTPException):
        return e
    logging.exception("Unhandled Exception in standard route")
    return "Internal Server Error", 500


@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    # Minimal CSP, allowing YouTube embeds and inline styles/scripts that the app uses.
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://www.youtube.com https://s.ytimg.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; frame-src https://www.youtube.com; img-src 'self' https://yt3.ggpht.com https://i.ytimg.com;"
    return response


# ── Pages ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/favicon.ico')
def favicon():
    return '', 204


# ── Queue API ───────────────────────────────────────────────────────────────

@app.route('/api/queue')
@rate_limit('queue', limit_seconds=1.0)
def get_queue():
    try:
        force = request.args.get('force') == 'true'
        return jsonify({
            'queue': sync_service.get_queue(force_sync=force),
            'history': storage_manager.load_history(),
            'api_key_configured': cfg.is_api_configured(),
        })
    except Exception as e:
        logging.exception("Endpoint /api/queue failed")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/sync/stream')
@rate_limit('sync_stream', limit_seconds=2.0)
def sync_stream():
    force = request.args.get('force') == 'true'

    def generate():
        req_id = str(uuid.uuid4())
        try:
            history = storage_manager.load_history()
            yield f"data: {json.dumps({'type': 'init', 'req_id': req_id, 'history': history, 'api_key_configured': cfg.is_api_configured()})}\n\n"
            yield from sync_service.get_queue_stream(force_sync=force)
        except Exception as e:
            logging.exception("SSE stream failed")
            yield f"data: {json.dumps({'type': 'error', 'message': 'Internal Server Error during sync'})}\n\n"

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
            'Transfer-Encoding': 'chunked'
        },
    )


@app.route('/api/sync/status')
@rate_limit('sync_status', limit_seconds=1.0)
def sync_status():
    """Get the current progress and status of the background sync."""
    return jsonify(sync_service._sync_manager.get_status())


@app.route('/api/sync/reset', methods=['POST'])
@rate_limit('sync_reset', limit_seconds=5.0)
def sync_reset():
    """Force-reset the sync state to recover from stalled locks."""
    sync_service._sync_manager.reset()
    return jsonify({'status': 'success', 'message': 'Sync lock released.'})


# ── Config API ──────────────────────────────────────────────────────────────

@app.route('/api/config')
@rate_limit('config', limit_seconds=1.0)
def get_config():
    try:
        config = cfg.load_config()
        if not config:
            return jsonify({'channels': [], 'start_date': ''})
        channels_data = config.get('channels')
        if not isinstance(channels_data, list):
            channels_data = []
        if len(channels_data) > 500:
            channels_data = channels_data[:500]

        return jsonify({
            'channels': channels_data,
            'start_date': str(config.get('start_date') or ''),
        })
    except Exception as e:
        logging.exception("Endpoint /api/config failed")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/channels', methods=['POST'])
@rate_limit('channels', limit_seconds=1.0)
def save_channels():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('channels'), list):
        return jsonify({'status': 'error', 'message': 'Invalid payload'}), 400
        
    try:
        cfg.save_channels(data.get('channels', []))
        return jsonify({'status': 'success'})
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/search_channels')
@rate_limit('search', 1.0)
def search_channels_route():

    try:
        query = request.args.get('q', '').strip()
        if not query or len(query) > 100:
            return jsonify([])
        config = cfg.load_config()
        if not config:
            return jsonify([])
        
        results = youtube_api.search_channels(query, config.get('api_key'))
        if isinstance(results, dict) and 'error' in results:
            status = results.get('status_code', 500)
            return jsonify(results), status
            
        return jsonify(results)
    except Exception as e:
        logging.exception("Endpoint /api/search_channels failed")
        return jsonify([]), 500


# ── Comments API ────────────────────────────────────────────────────────────

@app.route('/api/comments/<video_id>')
@rate_limit('comments', limit_seconds=1.0)
def get_comments(video_id):
    try:
        if len(video_id) > 50 or video_id.lower() in ('none', 'undefined', 'null'):
            return jsonify({"comments": [], "nextPageToken": None})

        page_token = request.args.get('pageToken')
        if page_token and len(page_token) > 200:
            return jsonify({"comments": [], "nextPageToken": None})

        config = cfg.load_config()
        if not config:
            return jsonify({"comments": [], "nextPageToken": None})

        data = youtube_api.fetch_video_comments(
            video_id, config.get('api_key'), page_token,
        )
        if isinstance(data, dict) and data.get('error'):
            status = data.get('status_code', 500)
            return jsonify(data), status
            
        return jsonify(data)
    except Exception as e:
        logging.exception("Endpoint /api/comments failed")
        return jsonify({"comments": [], "nextPageToken": None}), 500


# ── History API ─────────────────────────────────────────────────────────────

@app.route('/api/watched/<video_id>', methods=['POST'])
@rate_limit('watched', 0.05)
def mark_watched(video_id):
        
    try:
        if len(video_id) > 50:
            return jsonify({'status': 'error', 'message': 'ID too long'}), 400
        if video_id.lower() in ('none', 'undefined', 'null'):
            return jsonify({'status': 'error', 'message': 'Invalid ID'}), 400
        storage_manager.mark_watched(video_id)
        return jsonify({'status': 'success'})
    except Exception as e:
        logging.exception("Endpoint /api/watched failed")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/history')
@rate_limit('history', limit_seconds=1.0)
def get_history():
    try:
        return jsonify(storage_manager.load_history())
    except Exception as e:
        logging.exception("Endpoint /api/history failed")
        return jsonify(storage_manager._make_default()), 500


# ── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5001)
