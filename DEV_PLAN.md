# Development plan

## Confirmed requirements

- Python service for unattended SharePoint export using an Azure managed identity.
- Source sites and libraries are configured in environment variables; Site Pages and navigation are supported.
- Additional navigation-only sites use `SHAREPOINT_SITE_URLS_NAVIGATION`; individually selected `/SitePages/*.aspx` URLs use `SHAREPOINT_SITE_PAGES`.
- All navigation is consolidated into `Extended_Knowledge.md`; selected pages use metadata/ETag checks and confirmed-404 deletion without syncing other content.
- Documents and extracted page HTML use the external MarkItDown-compatible API (`POST /convert` multipart `file`, then GET `download_url`).
- Output and persistent sync state use an environment-configured mounted storage path.
- State is plain-text UTF-8 JSON in `OUTPUT_DIR/.sync-state.json`, not SQLite. Updates use same-mount temporary files and atomic replacement, with a transient thread lock and no persistent database locks.
- The Container App filesystem is ephemeral. All retained state (delta cursors, ETags, item/output mappings, and run history) must be stored on the persistent sync destination selected by `OUTPUT_DIR`; no retained state may rely on container-local storage.
- Use delta/ETag incremental synchronization and remove generated outputs when their SharePoint sources are deleted.
- Provide an asynchronous HTTP API. The sync API is reachable only through internal Container Apps ingress.
- Provide Bicep and manual Azure CLI deployment instructions. Target subscription, region, and resource group will be supplied later.
- Never read or log `.env` values or other credentials.

## Plan and status

1. [x] Record requirements and implementation sequence.
2. [x] Implement typed configuration, managed-identity clients, conversion, storage/state, SharePoint sync, and internal async HTTP API.
3. [x] Add focused unit tests for configuration, conversion, safe output paths, delta state/deletions, and API behavior.
4. [x] Add container packaging, Bicep infrastructure, and operator documentation including required permission grants.
5. [x] Run tests and static checks; document remaining tenant-specific setup and deployment prerequisites.

## Acceptance criteria

- The service starts with explicit configuration and reports missing/invalid settings clearly.
- A sync can be requested and its status queried without blocking the HTTP request.
- Changed SharePoint items are converted and atomically published; unchanged items are skipped; deleted items have their generated output removed.
- SharePoint and converter failures are visible in run status and logs without exposing credentials.
- The default Azure ingress is internal-only; Azure resources are parameterized rather than tied to one tenant/subscription.
- Tests run without live Azure access or real secrets.
- Reopening the store on the same persistent destination preserves completed runs and sync checkpoints; interrupted queued/running runs are marked failed and can be retried.
- Failed state writes preserve the last committed JSON; corrupt/unsupported state fails explicitly rather than resetting checkpoints. Only one process/container may write to a destination.

Deployment to Azure is intentionally pending until the owner supplies the target resource group and region and configures the required SharePoint permissions and converter endpoint. No live tenant or endpoint test has been performed.
