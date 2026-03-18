"""Scan folders, discover photos, read metadata, populate database."""

import logging
import os
from pathlib import Path

from PIL import Image

from image_loader import SUPPORTED_EXTENSIONS
from grouping import read_exif_timestamp

log = logging.getLogger(__name__)


def scan(root, db, progress_callback=None):
    """Walk a folder tree, discover photos, read metadata, populate database.

    Args:
        root: path to the root folder to scan
        db: Database instance
        progress_callback: optional callable(current, total) for progress reporting
    """
    root_path = Path(root)
    if not root_path.is_dir():
        log.warning("Root path does not exist or is not a directory: %s", root)
        return

    # Discover all image files
    image_files = sorted(
        f for f in root_path.rglob('*')
        if f.is_file()
        and f.suffix.lower() in SUPPORTED_EXTENSIONS
        and not f.name.startswith('.')
    )

    total = len(image_files)
    log.info("Found %d images in %s", total, root)

    # Build folder cache: path -> folder_id
    folder_cache = {}

    def _ensure_folder(folder_path):
        """Ensure a folder and all its parents exist in the DB. Returns folder_id."""
        folder_str = str(folder_path)
        if folder_str in folder_cache:
            return folder_cache[folder_str]

        # Ensure parent exists first
        parent_id = None
        if folder_path != root_path:
            parent_id = _ensure_folder(folder_path.parent)

        folder_id = db.add_folder(
            path=folder_str,
            name=folder_path.name,
            parent_id=parent_id,
        )
        folder_cache[folder_str] = folder_id
        return folder_id

    for i, image_path in enumerate(image_files):
        folder_id = _ensure_folder(image_path.parent)

        # Read dimensions without fully decoding the image
        width, height = None, None
        try:
            with Image.open(str(image_path)) as img:
                width, height = img.size
        except Exception:
            log.debug("Could not read dimensions from %s", image_path)

        # Read EXIF timestamp
        timestamp = None
        try:
            dt = read_exif_timestamp(str(image_path))
            if dt:
                timestamp = dt.isoformat()
        except Exception:
            log.debug("Could not read EXIF timestamp from %s", image_path)

        # File stats
        stat = image_path.stat()
        file_size = stat.st_size
        file_mtime = stat.st_mtime

        # Check for XMP sidecar mtime
        xmp_path = image_path.with_suffix('.xmp')
        xmp_mtime = None
        if xmp_path.exists():
            xmp_mtime = xmp_path.stat().st_mtime

        db.add_photo(
            folder_id=folder_id,
            filename=image_path.name,
            extension=image_path.suffix.lower(),
            file_size=file_size,
            file_mtime=file_mtime,
            xmp_mtime=xmp_mtime,
            timestamp=timestamp,
            width=width,
            height=height,
        )

        if progress_callback:
            progress_callback(i + 1, total)

    db.update_folder_counts()
    log.info("Scan complete: %d photos indexed", total)
