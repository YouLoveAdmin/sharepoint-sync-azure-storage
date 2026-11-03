import httpx
import pytest

from app.conversion import MarkItDownClient, UnsupportedFileTypeError, add_front_matter


def test_converter_uses_multipart_and_downloads_markdown() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/convert":
            assert request.method == "POST"
            assert b'name="file"; filename="report.pdf"' in request.content
            return httpx.Response(200, json={"download_url": "/downloads/report.md"})
        if request.url.path == "/downloads/report.md":
            return httpx.Response(200, content=b"# Report\n")
        raise AssertionError(f"Unexpected URL: {request.url}")

    client = MarkItDownClient(
        "http://converter:5001",
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    try:
        assert client.convert("folder/report.pdf", b"pdf") == "# Report\n"
    finally:
        client.close()


def test_converter_rejects_download_url_on_another_host() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"download_url": "http://127.0.0.1/private"})

    client = MarkItDownClient(
        "http://converter:5001",
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    try:
        with pytest.raises(RuntimeError, match="configured endpoint host"):
            client.convert("report.pdf", b"pdf")
    finally:
        client.close()


def test_converter_identifies_explicit_unsupported_file_response() -> None:
    client = MarkItDownClient(
        "http://converter:5001",
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": "File type not supported"})
        )),
    )
    try:
        with pytest.raises(UnsupportedFileTypeError, match="does not support this file type"):
            client.convert("video.mp4", b"video")
    finally:
        client.close()


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"error": "Invalid file"}),
    httpx.Response(400, content=b"Bad request"),
    httpx.Response(500, json={"error": "File type not supported"}),
])
def test_other_converter_rejections_remain_failures(response: httpx.Response) -> None:
    client = MarkItDownClient(
        "http://converter:5001",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: response)),
    )
    try:
        with pytest.raises(RuntimeError, match="conversion failed"):
            client.convert("report.pdf", b"pdf")
    finally:
        client.close()


def test_front_matter_quotes_metadata_as_yaml_strings() -> None:
    markdown = add_front_matter(
        "# Report",
        title='Report: "Q1"',
        source_url="https://contoso.sharepoint.com/report",
        source_file="Shared Documents/report.pdf",
        content_type="document",
        etag='"tag-1"',
        modified_utc="2025-01-01T00:00:00Z",
    )

    assert 'title: "Report: \\"Q1\\""' in markdown
    assert 'etag: "\\"tag-1\\""' in markdown
    assert markdown.endswith("\n\n# Report\n")
