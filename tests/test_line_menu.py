"""Offline contract between the rich-menu artwork and the local conversation simulator."""

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import socket

from PIL import Image
import pytest

from scripts import line_menu


@pytest.fixture
def payload():
    return json.loads(line_menu.MENU_PATH.read_text(encoding="utf-8"))


def write_payload(tmp_path, payload):
    path = tmp_path / "menu.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_bundled_assets_have_exact_six_actions_full_coverage_and_png():
    payload, data, digest = line_menu.validate()
    assert payload["name"] == "youth-service-v1"
    assert payload["size"] == {"width": 2500, "height": 1686}
    assert payload["selected"] is True
    expected = [("youth:" + key, label) for key, label in line_menu.ACTIONS.items()]
    assert [(area["action"]["data"], area["action"]["label"]) for area in payload["areas"]] == expected
    # A complete row boundary maps each pixel to one menu area, including x=1666 and the final pixel.
    for x in (0, 832, 833, 1665, 1666, 2499):
        for y in (0, 842, 843, 1685):
            matches = [area for area in payload["areas"] if
                       area["bounds"]["x"] <= x < area["bounds"]["x"] + area["bounds"]["width"] and
                       area["bounds"]["y"] <= y < area["bounds"]["y"] + area["bounds"]["height"]]
            assert len(matches) == 1
    assert 0 < len(data) <= 1_000_000
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (2500, 1686)
        assert image.mode == "RGB"
    assert digest == hashlib.sha256(line_menu.canonical(payload) + b"\0" + data).hexdigest()


@pytest.mark.parametrize("mutation,message", [
    (lambda p: p["areas"][0]["bounds"].update(width=834), "overlap"),
    (lambda p: p["areas"][0]["bounds"].update(width=832), "gaps"),
    (lambda p: p["areas"][5]["bounds"].update(width=835), "beyond"),
    (lambda p: p["areas"][0]["bounds"].update(x=-1), "schema"),
    (lambda p: p["areas"][0]["bounds"].update(width=0), "schema"),
    (lambda p: p["areas"][0]["bounds"].update(x=True), "schema"),
    (lambda p: p["areas"][0]["action"].update(data="youth:unknown"), "schema"),
    (lambda p: p["areas"][0]["action"].update(type="uri"), "schema"),
    (lambda p: p["areas"][0]["action"].update(label="incorrect"), "labelled"),
    (lambda p: p["areas"][1].update(action=deepcopy(p["areas"][0]["action"])), "exactly once"),
    (lambda p: p.update(name="youth-service"), "schema"),
    (lambda p: p.update(secret="synthetic-private-value"), "schema"),
    (lambda p: p["areas"].pop(), "schema"),
    (lambda p: p.update(chatBarText="x" * 15), "schema"),
])
def test_invalid_payload_is_rejected(tmp_path, payload, mutation, message):
    mutation(payload)
    with pytest.raises(line_menu.MenuValidationError, match=message):
        line_menu.validate(write_payload(tmp_path, payload))


def test_image_must_match_payload_dimensions(tmp_path):
    path = tmp_path / "wrong-size.png"
    Image.new("RGB", (2500, 843)).save(path)
    with pytest.raises(line_menu.MenuValidationError, match="dimensions"):
        line_menu.validate(image_path=path)


def test_png_is_required_even_when_filename_is_png(tmp_path):
    path = tmp_path / "wrong-format.png"
    Image.new("RGB", (2500, 1686)).save(path, "JPEG")
    with pytest.raises(line_menu.MenuValidationError, match="format"):
        line_menu.validate(image_path=path)


def test_oversized_image_is_rejected_before_decode(tmp_path):
    path = tmp_path / "oversized.png"
    path.write_bytes(b"x" * 1_000_001)
    with pytest.raises(line_menu.MenuValidationError, match="1 MB"):
        line_menu.validate(image_path=path)


def test_malformed_image_is_rejected(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"not-an-image")
    with pytest.raises(line_menu.MenuValidationError, match="Could not read"):
        line_menu.validate(image_path=path)


def test_hash_tracks_both_payload_and_image(tmp_path, payload):
    original = line_menu.validate()[2]
    payload["selected"] = False
    assert line_menu.validate(write_payload(tmp_path, payload))[2] != original
    path = tmp_path / "changed.png"
    with Image.open(line_menu.IMAGE_PATH) as image:
        image.putpixel((0, 0), (100, 100, 100))
        image.save(path)
    assert line_menu.validate(image_path=path)[2] != original


def test_default_command_has_alt_text_and_no_network_or_private_reads(monkeypatch, capsys):
    def disallowed(*args, **kwargs):
        raise AssertionError("Offline menu validation must not access network or unrelated files")

    original_read = Path.read_text

    def read_asset_only(path, *args, **kwargs):
        if path != line_menu.MENU_PATH:
            disallowed()
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", disallowed)
    monkeypatch.setattr(socket.socket, "connect", disallowed)
    monkeypatch.setattr(Path, "read_text", read_asset_only)
    monkeypatch.setenv("YOUTH_LINE_CHANNEL_ACCESS_TOKEN", "synthetic-do-not-read-or-print")
    assert line_menu.main([]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["mode"] == "offline"
    assert result["networkRequests"] == 0
    assert result["areas"] == 6
    for label in line_menu.ACTIONS.values():
        assert label in result["imageAlt"]
    assert "synthetic-do-not-read-or-print" not in captured.out + captured.err


@pytest.mark.parametrize("command", ["publish", "status", "configure-webhook"])
def test_remote_commands_are_not_available(command):
    with pytest.raises(SystemExit) as exc:
        line_menu.main([command])
    assert exc.value.code == 2


def test_failed_validation_does_not_echo_untrusted_data(tmp_path, monkeypatch, capsys):
    path = tmp_path / "broken.json"
    path.write_text('{"secret": "synthetic-untrusted-value",', encoding="utf-8")
    original_validate = line_menu.validate
    monkeypatch.setattr(line_menu, "validate", lambda: original_validate(path))
    assert line_menu.main(["validate"]) == 1
    captured = capsys.readouterr()
    assert "synthetic-untrusted-value" not in captured.out + captured.err
    assert json.loads(captured.err)["status"] == "error"
