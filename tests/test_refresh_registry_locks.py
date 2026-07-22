from __future__ import annotations

from email.message import Message
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import Request
import unittest


ROOT = Path(__file__).resolve().parents[1]
REFRESHER_PATH = ROOT / ".github" / "scripts" / "refresh_registry_locks.py"

spec = importlib.util.spec_from_file_location("refresh_registry_locks", REFRESHER_PATH)
assert spec is not None
refresh_registry_locks = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = refresh_registry_locks
spec.loader.exec_module(refresh_registry_locks)


class FakeUrlResponse:
    def __init__(self, headers: Message) -> None:
        self.headers = headers

    def __enter__(self) -> "FakeUrlResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class RefreshRegistryLocksTests(unittest.TestCase):
    def test_skips_latest_asset_without_remote_checksum_source(self) -> None:
        platform = {
            "url": "https://example.com/static-latest.tar.gz",
            "sha256": "a" * 64,
            "size": 123,
        }
        registry = {
            "schema_version": 2,
            "tools": [
                {
                    "name": "static-latest",
                    "versions": [
                        {
                            "version": "latest",
                            "platforms": {"linux-x86_64": platform},
                        }
                    ],
                }
            ],
            "pdks": [],
        }

        def unexpected_fetch(_url: str) -> object:
            self.fail("static-only latest assets must not be fetched")

        result = refresh_registry_locks.refresh_registry_data(
            registry,
            json_fetcher=unexpected_fetch,
            text_fetcher=unexpected_fetch,
            size_fetcher=unexpected_fetch,
        )

        self.assertEqual([], result.updates)
        self.assertEqual([], result.failures)
        self.assertEqual("a" * 64, platform["sha256"])
        self.assertEqual(123, platform["size"])

    def test_refreshes_latest_sidecar_asset_static_sha256_and_size(self) -> None:
        registry = {
            "schema_version": 2,
            "tools": [
                {
                    "name": "ecc-fe",
                    "versions": [
                        {
                            "version": "latest",
                            "platforms": {
                                "linux-x86_64": {
                                    "url": "https://example.com/ecc-fe-latest.tar.gz",
                                    "sha256_url": "https://example.com/ecc-fe-latest.tar.gz.sha256",
                                    "sha256": "a" * 64,
                                    "size": 1,
                                }
                            },
                        }
                    ],
                }
            ],
            "pdks": [],
        }

        result = refresh_registry_locks.refresh_registry_data(
            registry,
            text_fetcher=lambda _url: f"{'b' * 64}  ecc-fe-latest.tar.gz\n",
            json_fetcher=lambda _url: {},
            size_fetcher=lambda _url: 12345,
        )

        platform = registry["tools"][0]["versions"][0]["platforms"]["linux-x86_64"]
        self.assertEqual("b" * 64, platform["sha256"])
        self.assertEqual(12345, platform["size"])
        self.assertEqual(
            [
                "tools[0].versions[0].platforms.linux-x86_64.sha256 refreshed",
                "tools[0].versions[0].platforms.linux-x86_64.size refreshed",
            ],
            result.updates,
        )
        self.assertEqual([], result.failures)

    def test_metadata_json_takes_precedence_over_sha_sidecar(self) -> None:
        registry = {
            "schema_version": 2,
            "tools": [
                {
                    "name": "surfer",
                    "versions": [
                        {
                            "version": "latest",
                            "platforms": {
                                "linux-x86_64": {
                                    "url": "https://example.com/surfer-latest.tar.gz",
                                    "metadata_url": "https://example.com/surfer-latest.metadata.json",
                                    "sha256_url": "https://example.com/surfer-latest.tar.gz.sha256",
                                    "sha256": "a" * 64,
                                    "size": 1,
                                }
                            },
                        }
                    ],
                }
            ],
            "pdks": [],
        }

        result = refresh_registry_locks.refresh_registry_data(
            registry,
            json_fetcher=lambda _url: {"sha256": "c" * 64, "size": 987},
            text_fetcher=lambda _url: f"{'b' * 64}  surfer-latest.tar.gz\n",
            size_fetcher=lambda _url: 12345,
        )

        platform = registry["tools"][0]["versions"][0]["platforms"]["linux-x86_64"]
        self.assertEqual("c" * 64, platform["sha256"])
        self.assertEqual(987, platform["size"])
        self.assertEqual(2, len(result.updates))
        self.assertEqual([], result.failures)

    def test_failed_platform_does_not_block_successful_updates(self) -> None:
        failed_platform = {
            "url": "https://example.com/failed.tar.gz",
            "metadata_url": "https://example.com/failed.json",
            "sha256": "a" * 64,
            "size": 1,
        }
        successful_platform = {
            "url": "https://example.com/successful.tar.gz",
            "metadata_url": "https://example.com/successful.json",
            "sha256": "b" * 64,
            "size": 2,
        }
        registry = {
            "schema_version": 2,
            "tools": [
                {
                    "name": "multi-platform",
                    "versions": [
                        {
                            "version": "latest",
                            "platforms": {
                                "linux-x86_64": failed_platform,
                                "linux-arm64": successful_platform,
                            },
                        }
                    ],
                }
            ],
            "pdks": [],
        }

        def fetch_metadata(url: str) -> dict[str, object]:
            if url.endswith("failed.json"):
                raise RuntimeError("temporary metadata failure")
            return {"sha256": "c" * 64, "size": 300}

        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = Path(temp_dir) / "tool-registry.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            result = refresh_registry_locks.refresh_registry_file(
                registry_path,
                json_fetcher=fetch_metadata,
                text_fetcher=lambda _url: "",
                size_fetcher=lambda _url: 0,
            )
            written = json.loads(registry_path.read_text(encoding="utf-8"))

        written_platforms = written["tools"][0]["versions"][0]["platforms"]
        self.assertEqual("a" * 64, written_platforms["linux-x86_64"]["sha256"])
        self.assertEqual(1, written_platforms["linux-x86_64"]["size"])
        self.assertEqual("c" * 64, written_platforms["linux-arm64"]["sha256"])
        self.assertEqual(300, written_platforms["linux-arm64"]["size"])
        self.assertEqual(2, len(result.updates))
        self.assertEqual(1, len(result.failures))
        self.assertIn("linux-x86_64", result.failures[0])
        self.assertIn("temporary metadata failure", result.failures[0])

    def test_fetch_url_size_falls_back_from_head_to_range_get(self) -> None:
        cases = (
            (405, "Content-Range", "bytes 0-0/9876", 9876),
            (501, "Content-Length", "5432", 5432),
        )
        original_urlopen = refresh_registry_locks.urlopen
        try:
            for head_status, header_name, header_value, expected_size in cases:
                with self.subTest(head_status=head_status, header_name=header_name):
                    requests: list[Request] = []

                    def fake_urlopen(
                        request: Request,
                        *,
                        timeout: float,
                    ) -> FakeUrlResponse:
                        self.assertEqual(
                            refresh_registry_locks.URL_TIMEOUT_SECONDS,
                            timeout,
                        )
                        requests.append(request)
                        if request.get_method() == "HEAD":
                            raise HTTPError(
                                request.full_url,
                                head_status,
                                "HEAD unsupported",
                                Message(),
                                None,
                            )
                        headers = Message()
                        headers[header_name] = header_value
                        return FakeUrlResponse(headers)

                    refresh_registry_locks.urlopen = fake_urlopen
                    size = refresh_registry_locks.fetch_url_size(
                        "https://example.com/archive.tar.gz"
                    )

                    self.assertEqual(expected_size, size)
                    self.assertEqual(
                        ["HEAD", "GET"],
                        [request.get_method() for request in requests],
                    )
                    self.assertEqual("bytes=0-0", requests[1].headers["Range"])
        finally:
            refresh_registry_locks.urlopen = original_urlopen


if __name__ == "__main__":
    unittest.main()
