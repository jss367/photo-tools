"""Read Lightroom .lrcat catalogs and extract keyword data per image file."""

import sqlite3
from contextlib import closing


def _catalog_rows(conn, query, row_id, pause_callback):
    """Read bounded pages and release SQLite's read lock before pausing."""
    last_id = None
    while True:
        if pause_callback:
            pause_callback()
        where = f" WHERE {row_id} > ?" if last_id is not None else ""
        params = (last_id,) if last_id is not None else ()
        with closing(conn.execute(
            query + where + f" ORDER BY {row_id} LIMIT 256", params,
        )) as cursor:
            rows = cursor.fetchall()
        if not rows:
            return
        for row in rows:
            if pause_callback:
                pause_callback()
            yield row
        last_id = rows[-1][0]


def _build_keyword_map(conn, pause_callback=None):
    """Build a dict of keyword_id -> {name, parent, includeOnExport, includeParents}."""
    rows = _catalog_rows(conn,
        "SELECT id_local, name, parent, includeOnExport, includeParents "
        "FROM AgLibraryKeyword", "id_local", pause_callback,
    )
    keywords = {}
    for row in rows:
        keywords[row[0]] = {
            "name": row[1] or "",
            "parent": row[2],
            "include_on_export": row[3],
            "include_parents": row[4],
        }
    return keywords


def _build_hierarchy_path(keyword_id, keyword_map):
    """Walk up the parent chain to build a pipe-delimited hierarchy path.

    Skips the unnamed root keyword and any keywords with includeOnExport=0.
    Returns (flat_keyword, hierarchical_path) or (None, None) if not exportable.
    """
    kw = keyword_map.get(keyword_id)
    if not kw or not kw["include_on_export"] or not kw["name"]:
        return None, None

    parts = [kw["name"]]

    if kw["include_parents"]:
        seen = set()
        parent_id = kw["parent"]
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            parent = keyword_map.get(parent_id)
            if not parent or not parent["name"]:
                break
            if not parent["include_on_export"]:
                break
            parts.append(parent["name"])
            parent_id = parent["parent"]

    parts.reverse()
    flat_keyword = parts[-1]  # the leaf keyword
    hierarchical_path = "|".join(parts)
    return flat_keyword, hierarchical_path


def read_catalog(catalog_path, pause_callback=None):
    """Read a .lrcat catalog and return a dict mapping file paths to keyword data.

    Returns:
        dict: {file_path: {"flat_keywords": set, "hierarchical_keywords": set}}
    """
    conn = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True)
    try:
        keyword_map = _build_keyword_map(conn, pause_callback)

        # Query: join images -> files -> folders -> root folders, with keywords
        query = """
            SELECT
                ki.id_local,
                rf.absolutePath,
                f.pathFromRoot,
                fi.baseName,
                fi.extension,
                ki.tag
            FROM AgLibraryKeywordImage ki
            JOIN Adobe_images ai ON ai.id_local = ki.image
            JOIN AgLibraryFile fi ON fi.id_local = ai.rootFile
            JOIN AgLibraryFolder f ON f.id_local = fi.folder
            JOIN AgLibraryRootFolder rf ON rf.id_local = f.rootFolder
        """

        result = {}
        for row in _catalog_rows(conn, query, "ki.id_local", pause_callback):
            _row_id, abs_path, path_from_root, base_name, extension, keyword_id = row
            file_path = f"{abs_path}{path_from_root}{base_name}.{extension}"

            flat_kw, hier_kw = _build_hierarchy_path(keyword_id, keyword_map)
            if flat_kw is None:
                continue

            if file_path not in result:
                result[file_path] = {
                    "flat_keywords": set(),
                    "hierarchical_keywords": set(),
                }

            entry = result[file_path]
            entry["flat_keywords"].add(flat_kw)
            entry["hierarchical_keywords"].add(hier_kw)

            # Also add parent keywords as flat keywords if includeParents
            if "|" in hier_kw:
                for part in hier_kw.split("|")[:-1]:
                    entry["flat_keywords"].add(part)

        return result
    finally:
        conn.close()
