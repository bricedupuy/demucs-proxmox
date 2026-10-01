import importlib
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("boto3")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


@pytest.fixture()
def main(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMUX_API_KEY", "k")
    monkeypatch.setenv("DEMUX_DATA_DIR", str(tmp_path))
    m = importlib.import_module("main")
    m = importlib.reload(m)
    m.init_db()
    return m


def _job(main):
    jid = main.create_job("s.wav", "htdemucs", None, None, False, None, None, None, None, None)
    return jid


def test_analysis_null_until_stored(main):
    jid = _job(main)
    assert main.public_job(main.get_job(jid))["analysis"] is None


def test_analysis_exposed_in_job_and_webhook(main):
    jid = _job(main)
    a = {"tempo": {"bpm": 72.1, "confidence": 0.9}, "key": {"name": "G"}}
    with main.db() as conn:
        conn.execute("UPDATE jobs SET analysis_json=? WHERE id=?", (json.dumps(a), jid))
    assert main.public_job(main.get_job(jid))["analysis"] == a
    assert main.webhook_payload(jid, "fast.completed")["job"]["analysis"] == a


def test_analysis_failure_does_not_fail_job(main, monkeypatch):
    jid = _job(main)
    monkeypatch.setattr(main.analysis, "analyze_job", lambda *a, **k: 1 / 0)
    assert main.run_analysis(jid) is None


def test_health_reports_analysis(main):
    assert main.health()["analysis"]["enabled"] is True
