from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir.expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.output_dir / ".sync-state.json"
        self._lock = threading.RLock()
        if not self.state_path.exists():
            if (self.output_dir / ".sync-state.sqlite3").exists():
                logger.warning(
                    "Legacy SQLite state is not read or migrated. Starting JSON state; "
                    "the next sync will be full and previous run history is unavailable."
                )
            self._write_text(
                self.state_path,
                json.dumps({"version": 1, "runs": {}, "cursors": {}, "items": {}}, indent=2) + "\n",
            )
        with self._transaction() as state:
            for run in state["runs"].values():
                if run["status"] in {"queued", "running"}:
                    run.update(
                        status="failed",
                        finished_at=utc_now(),
                        error_message="Service restarted before the run completed.",
                    )

    def _read_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise ValueError("Persistent JSON sync state is corrupt; restore it from backup.") from None
        if (
            not isinstance(state, dict)
            or state.get("version") != 1
            or any(not isinstance(state.get(key), dict) for key in ("runs", "cursors", "items"))
        ):
            raise ValueError("Persistent JSON sync state has an unsupported schema.")
        for run_id, run in state["runs"].items():
            if (
                not isinstance(run, dict)
                or run.get("id") != run_id
                or not isinstance(run.get("status"), str)
                or run.get("status") not in {"queued", "running", "succeeded", "failed"}
                or not isinstance(run.get("created_at"), str)
                or any(run.get(key) is not None and not isinstance(run[key], str)
                       for key in ("started_at", "finished_at", "error_message"))
                or not isinstance(run.get("summary"), dict)
                or any(type(value) is not int for value in run["summary"].values())
            ):
                raise ValueError("Persistent JSON sync state contains an invalid run record.")
        if any(not isinstance(value, str) for value in state["cursors"].values()):
            raise ValueError("Persistent JSON sync state contains an invalid cursor.")
        for drive_id, items in state["items"].items():
            if not isinstance(items, dict):
                raise ValueError("Persistent JSON sync state contains an invalid item collection.")
            for item_id, item in items.items():
                if (
                    not isinstance(item, dict)
                    or any(not isinstance(item.get(key), str) for key in
                           ("drive_id", "item_id", "source_path", "output_path", "etag"))
                    or item["drive_id"] != drive_id
                    or item["item_id"] != item_id
                ):
                    raise ValueError("Persistent JSON sync state contains an invalid item record.")
        return state

    @contextmanager
    def _transaction(self) -> Iterator[dict[str, Any]]:
        with self._lock:
            state = self._read_state()
            yield state
            self._write_text(self.state_path, json.dumps(state, indent=2, ensure_ascii=False) + "\n")

    def create_run(self, run_id: str) -> None:
        with self._transaction() as state:
            if run_id in state["runs"]:
                raise ValueError("Synchronization run already exists.")
            state["runs"][run_id] = {
                "id": run_id, "status": "queued", "created_at": utc_now(),
                "started_at": None, "finished_at": None, "summary": {}, "error_message": None,
            }

    def update_run(
        self,
        run_id: str,
        status: str,
        summary: dict[str, int],
        error_message: str | None = None,
    ) -> None:
        if status not in {"queued", "running", "succeeded", "failed"}:
            raise ValueError("Invalid synchronization run status.")
        with self._transaction() as state:
            run = state["runs"].get(run_id)
            if run is None:
                raise LookupError("Synchronization run was not found.")
            now = utc_now()
            run.update(
                status=status,
                started_at=run["started_at"] or now,
                finished_at=now if status in {"succeeded", "failed"} else run["finished_at"],
                summary=dict(summary),
                error_message=error_message,
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read_state()["runs"].get(run_id)

    def get_cursor(self, drive_id: str) -> str | None:
        with self._lock:
            return self._read_state()["cursors"].get(drive_id)

    def set_cursor(self, drive_id: str, delta_url: str) -> None:
        with self._transaction() as state:
            state["cursors"][drive_id] = delta_url

    def get_item(self, drive_id: str, item_id: str) -> dict[str, str] | None:
        with self._lock:
            return self._read_state()["items"].get(drive_id, {}).get(item_id)

    def upsert_item(
        self, drive_id: str, item_id: str, source_path: str, output_path: str, etag: str
    ) -> None:
        with self._transaction() as state:
            state["items"].setdefault(drive_id, {})[item_id] = {
                "drive_id": drive_id, "item_id": item_id, "source_path": source_path,
                "output_path": output_path, "etag": etag,
            }

    def remove_item(self, drive_id: str, item_id: str) -> dict[str, str] | None:
        with self._transaction() as state:
            return state["items"].get(drive_id, {}).pop(item_id, None)

    def items_under_path(self, drive_id: str, source_path: str) -> list[dict[str, str]]:
        with self._lock:
            return self._matching_items(self._read_state(), drive_id, source_path)

    def remove_items_under_path(self, drive_id: str, source_path: str) -> list[dict[str, str]]:
        with self._transaction() as state:
            items = self._matching_items(state, drive_id, source_path)
            for item in items:
                del state["items"][drive_id][item["item_id"]]
            return items

    @staticmethod
    def _matching_items(
        state: dict[str, Any], drive_id: str, source_path: str
    ) -> list[dict[str, str]]:
        prefix = source_path.rstrip("/") + "/"
        return [
            item for item in state["items"].get(drive_id, {}).values()
            if item["source_path"] == source_path or item["source_path"].startswith(prefix)
        ]

    def write_markdown(self, relative_path: str, content: str) -> Path:
        destination = (self.output_dir / relative_path).resolve()
        if not destination.is_relative_to(self.output_dir):
            raise ValueError("Generated output path escapes OUTPUT_DIR.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._write_text(destination, content)
        return destination

    @staticmethod
    def _write_text(destination: Path, content: str) -> None:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".sync-", suffix=".tmp", dir=destination.parent
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            for attempt in range(4):
                try:
                    os.replace(temporary_name, destination)
                    break
                except PermissionError as exc:
                    if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 3:
                        raise
                    logger.warning("Windows access/sharing error during file replacement; retrying.")
                    time.sleep(0.05 * (2 ** attempt))
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    def delete_output(self, relative_path: str) -> None:
        destination = (self.output_dir / relative_path).resolve()
        if not destination.is_relative_to(self.output_dir):
            raise ValueError("Stored output path escapes OUTPUT_DIR.")
        destination.unlink(missing_ok=True)
        parent = destination.parent
        while parent != self.output_dir:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
