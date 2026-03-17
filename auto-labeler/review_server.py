"""Flask server for reviewing auto-labeler predictions.

Usage:
    python auto-labeler/review_server.py [--data-dir /tmp/photo-review] [--port 5000]
"""

import argparse
import json
import logging
import os
import sys
import webbrowser

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lr-migration'))

from flask import Flask, jsonify, request, send_from_directory, render_template
from xmp_writer import write_xmp_sidecar

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def create_app(data_dir):
    """Create the Flask app configured with a data directory.

    Args:
        data_dir: path containing results.json and thumbnails/
    """
    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
    app.config['DATA_DIR'] = data_dir

    def _load_results():
        with open(os.path.join(data_dir, 'results.json')) as f:
            return json.load(f)

    def _save_results(data):
        with open(os.path.join(data_dir, 'results.json'), 'w') as f:
            json.dump(data, f, indent=2)

    @app.route('/')
    def index():
        return render_template('review.html')

    @app.route('/api/photos')
    def get_photos():
        data = _load_results()
        category = request.args.get('category')
        if category:
            data['photos'] = [p for p in data['photos'] if p['category'] == category]
        return jsonify(data)

    @app.route('/api/accept/<filename>', methods=['POST'])
    def accept(filename):
        data = _load_results()
        for photo in data['photos']:
            if photo['filename'] == filename:
                # Write prediction to XMP as a plain keyword
                write_xmp_sidecar(
                    photo['xmp_path'],
                    flat_keywords={photo['prediction']},
                    hierarchical_keywords=set(),
                )
                photo['status'] = 'accepted'
                _save_results(data)
                return jsonify({'ok': True, 'status': 'accepted'})
        return jsonify({'error': 'not found'}), 404

    @app.route('/api/skip/<filename>', methods=['POST'])
    def skip(filename):
        data = _load_results()
        for photo in data['photos']:
            if photo['filename'] == filename:
                photo['status'] = 'skipped'
                _save_results(data)
                return jsonify({'ok': True, 'status': 'skipped'})
        return jsonify({'error': 'not found'}), 404

    @app.route('/api/accept-batch', methods=['POST'])
    def accept_batch():
        body = request.get_json()
        category = body.get('category')
        min_confidence = body.get('min_confidence', 0.0)

        data = _load_results()
        accepted = 0
        for photo in data['photos']:
            if photo['status'] != 'pending':
                continue
            if category and photo['category'] != category:
                continue
            if photo['confidence'] < min_confidence:
                continue
            try:
                write_xmp_sidecar(
                    photo['xmp_path'],
                    flat_keywords={photo['prediction']},
                    hierarchical_keywords=set(),
                )
                photo['status'] = 'accepted'
                accepted += 1
            except Exception:
                log.warning("Failed to write XMP for %s", photo['filename'], exc_info=True)

        _save_results(data)
        return jsonify({'ok': True, 'accepted': accepted})

    @app.route('/thumbnails/<filename>')
    def thumbnail(filename):
        return send_from_directory(os.path.join(data_dir, 'thumbnails'), filename)

    return app


def main():
    parser = argparse.ArgumentParser(description="Review auto-labeler predictions.")
    parser.add_argument("--data-dir", default="/tmp/photo-review", help="Directory with results.json")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    app = create_app(args.data_dir)
    webbrowser.open(f"http://localhost:{args.port}")
    app.run(host='127.0.0.1', port=args.port, debug=False)


if __name__ == "__main__":
    main()
