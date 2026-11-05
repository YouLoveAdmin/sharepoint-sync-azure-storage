import json
from unittest.mock import Mock

import httpx
import pytest

from app import converter_sidecar, job


@pytest.mark.parametrize("code", [0, 1])
def test_sidecar_stops_server_when_worker_finishes(tmp_path, monkeypatch, code):
    monkeypatch.setenv("SYNC_CONTROL_DIR", str(tmp_path))
    (tmp_path / "worker-done.json").write_text(json.dumps({"exit_code": code}))
    server = Mock()
    server.poll.return_value = None
    monkeypatch.setattr(converter_sidecar.subprocess, "Popen", Mock(return_value=server))
    assert converter_sidecar.main() == code
    server.terminate.assert_called_once()
    server.wait.assert_called_once_with(timeout=30)


def test_sidecar_reports_unexpected_server_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNC_CONTROL_DIR", str(tmp_path))
    server = Mock()
    server.poll.return_value = 2
    server.returncode = 2
    monkeypatch.setattr(converter_sidecar.subprocess, "Popen", Mock(return_value=server))
    assert converter_sidecar.main() == 2
    assert (tmp_path / "converter-failed").read_text() == "2"


def test_worker_waits_for_converter_health(tmp_path, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, json={"status": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(job.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(job.time, "sleep", lambda seconds: None)
    job.wait_for_converter("http://localhost:5001", tmp_path)
    assert len(calls) == 2
    assert str(calls[-1].url) == "http://localhost:5001/healthz"


def test_worker_fails_if_converter_exits(tmp_path):
    (tmp_path / "converter-failed").touch()
    with pytest.raises(RuntimeError, match="exited"):
        job.wait_for_converter("http://localhost:5001", tmp_path)


def test_converter_startup_has_timeout(tmp_path, monkeypatch):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    monkeypatch.setattr(job.httpx, "Client", lambda **kwargs: client)
    with pytest.raises(TimeoutError, match="startup timeout"):
        job.wait_for_converter("http://localhost:5001", tmp_path, timeout=0)


@pytest.mark.parametrize("code", [0, 1])
def test_worker_signals_completion_on_success_or_failure(tmp_path, monkeypatch, code):
    monkeypatch.setenv("SYNC_CONTROL_DIR", str(tmp_path))
    monkeypatch.setattr(job, "main", lambda: code)
    assert job.entrypoint() == code
    assert json.loads((tmp_path / "worker-done.json").read_text()) == {"exit_code": code}


def test_worker_signals_completion_when_setup_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNC_CONTROL_DIR", str(tmp_path))
    monkeypatch.setattr(job, "main", Mock(side_effect=RuntimeError("setup failed")))
    with pytest.raises(RuntimeError, match="setup"):
        job.entrypoint()
    assert json.loads((tmp_path / "worker-done.json").read_text())["exit_code"] == 1
