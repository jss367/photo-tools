# spotter/tests/test_app.py
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lr-migration'))

from PIL import Image


def _setup_app(tmp_path):
    """Create a test app with sample data."""
    from db import Database
    from app import create_app

    db_path = str(tmp_path / "test.db")
    thumb_dir = str(tmp_path / "thumbs")
    os.makedirs(thumb_dir)

    db = Database(db_path)
    fid = db.add_folder('/photos/2024', name='2024')
    fid2 = db.add_folder('/photos/2024/January', name='January', parent_id=fid)

    p1 = db.add_photo(folder_id=fid, filename='bird1.jpg', extension='.jpg',
                      file_size=1000, file_mtime=1.0, timestamp='2024-01-15T10:00:00')
    p2 = db.add_photo(folder_id=fid2, filename='bird2.jpg', extension='.jpg',
                      file_size=2000, file_mtime=2.0, timestamp='2024-01-20T14:00:00')
    p3 = db.add_photo(folder_id=fid, filename='bird3.jpg', extension='.jpg',
                      file_size=3000, file_mtime=3.0, timestamp='2024-06-10T09:00:00')

    db.update_photo_rating(p1, 3)
    db.update_photo_rating(p3, 5)

    k1 = db.add_keyword('Cardinal')
    k2 = db.add_keyword('Sparrow')
    db.tag_photo(p1, k1)
    db.tag_photo(p2, k2)

    # Create thumbnail files
    for pid in [p1, p2, p3]:
        Image.new('RGB', (100, 100)).save(os.path.join(thumb_dir, f"{pid}.jpg"))

    app = create_app(db_path=db_path, thumb_cache_dir=thumb_dir)
    return app, db


def test_index_redirects_to_browse(tmp_path):
    """GET / redirects to /browse."""
    app, _ = _setup_app(tmp_path)
    client = app.test_client()
    resp = client.get('/')
    assert resp.status_code == 302
    assert '/browse' in resp.headers['Location']


def test_browse_page(tmp_path):
    """GET /browse returns 200."""
    app, _ = _setup_app(tmp_path)
    client = app.test_client()
    resp = client.get('/browse')
    assert resp.status_code == 200


def test_api_folders(tmp_path):
    """GET /api/folders returns folder tree."""
    app, _ = _setup_app(tmp_path)
    client = app.test_client()
    resp = client.get('/api/folders')
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 2
    paths = {f['path'] for f in data}
    assert '/photos/2024' in paths


def test_api_photos_default(tmp_path):
    """GET /api/photos returns all photos."""
    app, _ = _setup_app(tmp_path)
    client = app.test_client()
    resp = client.get('/api/photos')
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data['photos']) == 3
    assert 'total' in data


def test_api_photos_pagination(tmp_path):
    """GET /api/photos supports pagination."""
    app, _ = _setup_app(tmp_path)
    client = app.test_client()
    resp = client.get('/api/photos?per_page=2&page=1')
    data = resp.get_json()
    assert len(data['photos']) == 2

    resp = client.get('/api/photos?per_page=2&page=2')
    data = resp.get_json()
    assert len(data['photos']) == 1


def test_api_photos_filter_folder(tmp_path):
    """GET /api/photos?folder_id= filters by folder."""
    app, db = _setup_app(tmp_path)
    folders = db.get_folder_tree()
    jan = [f for f in folders if f['name'] == 'January'][0]

    client = app.test_client()
    resp = client.get(f'/api/photos?folder_id={jan["id"]}')
    data = resp.get_json()
    assert len(data['photos']) == 1
    assert data['photos'][0]['filename'] == 'bird2.jpg'


def test_api_photos_filter_rating(tmp_path):
    """GET /api/photos?rating_min= filters by minimum rating."""
    app, _ = _setup_app(tmp_path)
    client = app.test_client()
    resp = client.get('/api/photos?rating_min=4')
    data = resp.get_json()
    assert len(data['photos']) == 1
    assert data['photos'][0]['filename'] == 'bird3.jpg'


def test_api_photos_filter_date_range(tmp_path):
    """GET /api/photos?date_from=&date_to= filters by date range."""
    app, _ = _setup_app(tmp_path)
    client = app.test_client()
    resp = client.get('/api/photos?date_from=2024-01-01&date_to=2024-02-01')
    data = resp.get_json()
    assert len(data['photos']) == 2


def test_api_photos_filter_keyword(tmp_path):
    """GET /api/photos?keyword= filters by keyword."""
    app, _ = _setup_app(tmp_path)
    client = app.test_client()
    resp = client.get('/api/photos?keyword=Cardinal')
    data = resp.get_json()
    assert len(data['photos']) == 1
    assert data['photos'][0]['filename'] == 'bird1.jpg'


def test_api_photo_detail(tmp_path):
    """GET /api/photos/<id> returns photo with keywords."""
    app, db = _setup_app(tmp_path)
    client = app.test_client()
    photos = db.get_photos()
    pid = photos[0]['id']

    resp = client.get(f'/api/photos/{pid}')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['filename'] == 'bird1.jpg'
    assert 'keywords' in data


def test_api_keywords(tmp_path):
    """GET /api/keywords returns keyword tree."""
    app, _ = _setup_app(tmp_path)
    client = app.test_client()
    resp = client.get('/api/keywords')
    assert resp.status_code == 200
    data = resp.get_json()
    names = {k['name'] for k in data}
    assert 'Cardinal' in names
    assert 'Sparrow' in names


def test_thumbnail_serving(tmp_path):
    """GET /thumbnails/<id>.jpg serves thumbnail from cache."""
    app, db = _setup_app(tmp_path)
    client = app.test_client()
    photos = db.get_photos()
    pid = photos[0]['id']

    resp = client.get(f'/thumbnails/{pid}.jpg')
    assert resp.status_code == 200
    assert resp.content_type in ('image/jpeg', 'image/jpg')
