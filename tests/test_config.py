from pathlib import Path

import pytest

from app.config import Settings


def test_settings_parse_semicolon_and_comma_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHAREPOINT_SITE_URLS", "https://contoso.sharepoint.com/sites/hr")
    monkeypatch.setenv("SHAREPOINT_LIBRARY_NAMES", "Documents;Site Pages")
    monkeypatch.setenv("MARKITDOWN_API_URL", "http://converter:5001/")
    monkeypatch.setenv("OUTPUT_DIR", "/mnt/output")
    monkeypatch.setenv("SHAREPOINT_INCLUDE_NAVIGATION", "false")

    settings = Settings.from_env()

    assert settings.site_urls == ("https://contoso.sharepoint.com/sites/hr",)
    assert settings.library_names == ("Documents", "Site Pages")
    assert settings.converter_url == "http://converter:5001"
    assert settings.output_dir == Path("/mnt/output")
    assert not settings.include_navigation


def test_settings_reject_converter_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHAREPOINT_SITE_URLS", "https://contoso.sharepoint.com")
    monkeypatch.setenv("SHAREPOINT_LIBRARY_NAMES", "Documents")
    monkeypatch.setenv("MARKITDOWN_API_URL", "https://user:password@converter:5001")
    monkeypatch.setenv("OUTPUT_DIR", "/mnt/output")

    with pytest.raises(ValueError, match="must not contain credentials"):
        Settings.from_env()


def test_settings_require_site_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHAREPOINT_SITE_URLS", raising=False)
    monkeypatch.setenv("SHAREPOINT_LIBRARY_NAMES", "Documents")
    monkeypatch.setenv("MARKITDOWN_API_URL", "http://converter:5001")
    monkeypatch.setenv("OUTPUT_DIR", "/mnt/output")

    with pytest.raises(ValueError, match="SHAREPOINT_SITE_URLS"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("SHAREPOINT_SITE_URLS_NAVIGATION", "https://contoso.sharepoint.com/sites/menu"),
        ("SHAREPOINT_SITE_PAGES", "https://contoso.sharepoint.com/SitePages/Home.aspx"),
    ],
)
def test_independent_scopes_do_not_require_main_sites_or_libraries(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv("SHAREPOINT_SITE_URLS", "")
    monkeypatch.setenv("SHAREPOINT_LIBRARY_NAMES", "")
    monkeypatch.setenv("SHAREPOINT_SITE_URLS_NAVIGATION", "")
    monkeypatch.setenv("SHAREPOINT_SITE_PAGES", "")
    monkeypatch.setenv(variable, value)
    monkeypatch.setenv("MARKITDOWN_API_URL", "http://converter:5001")
    monkeypatch.setenv("OUTPUT_DIR", "/mnt/output")

    settings = Settings.from_env()

    assert settings.site_urls == ()
    assert settings.library_names == ()
    assert value in (*settings.navigation_site_urls, *settings.page_urls)


def test_page_scope_rejects_site_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHAREPOINT_SITE_URLS", "https://contoso.sharepoint.com")
    monkeypatch.setenv("SHAREPOINT_LIBRARY_NAMES", "Documents")
    monkeypatch.setenv("SHAREPOINT_SITE_PAGES", "https://contoso.sharepoint.com/sites/hr")
    monkeypatch.setenv("MARKITDOWN_API_URL", "http://converter:5001")
    monkeypatch.setenv("OUTPUT_DIR", "/mnt/output")

    with pytest.raises(ValueError, match="SHAREPOINT_SITE_PAGES"):
        Settings.from_env()


@pytest.mark.parametrize(("value", "expected"), [
    (" .PDF; .DocX,.ASPX;.PDF ", (".pdf", ".docx", ".aspx")),
    ("", ()),
])
def test_source_file_types_are_normalized(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: tuple[str, ...],
) -> None:
    monkeypatch.setenv("SHAREPOINT_SITE_URLS", "https://contoso.sharepoint.com")
    monkeypatch.setenv("SHAREPOINT_LIBRARY_NAMES", "Documents")
    monkeypatch.setenv("MARKITDOWN_API_URL", "http://converter:5001")
    monkeypatch.setenv("OUTPUT_DIR", "/mnt/output")
    monkeypatch.setenv("SUPPORTED_SOURCE_FILE_TYPES", value)

    assert Settings.from_env().supported_source_file_types == expected


@pytest.mark.parametrize("value", ["PDF", "*.pdf", ".", ".pdf/path", ".pdf .docx"])
def test_invalid_source_file_types_fail(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv("SUPPORTED_SOURCE_FILE_TYPES", value)
    with pytest.raises(ValueError, match="SUPPORTED_SOURCE_FILE_TYPES"):
        Settings.from_env()


@pytest.mark.parametrize(("enabled", "interval"), [("true", "43200"), ("false", "60")])
def test_auto_sync_settings(
    monkeypatch: pytest.MonkeyPatch, enabled: str, interval: str,
) -> None:
    monkeypatch.setenv("SHAREPOINT_SITE_URLS", "https://contoso.sharepoint.com")
    monkeypatch.setenv("SHAREPOINT_LIBRARY_NAMES", "Documents")
    monkeypatch.setenv("MARKITDOWN_API_URL", "http://converter:5001")
    monkeypatch.setenv("OUTPUT_DIR", "/mnt/output")
    monkeypatch.setenv("AUTO_SYNC", enabled)
    monkeypatch.setenv("SYNC_INTERVAL", interval)
    settings = Settings.from_env()
    assert settings.auto_sync == (enabled == "true")
    assert settings.sync_interval == int(interval)


def test_auto_sync_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTO_SYNC", raising=False)
    monkeypatch.delenv("SYNC_INTERVAL", raising=False)
    monkeypatch.setenv("SHAREPOINT_SITE_URLS", "https://contoso.sharepoint.com")
    monkeypatch.setenv("SHAREPOINT_LIBRARY_NAMES", "Documents")
    monkeypatch.setenv("MARKITDOWN_API_URL", "http://converter:5001")
    monkeypatch.setenv("OUTPUT_DIR", "/mnt/output")
    settings = Settings.from_env()
    assert not settings.auto_sync
    assert settings.sync_interval == 43200


@pytest.mark.parametrize(("name", "value"), [
    ("AUTO_SYNC", "invalid"), ("SYNC_INTERVAL", "0"), ("SYNC_INTERVAL", "-1"),
    ("SYNC_INTERVAL", "12h"), ("SYNC_INTERVAL", "1.5"), ("SYNC_INTERVAL", "2147483648"),
])
def test_invalid_auto_sync_settings_fail(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()
