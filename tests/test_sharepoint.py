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


@pytest.mark.parametrize("library_name", ["Shared Documents", "shared documents", "Shared%20Documents"])
def test_library_lookup_accepts_url_folder_name(library_name: str) -> None:
    drive = {
        "id": "documents",
        "name": "Documents",
        "webUrl": "https://contoso.sharepoint.com/sites/hr/Shared%20Documents",
    }
    client = SharePointClient(
        FakeCredential(),
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"value": [drive]})
        )),
    )
    try:
        assert client.find_drive("site", library_name) == drive
    finally:
        client.close()


def test_display_name_takes_precedence_over_url_folder_alias() -> None:
    drives = [
        {"id": "alias", "name": "Other", "webUrl": "https://contoso.sharepoint.com/Documents"},
        {"id": "exact", "name": "Documents", "webUrl": "https://contoso.sharepoint.com/Shared%20Documents"},
    ]
    client = SharePointClient(
        FakeCredential(), http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"value": drives})
        )),
    )
    try:
        assert client.find_drive("site", "Documents")["id"] == "exact"
        with pytest.raises(LookupError, match="was not found"):
            client.find_drive("site", "Nonexistent")
    finally:
        client.close()


def test_ambiguous_folder_alias_fails_instead_of_selecting_wrong_library() -> None:
    drives = [
        {"id": "first", "name": "First", "webUrl": "https://contoso.sharepoint.com/one/Documents"},
        {"id": "second", "name": "Second", "webUrl": "https://contoso.sharepoint.com/two/Documents"},
    ]
    client = SharePointClient(
        FakeCredential(), http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"value": drives})
        )),
    )
    try:
        with pytest.raises(LookupError, match="ambiguous"):
            client.find_drive("site", "Documents")
    finally:
        client.close()


def test_drive_discovery_requests_system_libraries() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert "system" in request.url.params["$select"].split(",")
        return httpx.Response(200, json={"value": [{"id": "pages", "name": "Site Pages"}]})

    client = SharePointClient(
        FakeCredential(), http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    try:
        assert client.find_drive("site", "Site Pages")["id"] == "pages"
    finally:
        client.close()
