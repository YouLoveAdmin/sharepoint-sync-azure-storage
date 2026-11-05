from __future__ import annotations

import logging
import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any
from time import monotonic

from fastapi import FastAPI, HTTPException, Request, status

from app.config import Settings
from app.conversion import MarkItDownClient
from app.sharepoint import SharePointClient
from app.storage import RunStore
from app.sync import SyncService

logger = logging.getLogger(__name__)


def _create_service() -> SyncService:
    from azure.identity import AzureCliCredential, ChainedTokenCredential, ManagedIdentityCredential

    settings = Settings.from_env()
    credential = ChainedTokenCredential(
        ManagedIdentityCredential(client_id=settings.managed_identity_client_id),
        AzureCliCredential(),
    )
    store = RunStore(settings.output_dir, exclusive_writer=True)
    sharepoint = SharePointClient(credential, settings.max_upload_bytes)
    converter = MarkItDownClient(settings.converter_url, settings.max_upload_bytes)
    return SyncService(settings, store, sharepoint, converter)


def create_app(service: SyncService | None = None) -> FastAPI:
    runtime_service = service or _create_service()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sharepoint-sync")
    schedule_lock = threading.Lock()
    next_sync_at = 0.0

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            if runtime_service.settings.auto_sync:
                logger.info(
                    "Automatic sync enabled: immediate startup run, interval %s seconds; "
                    "Linux cron checks for due runs once per minute.",
                    runtime_service.settings.sync_interval,
                )
                sync_if_due()
            yield
        finally:
            await asyncio.to_thread(executor.shutdown, wait=True)
            runtime_service.sharepoint.close()
            runtime_service.converter.close()
            runtime_service.store.close()

    application = FastAPI(
        title="SharePoint Markdown Sync",
        version="1.0.0",
        lifespan=lifespan,
    )

    def execute_run(run_id: str, full_sync: bool) -> None:
        store = runtime_service.store
        summary: dict[str, int] = {}
        try:
            store.update_run(run_id, "running", summary)
            summary = runtime_service.execute(full_sync=full_sync)
            store.update_run(run_id, "succeeded", summary)
        except Exception as exc:
            logger.exception("SharePoint sync run %s failed.", run_id)
            try:
                store.update_run(
                    run_id,
                    "failed",
                    summary,
                    error_message=f"{type(exc).__name__}: {str(exc)[:900]}",
                )
            except Exception:
                logger.exception("Could not persist failure status for sync run %s.", run_id)
        finally:
            runtime_service.release_run()

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def submit_run(*, full_sync: bool = False) -> dict[str, str]:
        if not runtime_service.acquire_run():
            raise HTTPException(status_code=409, detail="A synchronization run is already active.")
        run_id = str(uuid.uuid4())
        try:
            runtime_service.store.create_run(run_id)
            executor.submit(execute_run, run_id, full_sync)
        except Exception:
            try:
                runtime_service.store.update_run(
                    run_id,
                    "failed",
                    {},
                    error_message="Could not submit the synchronization run.",
                )
            except Exception:
                logger.exception("Could not persist rejected sync run %s.", run_id)
            finally:
                runtime_service.release_run()
            logger.exception("Could not start a SharePoint sync run.")
            raise HTTPException(status_code=500, detail="Could not start a synchronization run.")
        return {"run_id": run_id, "status": "queued", "status_url": f"/sync/{run_id}"}

    def sync_if_due() -> dict[str, str]:
        nonlocal next_sync_at
        with schedule_lock:
            if not runtime_service.settings.auto_sync:
                return {"status": "disabled"}
            now = monotonic()
            if now < next_sync_at:
                return {"status": "not_due"}
            try:
                result = submit_run()
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                logger.warning("Automatic sync tick skipped because another run is active.")
                next_sync_at = now + runtime_service.settings.sync_interval
                return {"status": "busy"}
            next_sync_at = now + runtime_service.settings.sync_interval
            logger.info("Automatic sync queued run %s.", result["run_id"])
            return result

    @application.post("/sync", status_code=status.HTTP_202_ACCEPTED)
    def start_sync(full_sync: bool = False) -> dict[str, str]:
        return submit_run(full_sync=full_sync)

    @application.post("/internal/sync-due", include_in_schema=False)
    def scheduled_sync(request: Request) -> dict[str, str]:
        if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(status_code=403, detail="Scheduler requests must use loopback.")
        return sync_if_due()

    @application.get("/sync/{run_id}")
    def get_sync_status(run_id: str) -> dict[str, Any]:
        result = runtime_service.store.get_run(run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Synchronization run was not found.")
        return result

    return application
