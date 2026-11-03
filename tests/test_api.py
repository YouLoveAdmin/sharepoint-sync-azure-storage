import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.storage import RunStore


class FakeResource:
    def close(self) -> None:
        pass


class FakeService:
    def __init__(self, output_dir: Path) -> None:
        self.store = RunStore(output_dir)
        self.sharepoint = FakeResource()
        self.converter = FakeResource()
        self._lock = threading.Lock()
        self.started = threading.Event()
        self.finish = threading.Event()
        self.settings = SimpleNamespace(auto_sync=False, sync_interval=43200)

    def acquire_run(self) -> bool:
        return self._lock.acquire(blocking=False)

    def release_run(self) -> None:
        self._lock.release()

    def execute(self) -> dict[str, int]:
        self.started.set()
        self.finish.wait(timeout=2)
        return {"converted": 1, "skipped": 0, "deleted": 0, "navigation": 0}


def test_sync_endpoint_returns_run_id_and_status(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    with TestClient(create_app(service)) as client:
        response = client.post("/sync")
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        assert service.started.wait(timeout=1)
        assert client.get(f"/sync/{run_id}").json()["status"] == "running"
        assert client.post("/sync").status_code == 409

        service.finish.set()
        for _ in range(50):
            status_response = client.get(f"/sync/{run_id}")
            if status_response.json()["status"] == "succeeded":
                break
            time.sleep(0.01)

        assert status_response.json()["status"] == "succeeded"
        assert status_response.json()["summary"]["converted"] == 1
        assert client.get("/health").json() == {"status": "ok"}


def test_sync_status_returns_404_for_unknown_run(tmp_path: Path) -> None:
    with TestClient(create_app(FakeService(tmp_path))) as client:
        assert client.get("/sync/missing").status_code == 404


def test_cron_sync_starts_immediately_and_repeats_when_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr("app.api.monotonic", lambda: now[0])
    service = FakeService(tmp_path)
    service.settings.auto_sync = True
    with TestClient(create_app(service), client=("127.0.0.1", 50000)) as client:
        assert service.started.wait(timeout=1)
        assert client.post("/sync").status_code == 409
        assert client.post("/internal/sync-due").json()["status"] == "not_due"
        now[0] += 43200
        assert client.post("/internal/sync-due").json()["status"] == "busy"
        service.finish.set()
        for _ in range(100):
            if not service._lock.locked():
                break
            time.sleep(0.01)
        assert not service._lock.locked()
        service.started.clear()
        now[0] += 43199
        assert client.post("/internal/sync-due").json()["status"] == "not_due"
        now[0] += 1
        response = client.post("/internal/sync-due")
        assert response.json()["status"] == "queued"
        assert service.started.wait(timeout=1)
        run_id = response.json()["run_id"]
        for _ in range(100):
            result = client.get(f"/sync/{run_id}").json()
            if result["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert result["status"] == "succeeded"
        assert result["summary"]["converted"] == 1
    assert not service._lock.locked()


def test_cron_is_disabled_by_default_and_loopback_only(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    with TestClient(create_app(service), client=("127.0.0.1", 50000)) as client:
        assert not service.started.is_set()
        assert client.post("/internal/sync-due").json() == {"status": "disabled"}
    with TestClient(create_app(FakeService(tmp_path))) as client:
        assert client.post(
            "/internal/sync-due", headers={"X-Forwarded-For": "127.0.0.1"},
        ).status_code == 403


def test_failed_auto_run_is_persisted_and_next_tick_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingService(FakeService):
        def execute(self) -> dict[str, int]:
            raise RuntimeError("Conversion failed.")

    now = [0.0]
    monkeypatch.setattr("app.api.monotonic", lambda: now[0])
    service = FailingService(tmp_path)
    service.settings.auto_sync = True
    with TestClient(create_app(service), client=("127.0.0.1", 50000)) as client:
        for _ in range(100):
            if not service._lock.locked():
                break
            time.sleep(0.01)
        now[0] += 43200
        response = client.post("/internal/sync-due")
        assert response.json()["status"] == "queued"
        for _ in range(100):
            result = client.get(response.json()["status_url"]).json()
            if result["status"] == "failed":
                break
            time.sleep(0.01)
        assert result["status"] == "failed"
        assert "Conversion failed" in result["error_message"]
