from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


def page_url_parts(page_url: str) -> tuple[str, str]:
    parsed = urlparse(page_url)
    path = unquote(parsed.path)
    marker = "/sitepages/"
    index = path.casefold().find(marker)
    if index < 0 or not path.casefold().endswith(".aspx"):
        raise ValueError("SHAREPOINT_SITE_PAGES entries must be individual /SitePages/*.aspx URLs.")
    relative_path = path[index + len(marker):]
    if any(part in {"", ".", ".."} for part in relative_path.split("/")):
        raise ValueError("SHAREPOINT_SITE_PAGES contains an invalid page path.")
    site_url = f"{parsed.scheme}://{parsed.netloc}{path[:index]}"
    return site_url, relative_path


def _split_values(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.replace(",", ";").split(";") if part.strip())


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value.")


@dataclass(frozen=True)
class Settings:
    site_urls: tuple[str, ...]
    library_names: tuple[str, ...]
    navigation_locations: tuple[str, ...]
    include_navigation: bool
    converter_url: str
    output_dir: Path
    managed_identity_client_id: str | None
    max_upload_bytes: int = 104_857_600
    navigation_site_urls: tuple[str, ...] = ()
    page_urls: tuple[str, ...] = ()
    supported_source_file_types: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> Settings:
        source_file_types = tuple(dict.fromkeys(
            extension.casefold()
            for extension in _split_values(os.getenv("SUPPORTED_SOURCE_FILE_TYPES", ""))
        ))
        if any(not re.fullmatch(r"\.[a-z0-9]+", extension) for extension in source_file_types):
            raise ValueError(
                "SUPPORTED_SOURCE_FILE_TYPES must contain dot-prefixed extensions "
                "separated by semicolons or commas, for example .PDF;.DOCX."
            )
        sites = _split_values(os.getenv("SHAREPOINT_SITE_URLS", ""))
        navigation_sites = _split_values(os.getenv("SHAREPOINT_SITE_URLS_NAVIGATION", ""))
        pages = _split_values(os.getenv("SHAREPOINT_SITE_PAGES", ""))
        libraries = _split_values(os.getenv("SHAREPOINT_LIBRARY_NAMES", ""))
        navigation_locations = _split_values(
            os.getenv("SHAREPOINT_NAVIGATION_LOCATIONS", "Auto")
        )
        converter_url = os.getenv("MARKITDOWN_API_URL", "").strip().rstrip("/")
        output_dir_value = os.getenv("OUTPUT_DIR", "").strip()
        client_id = os.getenv("AZURE_CLIENT_ID", "").strip() or None

        if not (sites or navigation_sites or pages):
            raise ValueError(
                "Configure at least one source in SHAREPOINT_SITE_URLS, "
                "SHAREPOINT_SITE_URLS_NAVIGATION, or SHAREPOINT_SITE_PAGES."
            )
        if sites and not libraries:
            raise ValueError("SHAREPOINT_LIBRARY_NAMES must contain at least one library name.")
        if not converter_url:
            raise ValueError("MARKITDOWN_API_URL must be set.")
        parsed_converter = urlparse(converter_url)
        if parsed_converter.scheme not in {"http", "https"} or not parsed_converter.netloc:
            raise ValueError("MARKITDOWN_API_URL must be an absolute HTTP or HTTPS URL.")
        if parsed_converter.username or parsed_converter.password or parsed_converter.query or parsed_converter.fragment:
            raise ValueError("MARKITDOWN_API_URL must not contain credentials, a query, or a fragment.")
        if not output_dir_value:
            raise ValueError("OUTPUT_DIR must be set to the mounted output directory.")
        for name, urls in (
            ("SHAREPOINT_SITE_URLS", sites),
            ("SHAREPOINT_SITE_URLS_NAVIGATION", navigation_sites),
            ("SHAREPOINT_SITE_PAGES", pages),
        ):
            for source_url in urls:
                parsed_source = urlparse(source_url)
                if parsed_source.scheme != "https" or not parsed_source.netloc:
                    raise ValueError(f"Every {name} entry must be an absolute HTTPS URL.")
                if (
                    parsed_source.username
                    or parsed_source.password
                    or parsed_source.query
                    or parsed_source.fragment
                ):
                    raise ValueError(f"{name} entries must not contain credentials or query data.")
        for page_url in pages:
            page_url_parts(page_url)

        try:
            max_upload_bytes = int(os.getenv("MAX_UPLOAD_BYTES", str(cls.max_upload_bytes)))
        except ValueError as exc:
            raise ValueError("MAX_UPLOAD_BYTES must be an integer.") from exc
        if max_upload_bytes <= 0:
            raise ValueError("MAX_UPLOAD_BYTES must be greater than zero.")

        include_navigation = _read_bool("SHAREPOINT_INCLUDE_NAVIGATION", True)
        return cls(
            site_urls=sites,
            library_names=libraries,
            navigation_locations=navigation_locations,
            include_navigation=include_navigation,
            converter_url=converter_url,
            output_dir=Path(output_dir_value).expanduser(),
            managed_identity_client_id=client_id,
            max_upload_bytes=max_upload_bytes,
            navigation_site_urls=navigation_sites,
            page_urls=pages,
            supported_source_file_types=source_file_types,
        )
