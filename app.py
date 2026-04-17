"""
app.py — Flask route definitions.

Thin routing layer: each handler validates input, delegates to the appropriate
service module, and serializes the response.  No business logic lives here.
"""

import json
import logging
from flask import Flask, jsonify, request, render_template, Response

import config_manager as cfg
import youtube_api
import sync_service
import storage_manager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)

app = Flask(__name__)


# ── Pages ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/favicon.ico')
def favicon():
    return '', 204


# ── Queue API ───────────────────────────────────────────────────────────────

@app.route('/api/queue')
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
def sync_stream():
    force = request.args.get('force') == 'true'

    def generate():
        try:
            history = storage_manager.load_history()
            yield f"data: {json.dumps({'type': 'init', 'history': history, 'api_key_configured': cfg.is_api_configured()})}\n\n"
            yield from sync_service.get_queue_stream(force_sync=force)
        except Exception as e:
            logging.exception("SSE stream failed")
            yield f"data: {json.dumps({'type': 'error', 'message': 'Internal Server Error during sync'})}\n\n"

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ── Config API ──────────────────────────────────────────────────────────────

@app.route('/api/config')
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
def save_channels():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('channels'), list):
        return jsonify({'status': 'error', 'message': 'Invalid payload'}), 400
    channels = data.get('channels', [])
    if len(channels) > 100:
        return jsonify({'status': 'error', 'message': 'Too many channels'}), 400
    sanitized = []
    for c in channels:
        if not isinstance(c, (str, dict)):
            return jsonify({'status': 'error', 'message': 'Invalid channel format'}), 400
        if isinstance(c, str):
            c_str = c.strip()
            if len(c_str) > 200:
                return jsonify({'status': 'error', 'message': 'Channel string too large'}), 400
            sanitized.append(c_str)
        else:
            cid = str(c.get('id') or '').strip()
            cname = str(c.get('name') or '').strip()
            if len(cid) > 200 or len(cname) > 200:
                return jsonify({'status': 'error', 'message': 'Channel properties too large'}), 400
            sanitized.append({'id': cid, 'name': cname})
    try:
        cfg.save_channels(sanitized)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/search_channels')
def search_channels_route():
    try:
        query = request.args.get('q', '').strip()
        if not query or len(query) > 100:
            return jsonify([])
        config = cfg.load_config()
        if not config:
            return jsonify([])
        return jsonify(youtube_api.search_channels(query, config.get('api_key')))
    except Exception as e:
        logging.exception("Endpoint /api/search_channels failed")
        return jsonify([]), 500


# ── Comments API ────────────────────────────────────────────────────────────

@app.route('/api/comments/<video_id>')
def get_comments(video_id):
    try:
        if len(video_id) > 50:
            return jsonify({"comments": [], "nextPageToken": None})

        page_token = request.args.get('pageToken')
        if page_token and len(page_token) > 200:
            return jsonify({"comments": [], "nextPageToken": None})

        config = cfg.load_config()
        if not config:
            return jsonify({"comments": [], "nextPageToken": None})

        return jsonify(youtube_api.fetch_video_comments(
            video_id, config.get('api_key'), page_token,
        ))
    except Exception as e:
        logging.exception("Endpoint /api/comments failed")
        return jsonify({"comments": [], "nextPageToken": None}), 500


# ── History API ─────────────────────────────────────────────────────────────

@app.route('/api/watched/<video_id>', methods=['POST'])
def mark_watched(video_id):
    try:
        if len(video_id) > 50:
            return jsonify({'status': 'error', 'message': 'ID too long'}), 400
        storage_manager.mark_watched(video_id)
        return jsonify({'status': 'success'})
    except Exception as e:
        logging.exception("Endpoint /api/watched failed")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/history')
def get_history():
    try:
        return jsonify(storage_manager.load_history())
    except Exception as e:
        logging.exception("Endpoint /api/history failed")
        return jsonify(storage_manager._make_default()), 500


# ── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5002)
