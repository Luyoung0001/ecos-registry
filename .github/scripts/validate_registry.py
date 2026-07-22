#!/usr/bin/env python3

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from http.client import HTTPException
import json
import ntpath
from pathlib import Path
import posixpath
import re
import socket
import sys
from typing import Any
from typing import Protocol
from typing import TypeGuard
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


ALLOWED_TOP_LEVEL_KEYS = frozenset(("schema_version", "tools", "pdks"))
TOOL_REQUIRED_FIELDS = (
    "name",
    "display_name",
    "description",
    "category",
    "homepage",
    "versions",
)
PDK_REQUIRED_FIELDS = (
    "id",
    "display_name",
    "description",
    "category",
    "homepage",
    "versions",
)
VERSION_REQUIRED_FIELDS = ("version", "platforms")
ALLOWED_VERSION_FIELDS = frozenset(VERSION_REQUIRED_FIELDS + ("requires",))
PLATFORM_REQUIRED_FIELDS = ("url", "sha256", "size")
ALLOWED_PLATFORM_FIELDS = frozenset(
    PLATFORM_REQUIRED_FIELDS
    + (
        "sha256",
        "size",
        "metadata_url",
        "sha256_url",
        "strip_prefix",
        "supplemental_assets",
        "post_install",
    )
)
SUPPLEMENTAL_ASSET_REQUIRED_FIELDS = ("path", "url", "sha256", "size")
ALLOWED_SUPPLEMENTAL_ASSET_FIELDS = frozenset(SUPPLEMENTAL_ASSET_REQUIRED_FIELDS)
ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".txz", ".zip")
SIDECAR_URL_SUFFIXES = (".json", ".sha256", ".txt")
IDENTIFIER_RE = re.compile(r"^[a-z0-9_-]+$")
RESOURCE_DEPENDENCY_RE = re.compile(r"^(?:tool|pdk):[a-z0-9_-]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NUMERIC_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+$")
URL_TIMEOUT_SECONDS = 5.0


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


NO_REDIRECT_OPENER = build_opener(NoRedirectHandler)


class UrlResponse(Protocol):
    status: int

    def __enter__(self) -> "UrlResponse": ...

    def __exit__(self, *args: object) -> None: ...

    def read(self, size: int | None = None) -> bytes: ...


@dataclass(frozen=True)
class AssetUrl:
    path: str
    url: str


UrlOpener = Callable[[Request, float], UrlResponse]
UrlChecker = Callable[[str], str | None]


def validate_registry(
    path: Path,
    *,
    check_urls: bool = False,
    url_checker: UrlChecker | None = None,
) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"$: invalid JSON: {exc.msg}"]
    return validate_registry_data(data, check_urls=check_urls, url_checker=url_checker)


def validate_registry_data(
    data: object,
    *,
    check_urls: bool = False,
    url_checker: UrlChecker | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["$: must be a JSON object"]

    asset_urls: list[AssetUrl] = []
    _validate_top_level(data, errors)
    tools = _array_or_none(data.get("tools"), "tools", errors)
    pdks = _array_or_none(data.get("pdks"), "pdks", errors)
    resource_ids = _collect_resource_ids(tools, pdks)

    if tools is not None:
        _validate_entries(
            tools,
            "tools",
            "tool",
            "name",
            TOOL_REQUIRED_FIELDS,
            errors,
            asset_urls,
            resource_ids,
        )
    if pdks is not None:
        _validate_entries(
            pdks,
            "pdks",
            "PDK",
            "id",
            PDK_REQUIRED_FIELDS,
            errors,
            asset_urls,
            resource_ids,
        )

    if check_urls:
        checker = url_checker or check_url_reachable
        for asset_url in asset_urls:
            error = checker(asset_url.url)
            if error is not None:
                errors.append(
                    f"{asset_url.path}: URL check failed for {asset_url.url}: {error}"
                )

    return errors


def _validate_top_level(data: dict[str, Any], errors: list[str]) -> None:
    if data.get("schema_version") != 2:
        errors.append("schema_version: must equal 2")

    for key in data:
        if key not in ALLOWED_TOP_LEVEL_KEYS:
            errors.append(f"{key}: unknown top-level key")


def _array_or_none(value: object, path: str, errors: list[str]) -> list[Any] | None:
    if not isinstance(value, list):
        errors.append(f"{path}: must be an array")
        return None
    return value


def _collect_resource_ids(
    tools: list[Any] | None,
    pdks: list[Any] | None,
) -> frozenset[str]:
    resource_ids: set[str] = set()
    for prefix, entries, id_field in (
        ("tool", tools or [], "name"),
        ("pdk", pdks or [], "id"),
    ):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identifier = entry.get(id_field)
            if isinstance(identifier, str) and IDENTIFIER_RE.fullmatch(identifier):
                resource_ids.add(f"{prefix}:{identifier}")
    return frozenset(resource_ids)


def _validate_entries(
    entries: list[Any],
    collection_path: str,
    label: str,
    id_field: str,
    required_fields: tuple[str, ...],
    errors: list[str],
    asset_urls: list[AssetUrl],
    resource_ids: frozenset[str],
) -> None:
    seen_ids: dict[str, str] = {}
    for index, entry in enumerate(entries):
        entry_path = f"{collection_path}[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{entry_path}: must be an object")
            continue

        _require_fields(entry, required_fields, entry_path, errors)
        _validate_string_fields(
            entry,
            ("display_name", "description", "category", "homepage"),
            entry_path,
            errors,
        )
        _validate_identifier(entry.get(id_field), f"{entry_path}.{id_field}", errors)
        identifier = entry.get(id_field)
        if isinstance(identifier, str) and IDENTIFIER_RE.fullmatch(identifier):
            if identifier in seen_ids:
                errors.append(
                    f"{entry_path}.{id_field}: duplicate {label} {id_field} "
                    f"{identifier!r}; first seen at {seen_ids[identifier]}"
                )
            else:
                seen_ids[identifier] = f"{entry_path}.{id_field}"

        versions = entry.get("versions")
        if not isinstance(versions, list) or not versions:
            errors.append(f"{entry_path}.versions: must be a non-empty array")
            continue
        _validate_versions(
            versions,
            f"{entry_path}.versions",
            entry_type="tool" if collection_path == "tools" else "pdk",
            errors=errors,
            asset_urls=asset_urls,
            resource_ids=resource_ids,
        )


def _require_fields(
    entry: dict[str, Any],
    fields: tuple[str, ...],
    path: str,
    errors: list[str],
) -> None:
    for field in fields:
        if field not in entry:
            errors.append(f"{path}.{field}: missing required field")


def _validate_string_fields(
    entry: dict[str, Any],
    fields: tuple[str, ...],
    path: str,
    errors: list[str],
) -> None:
    for field in fields:
        if field in entry and not _is_non_empty_string(entry[field]):
            errors.append(f"{path}.{field}: must be a non-empty string")


def _validate_identifier(value: object, path: str, errors: list[str]) -> None:
    if not _is_non_empty_string(value):
        errors.append(f"{path}: must be a non-empty stable identifier")
        return
    if not IDENTIFIER_RE.fullmatch(value):
        errors.append(f"{path}: must match ^[a-z0-9_-]+$")


def _validate_versions(
    versions: list[Any],
    path: str,
    entry_type: str,
    errors: list[str],
    asset_urls: list[AssetUrl],
    resource_ids: frozenset[str],
) -> None:
    seen_versions: dict[str, str] = {}
    version_values: list[str] = []

    for index, version in enumerate(versions):
        version_path = f"{path}[{index}]"
        if not isinstance(version, dict):
            errors.append(f"{version_path}: must be an object")
            continue

        _require_fields(version, VERSION_REQUIRED_FIELDS, version_path, errors)
        for field in version:
            if field not in ALLOWED_VERSION_FIELDS:
                errors.append(f"{version_path}.{field}: unknown version field")
        version_value = version.get("version")
        if not _is_non_empty_string(version_value):
            errors.append(f"{version_path}.version: must be a non-empty string")
        else:
            version_values.append(version_value)
            if version_value in seen_versions:
                errors.append(
                    f"{version_path}.version: duplicate version {version_value!r}; "
                    f"first seen at {seen_versions[version_value]}"
                )
            else:
                seen_versions[version_value] = f"{version_path}.version"

        if "requires" in version:
            _validate_requires(
                version["requires"],
                f"{version_path}.requires",
                errors,
                resource_ids,
            )

        platforms = version.get("platforms")
        if not isinstance(platforms, dict) or not platforms:
            errors.append(f"{version_path}.platforms: must be a non-empty object")
            continue
        _validate_platforms(
            platforms,
            f"{version_path}.platforms",
            entry_type,
            errors,
            asset_urls,
        )

    _validate_version_order(version_values, path, errors)


def _validate_requires(
    value: object,
    path: str,
    errors: list[str],
    resource_ids: frozenset[str],
) -> None:
    if not isinstance(value, list):
        errors.append(f"{path}: must be an array")
        return

    seen_dependencies: dict[str, str] = {}
    for index, dependency in enumerate(value):
        dependency_path = f"{path}[{index}]"
        if not isinstance(dependency, str):
            errors.append(f"{dependency_path}: must be a string")
            continue
        if not RESOURCE_DEPENDENCY_RE.fullmatch(dependency):
            errors.append(
                f"{dependency_path}: must match tool:<id> or pdk:<id> using "
                "a lowercase stable identifier"
            )
            continue
        if dependency in seen_dependencies:
            errors.append(
                f"{dependency_path}: duplicate dependency {dependency!r}; "
                f"first seen at {seen_dependencies[dependency]}"
            )
        else:
            seen_dependencies[dependency] = dependency_path
        if dependency not in resource_ids:
            errors.append(f"{dependency_path}: unknown resource dependency {dependency!r}")


def _validate_version_order(
    versions: list[str],
    path: str,
    errors: list[str],
) -> None:
    if len(versions) < 2:
        return

    parsed_dates = [_parse_date_version(version) for version in versions]
    parsed_numbers = [_parse_numeric_version(version) for version in versions]

    if all(parsed is not None for parsed in parsed_dates):
        _validate_descending(parsed_dates, path, errors)
        return

    if all(parsed is not None for parsed in parsed_numbers):
        _validate_descending(parsed_numbers, path, errors)
        return

    errors.append(
        f"{path}: mixed or unsupported version format; use YYYY-MM-DD or dotted "
        "numeric versions and keep newest first"
    )


def _parse_date_version(version: str) -> date | None:
    if not DATE_VERSION_RE.fullmatch(version):
        return None
    try:
        return date.fromisoformat(version)
    except ValueError:
        return None


def _parse_numeric_version(version: str) -> tuple[int, ...] | None:
    if not NUMERIC_VERSION_RE.fullmatch(version):
        return None
    return tuple(int(part) for part in version.split("."))


def _validate_descending(
    parsed_versions: list[date | tuple[int, ...] | None],
    path: str,
    errors: list[str],
) -> None:
    comparable_versions = [version for version in parsed_versions if version is not None]
    if comparable_versions != sorted(comparable_versions, reverse=True):
        errors.append(f"{path}: newest version must appear first")


def _validate_platforms(
    platforms: dict[str, Any],
    path: str,
    entry_type: str,
    errors: list[str],
    asset_urls: list[AssetUrl],
) -> None:
    for platform_key, platform in platforms.items():
        if not _is_non_empty_string(platform_key):
            errors.append(f"{path}: platform key must be non-empty")
            continue

        platform_path = f"{path}.{platform_key}"
        if entry_type == "tool" and platform_key == "all-platform":
            errors.append(f"{platform_path}: all-platform is not allowed for tools")

        if not isinstance(platform, dict):
            errors.append(f"{platform_path}: must be an object")
            continue

        _require_fields(platform, PLATFORM_REQUIRED_FIELDS, platform_path, errors)
        for field in platform:
            if field not in ALLOWED_PLATFORM_FIELDS:
                errors.append(f"{platform_path}.{field}: unknown platform field")

        url_path = f"{platform_path}.url"
        if _validate_platform_url(platform.get("url"), url_path, errors):
            asset_urls.append(AssetUrl(path=url_path, url=platform["url"]))
        for field in ("metadata_url", "sha256_url"):
            if field in platform:
                sidecar_path = f"{platform_path}.{field}"
                if _validate_sidecar_url(platform.get(field), sidecar_path, errors):
                    asset_urls.append(AssetUrl(path=sidecar_path, url=platform[field]))
        if "sha256" in platform:
            _validate_sha256(platform.get("sha256"), f"{platform_path}.sha256", errors)
        if "size" in platform:
            _validate_size(platform.get("size"), f"{platform_path}.size", errors)
        if "strip_prefix" in platform and not _is_non_empty_string(
            platform["strip_prefix"]
        ):
            errors.append(f"{platform_path}.strip_prefix: must be a non-empty string")
        if "supplemental_assets" in platform:
            _validate_supplemental_assets(
                platform["supplemental_assets"],
                f"{platform_path}.supplemental_assets",
                errors,
                asset_urls,
            )
        if "post_install" in platform:
            _validate_post_install(
                platform["post_install"],
                f"{platform_path}.post_install",
                errors,
            )


def _validate_platform_url(value: object, path: str, errors: list[str]) -> bool:
    return _validate_http_url(
        value,
        path,
        errors,
        suffixes=ARCHIVE_SUFFIXES,
        suffix_error="unsupported archive suffix",
    )


def _validate_sidecar_url(value: object, path: str, errors: list[str]) -> bool:
    return _validate_http_url(
        value,
        path,
        errors,
        suffixes=SIDECAR_URL_SUFFIXES,
        suffix_error="unsupported sidecar URL suffix",
    )


def _validate_http_url(
    value: object,
    path: str,
    errors: list[str],
    *,
    suffixes: tuple[str, ...],
    suffix_error: str,
) -> bool:
    if not _is_non_empty_string(value):
        errors.append(f"{path}: must be a non-empty string")
        return False
    if _contains_url_control_character(value):
        errors.append(
            f"{path}: malformed URL: must not contain whitespace or control characters"
        )
        return False

    try:
        parsed = urlparse(value)
        _ = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        errors.append(f"{path}: malformed URL: {exc}")
        return False

    valid = True
    if parsed.scheme not in ("http", "https"):
        errors.append(f"{path}: must use http or https")
        valid = False
    if not parsed.netloc:
        errors.append(f"{path}: must include a host")
        valid = False
    elif hostname is None:
        errors.append(f"{path}: malformed URL: must include a valid host")
        valid = False
    if not parsed.path.lower().endswith(suffixes):
        errors.append(f"{path}: {suffix_error}")
        valid = False
    return valid


def _validate_sha256(value: object, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        errors.append(f"{path}: must be a lowercase 64-character hex string")


def _validate_size(value: object, path: str, errors: list[str]) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        errors.append(f"{path}: must be a positive integer")


def _validate_supplemental_assets(
    value: object,
    path: str,
    errors: list[str],
    asset_urls: list[AssetUrl],
) -> None:
    if not isinstance(value, list):
        errors.append(f"{path}: must be an array")
        return

    seen_paths: dict[str, str] = {}
    for index, asset in enumerate(value):
        asset_path = f"{path}[{index}]"
        if not isinstance(asset, dict):
            errors.append(f"{asset_path}: must be an object")
            continue

        _require_fields(asset, SUPPLEMENTAL_ASSET_REQUIRED_FIELDS, asset_path, errors)
        for field in asset:
            if field not in ALLOWED_SUPPLEMENTAL_ASSET_FIELDS:
                errors.append(f"{asset_path}.{field}: unknown supplemental asset field")

        relative_path = asset.get("path")
        path_error = _supplemental_asset_path_error(relative_path)
        if path_error is not None:
            errors.append(f"{asset_path}.path: {path_error}")
        elif isinstance(relative_path, str):
            if relative_path in seen_paths:
                errors.append(
                    f"{asset_path}.path: duplicate path {relative_path!r}; "
                    f"first seen at {seen_paths[relative_path]}"
                )
            else:
                seen_paths[relative_path] = f"{asset_path}.path"

        url_path = f"{asset_path}.url"
        if _validate_platform_url(asset.get("url"), url_path, errors):
            asset_urls.append(AssetUrl(path=url_path, url=asset["url"]))
        _validate_sha256(asset.get("sha256"), f"{asset_path}.sha256", errors)
        _validate_size(asset.get("size"), f"{asset_path}.size", errors)


def _supplemental_asset_path_error(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return "must be a normalized relative path"
    if (
        value != value.strip()
        or "\\" in value
        or _contains_url_control_character(value)
        or value.startswith("/")
        or Path(value).is_absolute()
        or ntpath.isabs(value)
        or ntpath.splitdrive(value)[0]
    ):
        return "must be a normalized relative path"
    normalized = posixpath.normpath(value)
    if (
        normalized != value
        or normalized in (".", "..")
        or normalized.startswith("../")
        or any(
            not part or part in (".", "..") or ":" in part for part in value.split("/")
        )
    ):
        return "must be a normalized relative path"
    return None


def _validate_post_install(value: object, path: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{path}: must be an array")
        return

    for index, command in enumerate(value):
        command_path = f"{path}[{index}]"
        if not isinstance(command, dict):
            errors.append(f"{command_path}: must be an object")
            continue
        if "command" not in command:
            errors.append(f"{command_path}.command: missing required field")
        else:
            _validate_command_array(command["command"], f"{command_path}.command", errors)
        if "cwd" in command:
            error = _post_install_cwd_error(command["cwd"])
            if error is not None:
                errors.append(f"{command_path}.cwd: {error}")


def _validate_command_array(value: object, path: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{path}: must be a non-empty string array")
        return

    for index, part in enumerate(value):
        if not isinstance(part, str):
            errors.append(f"{path}[{index}]: must be a string")
        elif not part:
            errors.append(f"{path}[{index}]: must be non-empty")


def _is_non_empty_string(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value)


def _contains_url_control_character(value: str) -> bool:
    return any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)


def _post_install_cwd_error(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return "must be a non-empty relative path"
    if (
        value.startswith(("/", "\\"))
        or Path(value).is_absolute()
        or ntpath.isabs(value)
        or ntpath.splitdrive(value)[0]
    ):
        return "must be a non-empty relative path"
    normalized = posixpath.normpath(value.replace("\\", "/"))
    if normalized == ".." or normalized.startswith("../"):
        return "must stay inside the extracted resource"
    return None


def urlopen(request: Request, *, timeout: float) -> UrlResponse:
    return NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _open_url(request: Request, timeout: float) -> UrlResponse:
    return urlopen(request, timeout=timeout)


def check_url_reachable(
    url: str,
    *,
    opener: UrlOpener = _open_url,
    timeout: float = URL_TIMEOUT_SECONDS,
) -> str | None:
    head_error = _request_url(url, "HEAD", opener=opener, timeout=timeout)
    if head_error is None:
        return None
    return _request_url(
        url,
        "GET",
        opener=opener,
        timeout=timeout,
        headers={"Range": "bytes=0-0"},
        read_limit=1,
    )


def _request_url(
    url: str,
    method: str,
    *,
    opener: UrlOpener,
    timeout: float,
    headers: dict[str, str] | None = None,
    read_limit: int | None = None,
) -> str | None:
    try:
        request = Request(url, headers=headers or {}, method=method)
        with opener(request, timeout) as response:
            if not 200 <= response.status < 300:
                return f"{method} returned HTTP {response.status}"
            if read_limit is not None:
                response.read(read_limit)
            return None
    except HTTPError as exc:
        if 300 <= exc.code < 400 and exc.headers.get("Location"):
            return None
        return f"{method} returned HTTP {exc.code}"
    except (TimeoutError, socket.timeout) as exc:
        return f"{method} timed out: {exc}"
    except URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError | socket.timeout):
            return f"{method} timed out: {reason}"
        return f"{method} failed: {reason}"
    except (HTTPException, ValueError) as exc:
        return f"{method} failed: {exc}"
    except OSError as exc:
        return f"{method} failed: {exc}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate an ECOS registry JSON file.")
    parser.add_argument(
        "registry",
        type=Path,
        nargs="?",
        default=Path("tool-registry.json"),
        help="Path to the registry JSON file.",
    )
    parser.add_argument(
        "--check-urls",
        action="store_true",
        help="Check lightweight reachability of each platform asset URL.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    errors = validate_registry(args.registry, check_urls=args.check_urls)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
