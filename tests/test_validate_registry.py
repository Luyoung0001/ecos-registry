from __future__ import annotations

import copy
from email.message import Message
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
from pathlib import Path
import sys
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request
import unittest


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / ".github" / "scripts" / "validate_registry.py"

spec = importlib.util.spec_from_file_location("validate_registry", VALIDATOR_PATH)
assert spec is not None
validate_registry = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = validate_registry
spec.loader.exec_module(validate_registry)


def valid_registry() -> dict[str, object]:
    return {
        "schema_version": 2,
        "tools": [
            {
                "name": "yosys",
                "display_name": "Yosys",
                "description": "Yosys from OSS CAD Suite.",
                "category": "synthesis",
                "homepage": "https://github.com/YosysHQ/oss-cad-suite-build",
                "versions": [
                    {
                        "version": "2026-05-13",
                        "platforms": {
                            "linux-x86_64": {
                                "url": "https://example.com/yosys.tar.gz",
                                "metadata_url": "https://example.com/yosys.metadata.json",
                                "sha256_url": "https://example.com/yosys.tar.gz.sha256",
                                "sha256": "a" * 64,
                                "size": 123,
                                "strip_prefix": "oss-cad-suite",
                            }
                        },
                        "requires": [],
                    }
                ],
            }
        ],
        "pdks": [
            {
                "id": "ics55",
                "display_name": "ICsprout 55nm PDK",
                "description": "ICsprout 55nm open-source process design kit.",
                "category": "pdk",
                "homepage": "https://github.com/openecos-projects/icsprout55-pdk",
                "versions": [
                    {
                        "version": "1.10.100",
                        "platforms": {
                            "all-platform": {
                                "url": "https://example.com/ics55.zip",
                                "sha256": "b" * 64,
                                "size": 456,
                                "strip_prefix": "ics55",
                                "post_install": [
                                    {
                                        "command": ["make", "unzip"],
                                        "cwd": ".",
                                    }
                                ],
                            }
                        },
                    }
                ],
            }
        ],
    }


class ValidateRegistryOfflineTests(unittest.TestCase):
    def errors_for(self, registry: object) -> list[str]:
        return validate_registry.validate_registry_data(registry)

    def assert_has_error(self, errors: list[str], expected: str) -> None:
        self.assertTrue(
            any(expected in error for error in errors),
            f"Expected {expected!r} in errors:\n" + "\n".join(errors),
        )

    def test_current_registry_passes_offline_validation(self) -> None:
        """Validate that the checked-in registry satisfies offline format rules."""
        errors = validate_registry.validate_registry(ROOT / "tool-registry.json")

        self.assertEqual([], errors)

    def test_invalid_json_shape_reports_pathful_errors(self) -> None:
        """Verify top-level JSON shape errors include actionable registry paths."""
        self.assert_has_error(self.errors_for([]), "$: must be a JSON object")

        registry = {"schema_version": 1, "tools": {}, "pdks": {}, "extra": True}
        errors = self.errors_for(registry)

        self.assert_has_error(errors, "schema_version: must equal 2")
        self.assert_has_error(errors, "tools: must be an array")
        self.assert_has_error(errors, "pdks: must be an array")
        self.assert_has_error(errors, "extra: unknown top-level key")

    def test_required_fields_and_identifier_rules_are_enforced(self) -> None:
        """Check required entry fields and stable identifier naming constraints."""
        registry = valid_registry()
        tool = registry["tools"][0]
        assert isinstance(tool, dict)
        tool["name"] = "Bad Name"
        del tool["display_name"]
        pdk = registry["pdks"][0]
        assert isinstance(pdk, dict)
        pdk["id"] = ""
        del pdk["description"]

        errors = self.errors_for(registry)

        self.assert_has_error(errors, "tools[0].display_name: missing required field")
        self.assert_has_error(errors, "tools[0].name: must match")
        self.assert_has_error(errors, "pdks[0].description: missing required field")
        self.assert_has_error(errors, "pdks[0].id: must be a non-empty stable identifier")

    def test_duplicate_entry_ids_and_empty_versions_or_platforms_fail(self) -> None:
        """Reject duplicate tool/PDK ids and empty version or platform sections."""
        registry = valid_registry()
        tool = copy.deepcopy(registry["tools"][0])
        pdk = copy.deepcopy(registry["pdks"][0])
        assert isinstance(registry["tools"], list)
        assert isinstance(registry["pdks"], list)
        registry["tools"].append(tool)
        registry["pdks"].append(pdk)
        first_tool = registry["tools"][0]
        first_pdk = registry["pdks"][0]
        assert isinstance(first_tool, dict)
        assert isinstance(first_pdk, dict)
        first_tool["versions"] = []
        versions = first_pdk["versions"]
        assert isinstance(versions, list)
        platforms = versions[0]["platforms"]
        assert isinstance(platforms, dict)
        platforms.clear()

        errors = self.errors_for(registry)

        self.assert_has_error(errors, "tools[1].name: duplicate tool name 'yosys'")
        self.assert_has_error(errors, "pdks[1].id: duplicate PDK id 'ics55'")
        self.assert_has_error(errors, "tools[0].versions: must be a non-empty array")
        self.assert_has_error(
            errors,
            "pdks[0].versions[0].platforms: must be a non-empty object",
        )

    def test_version_entries_and_ordering_are_validated(self) -> None:
        """Ensure version metadata, requires type, and newest-first order are checked."""
        registry = valid_registry()
        tool = registry["tools"][0]
        pdk = registry["pdks"][0]
        assert isinstance(tool, dict)
        assert isinstance(pdk, dict)
        tool["versions"] = [
            {
                "version": "2026-01-01",
                "platforms": copy.deepcopy(tool["versions"][0]["platforms"]),
                "requires": "yosys",
            },
            {
                "version": "2026-05-13",
                "platforms": copy.deepcopy(tool["versions"][0]["platforms"]),
            },
        ]
        pdk["versions"] = [
            {
                "version": "1.9.9",
                "platforms": copy.deepcopy(pdk["versions"][0]["platforms"]),
            },
            {
                "version": "1.10.100",
                "platforms": copy.deepcopy(pdk["versions"][0]["platforms"]),
            },
            {
                "version": "custom",
                "platforms": copy.deepcopy(pdk["versions"][0]["platforms"]),
            },
        ]

        errors = self.errors_for(registry)

        self.assert_has_error(errors, "tools[0].versions: newest version must appear first")
        self.assert_has_error(errors, "tools[0].versions[0].requires: must be an array")
        self.assert_has_error(
            errors,
            "pdks[0].versions: mixed or unsupported version format",
        )

    def test_requires_references_and_version_fields_are_validated(self) -> None:
        """Reject malformed, duplicate, missing, or misspelled dependencies."""
        registry = valid_registry()
        tool_version = registry["tools"][0]["versions"][0]
        pdk_version = registry["pdks"][0]["versions"][0]
        assert isinstance(tool_version, dict)
        assert isinstance(pdk_version, dict)
        tool_version["requires"] = [
            "pdk:ics55",
            "tool:missing",
            "yosys",
            "pdk:BadName",
            42,
            "pdk:ics55",
        ]
        tool_version["require"] = ["tool:yosys"]
        pdk_version["requires"] = ["tool:yosys"]

        errors = self.errors_for(registry)

        self.assert_has_error(
            errors,
            "tools[0].versions[0].requires[1]: unknown resource dependency 'tool:missing'",
        )
        self.assert_has_error(
            errors,
            "tools[0].versions[0].requires[2]: must match tool:<id> or pdk:<id>",
        )
        self.assert_has_error(
            errors,
            "tools[0].versions[0].requires[3]: must match tool:<id> or pdk:<id>",
        )
        self.assert_has_error(
            errors,
            "tools[0].versions[0].requires[4]: must be a string",
        )
        self.assert_has_error(
            errors,
            "tools[0].versions[0].requires[5]: duplicate dependency 'pdk:ics55'",
        )
        self.assert_has_error(
            errors,
            "tools[0].versions[0].require: unknown version field",
        )
        self.assertFalse(
            any("pdks[0].versions[0].requires" in error for error in errors),
            "PDK versions may depend on registered tools",
        )

    def test_platform_keys_and_fields_are_validated(self) -> None:
        """Verify platform names, asset fields, and archive metadata constraints."""
        registry = valid_registry()
        tool_version = registry["tools"][0]["versions"][0]
        pdk_version = registry["pdks"][0]["versions"][0]
        assert isinstance(tool_version, dict)
        assert isinstance(pdk_version, dict)
        tool_version["platforms"] = {
            "all-platform": copy.deepcopy(tool_version["platforms"]["linux-x86_64"]),
            "": copy.deepcopy(tool_version["platforms"]["linux-x86_64"]),
            "linux-x86_64": {
                "url": "ftp://example.com/yosys.bin",
                "metadata_url": "https://example.com/yosys.metadata.bin",
                "sha256_url": "https://exa mple.com/yosys.sha256",
                "sha256": None,
                "size": 0,
                "strip_prefix": "",
                "unknown": True,
            },
        }
        pdk_version["platforms"]["all-platform"]["url"] = "https://example.com/pdk.dmg"

        errors = self.errors_for(registry)

        self.assert_has_error(
            errors,
            "tools[0].versions[0].platforms.all-platform: all-platform is not allowed for tools",
        )
        self.assert_has_error(errors, "tools[0].versions[0].platforms: platform key must be non-empty")
        self.assert_has_error(errors, "tools[0].versions[0].platforms.linux-x86_64.url: must use http or https")
        self.assert_has_error(errors, "tools[0].versions[0].platforms.linux-x86_64.url: unsupported archive suffix")
        self.assert_has_error(errors, "tools[0].versions[0].platforms.linux-x86_64.metadata_url: unsupported sidecar URL suffix")
        self.assert_has_error(errors, "tools[0].versions[0].platforms.linux-x86_64.sha256_url: malformed URL")
        self.assert_has_error(errors, "tools[0].versions[0].platforms.linux-x86_64.sha256: must be a lowercase 64-character hex string")
        self.assert_has_error(errors, "tools[0].versions[0].platforms.linux-x86_64.size: must be a positive integer")
        self.assert_has_error(errors, "tools[0].versions[0].platforms.linux-x86_64.strip_prefix: must be a non-empty string")
        self.assert_has_error(errors, "tools[0].versions[0].platforms.linux-x86_64.unknown: unknown platform field")
        self.assert_has_error(errors, "pdks[0].versions[0].platforms.all-platform.url: unsupported archive suffix")

    def test_sidecar_metadata_does_not_replace_static_locks(self) -> None:
        """Require the static fields consumed by current installers."""
        registry = valid_registry()
        tool_platform = registry["tools"][0]["versions"][0]["platforms"]["linux-x86_64"]
        assert isinstance(tool_platform, dict)
        del tool_platform["sha256"]
        del tool_platform["size"]

        errors = self.errors_for(registry)

        self.assert_has_error(
            errors,
            "tools[0].versions[0].platforms.linux-x86_64.sha256: missing required field",
        )
        self.assert_has_error(
            errors,
            "tools[0].versions[0].platforms.linux-x86_64.size: missing required field",
        )
        self.assertEqual(
            1,
            sum(
                "platforms.linux-x86_64.sha256" in error
                for error in errors
            ),
        )
        self.assertEqual(
            1,
            sum("platforms.linux-x86_64.size" in error for error in errors),
        )

    def test_asset_must_have_checksum_and_size_source(self) -> None:
        """Require static archive facts for install safety."""
        registry = valid_registry()
        tool_platform = registry["tools"][0]["versions"][0]["platforms"]["linux-x86_64"]
        assert isinstance(tool_platform, dict)
        del tool_platform["sha256"]
        del tool_platform["sha256_url"]
        del tool_platform["metadata_url"]
        del tool_platform["size"]

        errors = self.errors_for(registry)

        self.assert_has_error(
            errors,
            "tools[0].versions[0].platforms.linux-x86_64.sha256: missing required field",
        )
        self.assert_has_error(
            errors,
            "tools[0].versions[0].platforms.linux-x86_64.size: missing required field",
        )

    def test_tar_xz_archives_are_supported(self) -> None:
        """Allow upstream binary releases distributed as tar.xz archives."""
        registry = valid_registry()
        tool_platform = registry["tools"][0]["versions"][0]["platforms"]["linux-x86_64"]
        assert isinstance(tool_platform, dict)
        tool_platform["url"] = "https://example.com/riscv64-elf-ubuntu-22.04-gcc.tar.xz"

        errors = self.errors_for(registry)

        self.assertEqual([], errors)

    def test_malformed_url_errors_are_pathful_offline(self) -> None:
        """Confirm malformed asset URLs fail offline with the exact platform path."""
        for url in (
            "https://[bad/foo.tar.gz",
            "https://exa mple.com/yosys.tar.gz",
            "http://:80/yosys.tar.gz",
        ):
            with self.subTest(url=url):
                registry = valid_registry()
                platform = registry["tools"][0]["versions"][0]["platforms"][
                    "linux-x86_64"
                ]
                assert isinstance(platform, dict)
                platform["url"] = url

                errors = self.errors_for(registry)

                self.assert_has_error(
                    errors,
                    "tools[0].versions[0].platforms.linux-x86_64.url: malformed URL",
                )

    def test_post_install_commands_are_validated(self) -> None:
        """Check post-install command arrays and cwd sandbox boundaries."""
        registry = valid_registry()
        platform = registry["pdks"][0]["versions"][0]["platforms"]["all-platform"]
        assert isinstance(platform, dict)
        platform["post_install"] = [
            {},
            {"command": []},
            {"command": ["make", 12], "cwd": "/tmp/build"},
            {"command": ["make"], "cwd": ".."},
            {"command": ["make"], "cwd": "C:\\tmp"},
            {"command": ["make"], "cwd": "\\tmp"},
            {"command": ["make"], "cwd": "C:tmp"},
        ]

        errors = self.errors_for(registry)

        self.assert_has_error(
            errors,
            "pdks[0].versions[0].platforms.all-platform.post_install[0].command: missing required field",
        )
        self.assert_has_error(
            errors,
            "pdks[0].versions[0].platforms.all-platform.post_install[1].command: must be a non-empty string array",
        )
        self.assert_has_error(
            errors,
            "pdks[0].versions[0].platforms.all-platform.post_install[2].command[1]: must be a string",
        )
        self.assert_has_error(
            errors,
            "pdks[0].versions[0].platforms.all-platform.post_install[2].cwd: must be a non-empty relative path",
        )
        self.assert_has_error(
            errors,
            "pdks[0].versions[0].platforms.all-platform.post_install[3].cwd: must stay inside the extracted resource",
        )
        self.assert_has_error(
            errors,
            "pdks[0].versions[0].platforms.all-platform.post_install[4].cwd: must be a non-empty relative path",
        )
        self.assert_has_error(
            errors,
            "pdks[0].versions[0].platforms.all-platform.post_install[5].cwd: must be a non-empty relative path",
        )
        self.assert_has_error(
            errors,
            "pdks[0].versions[0].platforms.all-platform.post_install[6].cwd: must be a non-empty relative path",
        )

    def test_supplemental_assets_are_locked_and_path_safe(self) -> None:
        """Reject mutable, malformed, duplicate, or escaping supplemental assets."""
        registry = valid_registry()
        platform = registry["pdks"][0]["versions"][0]["platforms"]["all-platform"]
        assert isinstance(platform, dict)
        platform["supplemental_assets"] = [
            "not-an-object",
            {
                "path": "../escape.tar.bz2",
                "url": "ftp://example.com/escape.bin",
                "sha256": "A" * 64,
                "size": 0,
                "extra": True,
            },
            {
                "path": "locked/asset.tar.bz2",
                "url": "https://example.com/asset.tar.bz2",
                "sha256": "c" * 64,
                "size": 123,
            },
            {
                "path": "locked/asset.tar.bz2",
                "url": "https://example.com/asset-copy.tar.bz2",
                "sha256": "d" * 64,
                "size": 456,
            },
            {"path": "missing-fields.tar.bz2"},
        ]

        errors = self.errors_for(registry)

        self.assert_has_error(
            errors,
            "pdks[0].versions[0].platforms.all-platform.supplemental_assets[0]: must be an object",
        )
        self.assert_has_error(
            errors,
            "supplemental_assets[1].path: must be a normalized relative path",
        )
        self.assert_has_error(
            errors, "supplemental_assets[1].url: must use http or https"
        )
        self.assert_has_error(
            errors, "supplemental_assets[1].url: unsupported archive suffix"
        )
        self.assert_has_error(
            errors,
            "supplemental_assets[1].sha256: must be a lowercase 64-character hex string",
        )
        self.assert_has_error(
            errors, "supplemental_assets[1].size: must be a positive integer"
        )
        self.assert_has_error(
            errors,
            "supplemental_assets[1].extra: unknown supplemental asset field",
        )
        self.assert_has_error(
            errors,
            "supplemental_assets[3].path: duplicate path 'locked/asset.tar.bz2'",
        )
        self.assert_has_error(
            errors,
            "supplemental_assets[4].url: missing required field",
        )
        self.assert_has_error(
            errors,
            "supplemental_assets[4].sha256: missing required field",
        )
        self.assert_has_error(
            errors,
            "supplemental_assets[4].size: missing required field",
        )


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.read_sizes: list[int | None] = []

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int | None = None) -> bytes:
        self.read_sizes.append(size)
        return b"x"


class ValidateRegistryUrlTests(unittest.TestCase):
    def test_url_checking_reports_registry_path_and_url(self) -> None:
        """Ensure URL check failures include both registry path and failing URL."""
        registry = valid_registry()

        def checker(url: str) -> str | None:
            return f"mock failure for {url}"

        errors = validate_registry.validate_registry_data(
            registry,
            check_urls=True,
            url_checker=checker,
        )

        self.assert_has_error(
            errors,
            "tools[0].versions[0].platforms.linux-x86_64.url: URL check failed for https://example.com/yosys.tar.gz: mock failure",
        )

    def test_url_checker_accepts_successful_head(self) -> None:
        """Accept a successful HEAD response without issuing a fallback GET."""
        requests: list[Request] = []

        def opener(request: Request, timeout: float) -> FakeResponse:
            requests.append(request)
            self.assertEqual(5.0, timeout)
            return FakeResponse(200)

        error = validate_registry.check_url_reachable(
            "https://example.com/yosys.tar.gz",
            opener=opener,
            timeout=5.0,
        )

        self.assertIsNone(error)
        self.assertEqual(["HEAD"], [request.get_method() for request in requests])

    def test_default_url_checker_passes_timeout_as_keyword(self) -> None:
        """Verify the default urlopen adapter passes timeout as a keyword argument."""
        calls: list[float] = []
        original_urlopen = validate_registry.urlopen

        def fake_urlopen(request: Request, *, timeout: float) -> FakeResponse:
            self.assertEqual("HEAD", request.get_method())
            calls.append(timeout)
            return FakeResponse(200)

        validate_registry.urlopen = fake_urlopen
        try:
            error = validate_registry.check_url_reachable(
                "https://example.com/yosys.tar.gz",
                timeout=7.0,
            )
        finally:
            validate_registry.urlopen = original_urlopen

        self.assertIsNone(error)
        self.assertEqual([7.0], calls)

    def test_url_checker_falls_back_to_ranged_get_without_full_download(self) -> None:
        """Use a one-byte ranged GET fallback when HEAD is not supported."""
        requests: list[Request] = []
        get_response = FakeResponse(206)

        def opener(request: Request, timeout: float) -> FakeResponse:
            del timeout
            requests.append(request)
            if request.get_method() == "HEAD":
                raise HTTPError(
                    request.full_url,
                    405,
                    "Method Not Allowed",
                    HTTPMessage(),
                    None,
                )
            return get_response

        error = validate_registry.check_url_reachable(
            "https://example.com/yosys.tar.gz",
            opener=opener,
        )

        self.assertIsNone(error)
        self.assertEqual(["HEAD", "GET"], [request.get_method() for request in requests])
        self.assertEqual("bytes=0-0", requests[1].headers["Range"])
        self.assertEqual([1], get_response.read_sizes)

    def test_url_checker_accepts_redirect_with_location(self) -> None:
        """Treat download redirects as reachable without probing large asset backends."""
        headers = Message()
        headers["Location"] = "https://downloads.example.com/yosys.tar.gz"

        def opener(request: Request, timeout: float) -> FakeResponse:
            del timeout
            raise HTTPError(
                request.full_url,
                302,
                "Found",
                headers,
                None,
            )

        error = validate_registry.check_url_reachable(
            "https://example.com/yosys.tar.gz",
            opener=opener,
        )

        self.assertIsNone(error)

    def test_real_no_redirect_opener_accepts_redirect_with_location(self) -> None:
        """Exercise the real no-redirect opener against a local HTTP server."""

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_HEAD(self) -> None:
                self.send_response(302)
                self.send_header("Location", "https://downloads.example.com/asset.tar.gz")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            error = validate_registry.check_url_reachable(
                f"http://{host}:{port}/asset.tar.gz"
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertIsNone(error)

    def test_url_checker_reports_timeout_and_non_success_status(self) -> None:
        """Report timeout and HTTP status failures from lightweight URL probes."""
        def timeout_opener(request: Request, timeout: float) -> FakeResponse:
            del request, timeout
            raise TimeoutError("timed out")

        timeout_error = validate_registry.check_url_reachable(
            "https://example.com/yosys.tar.gz",
            opener=timeout_opener,
        )

        self.assertIsNotNone(timeout_error)
        self.assertIn("timed out", timeout_error)

        def not_found_opener(request: Request, timeout: float) -> FakeResponse:
            del timeout
            raise HTTPError(request.full_url, 404, "Not Found", HTTPMessage(), None)

        status_error = validate_registry.check_url_reachable(
            "https://example.com/yosys.tar.gz",
            opener=not_found_opener,
        )

        self.assertIsNotNone(status_error)
        self.assertIn("GET returned HTTP 404", status_error)

    def test_url_checking_reports_malformed_url_without_crashing(self) -> None:
        """Return a normal URL-check error for malformed URLs instead of crashing."""
        error = validate_registry.check_url_reachable(
            "https://exa mple.com/yosys.tar.gz"
        )

        self.assertIsNotNone(error)
        self.assertIn("failed", error)

    def assert_has_error(self, errors: list[str], expected: str) -> None:
        self.assertTrue(
            any(expected in error for error in errors),
            f"Expected {expected!r} in errors:\n" + "\n".join(errors),
        )


if __name__ == "__main__":
    unittest.main()
