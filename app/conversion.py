from __future__ import annotations

import json
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlparse

import httpx


class UnsupportedFileTypeError(RuntimeError):
    pass


class MarkItDownClient:
    def __init__(self, base_url: str, max_upload_bytes: int = 104_857_600,
                 http_client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_upload_bytes = max_upload_bytes
        self.http = http_client or httpx.Client(timeout=300, follow_redirects=False)
        self._base_host = urlparse(self.base_url).netloc.casefold()

    def close(self) -> None:
        self.http.close()

    def convert(self, filename: str, content: bytes) -> str:
        if len(content) > self.max_upload_bytes:
            raise ValueError(f"Source file exceeds MAX_UPLOAD_BYTES ({self.max_upload_bytes}).")
        try:
            response = self.http.post(
                f"{self.base_url}/convert",
                files={"file": (PurePosixPath(filename).name, content)},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                try:
                    rejection = exc.response.json()
                except json.JSONDecodeError:
                    raise RuntimeError("MarkItDown conversion failed (HTTP 400).") from None
                if isinstance(rejection, dict) and rejection.get("error") == "File type not supported":
                    raise UnsupportedFileTypeError(
                        "MarkItDown does not support this file type."
                    ) from None
            raise RuntimeError(f"MarkItDown conversion failed (HTTP {exc.response.status_code}).") from None
        except httpx.RequestError:
            raise RuntimeError("MarkItDown request failed due to a network error.") from None
        try:
            result = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError("MarkItDown response was not valid JSON.") from exc
        download_url = result.get("download_url")
        if not isinstance(download_url, str) or not download_url.strip():
            raise RuntimeError("MarkItDown response did not include download_url.")
        resolved_url = urljoin(f"{self.base_url}/", download_url)
        parsed_url = urlparse(resolved_url)
        if parsed_url.scheme not in {"http", "https"} or parsed_url.netloc.casefold() != self._base_host:
            raise RuntimeError("MarkItDown download_url must resolve to the configured endpoint host.")
        try:
            with self.http.stream("GET", resolved_url) as download:
                download.raise_for_status()
                content = bytearray()
                for chunk in download.iter_bytes():
                    content.extend(chunk)
                    if len(content) > self.max_upload_bytes:
                        raise ValueError("Converted Markdown exceeds MAX_UPLOAD_BYTES.")
                return bytes(content).decode("utf-8-sig")
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"MarkItDown download failed (HTTP {exc.response.status_code}).") from None
        except httpx.RequestError:
            raise RuntimeError("MarkItDown download failed due to a network error.") from None


def add_front_matter(
    markdown: str,
    *,
    title: str,
    source_url: str,
    source_file: str,
    content_type: str,
    etag: str,
    modified_utc: str,
) -> str:
    fields = {
        "title": title,
        "source_url": source_url,
        "source_file": source_file,
        "content_type": content_type,
        "etag": etag,
        "modified_utc": modified_utc,
    }
    front_matter = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
    return f"---\n{front_matter}\n---\n\n{markdown.rstrip()}\n"
