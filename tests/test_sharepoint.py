import httpx
import pytest
from azure.core.credentials import AccessToken

from app.sharepoint import SharePointClient


class FakeCredential:
    def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        return AccessToken("test-placeholder", 2_000_000_000)


def test_selected_page_metadata_uses_encoded_drive_path() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "page", "eTag": "etag"})

    client = SharePointClient(
        FakeCredential(), http_client=httpx.Client(transport=httpx.MockTransport(handle))
    )
    try:
        assert client.get_file_item("drive", "folder/Home Page.aspx") == {"id": "page", "eTag": "etag"}
        assert requests[0].url.raw_path == b"/v1.0/drives/drive/root:/folder/Home%20Page.aspx"
    finally:
        client.close()


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_only_page_404_is_treated_as_missing(status: int) -> None:
    client = SharePointClient(
        FakeCredential(),
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(status))
        ),
    )
    try:
        if status == 404:
            assert client.get_file_item("drive", "Home.aspx") is None
        else:
            with pytest.raises(RuntimeError, match=f"HTTP {status}"):
                client.get_file_item("drive", "Home.aspx")
    finally:
        client.close()
