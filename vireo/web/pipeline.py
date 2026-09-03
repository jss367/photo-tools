"""Pipeline launch: the process job, its plan, saved processes and config.

Step 3 of the ``create_app`` split (launch half). Everything here moved
verbatim out of ``app.py``. The review-side pipeline endpoints (results,
reflow, regroup-live, detach, group state/apply, mask variant) are the
next slice.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os

from flask import Blueprint, jsonify, request
from jobs import SLOT_CAP
from runtime_warnings import build_cpu_runtime_warning, runtime_warning_work_units
from services.pipeline_launch import (
    apply_no_model_auto_skip,
    resolve_remote_archive_target,
)

log = logging.getLogger(__name__)


def create_pipeline_blueprint(
    get_db,
    json_error,
    get_runner,
    db_path,
    config,
    *,
    reject_visual_collection,
    coerce_collection_id,
    invalidate_missing_originals,
    read_raw_config_file,
    settings_write_lock,
):
    """Build the pipeline-launch blueprint.

    ``config`` is the Flask app's config mapping (``THUMB_CACHE_DIR`` and
    ``COMPUTATION_CACHE_DIR`` are read when the job runs). The keyword
    arguments are ``create_app`` closures shared with other domains:
    the collection guards, the missing-originals cache, and the settings
    file's raw reader + write lock (the pipeline route records recent
    destinations and process deletion clears the global default).
    """
    blueprint = Blueprint("pipeline", __name__)

    @blueprint.route("/api/pipeline/slots")
    def api_pipeline_slots():
        """Pipeline-slot occupancy for the Start/Queue button.

        The pipeline page polls this to decide whether the next click
        will start immediately (``active < slot_cap``) or land on the
        queue (``active >= slot_cap``), and to render the small
        "N running . M queued" status line near the Start button.

        Counts only ``type == 'pipeline'`` jobs — standalone scan,
        classify, ingest, etc. don't consume pipeline slots.
        """
        runner = get_runner()
        active = 0
        queued = 0
        for j in runner.list_jobs():
            if j.get("type") != "pipeline":
                continue
            status = j.get("status")
            if status in {"running", "pausing", "paused"}:
                active += 1
            elif status == "queued":
                queued += 1
        return jsonify({
            "active": active,
            "queued": queued,
            "slot_cap": SLOT_CAP,
        })

    @blueprint.route("/api/pipeline/plan", methods=["POST"])
    def api_pipeline_plan():
        """Return the per-stage plan for the pipeline given current UI selections.

        Truth source for the Pipeline page's status pills + plan summary —
        each stage's state ("Will run", "Will skip", "Already done") and
        its summary text are computed from the same gates the actual
        pipeline job uses, so the user is never told a stage is done when
        it isn't (or vice versa).

        Body mirrors a subset of the /api/jobs/pipeline body so the page
        can pass through the same selections the user is staging.
        """
        from pipeline_plan import PipelinePlanParams, compute_plan

        body = request.get_json(silent=True) or {}
        # source_paths bounds the request. A Z8 SD card can hold ~5k RAWs;
        # 50k is a generous ceiling that keeps the JSON body well under
        # Flask's defaults while covering realistic multi-card imports.
        # Beyond it the plan endpoint refuses rather than truncate, since
        # a silently-truncated list would mis-classify late files as new.
        #
        # An explicit empty list is *not* the same as a missing key: the
        # frontend sends ``[]`` when the user is in import mode and has
        # deselected every preview file (a genuine no-op import). Falling
        # back to whole-workspace scope in that case would re-introduce
        # the misleading "Already done" pills this endpoint exists to
        # prevent. So preserve None vs [] all the way through.
        if "source_paths" in body:
            source_paths = body.get("source_paths")
            if not isinstance(source_paths, list):
                return json_error("source_paths must be a list", 400)
            if len(source_paths) > 50000:
                return json_error(
                    f"source_paths too large ({len(source_paths)} > 50000)", 400,
                )
            if not all(isinstance(p, str) for p in source_paths):
                return json_error("source_paths entries must be strings", 400)
        else:
            source_paths = None
        deprecated_plan_fields = [
            key for key in (
                "destination", "local_processing",
                "remote_target_id", "remote_subpath",
            )
            if body.get(key) not in (None, "", False)
        ]
        if deprecated_plan_fields:
            return json_error(
                "import/archive fields are no longer accepted by the "
                "process planner; use the Import page for photo imports",
                400,
            )
        # Reject the previous strategy-name shape so the plan endpoint's
        # contract matches /api/jobs/pipeline: an old caller that still
        # sends {"strategy": "quick_look"} must fail here rather than
        # silently planning a full-pipeline run the job route would then
        # reject too.
        if "strategy" in body:
            return json_error(
                "strategy is no longer accepted; send process_id (a "
                "saved_processes id) or explicit stage flags",
                400,
            )
        # hash_duplicate_paths is the frontend's pre-computed set of source
        # paths that ingest() will skip via the global duplicate gate (copy
        # mode + skip_duplicates=True; metadata-first matching or content
        # hashes depending on the verify-by-hash toggle — the preview and
        # ingest share import_dedup.DuplicateChecker, so the set matches
        # what ingest will actually skip). Same shape/limits as
        # source_paths; the planner subtracts these from new_count so the
        # plan doesn't overstate work for duplicate-skipped files.
        if "hash_duplicate_paths" in body:
            hash_duplicate_paths = body.get("hash_duplicate_paths")
            if not isinstance(hash_duplicate_paths, list):
                return json_error("hash_duplicate_paths must be a list", 400)
            if len(hash_duplicate_paths) > 50000:
                return json_error(
                    f"hash_duplicate_paths too large "
                    f"({len(hash_duplicate_paths)} > 50000)", 400,
                )
            if not all(isinstance(p, str) for p in hash_duplicate_paths):
                return json_error(
                    "hash_duplicate_paths entries must be strings", 400,
                )
        else:
            hash_duplicate_paths = None
        # Folder scope (Process page): resolve folder_ids to their
        # active-workspace subtree photo ids with the same guards as
        # /api/jobs/pipeline, so the plan describes exactly the photos a
        # folder-scoped run would process.
        scope_photo_ids = None
        folder_ids = body.get("folder_ids")
        if folder_ids is not None:
            if (
                not isinstance(folder_ids, list)
                or not folder_ids
                or any(
                    isinstance(fid, bool) or not isinstance(fid, int)
                    for fid in folder_ids
                )
            ):
                return json_error(
                    "folder_ids must be a non-empty list of integers"
                )
            db = get_db()
            ws_for_folders = db._active_workspace_id
            subtree_ids = set()
            # Mirror the /api/jobs/pipeline folder resolution exactly so the
            # plan describes the same photos the run will process. Without
            # the path-based fallback, legacy folders with parent_id=NULL
            # (whose paths sit under the selected root) are silently omitted
            # from the plan even though the run walks them via
            # _folder_subtree_ids_by_path.
            from db import _chunks  # noqa: PLC0415
            for fid in folder_ids:
                linked = db.conn.execute(
                    "SELECT 1 FROM workspace_folders "
                    "WHERE workspace_id = ? AND folder_id = ?",
                    (ws_for_folders, fid),
                ).fetchone()
                if not linked:
                    return json_error("folder not found", 404)
                subtree_ids.update(db.get_folder_subtree_ids(fid))
                # Intersect the path-based descendants with the workspace's
                # folder set so nothing leaks in from another workspace that
                # happens to share a path prefix.
                for chunk in _chunks(db._folder_subtree_ids_by_path(fid)):
                    marks = ",".join("?" for _ in chunk)
                    rows = db.conn.execute(
                        f"SELECT folder_id FROM workspace_folders "
                        f"WHERE workspace_id = ? AND folder_id IN ({marks})",
                        [ws_for_folders] + list(chunk),
                    )
                    subtree_ids.update(r["folder_id"] for r in rows)
            scope_photo_ids = []
            for chunk in _chunks(list(subtree_ids)):
                marks = ",".join("?" for _ in chunk)
                scope_photo_ids.extend(
                    r["id"] for r in db.conn.execute(
                        f"SELECT id FROM photos WHERE folder_id IN ({marks})",
                        tuple(chunk),
                    )
                )
        # Expand a saved-process id the same way /api/jobs/pipeline does so
        # the plan describes the run the same job body would produce. Without
        # this, the Identify-birds process (skip_regroup=True + species review)
        # shows Group as "Disabled" in the plan even though the actual run
        # prepares species review results — the exact "plan summary is wrong
        # for the default workflow" transparency failure the review flagged.
        # Expansion supplies *defaults*; explicitly-present body keys still
        # win, so a Custom caller can pin one flag on top of a process
        # (mirrors the merge /api/jobs/pipeline runs). The process page's Run
        # sends explicit stage flags (including miss_enabled/review_mode), so
        # process_id is optional here.
        # Key presence — not truthiness — decides whether a process was
        # requested, matching /api/jobs/pipeline: a present-but-null process_id
        # must 400 (not be treated as omitted), so previewing and starting the
        # same body agree instead of the plan describing a run the job route
        # would reject.
        if "process_id" in body:
            pid = body.get("process_id")
            if not isinstance(pid, int) or isinstance(pid, bool):
                kind = "null" if pid is None else type(pid).__name__
                return json_error(
                    f"process_id must be an integer, got {kind}", 400
                )
            try:
                expanded = get_db().resolve_process(pid)
            except ValueError as e:
                return json_error(str(e), 404)
            body = {**expanded, **body}
            # Mirror the job path: a process with Eye Keypoints on opts into
            # eye scoring, so the plan reflects the eye stage running rather
            # than deferring to the workspace eye_detect_enabled default.
            if (
                not body.get("skip_eye_keypoints")
                and body.get("eye_detect_override") is None
            ):
                body["eye_detect_override"] = True
        review_mode = body.get("review_mode")
        if review_mode is not None and not isinstance(review_mode, str):
            return json_error(
                "review_mode must be a string or null, got "
                f"{type(review_mode).__name__}", 400,
            )
        # ``eye_detect_override`` is tri-state (None/True/False); accept
        # only bool or missing so the plan matches what /api/jobs/pipeline
        # would do.
        eye_detect_override_body = body.get("eye_detect_override")
        if (
            eye_detect_override_body is not None
            and not isinstance(eye_detect_override_body, bool)
        ):
            return json_error(
                f"eye_detect_override must be boolean, got "
                f"{type(eye_detect_override_body).__name__}", 400,
            )
        # Reject visual collections before the plan resolves the scope:
        # ``compute_plan`` → ``pipeline._resolve_collection_photo_ids()`` →
        # ``get_collection_photos()`` evaluates ``rules`` only and ignores
        # ``visual_json``. Without this guard, an API caller or stale/bookmarked
        # Process page body posting a visual collection id would receive a plan
        # computed over every metadata match — advertising a widened runnable
        # job even though ``/api/jobs/pipeline`` already rejects the same id at
        # start time (Codex review r3622041639).
        db = get_db()
        collection_id = coerce_collection_id(body.get("collection_id"))
        if collection_id is False:
            return json_error("collection_id must be an integer", 400)
        err = reject_visual_collection(db, collection_id)
        if err is not None:
            return err
        params = PipelinePlanParams(
            collection_id=collection_id,
            photo_ids=scope_photo_ids,
            exclude_photo_ids=body.get("exclude_photo_ids") or [],
            skip_classify=bool(body.get("skip_classify")),
            skip_extract_masks=bool(body.get("skip_extract_masks")),
            skip_eye_keypoints=bool(body.get("skip_eye_keypoints")),
            eye_detect_override=eye_detect_override_body,
            skip_regroup=bool(body.get("skip_regroup")),
            model_ids=body.get("model_ids") or (
                [body["model_id"]] if body.get("model_id") else []
            ),
            labels_files=body.get("labels_files") or [],
            reclassify=bool(body.get("reclassify")),
            source_paths=source_paths,
            hash_duplicate_paths=hash_duplicate_paths,
            preview_max_size=body.get("preview_max_size"),
            review_mode=review_mode,
        )
        return jsonify(compute_plan(db, params, db_path))

    def _coerce_process_fields(body, *, require_name):
        """Return ``(kwargs, error_string)`` for create/update.

        Only keys present in ``body`` are returned (so the update path treats
        an omitted key as "leave unchanged"). Flags must be real booleans and
        ``review_mode`` must be ``None`` or ``"species"`` — a bad value 400s
        here rather than being silently coerced.
        """
        kwargs = {}
        if require_name or "name" in body:
            name = body.get("name")
            if not isinstance(name, str) or not name.strip():
                return None, "name is required"
            kwargs["name"] = name
        for key in (
            "skip_classify", "skip_extract_masks", "skip_eye_keypoints",
            "skip_regroup", "miss_enabled",
        ):
            if key in body:
                if not isinstance(body[key], bool):
                    return None, f"{key} must be a boolean"
                kwargs[key] = body[key]
        if "review_mode" in body:
            rm = body["review_mode"]
            if rm is not None and rm != "species":
                return None, "review_mode must be 'species' or null"
            kwargs["review_mode"] = rm
        return kwargs, None

    @blueprint.route("/api/processes")
    def api_list_processes():
        """List all saved processes (global; ordered for the pickers)."""
        db = get_db()
        return jsonify(db.get_saved_processes())

    @blueprint.route("/api/processes", methods=["POST"])
    def api_create_process():
        db = get_db()
        body = request.get_json(silent=True) or {}
        kwargs, err = _coerce_process_fields(body, require_name=True)
        if err is not None:
            return json_error(err)
        try:
            pid = db.create_saved_process(**kwargs)
        except ValueError as e:
            return json_error(str(e))
        return jsonify(db.get_saved_process(pid))

    @blueprint.route("/api/processes/<int:process_id>", methods=["PUT"])
    def api_update_process(process_id):
        db = get_db()
        body = request.get_json(silent=True) or {}
        kwargs, err = _coerce_process_fields(body, require_name=False)
        if err is not None:
            return json_error(err)
        try:
            existed = db.update_saved_process(process_id, **kwargs)
        except ValueError as e:
            return json_error(str(e))
        if not existed:
            return json_error("process not found", 404)
        return jsonify(db.get_saved_process(process_id))

    @blueprint.route("/api/processes/<int:process_id>", methods=["DELETE"])
    def api_delete_process(process_id):
        db = get_db()
        if not db.delete_saved_process(process_id):
            return json_error("process not found", 404)
        # Per-workspace default pointers are nulled inside the DB call. Clear
        # the app-wide default too if it pointed here (config-file I/O the DB
        # layer intentionally leaves to the caller). Use the *raw* on-disk
        # config (not ``cfg.load()``) so we don't fossilize every current
        # DEFAULTS value as an explicit user override — a future change to
        # DEFAULTS would otherwise be invisible on this install.
        import config as cfg

        with settings_write_lock:
            raw = read_raw_config_file()
            pipeline_raw = raw.get("pipeline")
            if (
                isinstance(pipeline_raw, dict)
                and pipeline_raw.get("default_process_id") == process_id
            ):
                pipeline_raw["default_process_id"] = None
                cfg.save(raw)
        return jsonify({"ok": True})

    @blueprint.route("/api/pipeline/extract-readiness")
    def api_extract_readiness():
        """Report SAM2/DINOv2 download status for the Extract Features card."""
        import config as cfg
        from dino_embed import DINOV2_VARIANTS, dinov2_status
        from masking import SAM2_VARIANTS, sam2_status

        db = get_db()
        pipeline_cfg = db.get_effective_config(cfg.load()).get("pipeline", {})
        sam2_variant = request.args.get("sam2_variant") or pipeline_cfg.get("sam2_variant")
        dinov2_variant = request.args.get("dinov2_variant") or pipeline_cfg.get("dinov2_variant")

        return jsonify({
            "sam2": sam2_status(sam2_variant),
            "sam2_known": sam2_variant in SAM2_VARIANTS,
            "dinov2": dinov2_status(dinov2_variant),
            "dinov2_known": dinov2_variant in DINOV2_VARIANTS,
            "sam_variant_warning": db.sam_variant_rerun_warning(sam2_variant),
        })

    @blueprint.route("/api/jobs/pipeline", methods=["POST"])
    def api_job_pipeline():
        """Process cataloged photos through thumbnails and analysis stages.

        Overlaps I/O stages and interleaves detection with classification.
        Import is the sole catalog-admission boundary; this route accepts only
        existing workspace photos through folder or collection scope.
        """
        from pipeline_job import PipelineParams, run_pipeline_job

        body = request.get_json(silent=True) or {}

        # Reject the previous strategy-name shape ({"strategy": "quick_look"},
        # "identify", "full", "cull_ready"). That vocabulary was replaced by
        # saved-process ids; without an explicit 400 here, an old caller's
        # `strategy` field is silently dropped and the request falls through
        # to a default full-pipeline run — silently reclassifying/regrouping
        # the whole collection when the caller meant a quick-look scan.
        if "strategy" in body:
            return json_error(
                "strategy is no longer accepted; send process_id (a "
                "saved_processes id) or explicit stage flags",
                400,
            )

        # Saved process: expand a process id server-side into stage flags so
        # the import page, the process page, and import→process chaining share
        # one vocabulary (a saved_processes row). Key presence — not
        # truthiness — decides whether a process was requested: a
        # present-but-null process_id must 400 rather than silently fall
        # through to default processing for a caller who thought null meant
        # "no processing" (that case is expressed by not calling this
        # endpoint at all). The process page's Run instead sends explicit
        # stage flags (including miss_enabled/review_mode), so process_id is
        # optional.
        db = get_db()
        process_id = None
        if "process_id" in body:
            process_id = body.get("process_id")
            if not isinstance(process_id, int) or isinstance(process_id, bool):
                kind = (
                    "null" if process_id is None
                    else type(process_id).__name__
                )
                return json_error(f"process_id must be an integer, got {kind}")
            try:
                expanded = db.resolve_process(process_id)
            except ValueError as e:
                return json_error(str(e), 404)
            # Expansion supplies *defaults*; explicitly-present body keys
            # win, so a caller can pin one flag on top of a process.
            body = {**expanded, **body}
            # The process's Eye Keypoints toggle is an explicit opt-in (the
            # Process-page checkbox sends eye_detect_override with it). Mirror
            # that for a by-id run so the eye stage runs instead of deferring
            # to the workspace eye_detect_enabled default; an explicit caller
            # override still wins.
            if (
                not body.get("skip_eye_keypoints")
                and body.get("eye_detect_override") is None
            ):
                body["eye_detect_override"] = True

        source = body.get("source")
        sources = body.get("sources")
        collection_id = body.get("collection_id")
        source_snapshot_id = body.get("source_snapshot_id")

        filesystem_scopes = []
        if source:
            filesystem_scopes.append("source")
        if sources:
            filesystem_scopes.append("sources")
        if source_snapshot_id is not None:
            filesystem_scopes.append("source_snapshot_id")
        if filesystem_scopes:
            return json_error(
                "Process only accepts photos already in the workspace; "
                "use Import for " + ", ".join(filesystem_scopes)
            )

        # Pipeline stages iterate ``thread_db.get_collection_photos(...)``
        # (see pipeline_job / classify_job / sharpness / culling), which
        # evaluates ``rules`` only. Reject visual collections here so a
        # run isn't silently scoped to every metadata-matching photo
        # instead of the visually-matched subset.
        err = reject_visual_collection(db, collection_id)
        if err is not None:
            return err

        # Folder scope: resolve folder_ids to their active-workspace subtrees
        # and materialize an ad-hoc collection, then proceed as a collection
        # run — the same pattern import runs use (see pipeline_job's
        # collection_stage). The rest of the app treats a folder scope as
        # its subtree, so a workspace root must include every descendant's
        # photos, not just the ones hanging directly off the root.
        #
        # The ad-hoc collection is *not* inserted here: it is deferred
        # until after every other request check has passed AND after
        # ``PipelineParams`` has been fully constructed (see the block
        # just after the ``PipelineParams(...)`` call below). If we
        # inserted up front, a later 400 (relative destination,
        # remote_target_id mismatch, local_processing conflict,
        # folder_template with '..', or a coercion-time exception inside
        # PipelineParams) would leave a stray "Process …" collection in
        # the workspace even though no job was queued.
        # ``exclude_paths`` / ``exclude_photo_ids`` are validated up front —
        # before the folder-scope subtree is resolved — so a folder-scoped
        # request that includes preview deselections can honor them at
        # collection-materialization time. run_pipeline_job's
        # ``_filter_excluded`` only checks ``exclude_photo_ids``; without
        # applying ``exclude_paths`` at the ad-hoc collection boundary, a
        # deselected path is still thumbnailed, classified, and regrouped
        # once the folder collection materializes. Early validation also
        # matches the coercion the PipelineParams constructor would
        # otherwise perform via ``set(body.get(field, []))``, where a JSON
        # ``null`` or unhashable list entry would raise TypeError and leave
        # a stray "Process …" collection behind. Key-presence (not
        # truthiness) matters: missing keys fall through to the default,
        # but ``{"exclude_paths": null}`` must 400.
        excluded_paths_set: set[str] = set()
        if "exclude_paths" in body:
            _paths_value = body["exclude_paths"]
            if not isinstance(_paths_value, list):
                return json_error(
                    "exclude_paths must be a list, got "
                    f"{type(_paths_value).__name__}"
                )
            for _entry in _paths_value:
                if not isinstance(_entry, str):
                    return json_error(
                        "exclude_paths entries must be strings, got "
                        f"{type(_entry).__name__}"
                    )
            excluded_paths_set = set(_paths_value)
        excluded_photo_ids_set: set[int] = set()
        if "exclude_photo_ids" in body:
            _ids_value = body["exclude_photo_ids"]
            if not isinstance(_ids_value, list):
                return json_error(
                    "exclude_photo_ids must be a list, got "
                    f"{type(_ids_value).__name__}"
                )
            for _entry in _ids_value:
                # bool is a subclass of int; exclude it explicitly so the
                # accepted values stay honest integers.
                if isinstance(_entry, bool) or not isinstance(_entry, int):
                    return json_error(
                        "exclude_photo_ids entries must be integers, got "
                        f"{type(_entry).__name__}"
                    )
            excluded_photo_ids_set = set(_ids_value)

        folder_ids = body.get("folder_ids")
        pending_folder_collection = None
        if folder_ids is not None:
            # A folder scope is itself a scope — combining it with any other
            # scope selector is ambiguous. Reject *all* other scopes, not
            # just ``collection_id``: with ``source``/``sources``, later
            # ``skip_scan`` handling would silently ignore them; with
            # ``source_snapshot_id``, run_pipeline_job would clear the
            # folder-derived ``collection_id`` and run the snapshot scope
            # instead — the job would process a different scope than the
            # request implies.
            #
            # ``source``/``sources`` are checked for truthiness (not just
            # ``is not None``) because the rest of this endpoint treats
            # empty string / empty list as omitted (see the required-scope
            # check below and the ``if sources:`` / ``elif source:``
            # dispatch above). Otherwise a generic pipeline form that
            # always emits ``source: ""`` / ``sources: []`` would falsely
            # 400 every folder-scoped run.
            other_scopes = []
            if collection_id is not None:
                other_scopes.append("collection_id")
            if source:
                other_scopes.append("source")
            if sources:
                other_scopes.append("sources")
            if source_snapshot_id is not None:
                other_scopes.append("source_snapshot_id")
            if other_scopes:
                return json_error(
                    "folder_ids cannot be combined with "
                    + ", ".join(other_scopes)
                )
            if (
                not isinstance(folder_ids, list)
                or not folder_ids
                or any(
                    isinstance(fid, bool) or not isinstance(fid, int)
                    for fid in folder_ids
                )
            ):
                return json_error(
                    "folder_ids must be a non-empty list of integers"
                )
            # Range-check each folder id before it ever reaches sqlite3
            # parameter binding. A JSON payload can carry an integer wider
            # than SQLite's signed 64-bit column type (e.g. 2**63), which
            # would raise OverflowError inside the workspace-linked lookup
            # below and escape as a 500. source_snapshot_id already has the
            # same guard just below; folder-scoped runs need it too.
            _SQLITE_INT_MIN = -(1 << 63)
            _SQLITE_INT_MAX = (1 << 63) - 1
            if any(
                fid < _SQLITE_INT_MIN or fid > _SQLITE_INT_MAX
                for fid in folder_ids
            ):
                return json_error(
                    "folder_ids contains a value outside SQLite's "
                    "signed 64-bit integer range"
                )
            # Reject folders the active workspace has no claim on (mirrors
            # the rescan guard): a stale UI or crafted request must not
            # pollute this workspace with another workspace's scan output.
            # get_folder_subtree_ids itself refuses to walk out of the
            # active workspace, so unrelated descendants can never leak in.
            ws_for_folders = db._active_workspace_id
            subtree_ids = set()
            # Import _chunks so the path-prefix workspace filter below can
            # split large legacy subtrees across multiple IN(...) statements.
            from db import _SQLITE_PARAM_CHUNK_SIZE, _chunks  # noqa: PLC0415
            for fid in folder_ids:
                linked = db.conn.execute(
                    "SELECT 1 FROM workspace_folders "
                    "WHERE workspace_id = ? AND folder_id = ?",
                    (ws_for_folders, fid),
                ).fetchone()
                if not linked:
                    return json_error("folder not found", 404)
                # Union — a workspace can link both a root and a nested
                # folder, so the same descendant can appear twice.
                subtree_ids.update(db.get_folder_subtree_ids(fid))
                # get_folder_subtree_ids walks folders.parent_id only, so
                # legacy rows whose parent_id is NULL — but whose paths sit
                # under the requested folder — never appear. Every other
                # subtree consumer in db.py (folder deletion, rescan,
                # missing-originals, workspace linking) already reads through
                # _folder_subtree_ids_by_path for exactly this reason;
                # without the same fallback here, processing a workspace root
                # would silently skip legacy descendant photos the rest of
                # the app still treats as part of that folder. Intersect the
                # path-based descendants with the workspace's folder set so
                # nothing leaks in from another workspace that happens to
                # share a path prefix.
                for chunk in _chunks(db._folder_subtree_ids_by_path(fid)):
                    marks = ",".join("?" for _ in chunk)
                    rows = db.conn.execute(
                        f"SELECT folder_id FROM workspace_folders "
                        f"WHERE workspace_id = ? AND folder_id IN ({marks})",
                        [ws_for_folders] + list(chunk),
                    )
                    subtree_ids.update(r["folder_id"] for r in rows)
            # A large workspace root can expand into thousands of descendant
            # folder ids — more than SQLite's per-statement bound-parameter
            # cap on legacy builds (SQLITE_MAX_VARIABLE_NUMBER = 999). Chunk
            # the IN(...) lookup so a wide folder subtree doesn't blow up as
            # an OperationalError before the job is even queued. Same pattern
            # db.py uses for other large id scopes (see _chunks in db.py).
            subtree_id_list = list(subtree_ids)
            # Fetch the folder paths for the whole subtree so we can compose
            # per-photo full paths and honor ``exclude_paths`` at the
            # collection boundary. Skip the lookup entirely when the user
            # sent no path exclusions — a workspace-root scope can span
            # thousands of folders and the join is pointless when nothing
            # would be filtered.
            folder_path_by_id: dict[int, str] = {}
            if excluded_paths_set:
                for start in range(0, len(subtree_id_list), _SQLITE_PARAM_CHUNK_SIZE):
                    chunk = subtree_id_list[start:start + _SQLITE_PARAM_CHUNK_SIZE]
                    marks = ",".join("?" for _ in chunk)
                    for r in db.conn.execute(
                        f"SELECT id, path FROM folders WHERE id IN ({marks})",
                        tuple(chunk),
                    ):
                        folder_path_by_id[r["id"]] = r["path"] or ""
            photo_ids = []
            for start in range(0, len(subtree_id_list), _SQLITE_PARAM_CHUNK_SIZE):
                chunk = subtree_id_list[start:start + _SQLITE_PARAM_CHUNK_SIZE]
                marks = ",".join("?" for _ in chunk)
                # Select (id, folder_id, filename) so we can compose each
                # photo's full path in-Python to compare against
                # ``exclude_paths``. Matching the same os.path.join shape
                # scanner/ingest use for their ``skip_paths`` sets keeps
                # deselections consistent across import and process runs.
                # Filter ``exclude_photo_ids`` here too so the ad-hoc
                # collection reflects exactly what the user asked to
                # process; ``_filter_excluded`` would drop them again later
                # but there is no reason to bake them into the collection
                # membership.
                for r in db.conn.execute(
                    f"SELECT id, folder_id, filename FROM photos "
                    f"WHERE folder_id IN ({marks})",
                    tuple(chunk),
                ):
                    if r["id"] in excluded_photo_ids_set:
                        continue
                    if excluded_paths_set:
                        full = os.path.join(
                            folder_path_by_id.get(r["folder_id"], ""),
                            r["filename"] or "",
                        )
                        if full in excluded_paths_set:
                            continue
                    photo_ids.append(r["id"])
            first = db.get_folder(folder_ids[0])
            leaf = os.path.basename(
                (first["path"] or "").rstrip("/\\")
            ) or "folders"
            pending_folder_collection = {
                "leaf": leaf, "photo_ids": photo_ids,
            }

        if (
            not source and not sources and not collection_id
            and not source_snapshot_id and pending_folder_collection is None
        ):
            return json_error(
                "collection_id or folder_ids required"
            )

        # Validate type before touching SQLite. Non-integer bodies (objects,
        # arrays, non-numeric strings, floats, bools) would otherwise reach
        # sqlite3 parameter binding and raise ProgrammingError, surfacing as
        # an opaque 500 instead of a clean 4xx.
        if source_snapshot_id is not None and (
            isinstance(source_snapshot_id, bool)
            or not isinstance(source_snapshot_id, int)
        ):
            return json_error("source_snapshot_id must be an integer")

        # Resolve the snapshot synchronously so clients get 404 at request
        # time instead of a 200 followed by an asynchronous job failure.
        if (
            source_snapshot_id is not None
            and db.get_new_images_snapshot(source_snapshot_id) is None
        ):
            return json_error(
                f"source_snapshot_id {source_snapshot_id} not found",
                status=404,
            )

        # Validate source directories — skipped when a snapshot is present,
        # since run_pipeline_job overrides source/sources with the snapshot's
        # folders. Rejecting on stale placeholder paths would falsely 400 an
        # otherwise-valid snapshot-backed run.
        if source_snapshot_id is None:
            from image_loader import is_excluded_scan_path

            # Reject other-app data bundles (Apple Photos / Aperture / Photo
            # Booth) before any stat: ``os.path.isdir`` on a ``.photoslibrary``
            # path — or a symlink to one — itself trips the macOS
            # "access data from other apps" TCC prompt, defeating the guards
            # inside scan()/discover_source_files() that run_pipeline_job will
            # eventually call. Mirror the same pre-stat rejection here so a
            # saved or user-typed pipeline source can't reach isdir first.
            if sources:
                for s in sources:
                    if is_excluded_scan_path(s):
                        return json_error(
                            f"source is inside a macOS app-managed library "
                            f"and cannot be scanned: {s}"
                        )
                    if not os.path.isdir(s):
                        return json_error(f"source directory not found: {s}")
            elif source:
                if is_excluded_scan_path(source):
                    return json_error(
                        f"source is inside a macOS app-managed library and "
                        f"cannot be scanned: {source}"
                    )
                if not os.path.isdir(source):
                    return json_error(f"source directory not found: {source}")

        deprecated_import_fields = [
            key for key in (
                "destination", "local_processing",
                "remote_target_id", "remote_subpath",
            )
            if body.get(key) not in (None, "", False)
        ]
        if deprecated_import_fields:
            return json_error(
                "import/archive fields are no longer accepted by "
                "/api/jobs/pipeline; use /api/jobs/import-photos or the "
                "Import page for photo imports"
            )

        destination = body.get("destination")
        local_processing = bool(body.get("local_processing"))
        remote_target_id = (body.get("remote_target_id") or "").strip()
        remote_subpath = body.get("remote_subpath", "")
        if remote_subpath and not isinstance(remote_subpath, str):
            return json_error("remote_subpath must be a string")
        # Copy-ingest ("destination") is incompatible with snapshot runs:
        # ingest would copy entire source folders, then snapshot filtering
        # would drop the destination-scanned photo ids, producing empty
        # downstream stages after an expensive copy. Fail fast.
        if destination and source_snapshot_id is not None:
            return json_error(
                "destination is not allowed when source_snapshot_id is set"
            )
        # Same reasoning for collection- and folder-scope runs: any
        # ``collection_id`` (whether supplied directly or derived from
        # ``folder_ids`` below) sets ``skip_scan`` in run_pipeline_job, so
        # ``scanner_stage`` returns before the ingest block that would copy
        # to ``destination``. The job's step list still includes ingest for
        # ``params.destination``, so the user would see a queued process run
        # that ignores the requested copy target and leaves ingest pending.
        # Local-processing runs with these scopes are rejected below with a
        # dedicated message; this guards the plain-``destination`` case that
        # otherwise slips through when ``local_processing`` is false.
        if destination and (
            collection_id is not None or folder_ids is not None
        ):
            return json_error(
                "destination is not allowed with collection_id or folder_ids "
                "— collection and folder scopes skip ingest, so a copy "
                "destination would never be written"
            )
        if destination and not os.path.isabs(destination):
            return json_error("destination must be an absolute path")
        # Remote (SSH) archive destination — mirrors the Move page's
        # remote-target request shape (remote_target_id + subpath). It only
        # makes sense with local_processing: files must be staged and
        # processed on local disk, then rsynced to the NAS by the archive
        # stage; a plain copy-mode ingest writes through the local
        # filesystem and can't target an SSH path.
        remote_archive_config = None
        if remote_target_id and destination:
            return json_error(
                "destination and remote_target_id are mutually exclusive — "
                "pick a local archive path or a saved remote target, not both"
            )
        if remote_subpath and not remote_target_id:
            return json_error("remote_subpath requires remote_target_id")
        if remote_target_id and not local_processing:
            return json_error(
                "remote_target_id requires local_processing — files are "
                "staged and processed locally, then archived to the remote "
                "target over SSH"
            )
        if remote_target_id:
            # Refuse at request time when no GNU rsync exists or the
            # target is unknown/unsafe — starting a job guaranteed to
            # fail its storage preflight helps nobody (mirrors the
            # move-folder endpoint).
            remote_archive_config, _rsync_bin, err = (
                resolve_remote_archive_target(
                    get_db(), remote_target_id, remote_subpath,
                    json_error=json_error,
                )
            )
            if err is not None:
                return err
        if local_processing and not destination and not remote_target_id:
            return json_error(
                "local_processing requires a destination or a remote target"
            )
        if local_processing and destination:
            from local_processing import final_destination_name

            try:
                final_destination_name(destination)
            except ValueError:
                return json_error(
                    "local_processing destination cannot be a filesystem root"
                )
        # Local processing only makes sense for import pipelines (source or
        # sources). Collection pipelines set skip_scan and never run the
        # ingest stage, so the staging folder is never created or indexed —
        # the job would burn through every processing stage on the existing
        # photos and then fail in archive_stage with "local staging folder
        # was not indexed". Snapshot-scoped runs are likewise rejected: they
        # also bypass ingest because the snapshot's existing files drive
        # scan_roots directly. Folder-scope runs materialize a collection
        # (below) and share the collection contract, so reject them here
        # too — otherwise the check would fire on the derived collection_id
        # after we'd already inserted the collection row. Reject whenever
        # collection_id, source_snapshot_id, or folder_ids is set — a stale
        # source/sources field in the same request must not slip past,
        # because run_pipeline_job keys skip_scan off collection_id alone
        # and would still skip ingest.
        if local_processing and (
            collection_id is not None
            or source_snapshot_id is not None
            or pending_folder_collection is not None
        ):
            return json_error(
                "local_processing cannot be combined with collection_id, "
                "source_snapshot_id, or folder_ids — it requires source "
                "or sources"
            )
        if local_processing and not source and not sources:
            return json_error(
                "local_processing requires source or sources"
            )

        folder_template = body.get("folder_template", "%Y/%Y-%m-%d")
        # A remote archive still ingests through local staging, so the
        # template gets applied there too — validate it for both shapes.
        if (destination or remote_target_id) and folder_template:
            from ingest import _is_unsafe_path
            if _is_unsafe_path(folder_template):
                return json_error("folder_template must be a relative path without '..' or backslashes")

        # ``miss_enabled`` is tri-state (None / True / False) so ``body.get``
        # can't apply a default the way boolean skip_* flags do. Validate it
        # explicitly instead: pipeline_job.py branches on
        # ``params.miss_enabled is not None`` and then on truthiness, so a
        # non-bool value like the string "false" would flow through as
        # truthy — a caller expecting misses off would get them on.
        #
        # Validate BEFORE the folder-scope collection is materialized so a
        # bad value 400s cleanly; otherwise ``db.add_collection`` commits
        # first and the rejected request leaves a stray "Process …" row.
        miss_enabled_body = body.get("miss_enabled")
        if miss_enabled_body is not None and not isinstance(miss_enabled_body, bool):
            return json_error(
                f"miss_enabled must be boolean, got "
                f"{type(miss_enabled_body).__name__}"
            )

        # ``eye_detect_override`` mirrors ``miss_enabled`` as a tri-state
        # per-run switch (None = defer to workspace config). Same validation
        # rationale: a string "false" would flow through as truthy and force
        # eye detection on for a caller expecting the workspace default.
        eye_detect_override_body = body.get("eye_detect_override")
        if (
            eye_detect_override_body is not None
            and not isinstance(eye_detect_override_body, bool)
        ):
            return json_error(
                f"eye_detect_override must be boolean, got "
                f"{type(eye_detect_override_body).__name__}"
            )

        # Validate ``model_id`` / ``model_ids`` shape BEFORE the folder-scope
        # collection is materialized. The auto-skip-classify block further
        # down calls ``list(params.model_ids or [])`` and ``by_id.get(mid, ...)``
        # in ways that raise TypeError on non-list / non-hashable payloads
        # (e.g. ``model_ids: 5`` or an entry that's a list/dict). Without
        # this guard those escape as opaque 500s, and for folder-scoped
        # runs ``db.add_collection`` has already committed by then, leaving
        # a stray "Process …" collection in the workspace after the request
        # fails. Rejecting up front keeps error responses 4xx and prevents
        # orphaned rows on every scope shape.
        model_id_body = body.get("model_id")
        if model_id_body is not None and not isinstance(model_id_body, str):
            return json_error(
                f"model_id must be a string, got "
                f"{type(model_id_body).__name__}"
            )
        model_ids_body = body.get("model_ids")
        if model_ids_body is not None:
            if not isinstance(model_ids_body, list):
                return json_error(
                    f"model_ids must be a list, got "
                    f"{type(model_ids_body).__name__}"
                )
            for _mid in model_ids_body:
                if not isinstance(_mid, str):
                    return json_error(
                        f"model_ids entries must be strings, got "
                        f"{type(_mid).__name__}"
                    )

        # Snapshot the resolved target dict so the queued run archives to the
        # host/mount the user saw at click-Start, not whatever the saved target
        # gets edited to while another pipeline holds the slot. Mirrors how the
        # move-folder endpoint captures its remote spec at enqueue rather than
        # re-reading Settings at execution.
        remote_target_snapshot = (
            dict(remote_archive_config["target"])
            if remote_archive_config is not None else None
        )
        params = PipelineParams(
            collection_id=collection_id,
            source=source,
            sources=sources,
            source_snapshot_id=source_snapshot_id,
            destination=destination,
            local_processing=local_processing,
            remote_target_id=remote_target_id or None,
            remote_subpath=remote_subpath or "",
            remote_target_snapshot=remote_target_snapshot,
            file_types=body.get("file_types", "both"),
            folder_template=folder_template,
            skip_duplicates=body.get("skip_duplicates", True),
            verify_by_hash=bool(body.get("verify_by_hash")),
            labels_file=body.get("labels_file"),
            labels_files=body.get("labels_files"),
            model_id=body.get("model_id"),
            model_ids=body.get("model_ids"),
            reclassify=body.get("reclassify", False),
            skip_classify=body.get("skip_classify", False),
            download_taxonomy=body.get("download_taxonomy", True),
            skip_extract_masks=body.get("skip_extract_masks", False),
            skip_eye_keypoints=body.get("skip_eye_keypoints", False),
            eye_detect_override=eye_detect_override_body,
            skip_regroup=body.get("skip_regroup", False),
            # ``review_mode`` reaches ``body`` only through the strategy
            # expansion at the top of this handler (identify sets it to
            # ``"species"``). Callers who send skip_regroup without a
            # strategy — Advanced/Custom on the Process page, or API
            # clients refreshing classifications only — leave it None so
            # regroup_stage skips instead of overwriting the workspace
            # cache with all-REVIEW output.
            review_mode=body.get("review_mode"),
            miss_enabled=body.get("miss_enabled"),
            preview_max_size=body.get("preview_max_size"),
            exclude_paths=excluded_paths_set or None,
            exclude_photo_ids=excluded_photo_ids_set or None,
            recursive=body.get("recursive", True),
        )

        # All request validation and param coercion have passed — safe to
        # materialize the folder-scope collection row now. Deferring the
        # insert past PipelineParams construction closes the last window
        # where a coercion-time exception (e.g. an unhashable exclude
        # entry that slipped past the guards above) could leave a stray
        # "Process …" collection behind after the request failed.
        if pending_folder_collection is not None:
            from datetime import datetime as _dt

            collection_name = "Process {} {}".format(
                pending_folder_collection["leaf"],
                _dt.now().strftime("%Y-%m-%d %H:%M"),
            )
            collection_id = db.add_collection(
                collection_name,
                json.dumps([{
                    "field": "photo_ids",
                    "value": pending_folder_collection["photo_ids"],
                }]),
            )
            params.collection_id = collection_id
        elif collection_id is not None:
            collection_name = next(
                (
                    c["name"]
                    for c in db.get_collections()
                    if c["id"] == collection_id
                ),
                None,
            )
        else:
            collection_name = None

        # Auto-skip classify stages if no model is available. Shared with
        # the after-import chaining hook so a chained run and a manually
        # started one degrade identically.
        model_warning = apply_no_model_auto_skip(params)

        # Save destination to recent list (best-effort — don't block pipeline)
        if destination:
            try:
                import config as cfg
                # Lock + raw read-modify-write so a concurrent settings PATCH
                # isn't reverted and we don't pin every DEFAULTS value into
                # the user's file (see api_pipeline_save_grouping_defaults).
                with settings_write_lock:
                    raw = read_raw_config_file()
                    ingest_cfg = raw.get("ingest")
                    ingest_cfg = dict(ingest_cfg) if isinstance(ingest_cfg, dict) else {}
                    recents = ingest_cfg.get("recent_destinations")
                    recents = list(recents) if isinstance(recents, list) else []
                    if destination in recents:
                        recents.remove(destination)
                    recents.insert(0, destination)
                    recents = recents[:5]
                    ingest_cfg["recent_destinations"] = recents
                    raw["ingest"] = ingest_cfg
                    cfg.save(raw)
            except Exception:
                log.warning("Failed to save recent destination to config")

        runner = get_runner()
        active_ws = db._active_workspace_id
        work_units = None
        if collection_id:
            work_units = runtime_warning_work_units(
                "pipeline collection",
                lambda: db.count_collection_photos(collection_id),
            )
        elif source_snapshot_id is not None:
            work_units = runtime_warning_work_units(
                "pipeline new-images snapshot",
                lambda: (db.get_new_images_snapshot(source_snapshot_id) or {}).get(
                    "file_count"
                ),
            )

        runtime_warning = None
        if not (params.skip_classify and params.skip_extract_masks):
            runtime_warning = build_cpu_runtime_warning(
                "pipeline",
                work_units=work_units,
                reason="large_pipeline_ml_job_cpu_only",
            )

        def work(job):
            return run_pipeline_job(
                job, runner, db_path, active_ws, params,
                thumb_cache_dir=config["THUMB_CACHE_DIR"],
                missing_originals_invalidator=invalidate_missing_originals,
                computation_cache_dir=config["COMPUTATION_CACHE_DIR"],
            )

        # Enqueue rather than start directly: when pipeline slots are available,
        # ``enqueue_pipeline`` promotes inline
        # before returning, so this looks identical to the old ``start``
        # call. When a pipeline is already running, the new run waits in
        # ``status='queued'`` until the slot opens. Callers receive the
        # same {"job_id": ...} response either way; clients learn about
        # the queued state via /api/jobs/<id> polling or the SSE stream.
        job_config = {
            "source": source,
            "sources": sources,
            "collection_id": collection_id,
            "collection_name": collection_name,
            "destination": destination,
            "local_processing": local_processing,
            "process_id": process_id,
            "skip_classify": params.skip_classify,
            "skip_extract_masks": params.skip_extract_masks,
            "skip_regroup": params.skip_regroup,
            "review_mode": params.review_mode,
            "miss_enabled": params.miss_enabled,
            "eye_detect_override": params.eye_detect_override,
        }
        # Preserve the caller's original folder_ids alongside the derived
        # ad-hoc collection_id so the Jobs page can show both what the user
        # selected (folder subtree) and the collection the run was pinned to
        # without a secondary collection lookup.
        if folder_ids is not None:
            job_config["folder_ids"] = list(folder_ids)
        if remote_archive_config is not None:
            # Surface that the archive goes over SSH (and to where) so the
            # jobs panel can show it, per the UI-transparency rule.
            target = remote_archive_config["target"]
            job_config["remote_archive"] = {
                "target_id": target["id"],
                "target_name": target["name"],
                "host": target["host"],
                "user": target["user"],
                "subpath": remote_archive_config["subpath"],
                "ssh_destination": remote_archive_config["ssh_final"],
                "display": remote_archive_config["display"],
            }
        job_id = runner.enqueue_pipeline(
            work,
            config=job_config,
            workspace_id=active_ws,
            runtime_warning=runtime_warning,
        )
        result = {"job_id": job_id}
        if model_warning:
            result["model_warning"] = model_warning
        return jsonify(result)

    @blueprint.route("/api/pipeline/config", methods=["GET", "POST"])
    def api_pipeline_config():
        """Get or update pipeline model configuration.

        GET: Returns current effective pipeline config.
        POST: Saves pipeline config to workspace overrides.
              Accepts {sam2_variant, dinov2_variant, proxy_longest_edge}.
        """
        import config as cfg

        db = get_db()

        if request.method == "GET":
            effective = db.get_effective_config(cfg.load())
            return jsonify(effective.get("pipeline", {}))

        body = request.get_json(silent=True) or {}
        allowed_keys = {"sam2_variant", "dinov2_variant", "proxy_longest_edge"}
        pipeline_updates = {k: v for k, v in body.items() if k in allowed_keys}
        if not pipeline_updates:
            return json_error("No valid pipeline config keys provided")

        # Share the schema-driven settings write lock so a concurrent schema
        # autosave can't read this same overrides snapshot and overwrite the
        # pipeline change with stale data.
        with settings_write_lock:
            ws = db.get_workspace(db._active_workspace_id)
            current_overrides = {}
            if ws and ws["config_overrides"]:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    current_overrides = json.loads(ws["config_overrides"]) if isinstance(ws["config_overrides"], str) else ws["config_overrides"]
            if not isinstance(current_overrides, dict):
                current_overrides = {}

            pipeline_section = current_overrides.get("pipeline", {})
            if not isinstance(pipeline_section, dict):
                pipeline_section = {}
            pipeline_section.update(pipeline_updates)
            current_overrides["pipeline"] = pipeline_section

            db.update_workspace(db._active_workspace_id, config_overrides=current_overrides)

        return jsonify({"pipeline": pipeline_section, "status": "saved"})

    @blueprint.route("/api/pipeline/save-grouping-defaults", methods=["POST"])
    def api_pipeline_save_grouping_defaults():
        """Persist current grouping weights/thresholds to the global config.

        Whitelists known grouping keys and validates each value's type and
        range so a bad payload (e.g. {"hard_cut_time": "abc"}) can't poison
        the persistent config and crash future grouping/scoring math.
        Returns the new pipeline payload that was saved.
        """
        import config as cfg

        body = request.get_json(silent=True) or {}
        new_pipeline = body.get("pipeline", {})
        if not isinstance(new_pipeline, dict):
            return json_error("pipeline must be an object")
        # (kind, min, max). kind="unit" → 0..1 float; "score" → 0..1 float;
        # "seconds" → non-negative float.
        schema = {
            "w_time": ("unit", 0.0, 1.0),
            "w_subj": ("unit", 0.0, 1.0),
            "w_global": ("unit", 0.0, 1.0),
            "w_species": ("unit", 0.0, 1.0),
            "w_meta": ("unit", 0.0, 1.0),
            # tau_enc and merge_tau are denominators in encounters scoring
            # (exp(-dt/tau), exp(-gap/merge_tau)) — must be strictly positive
            # to avoid ZeroDivisionError when scoring runs against the saved
            # config.
            "tau_enc": ("seconds", 1.0, 86400.0),
            "hard_cut_time": ("seconds", 0.0, 86400.0),
            "hard_cut_score": ("score", 0.0, 1.0),
            "soft_cut_score": ("score", 0.0, 1.0),
            "species_hard_cut_confidence": ("score", 0.0, 1.0),
            "species_hard_cut_margin": ("score", 0.0, 1.0),
            "merge_score": ("score", 0.0, 1.0),
            "merge_max_gap": ("seconds", 0.0, 86400.0),
            "merge_tau": ("seconds", 1.0, 86400.0),
            "burst_time_gap": ("seconds", 0.0, 86400.0),
            "burst_embedding_threshold": ("score", 0.0, 1.0),
        }
        rejected = [k for k in new_pipeline if k not in schema]
        if rejected:
            return json_error(f"unknown keys: {rejected}")
        coerced = {}
        for k, raw_v in new_pipeline.items():
            kind, lo, hi = schema[k]
            if isinstance(raw_v, bool):
                # bool is an int subclass — reject explicitly so True/False
                # don't silently succeed for numeric keys.
                return json_error(f"{k} must be a number, got bool")
            if not isinstance(raw_v, int | float):
                return json_error(f"{k} must be a number")
            v = float(raw_v)
            if v != v or v in (float("inf"), float("-inf")):
                return json_error(f"{k} must be finite")
            if v < lo or v > hi:
                return json_error(f"{k} out of range [{lo}, {hi}]")
            coerced[k] = v
        # Hold the same lock as the settings write paths so a concurrent
        # autosave from /api/settings doesn't clobber half of one update.
        # Use the raw on-disk file (no DEFAULTS merge) so we only persist
        # the keys the user actually set — otherwise cfg.save would pin
        # every default to its current value and block future upgrades.
        with settings_write_lock:
            raw = read_raw_config_file()
            # If a hand-edit left raw["pipeline"] as a non-dict (string, list,
            # etc.), setdefault would return it as-is and .update would raise
            # AttributeError. Normalize to a dict — the endpoint's job is to
            # write a clean pipeline section, so a corrupt one should be
            # replaced rather than crash the save.
            if not isinstance(raw.get("pipeline"), dict):
                raw["pipeline"] = {}
            raw["pipeline"].update(coerced)
            cfg.save(raw)
        return jsonify({"saved": coerced})

    return blueprint
