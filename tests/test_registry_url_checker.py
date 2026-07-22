from __future__ import annotations

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
SCRIPTS_DIR = ROOT / ".github" / "scripts"
URL_CHECKER_PATH = SCRIPTS_DIR / "registry_url_checker.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

spec = importlib.util.spec_from_file_location("registry_url_checker", URL_CHECKER_PATH)
assert spec is not None
registry_url_checker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = registry_url_checker
spec.loader.exec_module(registry_url_checker)


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


class RegistryUrlCheckerTests(unittest.TestCase):
    def test_url_checker_accepts_successful_head(self) -> None:
        """Accept a successful HEAD response without issuing a fallback GET."""
        requests: list[Request] = []

        def opener(request: Request, timeout: float) -> FakeResponse:
            requests.append(request)
            self.assertEqual(5.0, timeout)
            return FakeResponse(200)

        error = registry_url_checker.check_url_reachable(
            "https://example.com/yosys.tar.gz",
            opener=opener,
            timeout=5.0,
        )

        self.assertIsNone(error)
        self.assertEqual(["HEAD"], [request.get_method() for request in requests])

    def test_default_url_checker_passes_timeout_as_keyword(self) -> None:
        """Verify the default urlopen adapter passes timeout as a keyword argument."""
        calls: list[float] = []
        original_urlopen = registry_url_checker.urlopen

        def fake_urlopen(request: Request, *, timeout: float) -> FakeResponse:
            self.assertEqual("HEAD", request.get_method())
            calls.append(timeout)
            return FakeResponse(200)

        registry_url_checker.urlopen = fake_urlopen
        try:
            error = registry_url_checker.check_url_reachable(
                "https://example.com/yosys.tar.gz",
                timeout=7.0,
            )
        finally:
            registry_url_checker.urlopen = original_urlopen

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

        error = registry_url_checker.check_url_reachable(
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

        error = registry_url_checker.check_url_reachable(
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
            error = registry_url_checker.check_url_reachable(
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

        timeout_error = registry_url_checker.check_url_reachable(
            "https://example.com/yosys.tar.gz",
            opener=timeout_opener,
        )

        self.assertIsNotNone(timeout_error)
        self.assertIn("timed out", timeout_error)

        def not_found_opener(request: Request, timeout: float) -> FakeResponse:
            del timeout
            raise HTTPError(request.full_url, 404, "Not Found", HTTPMessage(), None)

        status_error = registry_url_checker.check_url_reachable(
            "https://example.com/yosys.tar.gz",
            opener=not_found_opener,
        )

        self.assertIsNotNone(status_error)
        self.assertIn("GET returned HTTP 404", status_error)

    def test_url_checking_reports_malformed_url_without_crashing(self) -> None:
        """Return a normal URL-check error for malformed URLs instead of crashing."""
        error = registry_url_checker.check_url_reachable(
            "https://exa mple.com/yosys.tar.gz"
        )

        self.assertIsNotNone(error)
        self.assertIn("failed", error)


if __name__ == "__main__":
    unittest.main()
