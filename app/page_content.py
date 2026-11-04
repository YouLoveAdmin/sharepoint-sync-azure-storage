from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from html import escape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TEXT_KEYS = {
    "title", "text", "description", "label", "caption", "captiontext", "alttext",
    "overlaytext", "buttontext", "linktext", "heading", "subheading",
}
LINK_KEYS = {"url", "href", "link", "linkurl", "targeturl"}
IMAGE_KEYS = {"imageurl", "imagesource", "src"}


def _values(value: object) -> Iterator[object]:
    if isinstance(value, dict):
        yield from value.values()
    elif isinstance(value, list):
        for entry in value:
            if not isinstance(entry, dict) or "value" not in entry:
                raise ValueError("Invalid SharePoint web-part processed-content entry.")
            yield entry["value"]
    elif value is not None:
        raise ValueError("Invalid SharePoint web-part processed-content collection.")


def _property_strings(value: object, key: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _property_strings(child, child_key.lower())
    elif isinstance(value, list):
        for child in value:
            yield from _property_strings(child, key)
    elif isinstance(value, str) and value.strip():
        yield key, value


def _render_part(data: dict, page_url: str) -> str:
    output: list[str] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: object) -> None:
        if value is None:
            return
        if not isinstance(value, str):
            raise ValueError("Invalid SharePoint web-part content; expected a string.")
        if not value.strip():
            return
        value = value.strip()
        if kind in {"link", "image"}:
            value = urljoin(page_url, value)
            allowed = {"https", "http"} if kind == "image" else {"https", "http", "mailto", "tel"}
            if urlparse(value).scheme.lower() not in allowed:
                logger.warning("Ignoring an unsupported URL scheme in SharePoint web-part content.")
                return
        identity = kind, value
        if identity in seen:
            return
        seen.add(identity)
        if kind == "html":
            output.append(value)
        elif kind == "text":
            output.append(f"<p>{escape(value)}</p>")
        elif kind == "image":
            output.append(f'<img src="{escape(value, quote=True)}" alt="">')
        else:
            output.append(f'<p><a href="{escape(value, quote=True)}">{escape(value)}</a></p>')

    processed = data.get("serverProcessedContent")
    if processed is None:
        processed = {}
    if not isinstance(processed, dict):
        raise ValueError("Invalid SharePoint web-part serverProcessedContent.")
    for field, kind in (
        ("htmlStrings", "html"), ("searchablePlainTexts", "text"),
        ("links", "link"), ("imageSources", "image"),
    ):
        for value in _values(processed.get(field)):
            add(kind, value)
    for key, value in _property_strings(data.get("properties")):
        if key in TEXT_KEYS:
            add("text", value)
        elif key in LINK_KEYS:
            add("link", value)
        elif key in IMAGE_KEYS:
            add("image", value)
    return "\n".join(output)


def render_page(title: str, canvas: str, page_url: str) -> str:
    """Expand persisted web-part data without executing client-side components."""
    soup = BeautifulSoup(canvas, "html.parser")
    parts = soup.select("[data-sp-webpartdata]")
    for element in parts:
        raw_data = element.get("data-sp-webpartdata")
        if not isinstance(raw_data, str):
            raise ValueError("Invalid SharePoint web-part data attribute.")
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON in SharePoint web-part data.") from None
        if not isinstance(data, dict):
            raise ValueError("Invalid SharePoint web-part data; expected an object.")
        rendered = _render_part(data, page_url)
        del element["data-sp-webpartdata"]
        # These are serialized copies of serverProcessedContent, not rendered UI.
        for properties in element.select("[data-sp-htmlproperties]"):
            properties.decompose()
        fragment = BeautifulSoup(rendered, "html.parser")
        for child in list(fragment.contents):
            element.append(child)
        if not rendered and not element.get_text(strip=True) and not element.find(["img", "a"]):
            notice = soup.new_tag("p")
            notice.string = "Web-part content unavailable in saved page data; review the source page."
            element.append(notice)
    if parts:
        logger.warning(
            "Exported %d SharePoint web part(s) from saved page data; review required. "
            "Dynamic or custom web parts may require additional data retrieval.",
            len(parts),
        )

    # Relative links inside rich text need the page URL, not the converter's URL.
    for element in soup.find_all(["a", "img"]):
        attribute = "href" if element.name == "a" else "src"
        value = element.get(attribute)
        if isinstance(value, str):
            absolute = urljoin(page_url, value.strip())
            allowed = {"http", "https", "mailto", "tel"} if attribute == "href" else {"http", "https"}
            if urlparse(absolute).scheme.lower() in allowed:
                element[attribute] = absolute
            else:
                del element[attribute]
                logger.warning("Ignoring an unsupported URL scheme in SharePoint page content.")
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title></head><body>{soup}</body></html>"
    )
