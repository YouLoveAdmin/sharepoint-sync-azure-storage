# SharePoint to Markdown sync

This project exports configured SharePoint document libraries, modern Site Pages, and navigation to Markdown. It runs as a Python API in Azure Container Apps, uses a managed identity for unattended access, calls an external MarkItDown-compatible service, and writes Markdown plus delta-sync state to a mounted Azure Files share.

## What it does

- Reads configured libraries from configured SharePoint sites with Microsoft Graph drive delta queries.
- Converts changed files using `POST /convert` with a multipart `file` field. The converter returns JSON containing a `download_url`; the URL must use the same host as the configured converter endpoint.
- Extracts the `CanvasContent1` HTML for `.aspx` pages in the configured Site Pages library and sends the HTML to the converter.
- Reads additional navigation-only sites and individually selected pages without exporting other content from those sites.
- Exports the site's visible SharePoint navigation location to Markdown (`Auto` detects the modern communication-site top navigation or the team-site Quick Launch).
- Skips unchanged files, atomically replaces changed outputs, and removes outputs when Graph reports a source deletion.
- Persists delta cursors, item mappings, and run status in the plain-text `.sync-state.json` under `OUTPUT_DIR`.
- Exposes an asynchronous internal HTTP API: `POST /sync` starts a run; `GET /sync/{run_id}` reports its status; `GET /health` is a health probe.

Each library item or selected page is published under a site/library-relative folder as `<original-name>.md` (for example `report.pdf.md` or `Home.aspx.md`). All navigation is consolidated in `OUTPUT_DIR/Extended_Knowledge.md`, grouped by source site and navigation location with nested menu links. Item Markdown includes YAML front matter with the source URL, file path, ETag, and modified time.

### Source scopes

All URL lists accept semicolon-separated entries (commas are also supported):

| Setting | Content exported |
|---|---|
| `SHAREPOINT_SITE_URLS` | Configured libraries, plus navigation when `SHAREPOINT_INCLUDE_NAVIGATION=true` |
| `SHAREPOINT_SITE_URLS_NAVIGATION` | Navigation only; no documents or pages, regardless of `SHAREPOINT_INCLUDE_NAVIGATION` |
| `SHAREPOINT_SITE_PAGES` | Only the listed individual `/SitePages/*.aspx` page URLs; no other pages, documents, or navigation |

The main site list and library list can be empty when only the new scopes are used. At least one source list must be populated. Use full page URLs, not site URLs, in `SHAREPOINT_SITE_PAGES`, for example `https://contoso.sharepoint.com/sites/hr/SitePages/Home.aspx`. The two additional scopes are additive: if a site also appears in the main site list, its configured libraries still sync normally.

Selected pages are checked individually for metadata/ETag changes without enumerating their library items or advancing its delta cursor. A confirmed page `404` removes its tracked Markdown; permission and network failures fail the run without deleting that output.

The consolidated navigation file is atomically replaced after every configured navigation source has been read successfully, so removed menu links disappear and a failed read preserves the previous file. Old per-site `navigation-*.md` exports for those sites are removed after successful consolidation. When no navigation sources are selected, navigation output is left untouched.

## Configuration and local development

### Persistent storage requirement

`OUTPUT_DIR` is required and controls both the Markdown destination and the plain-text JSON state file `.sync-state.json`. In Azure, it **must point to your writable persistent storage mount**, for example `/mnt/sharepoint`. The sample `./output` value is only for local development; a directory inside the container filesystem does not survive replacement or reboot.

Delta cursors, ETags, item/output mappings, and run history are all stored at this destination. State updates are serialized within the process and written as UTF-8 JSON using a temporary file, flush/fsync, and replacement on the same mount. There are no SQLite databases, database journals, or persistent lock files used by the application. Temporary publication files also stay on that mount. There is no container-local state fallback: inaccessible or unwritable storage causes an error.

Mount the same persistent destination at the configured path after every reboot or redeployment. Preserve `.sync-state.json` along with the Markdown and exclude state and temporary files from downstream Markdown ingestion. The application cannot determine whether an arbitrary writable path is truly persistent; configuring the mount correctly is a deployment responsibility. The mount must support same-directory file replacement. Malformed or unsupported JSON state raises an error instead of silently resetting checkpoints; restore a known-good backup to recover.

Use only one process/replica writing to a destination, including during revision changes; the thread lock does not coordinate different containers. A process killed during a write can leave an unused `.sync-*.tmp` file, but restart reads only `.sync-state.json`, never that temporary file.

Existing `.sync-state.sqlite3` files are not opened, migrated, or deleted. If one exists without JSON state, startup emits a warning and starts fresh JSON state: previous run history is unavailable and the next sync re-exports current items. Old outputs for sources deleted before that initial scan cannot be reconciled from the legacy database automatically.

After restart, completed runs and checkpoints remain available. Interrupted queued/running runs are marked failed, not automatically resumed; start a new `POST /sync` run to continue using the saved checkpoints. In-memory worker threads and locks are intentionally transient and are recreated at startup.

1. Use Python 3.12 or later.
2. Copy `.env.sample` to `.env` and set the site URLs, library names, reachable MarkItDown API URL, and output directory. Keep actual credentials out of `.env.sample`, source control, logs, and chat.
3. Install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements-dev.txt
   ```

4. Sign in with Azure CLI as a developer identity that has the required SharePoint permissions, then start the API:

   ```powershell
   az login
   uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
   ```

5. Start and inspect a run:

   ```powershell
   $run = Invoke-RestMethod -Method Post http://127.0.0.1:8000/sync
   Invoke-RestMethod "http://127.0.0.1:8000$($run.status_url)"
   ```

The local API has no application-level login. Bind it to loopback as shown; do not expose it directly to the internet. In Azure, Container Apps ingress is internal-only.

## Azure identity and SharePoint permissions

The Container App uses its system-assigned managed identity by default. To use a user-assigned managed identity locally or in Azure, set `AZURE_CLIENT_ID` to its client ID. The runtime first tries managed identity and falls back to Azure CLI for local development.

Before the first sync, an Entra/SharePoint administrator must:

1. Grant the managed identity the required **application** permissions for Microsoft Graph and SharePoint Online. Use `Sites.Selected` for each API where available rather than tenant-wide read permissions.
2. Grant admin consent for those application permissions.
3. Grant the managed identity **read** access to each configured site using the selected-sites permission mechanism. `Sites.Selected` consent alone grants no site access.
4. Confirm the MarkItDown endpoint is network-reachable from the Container Apps environment. The endpoint must accept multipart `POST /convert`, return JSON with a `download_url` on the same origin, and serve the converted Markdown from that URL.

SharePoint navigation and page fields use SharePoint REST, while libraries and delta tokens use Microsoft Graph. Both resources need corresponding permission configuration. Do not grant write access to SharePoint; this application only reads source content.

The deployment returns the managed identity's service-principal object ID as `managedIdentityPrincipalId`. An administrator can look up its application/client ID with:

```powershell
$principalId = '<managedIdentityPrincipalId>'
$clientId = az ad sp show --id $principalId --query appId --output tsv
```

Grant the `Sites.Selected` **application** role on Microsoft Graph and SharePoint Online to that service principal, with administrator consent. Then grant read access to each site, for example with Microsoft Graph PowerShell:

```powershell
Connect-MgGraph -Scopes 'Sites.FullControl.All'
$siteId = '<site-id>'
New-MgSitePermission -SiteId $siteId -Roles Read -GrantedToIdentities @{
  Application = @{
    Id = $clientId
    DisplayName = 'SharePoint Markdown Sync'
  }
}
```

An administrator must perform the permission and site-grant steps; the Container App does not grant itself access.

## Azure deployment

`infra/main.bicep` provisions a system-assigned identity, internal-only Container Apps ingress, one Container App replica, a Container Apps managed environment, and an Azure Files share mounted at `OUTPUT_DIR`. The managed identity does not receive SharePoint permissions automatically; follow the administrator steps above after deployment.

Build and publish the image to a registry accessible by Container Apps, then deploy the Bicep template. Supply the target resource group, region, image, site URLs, and converter URL when those values are known:

```powershell
az deployment group create `
  --resource-group <resource-group> `
  --template-file infra/main.bicep `
  --parameters `
    location=<azure-region> `
    image=<registry>/<image>:<tag> `
    sharePointSiteUrls='https://tenant.sharepoint.com/sites/example' `
    sharePointSiteUrlsNavigation='https://tenant.sharepoint.com/sites/menu' `
    sharePointSitePages='https://tenant.sharepoint.com/SitePages/Home.aspx' `
    markItDownApiUrl='https://<reachable-converter-host>'
```

If the image is in a private registry, configure Container Apps image-pull authentication for that registry before expecting the revision to become healthy. Do not pass passwords or access tokens as ordinary Bicep parameters.

`sharePointSiteUrlsNavigation` and `sharePointSitePages` default to empty strings. For a navigation-only or selected-pages-only deployment, leave `sharePointSiteUrls` empty. Grant the managed identity read access to the sites in all three scopes, including sites hosting individually selected pages.

The sync endpoint is private to the Container Apps environment. Callers must have network access to that environment; the API does not perform end-user authentication. Keep the app at one replica because the current asynchronous run queue and lock are process-local. Run records and delta state survive restarts on Azure Files; a run interrupted by a restart is marked failed and can be started again.

## API

| Method | Path | Result |
|---|---|---|
| `GET` | `/health` | Liveness/readiness response |
| `POST` | `/sync` | `202` with a run ID, or `409` if a run is already active |
| `GET` | `/sync/{run_id}` | Queued/running/succeeded/failed state, counts, and a failure message when applicable |

## Validation

Run unit tests:

```powershell
python -m pytest
```

Tests use mock Graph/converter responses and do not require Azure credentials or live SharePoint data.

## Existing PowerShell prototypes

The `script` folder contains earlier local export and converter prototypes. The Python service is the deployable application; it does not execute those scripts.
