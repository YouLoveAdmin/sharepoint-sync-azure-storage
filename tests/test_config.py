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
