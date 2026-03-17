# auto-labeler/tests/test_review_server.py
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lr-migration'))

from PIL import Image


def _create_test_review_data(tmpdir):
    """Create a minimal results.json and thumbnails dir for testing."""
    thumb_dir = os.path.join(tmpdir, "thumbnails")
    os.makedirs(thumb_dir)

    # Create a test image and XMP (so accept can write to it)
    img = Image.new('RGB', (100, 100), color='red')
    img_path = os.path.join(tmpdir, "bird1.jpg")
    img.save(img_path)

    from xmp_writer import write_xmp_sidecar
    xmp_path = os.path.join(tmpdir, "bird1.xmp")
    write_xmp_sidecar(xmp_path, flat_keywords={'Dyke Marsh'}, hierarchical_keywords=set())

    # Create thumbnail
    thumb = Image.new('RGB', (100, 100), color='red')
    thumb.save(os.path.join(thumb_dir, "bird1.jpg"))

    results = {
        'folder': tmpdir,
        'settings': {'threshold': 0.4, 'thumbnail_size': 400},
        'stats': {'total': 1, 'new': 1, 'refinement': 0, 'disagreement': 0, 'match': 0},
        'photos': [
            {
                'filename': 'bird1.jpg',
                'image_path': img_path,
                'xmp_path': xmp_path,
                'existing_species': [],
                'prediction': 'Northern cardinal',
                'confidence': 0.85,
                'category': 'new',
                'status': 'pending',
            }
        ],
    }

    results_path = os.path.join(tmpdir, "results.json")
    with open(results_path, 'w') as f:
        json.dump(results, f)

    return results_path


def _create_multi_photo_data(tmpdir):
    """Create results.json with multiple photos for filter/batch testing."""
    thumb_dir = os.path.join(tmpdir, "thumbnails")
    os.makedirs(thumb_dir)

    from xmp_writer import write_xmp_sidecar

    photos = []
    for i, (name, category, confidence) in enumerate([
        ('bird1.jpg', 'new', 0.85),
        ('bird2.jpg', 'new', 0.60),
        ('bird3.jpg', 'disagreement', 0.90),
        ('bird4.jpg', 'refinement', 0.75),
    ]):
        img_path = os.path.join(tmpdir, name)
        xmp_path = os.path.join(tmpdir, name.replace('.jpg', '.xmp'))
        Image.new('RGB', (100, 100)).save(img_path)
        Image.new('RGB', (50, 50)).save(os.path.join(thumb_dir, name))
        write_xmp_sidecar(xmp_path, flat_keywords=set(), hierarchical_keywords=set())
        photos.append({
            'filename': name,
            'image_path': img_path,
            'xmp_path': xmp_path,
            'existing_species': [],
            'prediction': f'Species {i}',
            'confidence': confidence,
            'category': category,
            'status': 'pending',
        })

    results = {
        'folder': tmpdir,
        'settings': {'threshold': 0.4, 'thumbnail_size': 400},
        'stats': {'total': 4, 'new': 2, 'refinement': 1, 'disagreement': 1, 'match': 0},
        'photos': photos,
    }

    results_path = os.path.join(tmpdir, "results.json")
    with open(results_path, 'w') as f:
        json.dump(results, f)

    return results_path


def test_get_photos():
    """GET /api/photos returns the photo list."""
    from review_server import create_app

    with tempfile.TemporaryDirectory() as tmpdir:
        _create_test_review_data(tmpdir)
        app = create_app(tmpdir)
        client = app.test_client()

        resp = client.get('/api/photos')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data['photos']) == 1
        assert data['photos'][0]['prediction'] == 'Northern cardinal'


def test_get_photos_category_filter():
    """GET /api/photos?category=new returns only new photos."""
    from review_server import create_app

    with tempfile.TemporaryDirectory() as tmpdir:
        _create_multi_photo_data(tmpdir)
        app = create_app(tmpdir)
        client = app.test_client()

        resp = client.get('/api/photos?category=new')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data['photos']) == 2
        assert all(p['category'] == 'new' for p in data['photos'])

        resp = client.get('/api/photos?category=disagreement')
        data = resp.get_json()
        assert len(data['photos']) == 1
        assert data['photos'][0]['category'] == 'disagreement'


def test_accept_writes_xmp():
    """POST /api/accept/<filename> writes keyword to XMP and updates status."""
    from review_server import create_app
    from compare import read_xmp_keywords

    with tempfile.TemporaryDirectory() as tmpdir:
        results_path = _create_test_review_data(tmpdir)
        app = create_app(tmpdir)
        client = app.test_client()

        resp = client.post('/api/accept/bird1.jpg')
        assert resp.status_code == 200

        # Check XMP was updated
        xmp_path = os.path.join(tmpdir, "bird1.xmp")
        keywords = read_xmp_keywords(xmp_path)
        assert 'Northern cardinal' in keywords
        assert 'Dyke Marsh' in keywords  # existing keyword preserved

        # Check status updated in results.json
        with open(results_path) as f:
            data = json.load(f)
        assert data['photos'][0]['status'] == 'accepted'


def test_accept_not_found():
    """POST /api/accept/<filename> returns 404 for unknown file."""
    from review_server import create_app

    with tempfile.TemporaryDirectory() as tmpdir:
        _create_test_review_data(tmpdir)
        app = create_app(tmpdir)
        client = app.test_client()

        resp = client.post('/api/accept/nonexistent.jpg')
        assert resp.status_code == 404


def test_skip_updates_status():
    """POST /api/skip/<filename> marks photo as skipped."""
    from review_server import create_app

    with tempfile.TemporaryDirectory() as tmpdir:
        results_path = _create_test_review_data(tmpdir)
        app = create_app(tmpdir)
        client = app.test_client()

        resp = client.post('/api/skip/bird1.jpg')
        assert resp.status_code == 200

        with open(results_path) as f:
            data = json.load(f)
        assert data['photos'][0]['status'] == 'skipped'


def test_skip_not_found():
    """POST /api/skip/<filename> returns 404 for unknown file."""
    from review_server import create_app

    with tempfile.TemporaryDirectory() as tmpdir:
        _create_test_review_data(tmpdir)
        app = create_app(tmpdir)
        client = app.test_client()

        resp = client.post('/api/skip/nonexistent.jpg')
        assert resp.status_code == 404


def test_accept_batch():
    """POST /api/accept-batch accepts all pending photos matching criteria."""
    from review_server import create_app
    from compare import read_xmp_keywords

    with tempfile.TemporaryDirectory() as tmpdir:
        results_path = _create_multi_photo_data(tmpdir)
        app = create_app(tmpdir)
        client = app.test_client()

        resp = client.post('/api/accept-batch',
                           json={'category': 'new', 'min_confidence': 0.0})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['accepted'] == 2

        # Verify results.json was updated
        with open(results_path) as f:
            results = json.load(f)
        new_photos = [p for p in results['photos'] if p['category'] == 'new']
        assert all(p['status'] == 'accepted' for p in new_photos)
        # Non-new photos should still be pending
        other_photos = [p for p in results['photos'] if p['category'] != 'new']
        assert all(p['status'] == 'pending' for p in other_photos)


def test_accept_batch_with_min_confidence():
    """POST /api/accept-batch respects min_confidence filter."""
    from review_server import create_app

    with tempfile.TemporaryDirectory() as tmpdir:
        results_path = _create_multi_photo_data(tmpdir)
        app = create_app(tmpdir)
        client = app.test_client()

        # Only accept photos with confidence >= 0.80
        resp = client.post('/api/accept-batch',
                           json={'min_confidence': 0.80})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['accepted'] == 2  # bird1 (0.85) and bird3 (0.90)

        with open(results_path) as f:
            results = json.load(f)
        accepted = [p for p in results['photos'] if p['status'] == 'accepted']
        assert len(accepted) == 2
        assert all(p['confidence'] >= 0.80 for p in accepted)


def test_accept_batch_skips_already_accepted():
    """POST /api/accept-batch does not re-accept already accepted photos."""
    from review_server import create_app

    with tempfile.TemporaryDirectory() as tmpdir:
        _create_multi_photo_data(tmpdir)
        app = create_app(tmpdir)
        client = app.test_client()

        # Accept bird1 individually first
        client.post('/api/accept/bird1.jpg')

        # Batch accept 'new' category
        resp = client.post('/api/accept-batch',
                           json={'category': 'new', 'min_confidence': 0.0})
        data = resp.get_json()
        # Only bird2 should be newly accepted (bird1 already done)
        assert data['accepted'] == 1


def test_thumbnail_serving():
    """GET /thumbnails/<filename> serves thumbnail images."""
    from review_server import create_app

    with tempfile.TemporaryDirectory() as tmpdir:
        _create_test_review_data(tmpdir)
        app = create_app(tmpdir)
        client = app.test_client()

        resp = client.get('/thumbnails/bird1.jpg')
        assert resp.status_code == 200
        assert resp.content_type.startswith('image/')


def test_index_route():
    """GET / returns 200 (either template or placeholder)."""
    from review_server import create_app

    with tempfile.TemporaryDirectory() as tmpdir:
        _create_test_review_data(tmpdir)
        app = create_app(tmpdir)
        client = app.test_client()

        resp = client.get('/')
        assert resp.status_code == 200
