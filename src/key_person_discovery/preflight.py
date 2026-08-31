from __future__ import annotations

import hashlib
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from .sources import Crawl4aiCrawler, crawl_sync


EGRESS_CHECK_URLS = (
    "https://checkip.amazonaws.com/",
    "https://icanhazip.com/",
    "https://ifconfig.me/ip",
)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "host.docker.internal"}


def verify_proxy_setup(
    proxy_url: str,
    searxng_url: str,
    crawler: Crawl4aiCrawler,
    searxng_settings: Path,
) -> str:
    _validate_proxy_url(proxy_url)
    if (urlparse(searxng_url).hostname or "").lower() in LOCAL_HOSTS:
        _validate_local_searxng_proxy(proxy_url, searxng_settings)

    saw_mismatch = False
    for url in EGRESS_CHECK_URLS:
        try:
            expected = _http_proxy_fingerprint(proxy_url, url)
            page = crawl_sync(crawler, [url], allow_failed_content=True)[0]
            if not page.markdown:
                continue
            actual = _trace_fingerprint(page.markdown)
        except RuntimeError:
            continue
        if actual == expected:
            return expected
        saw_mismatch = True
    if saw_mismatch:
        raise RuntimeError("Crawl4AI egress does not match KEY_PERSON_PROXY_URL")
    raise RuntimeError("Unable to verify proxy egress with available check endpoints")


def _http_proxy_fingerprint(proxy_url: str, url: str) -> str:
    try:
        response = httpx.get(
            url,
            proxy=proxy_url,
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": "key-person-discovery/0.1"},
        )
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError("KEY_PERSON_PROXY_URL egress check failed") from exc
    return _trace_fingerprint(response.text)


def _trace_fingerprint(value: str) -> str:
    match = re.search(r"^ip=([^\s]+)$", value, re.MULTILINE)
    candidates = [match.group(1)] if match else re.findall(r"[0-9a-fA-F:.]+", value)
    for candidate in candidates:
        try:
            address = str(ipaddress.ip_address(candidate.strip(".,[]()")))
        except ValueError:
            continue
        return hashlib.sha256(address.encode()).hexdigest()[:12]
    raise RuntimeError("Egress check response does not contain an IP address")


def _validate_proxy_url(proxy_url: str) -> None:
    parsed = urlparse(proxy_url)
    if parsed.scheme not in {"http", "https", "socks4", "socks5", "socks5h"}:
        raise RuntimeError("KEY_PERSON_PROXY_URL must be an HTTP or SOCKS proxy URL")
    if not parsed.hostname or not parsed.port:
        raise RuntimeError("KEY_PERSON_PROXY_URL must include a host and port")


def _validate_local_searxng_proxy(proxy_url: str, settings_path: Path) -> None:
    if not settings_path.is_file():
        raise RuntimeError(f"Local SearXNG settings are missing: {settings_path}")
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    configured = (
        settings.get("outgoing", {})
        .get("proxies", {})
        .get("all://", [])
    )
    if not isinstance(configured, list) or not configured:
        raise RuntimeError("Local SearXNG has no outgoing proxy configured")
    expected = _proxy_signature(proxy_url)
    if expected not in {_proxy_signature(str(item)) for item in configured}:
        raise RuntimeError("Local SearXNG proxy does not match KEY_PERSON_PROXY_URL")


def _proxy_signature(value: str) -> tuple[str, str, int | None]:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host in LOCAL_HOSTS:
        host = "local-proxy"
    return parsed.scheme.lower(), host, parsed.port
