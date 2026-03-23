import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_find_darktable_returns_none_when_missing(monkeypatch):
    """find_darktable returns None when binary is not found."""
    from develop import find_darktable

    monkeypatch.setattr("shutil.which", lambda x: None)
    assert find_darktable("") is None


def test_find_darktable_returns_configured_path(tmp_path):
    """find_darktable returns the configured path if it exists."""
    from develop import find_darktable

    fake_bin = tmp_path / "darktable-cli"
    fake_bin.touch()
    fake_bin.chmod(0o755)
    assert find_darktable(str(fake_bin)) == str(fake_bin)


def test_find_darktable_returns_none_for_bad_configured_path():
    """find_darktable returns None when configured path doesn't exist."""
    from develop import find_darktable

    assert find_darktable("/nonexistent/darktable-cli") is None


def test_build_command_minimal():
    """build_command produces correct args for basic conversion."""
    from develop import build_command

    cmd = build_command(
        darktable_bin="/usr/bin/darktable-cli",
        input_path="/photos/bird.CR3",
        output_path="/output/bird.jpg",
    )
    assert cmd[0] == "/usr/bin/darktable-cli"
    assert "/photos/bird.CR3" in cmd
    assert "/output/bird.jpg" in cmd


def test_build_command_with_style():
    """build_command includes --style when provided."""
    from develop import build_command

    cmd = build_command(
        darktable_bin="/usr/bin/darktable-cli",
        input_path="/photos/bird.CR3",
        output_path="/output/bird.jpg",
        style="Wildlife",
    )
    assert "--style" in cmd
    idx = cmd.index("--style")
    assert cmd[idx + 1] == "Wildlife"


def test_build_command_with_width():
    """build_command includes --width when provided."""
    from develop import build_command

    cmd = build_command(
        darktable_bin="/usr/bin/darktable-cli",
        input_path="/photos/bird.CR3",
        output_path="/output/bird.jpg",
        width=2048,
    )
    assert "--width" in cmd
    idx = cmd.index("--width")
    assert cmd[idx + 1] == "2048"


def test_output_path_for_photo():
    """output_path_for_photo builds correct path."""
    from develop import output_path_for_photo

    result = output_path_for_photo(
        filename="bird.CR3",
        output_dir="/output",
        output_format="jpg",
    )
    assert result == "/output/bird.jpg"


def test_output_path_for_photo_tiff():
    """output_path_for_photo handles tiff format."""
    from develop import output_path_for_photo

    result = output_path_for_photo(
        filename="eagle.NEF",
        output_dir="/developed",
        output_format="tiff",
    )
    assert result == "/developed/eagle.tiff"


def test_develop_photo_returns_error_when_no_binary():
    """develop_photo returns error dict when darktable not found."""
    from develop import develop_photo

    result = develop_photo(
        darktable_bin="",
        input_path="/photos/bird.CR3",
        output_path="/output/bird.jpg",
    )
    assert result["success"] is False
    assert "not found" in result["error"].lower() or "not configured" in result["error"].lower()


def test_develop_photo_returns_error_when_input_missing():
    """develop_photo returns error when input file doesn't exist."""
    from develop import develop_photo

    result = develop_photo(
        darktable_bin="/usr/bin/darktable-cli",
        input_path="/nonexistent/bird.CR3",
        output_path="/output/bird.jpg",
    )
    assert result["success"] is False
    assert "not found" in result["error"].lower()
