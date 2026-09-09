"""Human-readable text for finished job results.

Jobs return small dicts (``{"deleted": 28, "trashed": 28, ...}``) that are
handy for callers and tests but unreadable when dumped on the Jobs page.
``describe_result`` turns those dicts into one summary sentence plus optional
detail lines so both the history list and the detail panel show prose
instead of raw JSON or ``key: value`` pairs.

Per-type describers handle the shapes jobs are known to return today; the
generic fallback humanizes any other dict by spelling out its scalar fields
and counting its list fields, so a new job type never regresses to JSON.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

MAX_DETAIL_ITEMS = 10

# Bookkeeping fields that never help a user understand what a job did.
_SKIPPED_KEYS = {
    "ok", "summary", "photo_ids", "failed_photo_ids", "stages", "duration",
    "collection_id", "workspace_id", "mode", "cancelled", "root_folder_id",
    "interrupted", "last_progress_at", "checkpoint_at",
}


def _n(count: Any, singular: str, plural: str | None = None) -> str:
    """``_n(3, "photo")`` -> ``"3 photos"``; ``_n(1, "file")`` -> ``"1 file"``."""
    try:
        value = int(count)
    except (TypeError, ValueError):
        value = 0
    word = singular if value == 1 else (plural or singular + "s")
    return f"{value:,} {word}"


def _int(result: dict, key: str, default: int = 0) -> int:
    try:
        return int(result.get(key) or default)
    except (TypeError, ValueError):
        return default


def _human_bytes(size: Any) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _humanize_key(key: str) -> str:
    words = str(key).replace("_", " ").replace("-", " ").split()
    if not words:
        return str(key)
    return " ".join(words).capitalize()


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return str(value)


def _item_text(item: Any) -> str:
    """Render one list entry (string or small dict) as a detail line."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        label = (
            item.get("filename") or item.get("path") or item.get("name")
            or item.get("model_id") or item.get("label")
        )
        reason = item.get("error") or item.get("reason") or item.get("message")
        if label and reason:
            return f"{label}: {reason}"
        if label:
            return str(label)
        if reason:
            return str(reason)
        # Structured records with no obvious label (per-folder stats and
        # the like) don't read well as a line; the count in the summary
        # is enough.
        return ""
    return _format_scalar(item)


def _list_details(items: Any, heading: str) -> list[str]:
    """``heading`` line followed by up to MAX_DETAIL_ITEMS entries.

    Returns nothing at all when no entry renders as text, so a list of
    opaque records never leaves a dangling heading behind.
    """
    if not isinstance(items, list) or not items:
        return []
    lines = []
    for item in items[:MAX_DETAIL_ITEMS]:
        text = _item_text(item)
        if text:
            lines.append(text)
    if not lines:
        return []
    remaining = len(items) - MAX_DETAIL_ITEMS
    if remaining > 0:
        lines.append(f"and {remaining:,} more")
    return [heading] + lines


def _error_details(result: dict, key: str = "errors") -> list[str]:
    items = result.get(key)
    if isinstance(items, list) and items:
        return _list_details(items, _n(len(items), "error") + ":")
    return []


# --- Per-type describers -------------------------------------------------


def _batch_delete(result: dict, config: dict) -> tuple[str, list[str]]:
    deleted = _int(result, "deleted")
    trashed = _int(result, "trashed")
    trash_failed = result.get("trash_failed") or []
    failed_ids = result.get("failed_photo_ids") or []
    mode = (config or {}).get("mode")

    if mode == "disk_permanent":
        file_phrase = _n(trashed, "file") + " deleted permanently"
    else:
        file_phrase = _n(trashed, "file") + " moved to Trash"

    if deleted and trashed:
        summary = f"{_n(deleted, 'photo')} removed from Vireo, {file_phrase}"
    elif deleted:
        summary = f"{_n(deleted, 'photo')} removed from Vireo"
        if mode in ("disk", "disk_permanent"):
            summary += ", no files changed"
        else:
            summary += ", files kept on disk"
    elif trashed:
        summary = file_phrase[0].upper() + file_phrase[1:]
    else:
        summary = "Nothing deleted"
    if failed_ids:
        summary += f", {_n(len(failed_ids), 'photo')} failed"

    details = []
    if trash_failed:
        verb = "deleted" if mode == "disk_permanent" else "moved to Trash"
        details += _list_details(
            trash_failed, f"{_n(len(trash_failed), 'file')} could not be {verb}:"
        )
    return summary, details


def _generated(result: dict, config: dict, noun: str = "thumbnail") -> tuple[str, list[str]]:
    generated = _int(result, "generated")
    skipped = _int(result, "skipped")
    failed = _int(result, "failed")
    parts = [_n(generated, noun) + " generated"]
    if skipped:
        parts.append(f"{skipped:,} already up to date")
    if failed:
        parts.append(f"{failed:,} failed")
    return ", ".join(parts), _error_details(result)


def _thumbnails(result, config):
    return _generated(result, config, "thumbnail")


def _previews(result, config):
    return _generated(result, config, "preview")


def _classify(result: dict, config: dict) -> tuple[str, list[str]]:
    classified = _int(result, "classified")
    total = _int(result, "total")
    failed = _int(result, "failed")
    groups = _int(result, "groups")
    if total:
        summary = f"{classified:,} of {_n(total, 'photo')} classified"
    else:
        summary = _n(classified, "photo") + " classified"
    if groups:
        summary += f" in {_n(groups, 'group')}"
    if failed:
        summary += f", {failed:,} failed"
    return summary, _error_details(result)


def _export(result: dict, config: dict) -> tuple[str, list[str]]:
    exported = _int(result, "exported")
    errors = result.get("errors") or []
    summary = _n(exported, "photo") + " exported"
    destination = result.get("destination")
    subfolder = result.get("subfolder")
    if destination:
        summary += f" to {destination}"
        if subfolder:
            summary += f"/{subfolder}"
    if isinstance(errors, list) and errors:
        summary += f", {_n(len(errors), 'error')}"
    return summary, _error_details(result)


def _cull(result: dict, config: dict) -> tuple[str, list[str]]:
    total = _int(result, "total_photos")
    keepers = _int(result, "suggested_keepers")
    rejects = _int(result, "suggested_rejects")
    species = _int(result, "species_count")
    summary = (
        f"{_n(total, 'photo')} reviewed: {keepers:,} suggested keepers, "
        f"{rejects:,} suggested rejects"
    )
    if species:
        summary += f" across {_n(species, 'species', 'species')}"
    return summary, []


def _regroup(result: dict, config: dict) -> tuple[str, list[str]]:
    total = _int(result, "total_photos")
    encounters = _int(result, "encounter_count")
    bursts = _int(result, "burst_count")
    summary = (
        f"{_n(total, 'photo')} grouped into {_n(encounters, 'encounter')} "
        f"and {_n(bursts, 'burst')}"
    )
    details = []
    keep = _int(result, "keep_count")
    review = _int(result, "review_count")
    reject = _int(result, "reject_count")
    if keep or review or reject:
        details.append(f"{keep:,} keep, {review:,} review, {reject:,} reject")
    protected = _int(result, "rarity_protected")
    if protected:
        details.append(_n(protected, "photo") + " protected as rare species")
    return summary, details


def _sharpness(result: dict, config: dict) -> tuple[str, list[str]]:
    scored = _int(result, "scored_count")
    groups = _int(result, "group_count")
    flagged = _int(result, "auto_flagged")
    summary = _n(scored, "photo") + " scored for sharpness"
    if groups:
        summary += f" in {_n(groups, 'group')}"
    if flagged:
        summary += f", {flagged:,} auto-flagged"
    return summary, []


def _prepare_full_resolution(result: dict, config: dict) -> tuple[str, list[str]]:
    ready = _int(result, "ready")
    total = _int(result, "total")
    copied = _int(result, "copied")
    reused = _int(result, "reused")
    failed = _int(result, "failed")
    summary = f"{ready:,} of {_n(total, 'full-resolution file')} ready"
    extras = []
    if copied:
        extras.append(f"{copied:,} copied")
    if reused:
        extras.append(f"{reused:,} reused")
    size = _human_bytes(result.get("bytes"))
    if size and _int(result, "bytes"):
        extras.append(size)
    if extras:
        summary += " (" + ", ".join(extras) + ")"
    if failed:
        summary += f", {failed:,} failed"
    return summary, _error_details(result)


def _verify_models(result: dict, config: dict) -> tuple[str, list[str]]:
    verified = _int(result, "verified")
    failed = result.get("failed") or []
    summary = _n(verified, "model") + " verified"
    if isinstance(failed, list) and failed:
        summary += f", {len(failed):,} failed"
    return summary, _list_details(failed, "Failed verification:")


def _fetch_labels(result: dict, config: dict) -> tuple[str, list[str]]:
    count = _int(result, "species_count")
    details = []
    if result.get("labels_file"):
        details.append(f"Saved to {result['labels_file']}")
    return _n(count, "species label") + " fetched", details


def _extract_masks(result: dict, config: dict) -> tuple[str, list[str]]:
    masked = _int(result, "masked")
    skipped = _int(result, "skipped")
    failed = _int(result, "failed")
    parts = [_n(masked, "mask") + " extracted"]
    if skipped:
        parts.append(f"{skipped:,} skipped")
    if failed:
        parts.append(f"{failed:,} failed")
    return ", ".join(parts), _error_details(result)


def _develop(result: dict, config: dict) -> tuple[str, list[str]]:
    developed = _int(result, "developed")
    total = _int(result, "total")
    errors = result.get("errors")
    if total:
        summary = f"{developed:,} of {_n(total, 'photo')} developed"
    else:
        summary = _n(developed, "photo") + " developed"
    details = []
    if isinstance(errors, list):
        if errors:
            summary += f", {_n(len(errors), 'error')}"
        details = _error_details(result)
    elif _int(result, "errors"):
        summary += f", {_n(_int(result, 'errors'), 'error')}"
    return summary, details


def _ingest(result: dict, config: dict) -> tuple[str, list[str]]:
    copied = _int(result, "copied")
    dupes = _int(result, "skipped_duplicate")
    failed = _int(result, "failed")
    total = _int(result, "total")
    summary = _n(copied, "photo") + " imported"
    if total and total != copied:
        summary = f"{copied:,} of {_n(total, 'photo')} imported"
    if dupes:
        summary += f", {dupes:,} already present"
    if failed:
        summary += f", {failed:,} failed"
    return summary, _error_details(result)


def _capture_time(result: dict, config: dict) -> tuple[str, list[str]]:
    updated = _int(result, "updated")
    failed = _int(result, "failed")
    skipped = _int(result, "skipped")
    summary = _n(updated, "photo") + " updated"
    if skipped:
        summary += f", {skipped:,} already correct"
    shift = result.get("shift_minutes")
    if isinstance(shift, (int, float)) and shift and not result.get("shifts_vary"):
        sign = "+" if shift > 0 else "-"
        minutes = abs(int(shift))
        hours, mins = divmod(minutes, 60)
        if hours and mins:
            amount = f"{hours} h {mins} min"
        elif hours:
            amount = _n(hours, "hour")
        else:
            amount = _n(mins, "minute")
        summary += f", capture time shifted {sign}{amount}"
    elif result.get("shifts_vary"):
        summary += ", capture times shifted per photo"
    if failed:
        summary += f", {failed:,} failed"
    return summary, _list_details(result.get("failures"), "Failed:")


def _download(result: dict, config: dict) -> tuple[str, list[str]]:
    """``download-<model_id>`` jobs and anything else named ``download-*``.

    Model downloads return ``{"status": "downloaded", "model_id": ...}``.
    Other downloads carry their own useful fields (size, path, version),
    so when there is no ``model_id`` the remaining scalar fields become
    detail lines instead of being flattened to "Download complete".
    """
    model_id = result.get("model_id") or (config or {}).get("model_id")
    status = result.get("status")
    if status and status != "downloaded":
        return (_humanize_key(status) + (f" ({model_id})" if model_id else "")), []
    if model_id:
        return f"Model {model_id} downloaded", []
    rest = {k: v for k, v in result.items() if k != "status"}
    _, details = _generic(rest, config)
    for key, value in rest.items():
        if isinstance(value, (dict, list)) or value is None or value == "":
            continue
        if key in _SKIPPED_KEYS:
            continue
        details.append(f"{_humanize_key(key)}: {_format_scalar(value)}")
    return "Download complete", details


def _download_megadetector(result: dict, config: dict) -> tuple[str, list[str]]:
    summary = "MegaDetector downloaded"
    if result.get("size"):
        summary += f" ({result['size']})"
    details = []
    if result.get("path"):
        details.append(f"Saved to {result['path']}")
    return summary, details


def _download_darktable(result: dict, config: dict) -> tuple[str, list[str]]:
    version = result.get("version")
    action = result.get("action")
    summary = f"darktable {version} downloaded" if version else "darktable downloaded"
    if action == "installed":
        summary = f"darktable {version} installed" if version else "darktable installed"
    elif action == "opened-installer":
        summary += ", installer opened"
    details = []
    if result.get("bin_path"):
        details.append(f"Vireo will use {result['bin_path']}")
        if result.get("config_written"):
            details.append("Saved as the darktable path in Settings")
    elif result.get("downloaded_to"):
        details.append(f"Saved to {result['downloaded_to']}")
    if result.get("verified"):
        details.append(str(result["verified"]))
    if result.get("quarantined"):
        details.append("macOS quarantine flag is set; approve it in System Settings if launch is blocked")
    return summary, details


def _download_taxonomy(result: dict, config: dict) -> tuple[str, list[str]]:
    return "Taxonomy downloaded", []


def _precompute_embeddings(result: dict, config: dict) -> tuple[str, list[str]]:
    labels = _int(result, "labels")
    summary = _n(labels, "label embedding") + " precomputed"
    if result.get("model"):
        summary += f" with {result['model']}"
    return summary, []


def _scan(result: dict, config: dict) -> tuple[str, list[str]]:
    return _n(_int(result, "photos_indexed"), "photo") + " indexed", []


def _sync(result: dict, config: dict) -> tuple[str, list[str]]:
    synced = _int(result, "synced")
    failed = _int(result, "failed")
    summary = _n(synced, "photo") + " synced"
    if failed:
        summary += f", {failed:,} failed"
    return summary, _list_details(result.get("failures"), "Failed:")


def _publish_site(result: dict, config: dict) -> tuple[str, list[str]]:
    images = _int(result, "exported_images")
    errors = result.get("errors") or []
    summary = _n(images, "image") + " published"
    if result.get("destination"):
        summary += f" to {result['destination']}"
    if isinstance(errors, list) and errors:
        summary += f", {_n(len(errors), 'error')}"
    return summary, _error_details(result)


def _duplicate_scan(result: dict, config: dict) -> tuple[str, list[str]]:
    proposals = result.get("proposals")
    if not isinstance(proposals, list):
        return _generic(result, config)
    if not proposals:
        return "No duplicates found", []
    resolved = sum(1 for p in proposals if isinstance(p, dict) and p.get("status") == "resolved")
    summary = _n(len(proposals), "duplicate group") + " found"
    if resolved:
        summary += f", {resolved:,} resolved"
    return summary, []


def _move(result: dict, config: dict) -> tuple[str, list[str]]:
    moved = _int(result, "moved")
    errors = result.get("errors") or []
    summary = _n(moved, "photo") + " moved"
    if isinstance(errors, list) and errors:
        summary += f", {_n(len(errors), 'error')}"
    return summary, _error_details(result)


_DESCRIBERS: dict[str, Callable[[dict, dict], tuple[str, list[str]]]] = {
    "batch-delete": _batch_delete,
    "thumbnails": _thumbnails,
    "previews": _previews,
    "classify": _classify,
    "export": _export,
    "cull": _cull,
    "regroup": _regroup,
    "sharpness": _sharpness,
    "prepare-full-resolution": _prepare_full_resolution,
    "verify-models": _verify_models,
    "fetch-labels": _fetch_labels,
    "extract-masks": _extract_masks,
    "develop": _develop,
    "ingest": _ingest,
    "capture-time": _capture_time,
    "publish-site": _publish_site,
    "duplicate-scan": _duplicate_scan,
    "move-folder": _move,
    "move-photos": _move,
    "download-taxonomy": _download_taxonomy,
    "download-megadetector": _download_megadetector,
    "download-darktable": _download_darktable,
    "precompute-embeddings": _precompute_embeddings,
    "scan": _scan,
    "sync": _sync,
}


def _generic(result: dict, config: dict) -> tuple[str, list[str]]:
    """Spell out scalar fields; count list fields and list their entries."""
    parts = []
    details: list[str] = []
    for key, value in result.items():
        if key in _SKIPPED_KEYS or key == "error":
            continue
        if key.endswith("_id") or key.endswith("_ids"):
            continue
        if isinstance(value, dict):
            continue
        if isinstance(value, list):
            if not value:
                continue
            noun = _humanize_key(key).lower()
            if len(value) == 1 and noun.endswith("s"):
                noun = noun[:-1]
            parts.append(f"{len(value):,} {noun}")
            details += _list_details(value, f"{len(value):,} {noun}:")
            continue
        if value is None or value == "":
            continue
        parts.append(f"{_humanize_key(key)}: {_format_scalar(value)}")
    if not parts and result.get("ok") is True:
        return "Completed", details
    return ", ".join(parts[:4]), details


def describe_result(job_type: str, result: Any, config: dict | None = None) -> dict:
    """Return ``{"summary": str, "details": [str], "error": str | None}``.

    ``summary`` is one sentence suitable for a list row; ``details`` are
    extra lines for the detail panel (failed files, error messages, paths).
    ``error`` carries the job's fatal error text when the result has one.
    All three are empty/None when there is nothing to say.
    """
    config = config or {}
    if not isinstance(result, dict):
        text = str(result).strip() if isinstance(result, str) else ""
        return {"summary": text, "details": [], "error": None}

    error = result.get("error")
    error_text = str(error).strip() if error else None

    explicit = result.get("summary")
    payload = {k: v for k, v in result.items() if k != "error"}
    if isinstance(explicit, str) and explicit.strip():
        summary, details = explicit.strip(), []
    elif error_text and not any(k not in _SKIPPED_KEYS for k in payload):
        # ``{"error": ...}`` alone (or only bookkeeping such as the
        # startup sweep's ``interrupted`` stamp): nothing happened worth
        # describing, and running a describer over it would invent "0
        # photos classified" or "Download complete" next to the failure.
        summary, details = "", []
    else:
        describer = _DESCRIBERS.get(job_type)
        if describer is None and job_type.startswith("download-"):
            describer = _download
        if describer is None:
            describer = _generic
        try:
            summary, details = describer(payload, config)
        except Exception:
            summary, details = _generic(payload, config)

    details = list(details)
    authored = isinstance(explicit, str) and bool(explicit.strip())
    if error_text and not authored:
        # A failed job leads with why it failed. Whatever partial progress
        # the result records ("344 of 548 photos classified") stays
        # visible as the first detail line rather than competing with the
        # error for the one-line summary.
        if summary and summary != error_text:
            details.insert(0, summary)
        summary = error_text
    return {"summary": summary or "", "details": details, "error": error_text}
