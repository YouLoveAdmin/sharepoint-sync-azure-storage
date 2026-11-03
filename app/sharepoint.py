from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import ParseResult, quote, unquote, urlparse

import httpx
from azure.core.credentials import TokenCredential

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


class SharePointClient:
    def __init__(
        self,
        credential: TokenCredential,
        max_upload_bytes: int = 104_857_600,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.credential = credential
        self.max_upload_bytes = max_upload_bytes
        self.http = http_client or httpx.Client(timeout=60, follow_redirects=True)

    def close(self) -> None:
        self.http.close()

    def _headers(self, scope: str) -> dict[str, str]:
        token = self.credential.get_token(scope).token
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _get_json(self, url: str, scope: str) -> dict:
        try:
            response = self.http.get(url, headers=self._headers(scope))
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"SharePoint request failed (HTTP {exc.response.status_code}).") from None
        except httpx.RequestError:
            raise RuntimeError("SharePoint request failed due to a network error.") from None
        return response.json()

    def _graph_get(self, url: str) -> dict:
        return self._get_json(url, "https://graph.microsoft.com/.default")

    def resolve_site(self, site_url: str) -> dict:
        parsed = urlparse(site_url)
        path = parsed.path.strip("/")
        encoded_path = quote(path, safe="/")
        address = f"{GRAPH_ROOT}/sites/{parsed.netloc}:/{encoded_path}"
        return self._graph_get(address)

    def get_drives(self, site_id: str) -> list[dict]:
        return self._collect(f"{GRAPH_ROOT}/sites/{quote(site_id, safe=',')}/drives")

    def find_drive(self, site_id: str, library_name: str) -> dict:
        drives = self.get_drives(site_id)
        name = library_name.strip().casefold()
        matches = [drive for drive in drives if drive.get("name", "").casefold() == name]
        if not matches:
            folder_name = unquote(library_name.strip()).casefold()
            matches = [
                drive for drive in drives
                if unquote(urlparse(drive.get("webUrl", "")).path.rstrip("/").rsplit("/", 1)[-1]).casefold()
                == folder_name
            ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise LookupError(
                f"SharePoint library '{library_name}' is ambiguous; use its unique display name."
            )
        raise LookupError(
            f"SharePoint library '{library_name}' was not found on a configured site. "
            "Check SHAREPOINT_LIBRARY_NAMES against library display names or URL folder names; "
            "each configured library must exist on every main source site."
        )

    def delta_items(self, drive_id: str, cursor: str | None) -> tuple[list[dict], str]:
        address = cursor or f"{GRAPH_ROOT}/drives/{quote(drive_id, safe='')}/root/delta"
        items: list[dict] = []
        delta_url: str | None = None
        while address:
            page = self._graph_get(address)
            items.extend(page.get("value", []))
            address = page.get("@odata.nextLink")
            delta_url = page.get("@odata.deltaLink", delta_url)
        if not delta_url:
            raise RuntimeError("SharePoint delta response did not include a delta link.")
        return items, delta_url

    def get_file_item(self, drive_id: str, relative_path: str) -> dict | None:
        url = (
            f"{GRAPH_ROOT}/drives/{quote(drive_id, safe='')}"
            f"/root:/{quote(relative_path, safe='/')}"
        )
        try:
            response = self.http.get(
                url, headers=self._headers("https://graph.microsoft.com/.default")
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"SharePoint page metadata request failed (HTTP {exc.response.status_code})."
            ) from None
        except httpx.RequestError:
            raise RuntimeError("SharePoint page metadata request failed due to a network error.") from None
        return response.json()

    def download_file(self, drive_id: str, item_id: str) -> bytes:
        url = (
            f"{GRAPH_ROOT}/drives/{quote(drive_id, safe='')}/items/"
            f"{quote(item_id, safe='')}/content"
        )
        try:
            with self.http.stream(
                "GET",
                url,
                headers=self._headers("https://graph.microsoft.com/.default"),
            ) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > self.max_upload_bytes:
                        raise ValueError(
                            f"Source file exceeds MAX_UPLOAD_BYTES ({self.max_upload_bytes})."
                        )
                return bytes(content)
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"SharePoint file download failed (HTTP {exc.response.status_code}).") from None
        except httpx.RequestError:
            raise RuntimeError("SharePoint file download failed due to a network error.") from None

    def get_page_html(self, site_url: str, server_relative_path: str) -> tuple[str, str]:
        parsed = urlparse(site_url)
        encoded_site_path = quote(parsed.path.rstrip("/"), safe="/")
        decoded_path = server_relative_path.replace("'", "''")
        endpoint = (
            f"{parsed.scheme}://{parsed.netloc}{encoded_site_path}"
            f"/_api/web/GetFileByServerRelativePath(decodedurl='{quote(decoded_path, safe='/')}')"
            "/ListItemAllFields?$select=Title,CanvasContent1,Modified"
        )
        token = self.credential.get_token(f"https://{parsed.netloc}/.default").token
        response = self.http.get(
            endpoint,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json;odata=nometadata",
            },
        )
        response.raise_for_status()
        payload = response.json()
        fields = payload.get("d", payload)
        title = str(fields.get("Title") or server_relative_path.rsplit("/", 1)[-1])
        canvas = str(fields.get("CanvasContent1") or "")
        modified = str(fields.get("Modified") or "")
        return f"<html><head><title>{title}</title></head><body>{canvas}</body></html>", modified

    def navigation_nodes(self, site_url: str, location: str) -> list[dict]:
        parsed = urlparse(site_url)
        site_path = quote(parsed.path.rstrip("/"), safe="/")
        root_name = quote(location, safe="")
        base_url = f"{parsed.scheme}://{parsed.netloc}{site_path}/_api/web/navigation/{root_name}"
        scope = f"https://{parsed.netloc}/.default"
        headers = self._headers(scope)
        headers["Accept"] = "application/json;odata=nometadata"
        root = self.http.get(base_url, headers=headers)
        root.raise_for_status()
        return [
            self._read_navigation_node(parsed, headers, node)
            for node in self._collection(root.json())
        ]

    def get_live_navigation_locations(self, site_url: str) -> tuple[str, ...]:
        parsed = urlparse(site_url)
        site_path = quote(parsed.path.rstrip("/"), safe="/")
        endpoint = f"{parsed.scheme}://{parsed.netloc}{site_path}/_api/web"
        payload = self._get_json(
            endpoint + "?$select=WebTemplate,QuickLaunchEnabled",
            f"https://{parsed.netloc}/.default",
        )
        web = payload.get("d", payload)
        if web.get("WebTemplate") == "SITEPAGEPUBLISHING":
            return ("TopNavigationBar",)
        if web.get("QuickLaunchEnabled"):
            return ("QuickLaunch",)
        return ()

    def _read_navigation_node(self, site: ParseResult, headers: dict[str, str], node: dict) -> dict:
        parsed = site
        node_id = node.get("Id", node.get("id"))
        children: list[dict] = []
        if node_id is not None:
            child_url = (
                f"{parsed.scheme}://{parsed.netloc}{quote(parsed.path.rstrip('/'), safe='/')}"
                f"/_api/web/navigation/GetNodeById({int(node_id)})/Children"
            )
            response = self.http.get(child_url, headers=headers)
            response.raise_for_status()
            children = [
                self._read_navigation_node(parsed, headers, child)
                for child in self._collection(response.json())
            ]
        return {
            "title": str(node.get("Title", node.get("title", ""))),
            "url": str(node.get("Url", node.get("url", ""))),
            "children": children,
        }

    def _collect(self, url: str) -> list[dict]:
        results: list[dict] = []
        for item in self._iterate(url):
            results.append(item)
        return results

    def _iterate(self, url: str) -> Iterator[dict]:
        while url:
            page = self._graph_get(url)
            yield from page.get("value", [])
            url = page.get("@odata.nextLink", "")

    @staticmethod
    def _collection(payload: dict) -> list[dict]:
        if isinstance(payload.get("d"), dict):
            payload = payload["d"]
        if isinstance(payload.get("results"), list):
            return payload["results"]
        if isinstance(payload.get("value"), list):
            return payload["value"]
        return []
