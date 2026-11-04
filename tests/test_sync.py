from dataclasses import replace
from html import escape
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import httpx
from bs4 import BeautifulSoup

from app.config import Settings
from app.conversion import MarkItDownClient, UnsupportedFileTypeError
from app.storage import RunStore
from app.sharepoint import SharePointClient
from app.sync import SyncService


class FakeSharePoint:
    def __init__(self) -> None:
        self.changes: list[dict] = []
        self.page_paths: list[str] = []
        self.resolved_sites: list[str] = []
        self.metadata_paths: list[str] = []
        self.navigation_calls: list[str] = []
        self.navigation_by_site: dict[str, list[dict]] = {}
        self.missing_page = False
        self.fail_navigation_site: str | None = None
        self.fail_page = False
        self.delta_calls = 0

    def resolve_site(self, site_url: str) -> dict:
        self.resolved_sites.append(site_url)
        return {"id": "site-id"}

    def find_drive(self, site_id: str, library_name: str) -> dict:
        return {"id": "drive-id", "name": library_name}

    def delta_items(self, drive_id: str, cursor: str | None) -> tuple[list[dict], str]:
        self.delta_calls += 1
        return self.changes, "delta-link"

    def download_file(self, drive_id: str, item_id: str) -> bytes:
        return b"source data"

    def get_page_html(self, site_url: str, server_relative_path: str) -> tuple[str, str]:
        self.page_paths.append(server_relative_path)
        return "<html><body>page</body></html>", "2025-01-01T00:00:00Z"

    def navigation_nodes(self, site_url: str, location: str) -> list[dict]:
        self.navigation_calls.append(site_url)
        if site_url == self.fail_navigation_site:
            raise RuntimeError("Navigation request failed.")
        return self.navigation_by_site.get(site_url, [])

    def get_file_item(self, drive_id: str, relative_path: str) -> dict | None:
        self.metadata_paths.append(relative_path)
        if self.fail_page:
            raise RuntimeError("SharePoint page metadata request failed (HTTP 403).")
        if self.missing_page:
            return None
        return {
            "id": "selected-page",
            "name": Path(relative_path).name,
            "eTag": "selected-etag",
            "file": {"mimeType": "text/html"},
            "webUrl": "https://contoso.sharepoint.com/sites/pages/SitePages/Home.aspx",
        }


class FakeConverter:
    def __init__(self) -> None:
        self.calls = 0

    def check_file_type(self, filename: str) -> None:
        return None

    def convert(self, filename: str, content: bytes) -> str:
        self.calls += 1
        return f"# Converted {filename}"


def make_service(
    tmp_path: Path,
    sharepoint: FakeSharePoint,
    converter: FakeConverter,
    libraries: tuple[str, ...] = ("Documents",),
) -> SyncService:
    settings = Settings(
        site_urls=("https://contoso.sharepoint.com/sites/hr",),
        library_names=libraries,
        navigation_locations=(),
        include_navigation=False,
        converter_url="http://converter:5001",
        output_dir=tmp_path,
        managed_identity_client_id=None,
    )
    return SyncService(settings, RunStore(tmp_path), sharepoint, converter)


def test_delta_sync_converts_once_and_removes_deleted_output(tmp_path: Path) -> None:
    sharepoint = FakeSharePoint()
    converter = FakeConverter()
    service = make_service(tmp_path, sharepoint, converter)
    sharepoint.changes = [
        {
            "id": "file-1",
            "name": "report.pdf",
            "eTag": "etag-1",
            "webUrl": "https://contoso.sharepoint.com/report.pdf",
            "parentReference": {"path": "/drives/drive-id/root:/"},
            "file": {"mimeType": "application/pdf"},
        }
    ]

    first = service.execute()
    second = service.execute()
    assert first["converted"] == 1
    assert second["skipped"] == 1
    assert converter.calls == 1
    output = next(tmp_path.glob("**/report.pdf.md"))
    assert 'source_file: "report.pdf"' in output.read_text(encoding="utf-8")

    sharepoint.changes = [{"id": "file-1", "deleted": {}}]
    removed = service.execute()
    assert removed["deleted"] == 1
    assert not output.exists()


def test_allowlist_filters_before_download_and_preserves_deletions(tmp_path: Path) -> None:
    class GuardedSharePoint(FakeSharePoint):
        def download_file(self, drive_id: str, item_id: str) -> bytes:
            assert item_id == "allowed", "Excluded files must never be downloaded."
            return b"pdf"

    sharepoint = GuardedSharePoint()
    converter = FakeConverter()
    service = make_service(tmp_path, sharepoint, converter)
    service.settings = replace(service.settings, supported_source_file_types=(".pdf",))
    service.store.write_markdown("old.md", "old")
    service.store.upsert_item("drive-id", "removed", "old.mp4", "old.md", "old")
    sharepoint.changes = [
        {"id": "removed", "deleted": {}},
        {"id": "folder", "name": "folder.mp4", "folder": {}},
        {"id": "video", "name": "large.mp4", "size": 500_000_000, "file": {}},
        {"id": "no-extension", "name": "README", "file": {}},
        {"id": "allowed", "name": "report.PDF", "file": {}, "eTag": "v1"},
    ]

    result = service.execute()

    assert result["filtered"] == 2
    assert result["converted"] == 1
    assert result["unsupported"] == 0
    assert result["deleted"] == 1
    assert converter.calls == 1
    assert service.store.get_cursor("drive-id") == "delta-link"
    assert service.store.get_item("drive-id", "folder") is not None
    assert service.store.get_item("drive-id", "video") is None
    assert not (tmp_path / "old.md").exists()


@pytest.mark.parametrize(("types", "converted", "filtered"), [
    ((".aspx",), 1, 0), ((".html",), 0, 1),
])
def test_allowlist_checks_page_source_extension(
    tmp_path: Path, types: tuple[str, ...], converted: int, filtered: int,
) -> None:
    sharepoint = FakeSharePoint()
    service = make_service(tmp_path, sharepoint, FakeConverter())
    service.settings = replace(
        service.settings, site_urls=(), supported_source_file_types=types,
        page_urls=("https://contoso.sharepoint.com/sites/hr/SitePages/Home.aspx",),
    )
    result = service.execute()
    assert result["converted"] == converted
    assert result["filtered"] == filtered
    assert len(sharepoint.page_paths) == converted


def test_site_page_uses_sharepoint_server_relative_path(tmp_path: Path) -> None:
    sharepoint = FakeSharePoint()
    converter = FakeConverter()
    service = make_service(tmp_path, sharepoint, converter, ("Site Pages",))
    sharepoint.changes = [
        {
            "id": "page-1",
            "name": "home.aspx",
            "eTag": "page-etag",
            "webUrl": "https://contoso.sharepoint.com/sites/hr/SitePages/home.aspx",
            "parentReference": {"path": "/drives/drive-id/root:/"},
            "file": {"mimeType": "text/html"},
        }
    ]

    result = service.execute()

    assert result["converted"] == 1
    assert sharepoint.page_paths == ["/sites/hr/SitePages/home.aspx"]
    assert converter.calls == 1


@pytest.mark.parametrize("selected", [False, True])
def test_webpart_content_reaches_converter_and_persistent_output(
    tmp_path: Path, selected: bool,
) -> None:
    data = {"properties": {"description": "Saved web-part knowledge"}}
    canvas = f'<div data-sp-webpartdata="{escape(json.dumps(data), quote=True)}"></div>'
    client = SharePointClient(
        Mock(),
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"Title": "Home", "CanvasContent1": canvas})
        )),
    )

    class PageSharePoint(FakeSharePoint):
        def get_page_html(self, site_url: str, server_relative_path: str) -> tuple[str, str]:
            return client.get_page_html(site_url, server_relative_path)

    class PageConverter(FakeConverter):
        def convert(self, filename: str, content: bytes) -> str:
            assert filename == "Home.html"
            html = content.decode("utf-8")
            assert "data-sp-webpartdata" not in html
            return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)

    sharepoint = PageSharePoint()
    sharepoint.changes = [{
        "id": "page", "name": "Home.aspx", "eTag": "v1", "file": {},
        "parentReference": {"path": "/drives/drive-id/root:/"},
    }]
    service = make_service(tmp_path, sharepoint, PageConverter(), ("Site Pages",))
    if selected:
        service.settings = replace(
            service.settings, site_urls=(),
            page_urls=("https://contoso.sharepoint.com/sites/hr/SitePages/Home.aspx",),
        )
    try:
        assert service.execute()["converted"] == 1
        assert service.execute()["skipped"] == 1
    finally:
        client.close()

    output = next(tmp_path.rglob("Home.aspx.md"))
    assert "Saved web-part knowledge" in output.read_text(encoding="utf-8")


def test_invalid_webpart_preserves_previous_output_and_delta_cursor(tmp_path: Path) -> None:
    client = SharePointClient(
        Mock(),
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "CanvasContent1": '<div data-sp-webpartdata="{broken"></div>',
            })
        )),
    )

    class BrokenPageSharePoint(FakeSharePoint):
        def get_page_html(self, site_url: str, server_relative_path: str) -> tuple[str, str]:
            return client.get_page_html(site_url, server_relative_path)

    sharepoint = BrokenPageSharePoint()
    converter = FakeConverter()
    service = make_service(tmp_path, sharepoint, converter, ("Site Pages",))
    output = service._output_path(
        "https://contoso.sharepoint.com/sites/hr", "Site Pages", "Home.aspx",
    )
    service.store.write_markdown(output, "previous complete export")
    service.store.upsert_item("drive-id", "page", "Home.aspx", output, "v1")
    service.store.set_cursor("drive-id", "previous-cursor")
    sharepoint.changes = [{
        "id": "page", "name": "Home.aspx", "eTag": "v2", "file": {},
        "parentReference": {"path": "/drives/drive-id/root:/"},
    }]
    try:
        with pytest.raises(ValueError, match="web-part"):
            service.execute()
    finally:
        client.close()

    assert (tmp_path / output).read_text(encoding="utf-8") == "previous complete export"
    assert service.store.get_item("drive-id", "page")["etag"] == "v1"
    assert service.store.get_cursor("drive-id") == "previous-cursor"
    assert converter.calls == 0


@pytest.mark.parametrize("unsupported", [True, False])
def test_only_explicitly_unsupported_files_are_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, unsupported: bool,
) -> None:
    class RejectingConverter(FakeConverter):
        def convert(self, filename: str, content: bytes) -> str:
            if filename.endswith(".mp4"):
                if unsupported:
                    raise UnsupportedFileTypeError("MarkItDown does not support this file type.")
                raise RuntimeError("MarkItDown conversion failed (HTTP 400).")
            return super().convert(filename, content)

    sharepoint = FakeSharePoint()
    service = make_service(tmp_path, sharepoint, RejectingConverter())
    sharepoint.changes = [
        {"id": "video", "name": "video.mp4", "eTag": "v1", "file": {}},
        {"id": "report", "name": "report.pdf", "eTag": "r1", "file": {}},
    ]
    service.store.write_markdown("previous.md", "previous export")
    service.store.upsert_item("drive-id", "video", "video.mp4", "previous.md", "old")
    if not unsupported:
        with pytest.raises(RuntimeError, match="HTTP 400"):
            service.execute()
        assert service.store.get_cursor("drive-id") is None
        return

    result = service.execute()

    assert result["unsupported"] == 1
    assert result["converted"] == 1
    assert result["skipped"] == 0
    assert service.store.get_cursor("drive-id") == "delta-link"
    assert service.store.get_item("drive-id", "video")["etag"] == "old"
    assert (tmp_path / "previous.md").read_text() == "previous export"
    assert "explicitly rejected as unsupported" in caplog.text
    assert next(tmp_path.rglob("report.pdf.md")).is_file()


def test_known_unsupported_extensions_are_skipped_before_downloading(tmp_path: Path) -> None:
    downloaded: list[str] = []

    class RecordingSharePoint(FakeSharePoint):
        def download_file(self, drive_id: str, item_id: str) -> bytes:
            assert item_id != "large-video", "Known unsupported videos must not be downloaded."
            downloaded.append(item_id)
            return b"source data"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/download":
            return httpx.Response(200, content=b"# Report")
        if b'filename="small.mp4"' in request.content:
            return httpx.Response(400, json={"error": "File type not supported"})
        return httpx.Response(200, json={"download_url": "/download"})

    sharepoint = RecordingSharePoint()
    sharepoint.changes = [
        {"id": "small-video", "name": "small.mp4", "eTag": "v1", "file": {}},
        {"id": "large-video", "name": "large.MP4", "eTag": "v2", "file": {}, "size": 500_000_000},
        {"id": "report", "name": "report.pdf", "eTag": "r1", "file": {}},
    ]
    service = make_service(tmp_path, sharepoint, FakeConverter())
    service.converter = MarkItDownClient(
        "http://converter:5001",
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    try:
        result = service.execute()
    finally:
        service.converter.close()

    assert result["unsupported"] == 2
    assert result["converted"] == 1
    assert downloaded == ["small-video", "report"]
    assert service.store.get_cursor("drive-id") == "delta-link"


def test_deleted_folder_removes_descendants_without_matching_wildcards(tmp_path: Path) -> None:
    sharepoint = FakeSharePoint()
    service = make_service(tmp_path, sharepoint, FakeConverter())
    store = service.store
    store.write_markdown("site/folder/report.md", "child")
    store.write_markdown("site/sibling/report.md", "sibling")
    store.upsert_item("drive-id", "folder-id", "docs/a%_b", "", "folder-etag")
    store.upsert_item(
        "drive-id", "file-id", "docs/a%_b/report.pdf", "site/folder/report.md", "file-etag"
    )
    store.upsert_item(
        "drive-id", "sibling-id", "docs/axb/report.pdf", "site/sibling/report.md", "sibling-etag"
    )
    summary = {"deleted": 0}

    service._remove_deleted_item("drive-id", {"id": "folder-id", "deleted": {}}, summary)

    assert summary["deleted"] == 1
    assert not (tmp_path / "site" / "folder" / "report.md").exists()
    assert (tmp_path / "site" / "sibling" / "report.md").is_file()


def test_navigation_only_sites_are_consolidated_without_reading_libraries(tmp_path: Path) -> None:
    sharepoint = FakeSharePoint()
    converter = FakeConverter()
    service = make_service(tmp_path, sharepoint, converter)
    sites = (
        "https://contoso.sharepoint.com/sites/menu",
        "https://contoso.sharepoint.com",
    )
    service.settings = replace(
        service.settings,
        site_urls=(),
        library_names=(),
        navigation_site_urls=(*sites, sites[0]),
        navigation_locations=("TopNavigationBar",),
    )
    sharepoint.navigation_by_site = {
        sites[0]: [
            {"title": "HR", "url": "SitePages/Home.aspx", "children": [
                {"title": "Policy", "url": "/sites/hr/policy", "children": []},
            ]},
        ],
        sites[1]: [{"title": "News", "url": "/SitePages/News.aspx", "children": []}],
    }
    old_path = f"{service._site_folder(sites[0])}/navigation-TopNavigationBar.md"
    service.store.write_markdown(old_path, "old navigation")

    result = service.execute()
    output = (tmp_path / "Extended_Knowledge.md").read_text(encoding="utf-8")

    assert result["navigation"] == 2
    assert sharepoint.navigation_calls == list(sites)
    assert sharepoint.resolved_sites == []
    assert sharepoint.delta_calls == 0
    assert converter.calls == 0
    assert "# Extended Knowledge" in output
    assert "## " + sites[0] in output
    assert "## " + sites[1] in output
    assert "[HR](<https://contoso.sharepoint.com/sites/menu/SitePages/Home.aspx>)" in output
    assert "  - [Policy](<https://contoso.sharepoint.com/sites/hr/policy>)" in output
    assert "[News]" in output
    assert not (tmp_path / old_path).exists()

    sharepoint.navigation_by_site = {}
    service.execute()
    output = (tmp_path / "Extended_Knowledge.md").read_text(encoding="utf-8")
    assert "[HR]" not in output
    assert "[News]" not in output


def test_navigation_read_failure_preserves_previous_exports(tmp_path: Path) -> None:
    sharepoint = FakeSharePoint()
    service = make_service(tmp_path, sharepoint, FakeConverter())
    service.settings = replace(
        service.settings,
        site_urls=(),
        navigation_site_urls=("https://contoso.sharepoint.com",),
        navigation_locations=("TopNavigationBar",),
    )
    sharepoint.fail_navigation_site = service.settings.navigation_site_urls[0]
    service.store.write_markdown("Extended_Knowledge.md", "last successful export")
    old_path = f"{service._site_folder(sharepoint.fail_navigation_site)}/navigation-TopNavigationBar.md"
    service.store.write_markdown(old_path, "legacy export")

    with pytest.raises(RuntimeError, match="Navigation request failed"):
        service.execute()

    assert (tmp_path / "Extended_Knowledge.md").read_text() == "last successful export"
    assert (tmp_path / old_path).read_text() == "legacy export"


def test_selected_pages_only_sync_metadata_skip_unchanged_and_delete_on_404(tmp_path: Path) -> None:
    sharepoint = FakeSharePoint()
    converter = FakeConverter()
    service = make_service(tmp_path, sharepoint, converter)
    page_url = "https://contoso.sharepoint.com/sites/pages/SitePages/folder/Home%20Page.aspx"
    service.settings = replace(
        service.settings, site_urls=(), library_names=(), page_urls=(page_url,)
    )

    assert service.execute()["converted"] == 1
    assert service.execute()["skipped"] == 1
    assert converter.calls == 1
    assert sharepoint.metadata_paths == ["folder/Home Page.aspx"] * 2
    assert sharepoint.page_paths == ["/sites/pages/SitePages/folder/Home Page.aspx"]
    assert sharepoint.navigation_calls == []
    assert sharepoint.delta_calls == 0
    assert service.store.get_cursor("drive-id") is None
    output = next(tmp_path.glob("**/Home_Page.aspx.md"))

    sharepoint.fail_page = True
    with pytest.raises(RuntimeError, match="HTTP 403"):
        service.execute()
    assert output.is_file()

    sharepoint.fail_page = False
    sharepoint.missing_page = True
    assert service.execute()["deleted"] == 1
    assert not output.exists()
    assert service.execute()["deleted"] == 0


def test_main_navigation_and_additional_sites_are_additive_and_deduplicated(tmp_path: Path) -> None:
    sharepoint = FakeSharePoint()
    service = make_service(tmp_path, sharepoint, FakeConverter())
    main_site = service.settings.site_urls[0]
    extra_site = "https://contoso.sharepoint.com/sites/menu"
    service.settings = replace(
        service.settings,
        include_navigation=True,
        navigation_site_urls=(main_site, extra_site),
        navigation_locations=("TopNavigationBar",),
    )

    service.execute()

    assert sharepoint.delta_calls == 1
    assert sharepoint.navigation_calls == [main_site, extra_site]
