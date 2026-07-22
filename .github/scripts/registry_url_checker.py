from __future__ import annotations

from collections.abc import Callable
from http.client import HTTPException
import socket
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


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


UrlOpener = Callable[[Request, float], UrlResponse]
UrlChecker = Callable[[str], str | None]


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
