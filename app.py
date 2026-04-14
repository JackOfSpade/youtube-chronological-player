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
    force = request.args.get('force') == 'true'
    return jsonify({
        'queue': sync_service.get_queue(force_sync=force),
        'history': storage_manager.load_history(),
        'api_key_configured': cfg.is_api_configured(),
    })


@app.route('/api/sync/stream')
def sync_stream():
    force = request.args.get('force') == 'true'

    def generate():
        history = storage_manager.load_history()
        yield f"data: {json.dumps({'type': 'init', 'history': history, 'api_key_configured': cfg.is_api_configured()})}\n\n"
        yield from sync_service.get_queue_stream(force_sync=force)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ── Config API ──────────────────────────────────────────────────────────────

@app.route('/api/config')
def get_config():
    config = cfg.load_config()
    if not config:
        return jsonify({'channels': [], 'start_date': ''})
    return jsonify({
        'channels': config.get('channels', []),
        'start_date': config.get('start_date', ''),
    })


@app.route('/api/channels', methods=['POST'])
def save_channels():
    data = request.json
    if not data or not isinstance(data.get('channels'), list):
        return jsonify({'status': 'error', 'message': 'Invalid payload'}), 400
    cfg.save_channels(data['channels'])
    return jsonify({'status': 'success'})


@app.route('/api/search_channels')
def search_channels_route():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    config = cfg.load_config()
    if not config:
        return jsonify([])
    return jsonify(youtube_api.search_channels(query, config.get('api_key')))


# ── Comments API ────────────────────────────────────────────────────────────

@app.route('/api/comments/<video_id>')
def get_comments(video_id):
    config = cfg.load_config()
    if not config:
        return jsonify({"comments": [], "nextPageToken": None})
    return jsonify(youtube_api.fetch_video_comments(
        video_id, config.get('api_key'), request.args.get('pageToken'),
    ))


# ── History API ─────────────────────────────────────────────────────────────

@app.route('/api/watched/<video_id>', methods=['POST'])
def mark_watched(video_id):
    storage_manager.mark_watched(video_id)
    return jsonify({'status': 'success'})


@app.route('/api/history')
def get_history():
    return jsonify(storage_manager.load_history())


# ── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5001)
