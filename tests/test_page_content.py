import json
from html import escape
from unittest.mock import Mock

import httpx
import pytest
from azure.core.credentials import AccessToken
from bs4 import BeautifulSoup

from app.page_content import render_page
from app.sharepoint import SharePointClient


def credential() -> Mock:
    return Mock(get_token=Mock(return_value=AccessToken("test-placeholder", 2_000_000_000)))


def test_page_export_extracts_webparts_in_canvas_order(caplog: pytest.LogCaptureFixture) -> None:
    data = {
        "id": "part-type",
        "serverProcessedContent": {
            "htmlStrings": {"content": "<p>Rich <strong>content</strong></p>"},
            "searchablePlainTexts": [{"key": "title", "value": "Benefits & policies"}],
            "links": {"items[0].url": "/sites/hr/benefits"},
            "imageSources": [{"key": "image", "value": "/images/banner.png"}],
        },
        "properties": {
            "items": [{"title": "Benefits & policies", "description": "Open enrollment",
                       "url": "details.aspx", "imageUrl": "/images/banner.png"}],
            "title": "<script>not HTML</script>",
            "url": "javascript:alert(1)",
        },
    }
    canvas = (
        '<div data-sp-canvascontrol=""><div data-sp-rte="">Before</div></div>'
        f'<div data-sp-webpartdata="{escape(json.dumps(data), quote=True)}">'
        '<div data-sp-htmlproperties=""><div>Benefits &amp; policies</div></div></div>'
        '<div data-sp-rte="">After</div>'
    )
    client = SharePointClient(
        credential(),
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "Title": "HR & <Benefits>", "CanvasContent1": canvas, "Modified": "modified",
            })
        )),
    )
    try:
        html, modified = client.get_page_html(
            "https://contoso.sharepoint.com/sites/hr", "/sites/hr/SitePages/Home.aspx"
        )
    finally:
        client.close()

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    assert modified == "modified"
    assert soup.title.string == "HR & <Benefits>"
    assert text.index("Before") < text.index("Rich") < text.index("After")
    assert soup.strong.string == "content"
    assert text.count("Benefits & policies") == 1
    assert "Open enrollment" in text
    assert "<script>not HTML</script>" in text
    assert soup.find("script") is None
    assert [link["href"] for link in soup.find_all("a")] == [
        "https://contoso.sharepoint.com/sites/hr/benefits",
        "https://contoso.sharepoint.com/sites/hr/SitePages/details.aspx",
    ]
    assert [image["src"] for image in soup.find_all("img")] == [
        "https://contoso.sharepoint.com/images/banner.png",
    ]
    assert "data-sp-webpartdata" not in html
    assert "review required" in caplog.text


def test_text_only_page_preserves_markup_and_resolves_links() -> None:
    html = render_page(
        "Page", '<p>Welcome <em>everyone</em></p><a href="../Documents/Guide.pdf">Guide</a>',
        "https://contoso.sharepoint.com/sites/hr/SitePages/Home.aspx",
    )
    soup = BeautifulSoup(html, "html.parser")
    assert soup.em.string == "everyone"
    assert soup.a["href"] == "https://contoso.sharepoint.com/sites/hr/Documents/Guide.pdf"
    assert soup.a.string == "Guide"


def test_dynamic_part_has_visible_notice_and_warning(caplog: pytest.LogCaptureFixture) -> None:
    html = render_page(
        "Page", '<div data-sp-webpartdata="{}"></div>', "https://contoso.sharepoint.com/Home.aspx",
    )
    assert "Web-part content unavailable in saved page data" in html
    assert "Dynamic or custom web parts" in caplog.text


@pytest.mark.parametrize("processed", [
    "invalid", [], {"links": "invalid"}, {"imageSources": ["invalid"]},
    {"searchablePlainTexts": {"title": 123}},
])
def test_invalid_processed_content_is_not_silently_discarded(processed: object) -> None:
    data = escape(json.dumps({"serverProcessedContent": processed}), quote=True)
    with pytest.raises(ValueError, match="web-part"):
        render_page("Page", f'<div data-sp-webpartdata="{data}"></div>', "https://example.com/")


@pytest.mark.parametrize("data", ["{bad json", "[]"])
def test_invalid_webpart_data_fails_export(data: str) -> None:
    client = SharePointClient(
        credential(),
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "CanvasContent1": f'<div data-sp-webpartdata="{escape(data)}"></div>',
            })
        )),
    )
    try:
        with pytest.raises(ValueError, match="web-part"):
            client.get_page_html(
                "https://contoso.sharepoint.com/sites/hr", "/sites/hr/SitePages/Home.aspx"
            )
    finally:
        client.close()
