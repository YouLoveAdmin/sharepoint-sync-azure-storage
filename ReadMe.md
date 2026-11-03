# SharePoint to Markdown sync

This project exports configured SharePoint document libraries, modern Site Pages, and navigation to Markdown. It runs as a Python API in Azure Container Apps, uses a managed identity for unattended access, calls an external MarkItDown-compatible service, and writes Markdown plus delta-sync state to a mounted Azure Files share.

## What it does

- Reads configured libraries from configured SharePoint sites with Microsoft Graph drive delta queries.
- Converts changed files using `POST /convert` with a multipart `file` field. The converter returns JSON containing a `download_url`; the URL must use the same host as the configured converter endpoint.
- Skips files explicitly rejected by the converter as unsupported, logs a warning, and reports them separately as `unsupported` in the run summary. Other conversion errors fail the run.
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

`SHAREPOINT_LIBRARY_NAMES` accepts library display names (for example `Documents`) or library URL folder names (for example `Shared Documents` or `Shared%20Documents`). Matching is case-insensitive, with display names taking precedence. A missing or ambiguous library fails the run rather than silently skipping content. Every listed library must exist on every site in `SHAREPOINT_SITE_URLS`; use the navigation-only and selected-page scopes for sites that do not require a full library export.

Library discovery explicitly includes Graph system drives so that hidden system
libraries such as `Site Pages` can be selected. Other system libraries are not
exported unless configured.

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

### Converter connectivity in Azure

When MarkItDown runs in Azure Container Apps, set `MARKITDOWN_API_URL` to
`https://<converter-ingress-fqdn>` without the container's target port. For example,
target port `5001` is used between ingress and the converter container, not by HTTPS
clients, which connect to port `443`. Both apps must have network access to the
converter's ingress; an internal converter must be reachable within its environment.

If a run reports a MarkItDown network error, check DNS and TCP connectivity from
the sync container before changing SharePoint permissions. A successful `/health`
response from the sync API does not verify connectivity to the converter. After
correcting the deployed setting, wait for the replacement revision to become ready
and start a new sync. Preserve the existing output mount and state.

An HTTP `400` response with JSON `{"error":"File type not supported"}` is an
explicit unsupported-file rejection, not a successful conversion. These files
produce no new Markdown and do not prevent supported files from syncing. Any
previously exported version remains unchanged. Successful library scans still
advance their delta cursor, so unchanged unsupported files are not retried on
every incremental run; a source change or a full scan will retry them. The
`unsupported` count describes rejections encountered in that run, not the total
number of unsupported files in the source.

The converter client remembers explicitly rejected filename extensions for its
process lifetime. Subsequent files with those extensions are counted and skipped
before download, avoiding unnecessary transfers (including oversized videos).
This cache resets on restart; source and conversion size limits still apply to
files whose types have not been rejected.

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

### Permission setup helper

The local [assign_permission.ps1](script/assign_permission.ps1) reads deployment and source settings from the repository configuration at runtime, discovers the deployed app's system-assigned identity, and processes all sites in `SHAREPOINT_SITE_URLS`, `SHAREPOINT_SITE_URLS_NAVIGATION`, and the hosting sites derived from `SHAREPOINT_SITE_PAGES`. Site URLs are deduplicated.

Install the Graph authentication module once if it is missing, then sign into Azure CLI with the intended subscription:

```powershell
Install-Module Microsoft.Graph.Authentication -Scope CurrentUser
az login
az account set --subscription '<subscription-id-or-name>'

# Local preview only; no sign-in or permission changes.
.\script\assign_permission.ps1
.\script\assign_permission.ps1 -Apply -WhatIf

# Prompts for confirmation and administrator Graph sign-in.
.\script\assign_permission.ps1 -Apply
```

The administrator Graph session requests `Application.Read.All`, `AppRoleAssignment.ReadWrite.All`, and `Sites.FullControl.All`. Use an account authorized for app-role assignments and SharePoint site permission management (activate applicable PIM roles first); delegated site operations also require appropriate target-site membership. Tenant consent policy may require a separate administrator. These setup scopes are **not assigned to the runtime identity**. The helper grants only `Sites.Selected` for Graph/SharePoint and site-level `read`. It skips existing grants, warns about broader permissions without removing them, disconnects its process-scoped Graph session, and never prints URLs or identity/configuration values. Partial success is possible; rerun after resolving a failure. Allow permission propagation before testing a sync.

The helper and shared [Configuration.ps1](script/Configuration.ps1) remain local-only under the Git-ignored `script` folder. No configuration file is uploaded or executed as PowerShell.

The permission helper uses core `az resource show` to discover the app identity and does not require the `containerapp` CLI extension. This does not change the separate deployment helper, which still uses Container Apps commands.

If permission setup fails during Azure discovery, its error identifies `account show` or `resource show` and provides a sanitized diagnosis. Azure CLI login is local to each computer/user; signing into Graph or deploying from another machine does not sign in Azure CLI on the machine running this helper. Run `az login`, select the intended subscription with `az account set --subscription '<subscription-id-or-name>'`, and check the deployment settings on that computer. Azure RBAC read access to the app is required in addition to Graph administrator privileges.

## Azure deployment

`infra/main.bicep` provisions a system-assigned identity, internal-only Container Apps ingress, one Container App replica, a Container Apps managed environment, and an Azure Files share mounted at `OUTPUT_DIR`. The managed identity does not receive SharePoint permissions automatically; follow the administrator steps above after deployment.

### Publish the image

Every push to `master` and every manual run of the **Publish container image** GitHub Actions workflow builds and publishes the Docker image to GitHub Container Registry (GHCR). A successful run updates the rolling `latest` tag and publishes an immutable `sha-<commit>` tag.

Image: `ghcr.io/youloveadmin/sharepoint-sync-azure-storage`

**Before deploying:** the GitHub repository is public, but GHCR package visibility is configured separately. Make the GHCR package **Public** in its package settings. Verify it can be pulled without registry credentials:

```powershell
docker pull ghcr.io/youloveadmin/sharepoint-sync-azure-storage:latest
```

If `docker pull` returns `unauthorized`, the package is still private. The Bicep template below does not configure private-registry credentials, so make the package public before using it.

### Deploy to Azure Container Apps

#### PowerShell deployment helper

The local helper [Deploy-SharePointSync.ps1](script/Deploy-SharePointSync.ps1) reads `..\.env` relative to its own location **only when you execute it**. Populate the variable names in [.env.sample](.env.sample), including `AZURE_RESOURCE_GROUP`, `AZURE_LOCATION`, `AZURE_CONTAINER_APP_NAME`, and `AZURE_CONTAINER_APPS_ENVIRONMENT`. Optionally set `AZURE_SUBSCRIPTION_ID`; otherwise it uses the current Azure CLI subscription. `AZURE_TENANT_ID` is optional: when blank, the helper uses that subscription's tenant; when supplied, it refuses deployment if the Azure CLI tenant does not match.

Before deploying, sign in with `az login` and select the intended subscription with `az account set`. Set `OUTPUT_DIR` to an absolute Linux mount path such as `/mnt/sharepoint`, not the sample's local-development `./output`. Set `MARKITDOWN_API_URL` to a converter reachable from Azure, not localhost. Leave `AZURE_CLIENT_ID` empty: the template provisions a system-assigned identity.

Run from the repository root with PowerShell 7:

```powershell
# Validate settings and preview the operation without reading Azure or deploying.
.\script\Deploy-SharePointSync.ps1 -Mode Deploy -WhatIf

# Create/update the resource group, managed environment, persistent share, and app.
.\script\Deploy-SharePointSync.ps1 -Mode Deploy

# Require the app to exist, then reapply configuration and refresh its image.
.\script\Deploy-SharePointSync.ps1 -Mode Update

# Optionally override AZURE_CONTAINER_IMAGE for a specific published commit.
.\script\Deploy-SharePointSync.ps1 -Mode Update `
  -Image 'ghcr.io/youloveadmin/sharepoint-sync-azure-storage:sha-<full-commit-sha>'
```

`AZURE_CONTAINER_IMAGE` defaults to the published `:latest` image. Both modes apply the same Bicep template, including runtime settings, and force a uniquely named revision so a mutable image tag is pulled again. `Update` is intended for apps created by this template, not arbitrary pre-existing apps; it does not import an existing file share. Keep resource group, environment, and destination settings stable to retain the same output/state mount. Do not update while a sync is running, and avoid overlapping writer processes during revision transitions.

The helper does not print configuration values or raw Azure CLI errors. It uses a temporary parameter file containing only mapped deployment settings and removes it when finished; it never uploads the configuration file itself. Azure resource configuration/deployment history still contains the mapped non-secret settings, so do not put credentials in these values. The image must be publicly pullable: this template does not configure private-registry credentials. SharePoint permission grants remain a separate administrator step.

The helper prints timestamped Azure CLI step start/completion messages, then polls the deployment and container readiness every 15 seconds. Initial template compilation/submission can take several minutes. Polling has a 30-minute overall timeout, configurable with `-DeploymentTimeoutSeconds` and `-PollIntervalSeconds`. A timeout stops local polling, not the Azure deployment; check the resource group's **Deployments** blade before retrying. These limits apply after submission and do not interrupt a blocked Azure CLI call. If the PowerShell prompt has returned without a success message, do not assume deployment succeeded.

If revisions remain **Activating**, inspect replica restart counts and console logs. `CrashLoopBackOff` indicates a startup crash rather than a slow deployment. A startup `ValueError` naming a source setting must be corrected before redeploying. URL lists must contain full `https://` URLs separated by semicolons, without individually quoted entries, PowerShell arrays, or inline comments. Use `/SitePages/*.aspx` URLs for selected pages. The helper validates this syntax locally, including during `-WhatIf`, and reports only the setting name and entry number. Single revision mode can temporarily show both an older revision and its activating replacement; do not switch to multiple revision mode to fix a startup crash.

The entire `script` folder remains Git-ignored as previously requested, so this helper is local-only and will not appear in the GitHub repository unless you explicitly change that policy.

#### Manual Azure CLI alternative

Prerequisites: Azure CLI logged into the target subscription, permission to create the resource group and Azure resources, the public image above, and a MarkItDown endpoint reachable from the Container Apps environment. The template creates an Azure Files share and mounts it at `OUTPUT_DIR`; `.sync-state.json` and converted Markdown persist there across container replacements.

Set deployment values in PowerShell, then create the resource group:

```powershell
az login
az account set --subscription '<subscription-id-or-name>'

$resourceGroup = '<resource-group>'
$location = '<azure-region>'
$appName = 'ca-sharepoint-sync'
$image = 'ghcr.io/youloveadmin/sharepoint-sync-azure-storage:latest'
$converterUrl = 'https://<reachable-converter-host>'

az group create `
  --name $resourceGroup `
  --location $location
```

Deploy the Bicep template. Supply semicolon-separated URLs where a scope has multiple entries. Set unused scopes to an empty string. `sharePointSiteUrls` is for library sync; navigation-only and individually selected page URLs use their separate parameters.

```powershell
az deployment group create `
  --name ca-sharepoint-sync `
  --resource-group $resourceGroup `
  --template-file infra/main.bicep `
  --parameters `
    location=$location `
    appName=$appName `
    image=$image `
    sharePointSiteUrls='https://tenant.sharepoint.com/sites/hr' `
    sharePointSiteUrlsNavigation='https://tenant.sharepoint.com/sites/menu' `
    sharePointSitePages='https://tenant.sharepoint.com/sites/hr/SitePages/Home.aspx' `
    sharePointLibraryNames='Shared Documents;Site Pages' `
    markItDownApiUrl=$converterUrl `
    outputPath='/mnt/sharepoint'
```

To sync only navigation or selected pages, leave `sharePointSiteUrls` empty. To sync only libraries and their navigation, leave `sharePointSiteUrlsNavigation` and `sharePointSitePages` empty. You can also customize `outputShareName`, `shareQuotaGiB`, `includeNavigation`, and `navigationLocations`; by default the template creates a 100-GiB Azure Files share and mounts it at `/mnt/sharepoint`.

Read the deployment outputs and give the system-assigned identity read access to the configured SharePoint sites. An administrator must grant the required Microsoft Graph and SharePoint `Sites.Selected` application permissions and site-level Read grants as described above; deployment alone does not grant SharePoint access.

```powershell
$deployment = az deployment group show `
  --name ca-sharepoint-sync `
  --resource-group $resourceGroup `
  --query properties.outputs `
  --output json | ConvertFrom-Json

$appId = $deployment.appId.value
$principalId = $deployment.managedIdentityPrincipalId.value
$internalFqdn = $deployment.internalFqdn.value
$storageAccount = $deployment.outputStorageAccountName.value

az containerapp show `
  --name $appName `
  --resource-group $resourceGroup `
  --query properties.provisioningState `
  --output tsv
```

`internalFqdn` is only reachable from the Container Apps environment/network; ingress is intentionally not public. After site permissions are granted, call `POST https://<internalFqdn>/sync` from a client that can reach the internal environment, then poll the returned `status_url`.

### Deploy a newly published image

`latest` is a mutable registry tag; publishing a new image does **not** by itself restart an existing Container App. For each successful workflow run, deploy its immutable commit tag to create a new Container Apps revision:

```powershell
gh run list `
  --repo YouLoveAdmin/sharepoint-sync-azure-storage `
  --workflow publish-container.yml `
  --branch master `
  --limit 1 `
  --json headSha,status,conclusion

$commit = '<headSha from the successful workflow run>'
$image = "ghcr.io/youloveadmin/sharepoint-sync-azure-storage:sha-$commit"

az containerapp update `
  --name $appName `
  --resource-group $resourceGroup `
  --image $image
```

Use the immutable tag rather than relying on a running app to detect changes to `latest`. After checking the new revision, you can use `:latest` for a fresh deployment or update by explicitly supplying the newly published image reference.

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
