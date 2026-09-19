"""Local LINE profile loading boundaries; credentials below are synthetic."""

import json
import os

import pytest

from scripts.run_precheck_demo import load_line_profile


pytestmark = pytest.mark.skipif(os.name != "posix", reason="The local launcher uses POSIX private storage")


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "line.json"
    path.write_text(json.dumps({
        "YOUTH_LINE_MESSAGING_CHANNEL_ID": "1234567890",
        "YOUTH_LINE_CHANNEL_SECRET": "synthetic-secret",
        "YOUTH_LINE_CHANNEL_ACCESS_TOKEN": "synthetic-access-token",
        "YOUTH_LINE_DESTINATION_USER_ID": "U" + "a" * 32,
    }))
    path.chmod(0o600)
    return path


def test_loads_messaging_profile_without_faking_login_ids(profile):
    values = load_line_profile(profile)
    assert values["YOUTH_LINE_MESSAGING_CHANNEL_ID"] == "1234567890"
    assert "YOUTH_LINE_CHANNEL_ID" not in values
    assert "YOUTH_LINE_PROVIDER_ID" not in values


def test_rejects_shared_readable_profile(profile):
    profile.chmod(0o644)
    with pytest.raises(ValueError, match="private file"):
        load_line_profile(profile)


def test_rejects_symlink_profile(profile):
    link = profile.with_name("link.json")
    link.symlink_to(profile)
    with pytest.raises(ValueError, match="symlink"):
        load_line_profile(link)


def test_does_not_import_arbitrary_environment_settings(profile):
    values = json.loads(profile.read_text())
    values["YOUTH_AUTO_CREATE_SCHEMA"] = "true"
    profile.write_text(json.dumps(values))
    with pytest.raises(ValueError, match="unsupported settings"):
        load_line_profile(profile)


def test_rejects_incomplete_messaging_profile(profile):
    profile.write_text("{}")
    with pytest.raises(ValueError, match="incomplete"):
        load_line_profile(profile)
