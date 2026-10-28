import subprocess

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings
from app.main import create_app


def test_healthz():
    client = TestClient(create_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_settings_defaults_without_env(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    s = Settings(_env_file=None)
    assert s.classifier_confidence_threshold == 0.75
    assert s.digest_hour == 20
    assert s.outreach_daily_cap == 30
    assert (s.outreach_min_gap_seconds, s.outreach_max_gap_seconds) == (60, 180)
    assert s.anthropic_api_key is None


def test_settings_reject_inverted_gap():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, outreach_min_gap_seconds=200, outreach_max_gap_seconds=100)


def test_secrets_are_masked_in_repr():
    s = Settings(_env_file=None, anthropic_api_key="sk-test-secret")
    assert "sk-test-secret" not in repr(s)


def test_env_example_covers_every_setting():
    keys = {
        line.split("=", 1)[0].strip()
        for line in (PROJECT_ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert keys == {name.upper() for name in Settings.model_fields}


@pytest.mark.parametrize(
    "path", [".env", ".env.local", "credentials.json", "token.json", "token_send.json"]
)
def test_secret_files_are_gitignored(path):
    result = subprocess.run(
        ["git", "check-ignore", "-q", path], cwd=PROJECT_ROOT, capture_output=True
    )
    assert result.returncode == 0, f"{path} is not git-ignored"


def test_env_example_is_not_ignored():
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".env.example"], cwd=PROJECT_ROOT, capture_output=True
    )
    assert result.returncode == 1
