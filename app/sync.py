from __future__ import annotations

import logging
import re
import threading
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlparse

from app.config import Settings, page_url_parts
from app.conversion import MarkItDownClient, UnsupportedFileTypeError, add_front_matter
from app.sharepoint import SharePointClient
from app.storage import RunStore

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(
        self,
        settings: Settings,
        store: RunStore,
        sharepoint: SharePointClient,
        converter: MarkItDownClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.sharepoint = sharepoint
        self.converter = converter
        self._run_lock = threading.Lock()

    def acquire_run(self) -> bool:
        return self._run_lock.acquire(blocking=False)

    def release_run(self) -> None:
        self._run_lock.release()

    def execute(self) -> dict[str, int]:
        summary = {"converted": 0, "skipped": 0, "deleted": 0, "navigation": 0, "unsupported": 0}
        for site_url in self.settings.site_urls:
            site = self.sharepoint.resolve_site(site_url)
            for library_name in self.settings.library_names:
                drive = self.sharepoint.find_drive(site["id"], library_name)
                self._sync_drive(site_url, drive, summary)
        for page_url in dict.fromkeys(self.settings.page_urls):
            self._sync_selected_page(page_url, summary)
        navigation_sites = tuple(dict.fromkeys((
            *(self.settings.site_urls if self.settings.include_navigation else ()),
            *self.settings.navigation_site_urls,
        )))
        if navigation_sites:
            self._sync_navigation(navigation_sites, summary)
        return summary

    def _sync_selected_page(self, page_url: str, summary: dict[str, int]) -> None:
        site_url, relative_path = page_url_parts(page_url)
        site = self.sharepoint.resolve_site(site_url)
        drive = self.sharepoint.find_drive(site["id"], "Site Pages")
        item = self.sharepoint.get_file_item(drive["id"], relative_path)
        if item is None:
            logger.warning("A configured SharePoint page returned 404; removing its tracked output.")
            for stored_item in self.store.items_under_path(drive["id"], relative_path):
                self._remove_deleted_item(
                    drive["id"], {"id": stored_item["item_id"], "deleted": {}}, summary
                )
            return
        item = dict(item)
        parent_path = PurePosixPath(relative_path).parent.as_posix()
        item["parentReference"] = {"path": f"root:/{'' if parent_path == '.' else parent_path}"}
        self._sync_item(site_url, drive, item, summary)

    def _sync_drive(self, site_url: str, drive: dict, summary: dict[str, int]) -> None:
        drive_id = drive["id"]
        items, new_cursor = self.sharepoint.delta_items(drive_id, self.store.get_cursor(drive_id))
        for item in items:
            if item.get("deleted") is not None:
                self._remove_deleted_item(drive_id, item, summary)
                continue
            if item.get("folder") is not None:
                etag = str(item.get("eTag") or item.get("cTag") or "")
                self.store.upsert_item(
                    drive_id,
                    str(item["id"]),
                    self._source_path(item),
                    "",
                    etag,
                )
                continue
            if item.get("file") is None:
                continue
            self._sync_item(site_url, drive, item, summary)
        self.store.set_cursor(drive_id, new_cursor)

    def _sync_item(self, site_url: str, drive: dict, item: dict, summary: dict[str, int]) -> None:
        item_id = str(item["id"])
        etag = str(item.get("eTag") or item.get("cTag") or item.get("lastModifiedDateTime") or "")
        source_path = self._source_path(item)
        previous = self.store.get_item(drive["id"], item_id)
        relative_output = self._output_path(site_url, drive["name"], source_path)
        output_file = (self.store.output_dir / relative_output).resolve()
        if etag and previous and previous["etag"] == etag and output_file.is_file():
            summary["skipped"] += 1
            return

        filename = str(item.get("name") or PurePosixPath(source_path).name)
        is_page = filename.casefold().endswith(".aspx")
        conversion_name = f"{PurePosixPath(filename).stem}.html" if is_page else filename
        try:
            self.converter.check_file_type(conversion_name)
            if is_page:
                parsed_site = urlparse(site_url)
                library_path = "SitePages" if drive["name"].casefold() == "site pages" else drive["name"]
                server_relative_path = "/".join(
                    part.strip("/") for part in (parsed_site.path, library_path, source_path) if part
                )
                html, modified = self.sharepoint.get_page_html(
                    site_url, f"/{server_relative_path}"
                )
                source_content = html.encode("utf-8")
                content_type = "page"
            else:
                source_content = self.sharepoint.download_file(drive["id"], item_id)
                modified = str(item.get("lastModifiedDateTime") or "")
                content_type = "document"
            markdown = self.converter.convert(conversion_name, source_content)
        except UnsupportedFileTypeError:
            logger.warning(
                "Skipping a file explicitly rejected as unsupported by MarkItDown (extension %s). "
                "Any previously exported output is unchanged.",
                PurePosixPath(filename).suffix,
            )
            summary["unsupported"] += 1
            return
        title = self._title(markdown, filename)
        content = add_front_matter(
            markdown,
            title=title,
            source_url=str(item.get("webUrl") or ""),
            source_file=source_path,
            content_type=content_type,
            etag=etag,
            modified_utc=modified,
        )
        self.store.write_markdown(relative_output, content)
        if previous and previous["output_path"] != relative_output:
            self.store.delete_output(previous["output_path"])
        self.store.upsert_item(drive["id"], item_id, source_path, relative_output, etag)
        summary["converted"] += 1

    def _remove_deleted_item(self, drive_id: str, item: dict, summary: dict[str, int]) -> None:
        item_id = str(item.get("id") or "")
        stored_item = self.store.get_item(drive_id, item_id)
        source_path = (
            stored_item["source_path"]
            if stored_item
            else self._source_path(item) if item.get("name") else ""
        )
        if source_path:
            removed_items = self.store.remove_items_under_path(drive_id, source_path)
        else:
            stored = self.store.remove_item(drive_id, item_id)
            removed_items = [stored] if stored else []
        for stored_item in removed_items:
            if stored_item["output_path"]:
                self.store.delete_output(stored_item["output_path"])
                summary["deleted"] += 1

    def _sync_navigation(self, site_urls: tuple[str, ...], summary: dict[str, int]) -> None:
        lines = ["# Extended Knowledge"]
        navigation_count = 0
        for site_url in site_urls:
            locations = self.settings.navigation_locations
            if any(location.casefold() == "auto" for location in locations):
                locations = self.sharepoint.get_live_navigation_locations(site_url)
            lines.extend(("", f"## {site_url.rstrip('/')}"))
            for location in locations:
                try:
                    nodes = self.sharepoint.navigation_nodes(site_url, location)
                except Exception:
                    logger.exception("Could not read SharePoint navigation location %s.", location)
                    raise
                if nodes:
                    lines.extend(("", f"### {location}"))
                    self._append_navigation(lines, nodes, 0, site_url)
                    navigation_count += 1
        self.store.write_markdown("Extended_Knowledge.md", "\n".join(lines) + "\n")
        summary["navigation"] += navigation_count
        for site_url in site_urls:
            navigation_dir = self.store.output_dir / self._site_folder(site_url)
            if navigation_dir.is_dir():
                for old_output in navigation_dir.glob("navigation-*.md"):
                    self.store.delete_output(old_output.relative_to(self.store.output_dir).as_posix())

    def _append_navigation(
        self, lines: list[str], nodes: list[dict], depth: int, site_url: str
    ) -> None:
        for node in nodes:
            title = str(node.get("title") or "").strip()
            url = str(node.get("url") or "").strip()
            title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            title = " ".join(title.splitlines())
            if url and "linkless.header" not in url.casefold():
                absolute_url = urljoin(site_url.rstrip("/") + "/", url)
                label = f"[{title}](<{absolute_url.replace('>', '%3E').replace('<', '%3C')}>)"
            else:
                label = title
            lines.append(f"{'  ' * depth}- {label}")
            self._append_navigation(lines, node.get("children", []), depth + 1, site_url)

    @staticmethod
    def _title(markdown: str, fallback: str) -> str:
        for line in markdown.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return PurePosixPath(fallback).stem

    @staticmethod
    def _source_path(item: dict) -> str:
        name = str(item.get("name") or "")
        parent_path = str(item.get("parentReference", {}).get("path") or "")
        marker = "root:"
        if marker in parent_path:
            parent_path = parent_path.split(marker, 1)[1]
        parent_path = parent_path.strip("/")
        return str(PurePosixPath(parent_path, name)) if parent_path else name

    @classmethod
    def _output_path(cls, site_url: str, library_name: str, source_path: str) -> str:
        source_parts = PurePosixPath(source_path).parts
        safe_parts = [cls._safe_name(part) for part in source_parts if part not in {"", ".", ".."}]
        if not safe_parts:
            raise ValueError("SharePoint item did not provide a usable file path.")
        safe_parts[-1] = f"{safe_parts[-1]}.md"
        return "/".join((cls._site_folder(site_url), cls._safe_name(library_name), *safe_parts))

    @classmethod
    def _site_folder(cls, site_url: str) -> str:
        parsed = urlparse(site_url)
        site_path = parsed.path.strip("/").replace("/", "_")
        name = parsed.netloc if not site_path else f"{parsed.netloc}_{site_path}"
        return cls._safe_name(name)

    @staticmethod
    def _safe_name(value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
        return safe[:120] or "item"
