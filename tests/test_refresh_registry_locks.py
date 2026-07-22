from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
REFRESHER_PATH = ROOT / ".github" / "scripts" / "refresh_registry_locks.py"

spec = importlib.util.spec_from_file_location("refresh_registry_locks", REFRESHER_PATH)
assert spec is not None
refresh_registry_locks = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = refresh_registry_locks
spec.loader.exec_module(refresh_registry_locks)


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

        updates = refresh_registry_locks.refresh_registry_data(
            registry,
            json_fetcher=unexpected_fetch,
            text_fetcher=unexpected_fetch,
            size_fetcher=unexpected_fetch,
        )

        self.assertEqual([], updates)
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

        updates = refresh_registry_locks.refresh_registry_data(
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
            updates,
        )

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

        updates = refresh_registry_locks.refresh_registry_data(
            registry,
            json_fetcher=lambda _url: {"sha256": "c" * 64, "size": 987},
            text_fetcher=lambda _url: f"{'b' * 64}  surfer-latest.tar.gz\n",
            size_fetcher=lambda _url: 12345,
        )

        platform = registry["tools"][0]["versions"][0]["platforms"]["linux-x86_64"]
        self.assertEqual("c" * 64, platform["sha256"])
        self.assertEqual(987, platform["size"])
        self.assertEqual(2, len(updates))


if __name__ == "__main__":
    unittest.main()
