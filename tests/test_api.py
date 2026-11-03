import threading
import time
from pathlib import Path

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
