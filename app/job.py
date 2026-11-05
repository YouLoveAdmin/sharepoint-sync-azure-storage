from __future__ import annotations

import argparse
import json
import logging
import os
import time
import uuid
from pathlib import Path

import httpx

from azure.identity import ManagedIdentityCredential

from app.config import Settings
from app.conversion import MarkItDownClient
from app.sharepoint import SharePointClient
from app.storage import RunStore
from app.sync import SyncService

logger = logging.getLogger(__name__)


def wait_for_converter(url: str, control: Path, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=5) as client:
        while True:
            if (control / "converter-failed").exists():
                raise RuntimeError("Converter container exited before sync.")
            try:
                response = client.get(f"{url.rstrip('/')}/healthz")
                if response.status_code == 200 and response.json().get("status") == "ok":
                    return
            except (httpx.RequestError, ValueError):
                logger.info("Waiting for the local converter to become ready.")
            if time.monotonic() >= deadline:
                raise TimeoutError("Local converter did not become ready within the startup timeout.")
            time.sleep(2)


def run_once(service: SyncService, *, full_sync: bool = False) -> int:
    run_id = str(uuid.uuid4())
    service.store.create_run(run_id)
    service.store.update_run(run_id, "running", {})
    try:
        summary = service.execute(full_sync=full_sync)
    except Exception as exc:
        service.store.update_run(run_id, "failed", {}, error_message=type(exc).__name__)
        logger.exception("Job sync %s failed; persistent checkpoints retained.", run_id)
        return 1
    service.store.update_run(run_id, "succeeded", summary)
    logger.info("Job sync %s succeeded: %s", run_id, json.dumps(summary))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one SharePoint sync, then exit.")
    parser.add_argument("--full-sync", action="store_true", help="Ignore delta cursors and ETag skips.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if os.name != "nt" and os.geteuid() == 0:
        import pwd

        user = pwd.getpwnam("appuser")
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    try:
        settings = Settings.from_env()
        if os.getenv("SYNC_CONTROL_DIR"):
            wait_for_converter(settings.converter_url, Path(os.environ["SYNC_CONTROL_DIR"]))
        credential = ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
        try:
            store = RunStore(settings.output_dir, exclusive_writer=True)
            try:
                sharepoint = SharePointClient(credential, settings.max_upload_bytes)
                try:
                    converter = MarkItDownClient(settings.converter_url, settings.max_upload_bytes)
                    try:
                        return run_once(
                            SyncService(settings, store, sharepoint, converter), full_sync=args.full_sync,
                        )
                    finally:
                        converter.close()
                finally:
                    sharepoint.close()
            finally:
                store.close()
        finally:
            credential.close()
    except Exception:
        logger.exception("Job setup or persistence failed.")
        return 1


def entrypoint() -> int:
    code = 1
    try:
        code = main()
        return code
    finally:
        if os.getenv("SYNC_CONTROL_DIR"):
            control = Path(os.environ["SYNC_CONTROL_DIR"])
            temporary = control / "worker-done.tmp"
            temporary.write_text(json.dumps({"exit_code": code}))
            temporary.replace(control / "worker-done.json")


if __name__ == "__main__":
    raise SystemExit(entrypoint())
