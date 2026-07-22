#!/usr/bin/env python3

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from registry_schema import has_remote_lock_source, iter_registry_platforms


SHA256_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
URL_TIMEOUT_SECONDS = 30.0

JsonFetcher = Callable[[str], dict[str, Any]]
TextFetcher = Callable[[str], str]
SizeFetcher = Callable[[str], int]


@dataclass
class RefreshResult:
    updates: list[str]
    failures: list[str]


def refresh_registry_file(
    path: Path,
    *,
    json_fetcher: JsonFetcher | None = None,
    text_fetcher: TextFetcher | None = None,
    size_fetcher: SizeFetcher | None = None,
) -> RefreshResult:
    data = json.loads(path.read_text(encoding="utf-8"))
    result = refresh_registry_data(
        data,
        json_fetcher=json_fetcher,
        text_fetcher=text_fetcher,
        size_fetcher=size_fetcher,
    )
    if result.updates:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for update in result.updates:
        print(update)
    for failure in result.failures:
        print(failure, file=sys.stderr)
    print(f"refreshed {len(result.updates)} platform lock field set(s)")
    return result


def refresh_registry_data(
    data: dict[str, Any],
    *,
    json_fetcher: JsonFetcher | None = None,
    text_fetcher: TextFetcher | None = None,
    size_fetcher: SizeFetcher | None = None,
) -> RefreshResult:
    json_fetcher = json_fetcher or fetch_json_url
    text_fetcher = text_fetcher or fetch_text_url
    size_fetcher = size_fetcher or fetch_url_size
    updates: list[str] = []
    failures: list[str] = []
    for platform_entry in iter_registry_platforms(data):
        platform = platform_entry.value
        if not has_remote_lock_source(platform):
            continue
        try:
            updates.extend(
                refresh_platform_lock(
                    platform,
                    path=platform_entry.path,
                    json_fetcher=json_fetcher,
                    text_fetcher=text_fetcher,
                    size_fetcher=size_fetcher,
                )
            )
        except Exception as exc:
            failures.append(f"{platform_entry.path}: refresh failed: {exc}")
    return RefreshResult(updates=updates, failures=failures)


def refresh_platform_lock(
    platform: dict[str, Any],
    *,
    path: str,
    json_fetcher: JsonFetcher,
    text_fetcher: TextFetcher,
    size_fetcher: SizeFetcher,
) -> list[str]:
    updates: list[str] = []
    metadata = fetch_metadata(platform, json_fetcher)
    sha256 = metadata.get("sha256")
    size = metadata.get("size")

    if not isinstance(sha256, str):
        sha256_url = platform.get("sha256_url")
        if isinstance(sha256_url, str) and sha256_url:
            sha256 = parse_sha256_text(text_fetcher(sha256_url))

    if not isinstance(size, int) or size <= 0:
        url = platform.get("url")
        if isinstance(url, str) and url:
            size = size_fetcher(url)

    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise RuntimeError(f"{path}: could not resolve a valid sha256")
    if not isinstance(size, int) or size <= 0:
        raise RuntimeError(f"{path}: could not resolve a positive size")

    normalized_sha = sha256.lower()
    if platform.get("sha256") != normalized_sha:
        platform["sha256"] = normalized_sha
        updates.append(f"{path}.sha256 refreshed")
    if platform.get("size") != size:
        platform["size"] = size
        updates.append(f"{path}.size refreshed")
    return updates


def fetch_metadata(platform: dict[str, Any], json_fetcher: JsonFetcher) -> dict[str, Any]:
    metadata_url = platform.get("metadata_url")
    if not isinstance(metadata_url, str) or not metadata_url:
        return {}
    metadata = json_fetcher(metadata_url)
    return {
        "sha256": metadata.get("sha256"),
        "size": metadata.get("size"),
    }


def fetch_json_url(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=URL_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text_url(url: str) -> str:
    with urlopen(url, timeout=URL_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8")


def fetch_url_size(url: str) -> int:
    request = Request(url, method="HEAD")
    try:
        with urlopen(request, timeout=URL_TIMEOUT_SECONDS) as response:
            length = response.headers.get("Content-Length")
            if length is not None:
                return int(length)
    except HTTPError as error:
        if error.code not in (405, 501):
            raise

    request = Request(url, headers={"Range": "bytes=0-0"})
    with urlopen(request, timeout=URL_TIMEOUT_SECONDS) as response:
        content_range = response.headers.get("Content-Range")
        if content_range and "/" in content_range:
            return int(content_range.rsplit("/", 1)[1])
        length = response.headers.get("Content-Length")
        if length is not None:
            return int(length)
    raise RuntimeError(f"could not resolve size for {url}")


def parse_sha256_text(text: str) -> str:
    match = SHA256_RE.search(text)
    if match is None:
        raise RuntimeError("sha256 sidecar does not contain a 64-character hex hash")
    return match.group(0).lower()


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh static sha256/size lock fields in tool-registry.json")
    parser.add_argument("registry", nargs="?", default="tool-registry.json", type=Path)
    args = parser.parse_args()
    result = refresh_registry_file(args.registry)
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
