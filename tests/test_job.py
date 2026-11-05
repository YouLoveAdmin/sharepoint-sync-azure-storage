import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from app import job
from app.storage import RunStore


@pytest.mark.parametrize("full_sync", [False, True])
def test_job_runs_once_and_persists_success(tmp_path: Path, full_sync: bool) -> None:
    store = RunStore(tmp_path, exclusive_writer=True)
    service = Mock(store=store)
    service.execute.return_value = {"converted": 2, "skipped": 0}
    try:
        assert job.run_once(service, full_sync=full_sync) == 0
        service.execute.assert_called_once_with(full_sync=full_sync)
        saved = json.loads(store.state_path.read_text())
        run = next(iter(saved["runs"].values()))
        assert run["status"] == "succeeded"
        assert run["summary"] == {"converted": 2, "skipped": 0}
        assert run["finished_at"] is not None
    finally:
        store.close()


def test_failed_job_returns_nonzero_and_preserves_checkpoint(tmp_path: Path) -> None:
    store = RunStore(tmp_path, exclusive_writer=True)
    store.set_cursor("drive", "previous")
    service = Mock(store=store)
    service.execute.side_effect = RuntimeError("Conversion failed")
    try:
        assert job.run_once(service) == 1
        assert store.get_cursor("drive") == "previous"
        run = next(iter(json.loads(store.state_path.read_text())["runs"].values()))
        assert run["status"] == "failed"
        assert run["error_message"] == "RuntimeError"
    finally:
        store.close()


def test_writer_lock_blocks_other_process_before_state_recovery(tmp_path: Path) -> None:
    store = RunStore(tmp_path, exclusive_writer=True)
    store.create_run("active")
    store.update_run("active", "running", {})
    code = (
        "from pathlib import Path; from app.storage import RunStore; import sys\n"
        "try:\n"
        "    store = RunStore(Path(sys.argv[1]), exclusive_writer=True)\n"
        "except RuntimeError:\n"
        "    sys.exit(7)\n"
        "store.close()\n"
    )
    try:
        child = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, timeout=15)
        assert child.returncode == 7, child.stderr.decode()
        assert store.get_run("active")["status"] == "running"
    finally:
        store.close()
    restarted = RunStore(tmp_path, exclusive_writer=True)
    try:
        assert restarted.get_run("active")["status"] == "failed"
    finally:
        restarted.close()
    assert (tmp_path / ".sync-writer.lock").is_file()


def test_invalid_state_releases_writer_lock(tmp_path: Path) -> None:
    state_path = tmp_path / ".sync-state.json"
    state_path.write_text("invalid")
    with pytest.raises(ValueError, match="corrupt"):
        RunStore(tmp_path, exclusive_writer=True)
    state_path.write_text('{"version":1,"runs":{},"items":{},"cursors":{}}')
    store = RunStore(tmp_path, exclusive_writer=True)
    store.close()


def test_job_main_cleans_up_and_ignores_application_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Mock(
        output_dir=tmp_path, managed_identity_client_id="identity", max_upload_bytes=100,
        converter_url="https://converter.test", auto_sync=True, sync_interval=43200,
    )
    monkeypatch.setattr(job.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(job.os, "geteuid", lambda: 10001, raising=False)
    credential, sharepoint, converter = Mock(), Mock(), Mock()
    monkeypatch.setattr(job, "ManagedIdentityCredential", Mock(return_value=credential))
    monkeypatch.setattr(job, "SharePointClient", Mock(return_value=sharepoint))
    monkeypatch.setattr(job, "MarkItDownClient", Mock(return_value=converter))
    service = Mock()
    service.execute.return_value = {"converted": 1}
    def create_service(settings, store, sharepoint, converter):
        service.store = store
        return service
    monkeypatch.setattr(job, "SyncService", create_service)
    monkeypatch.setattr(sys, "argv", ["job", "--full-sync"])
    assert job.main() == 0
    service.execute.assert_called_once_with(full_sync=True)
    credential.close.assert_called_once()
    sharepoint.close.assert_called_once()
    converter.close.assert_called_once()
    reopened = RunStore(tmp_path, exclusive_writer=True)
    reopened.close()
