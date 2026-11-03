import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.storage import RunStore


def test_store_tracks_runs_items_and_cursors(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create_run("run-1")
    store.update_run("run-1", "succeeded", {"converted": 2})
    store.upsert_item("drive-1", "item-1", "docs/report.pdf", "site/docs/report.pdf.md", "etag-1")
    store.set_cursor("drive-1", "https://graph.microsoft.com/delta?token=opaque")

    assert store.get_run("run-1")["summary"] == {"converted": 2}
    assert store.get_item("drive-1", "item-1")["etag"] == "etag-1"
    assert store.get_cursor("drive-1") == "https://graph.microsoft.com/delta?token=opaque"


def test_output_paths_cannot_escape_output_root(tmp_path: Path) -> None:
    store = RunStore(tmp_path)

    with pytest.raises(ValueError, match="escapes OUTPUT_DIR"):
        store.write_markdown("../outside.md", "do not write")


def test_delete_removes_only_tracked_output(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.write_markdown("site/docs/report.md", "content")
    store.upsert_item("drive", "item", "docs/report.pdf", "site/docs/report.md", "etag")

    item = store.remove_item("drive", "item")
    assert item is not None
    store.delete_output(item["output_path"])

    assert not (tmp_path / "site" / "docs" / "report.md").exists()


def test_restart_preserves_output_checkpoints_and_completed_run_history(tmp_path: Path) -> None:
    destination = tmp_path / "persistent-destination"
    store = RunStore(destination)
    store.write_markdown("site/report.md", "# Report\n")
    store.upsert_item("drive", "item", "report.pdf", "site/report.md", "etag-1")
    store.set_cursor("drive", "saved-delta-link")
    store.create_run("completed")
    store.update_run("completed", "succeeded", {"converted": 1})
    store.create_run("queued")
    store.create_run("interrupted")
    store.update_run("interrupted", "running", {})

    restarted = RunStore(destination)

    assert restarted.state_path == destination / ".sync-state.json"
    assert restarted.get_cursor("drive") == "saved-delta-link"
    item = restarted.get_item("drive", "item")
    assert item is not None
    assert item["etag"] == "etag-1"
    assert item["output_path"] == "site/report.md"
    assert (destination / item["output_path"]).read_text(encoding="utf-8") == "# Report\n"
    completed = restarted.get_run("completed")
    assert completed is not None
    assert completed["status"] == "succeeded"
    assert completed["summary"] == {"converted": 1}
    for run_id in ("queued", "interrupted"):
        run = restarted.get_run(run_id)
        assert run is not None
        assert run["status"] == "failed"
        assert run["finished_at"] is not None
        assert run["error_message"] == "Service restarted before the run completed."


def test_invalid_storage_destination_fails_without_local_fallback(tmp_path: Path) -> None:
    destination = tmp_path / "not-a-directory"
    destination.write_text("existing file", encoding="utf-8")

    with pytest.raises(FileExistsError):
        RunStore(destination)

    assert destination.read_text(encoding="utf-8") == "existing file"
    assert not (tmp_path / ".sync-state.json").exists()


def test_state_is_plain_text_json_without_database_or_lock_files(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.set_cursor("drive", "saved-cursor")
    state = json.loads(store.state_path.read_text(encoding="utf-8"))

    assert state["version"] == 1
    assert state["cursors"] == {"drive": "saved-cursor"}
    assert {path.name for path in tmp_path.iterdir()} == {".sync-state.json"}


def test_failed_atomic_write_preserves_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore(tmp_path)
    store.set_cursor("drive", "original-cursor")
    previous = store.state_path.read_bytes()

    def fail_replace(source: str, destination: Path) -> None:
        raise OSError("Simulated storage failure.")

    monkeypatch.setattr("app.storage.os.replace", fail_replace)
    with pytest.raises(OSError, match="Simulated storage failure"):
        store.set_cursor("drive", "new-cursor")

    assert store.state_path.read_bytes() == previous
    assert store.get_cursor("drive") == "original-cursor"
    assert not list(tmp_path.glob(".sync-*.tmp"))


@pytest.mark.parametrize("content", [
    '{"version":',
    '{"version": 2, "runs": {}, "cursors": {}, "items": {}}',
    '{"version": 1, "runs": {}, "cursors": {"drive": null}, "items": {}}',
    '{"version": 1, "runs": {"broken": {}}, "cursors": {}, "items": {}}',
])
def test_invalid_state_is_not_silently_reset(tmp_path: Path, content: str) -> None:
    state_path = tmp_path / ".sync-state.json"
    state_path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="Persistent JSON sync state"):
        RunStore(tmp_path)

    assert state_path.read_text(encoding="utf-8") == content


def test_concurrent_threads_do_not_lose_state_updates(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(
            lambda index: store.upsert_item(
                "drive", str(index), f"{index}.pdf", f"{index}.md", "etag"
            ),
            range(30),
        ))

    assert all(store.get_item("drive", str(index)) is not None for index in range(30))
    assert len(json.loads(store.state_path.read_text())["items"]["drive"]) == 30


def test_legacy_sqlite_is_left_untouched_with_explicit_warning(tmp_path: Path, caplog) -> None:
    legacy = tmp_path / ".sync-state.sqlite3"
    legacy.write_bytes(b"legacy-placeholder")

    store = RunStore(tmp_path)

    assert legacy.read_bytes() == b"legacy-placeholder"
    assert store.state_path.is_file()
    assert "not read or migrated" in caplog.text


@pytest.mark.parametrize("persistent_failure", [False, True])
def test_windows_replacement_errors_have_bounded_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistent_failure: bool
) -> None:
    store = RunStore(tmp_path)
    store.set_cursor("drive", "original")
    original_replace = os.replace
    calls = 0

    def replace_with_sharing_error(source: str, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if persistent_failure or calls < 3:
            error = PermissionError("Simulated Windows sharing error.")
            error.winerror = 32
            raise error
        original_replace(source, destination)

    monkeypatch.setattr("app.storage.os.replace", replace_with_sharing_error)
    monkeypatch.setattr("app.storage.time.sleep", lambda seconds: None)
    if persistent_failure:
        with pytest.raises(PermissionError, match="Simulated Windows sharing error"):
            store.set_cursor("drive", "new")
        assert calls == 4
        assert store.get_cursor("drive") == "original"
    else:
        store.set_cursor("drive", "new")
        assert calls == 3
        assert store.get_cursor("drive") == "new"
    assert not list(tmp_path.glob(".sync-*.tmp"))
