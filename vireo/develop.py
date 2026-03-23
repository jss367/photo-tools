"""Wrapper for darktable-cli to develop RAW photos."""

import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)


def find_darktable(configured_path):
    """Find the darktable-cli binary.

    Args:
        configured_path: user-configured path from config, or empty string

    Returns:
        absolute path to darktable-cli, or None if not found
    """
    if configured_path and os.path.isfile(configured_path):
        return configured_path
    found = shutil.which("darktable-cli")
    return found


def build_command(darktable_bin, input_path, output_path, style=None, width=None):
    """Build the darktable-cli command list.

    Args:
        darktable_bin: path to darktable-cli binary
        input_path: path to input RAW file
        output_path: path for output file
        style: optional darktable style name
        width: optional max output width in pixels

    Returns:
        list of command arguments
    """
    cmd = [darktable_bin, input_path, output_path]
    if style:
        cmd.extend(["--style", style])
    if width:
        cmd.extend(["--width", str(width)])
    return cmd


def output_path_for_photo(filename, output_dir, output_format):
    """Build the output file path for a given photo.

    Args:
        filename: original filename (e.g. "bird.CR3")
        output_dir: directory for developed outputs
        output_format: output format ("jpg" or "tiff")

    Returns:
        full output path
    """
    stem = os.path.splitext(filename)[0]
    return os.path.join(output_dir, f"{stem}.{output_format}")


def develop_photo(darktable_bin, input_path, output_path, style=None, width=None):
    """Develop a single photo using darktable-cli.

    Args:
        darktable_bin: path to darktable-cli (empty string = auto-detect)
        input_path: path to input RAW file
        output_path: path for output file
        style: optional darktable style name
        width: optional max width in pixels

    Returns:
        dict with keys: success (bool), output_path (str), error (str or None)
    """
    binary = find_darktable(darktable_bin)
    if not binary:
        return {"success": False, "output_path": output_path, "error": "darktable-cli not found or not configured"}

    if not os.path.isfile(input_path):
        return {"success": False, "output_path": output_path, "error": f"Input file not found: {input_path}"}

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    cmd = build_command(binary, input_path, output_path, style=style, width=width)

    try:
        log.info("Developing %s -> %s", os.path.basename(input_path), output_path)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return {
                "success": False,
                "output_path": output_path,
                "error": f"darktable-cli exited with code {result.returncode}: {result.stderr.strip()}",
            }
        if not os.path.isfile(output_path):
            return {"success": False, "output_path": output_path, "error": "Output file was not created"}
        return {"success": True, "output_path": output_path, "error": None}
    except subprocess.TimeoutExpired:
        return {"success": False, "output_path": output_path, "error": "darktable-cli timed out after 120 seconds"}
    except FileNotFoundError:
        return {"success": False, "output_path": output_path, "error": f"darktable-cli binary not found at {binary}"}
