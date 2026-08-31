from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Callable, Protocol
from urllib.parse import unquote, urlencode, urljoin, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from xml.etree import ElementTree

from .models import CompanyProfile, CrawledPage, SearchOutcome, SearchResult


ANYSEARCH_ENDPOINT = "https://api.anysearch.com/mcp"
TAVILY_SEARCH_ENDPOINT = "https://api.tavily.com/search"
_OFFICIAL_PATH_TERMS = (
    "about",
    "company",
    "contact",
    "contato",
    "equipe",
    "leadership",
    "management",
    "news",
    "noticias",
    "people",
    "staff",
    "team",
    "quem-somos",
    "sobre",
)


class SearchClient(Protocol):
    name: str

    def search(self, query: str, limit: int = 5) -> SearchOutcome: ...


def build_queries(company: CompanyProfile) -> list[str]:
    name = f'"{company.name}"'
    queries = [
        f"{name} (procurement OR purchasing OR sourcing OR buyer)",
        f'{name} ("refractory engineer" OR metallurgist OR "technical manager")',
        f"site:linkedin.com/in {name}",
        f'{name} (email OR phone OR WhatsApp OR "wa.me")',
    ]
    domain = urlparse(company.website).hostname or ""
    domain = domain.removeprefix("www.")
    if domain:
        queries.extend(
            [
                f"site:{domain} (team OR management OR contact)",
                f"site:{domain} filetype:pdf",
            ]
        )
    if company.target_contact_count >= 4:
        queries.append(
            f'{name} (leadership OR director OR manager OR engineer OR "sales contact")'
        )
        if domain:
            queries.append(
                f'site:{domain} (email OR phone OR WhatsApp OR "mailto:" OR "tel:")'
            )
    return queries


def build_official_query(company: CompanyProfile) -> str:
    return f'"{company.name}" official website'


def build_customs_queries(company: CompanyProfile) -> list[str]:
    name = f'"{company.name}"'
    products = " OR ".join(
        f'"{item.replace(chr(34), " ")}"' for item in company.products
    )
    product_clause = f" ({products})" if products else ""
    return [
        f"{name} (import OR importer OR shipment OR customs){product_clause}",
        f"{name} (Panjiva OR ImportGenius OR Volza OR Trademo){product_clause}",
        f'{name} (consignee OR buyer OR supplier) (shipment OR "bill of lading"){product_clause}',
    ]


class SearxngClient:
    name = "searxng"

    def __init__(
        self,
        base_url: str,
        timeout: float = 60,
        engines: list[str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.engines = tuple(engine for engine in (engines or ()) if engine)
        self._active_engines: set[str] | None = None

    def search(self, query: str, limit: int = 5) -> SearchOutcome:
        if not self.base_url:
            raise RuntimeError("SEARXNG_URL is required")
        params = {"q": query, "format": "json"}
        if self.engines:
            params["engines"] = ",".join(self.engines)
        url = f"{self.base_url}/search?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "key-person-discovery/0.1"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except Exception as exc:
            raise RuntimeError(f"SearXNG request failed for {query!r}: {exc}") from exc

        results: list[SearchResult] = []
        retrieved_at = _utc_now()
        for rank, item in enumerate(payload.get("results", []), start=1):
            target = str(item.get("url", "")).strip()
            if urlparse(target).scheme not in {"http", "https"}:
                continue
            results.append(
                SearchResult(
                    query=query,
                    title=str(item.get("title", "")).strip(),
                    url=target,
                    snippet=str(item.get("content", "")).strip(),
                    provider=self.name,
                    rank=rank,
                    retrieved_at=retrieved_at,
                )
            )
            if len(results) >= limit:
                break
        unresponsive = _unresponsive_engines(payload.get("unresponsive_engines"))
        if not results and unresponsive:
            failed = {name for name, _ in unresponsive}
            active = self.active_engines()
            if active and active.issubset(failed):
                details = ", ".join(f"{name}: {reason}" for name, reason in unresponsive)
                raise RuntimeError(f"All active SearXNG engines failed for {query!r}: {details}")
        return SearchOutcome(results=results, unresponsive_engines=unresponsive)

    def active_engines(self) -> set[str]:
        if self.engines:
            return set(self.engines)
        if self._active_engines is not None:
            return self._active_engines
        request = Request(
            f"{self.base_url}/config",
            headers={"User-Agent": "key-person-discovery/0.1"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except Exception as exc:
            raise RuntimeError(f"SearXNG config request failed: {exc}") from exc
        self._active_engines = {
            str(item.get("name", "")).strip()
            for item in payload.get("engines", [])
            if item.get("enabled") is True and str(item.get("name", "")).strip()
        }
        if not self._active_engines:
            raise RuntimeError("SearXNG has no active engines")
        return self._active_engines


class AnySearchClient:
    name = "anysearch"

    def __init__(
        self,
        api_key: str = "",
        proxy_url: str = "",
        timeout: float = 30,
        endpoint: str = ANYSEARCH_ENDPOINT,
    ):
        self.api_key = api_key.strip()
        self.proxy_url = proxy_url.strip()
        self.timeout = timeout
        self.endpoint = endpoint

    def search(self, query: str, limit: int = 5) -> SearchOutcome:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {"query": query, "max_results": min(max(limit, 1), 10)},
            },
        }
        headers = {
            "Content-Type": "application/json",
            "X-Anysearch-Client": "key-person-discovery/0.2",
            "User-Agent": "key-person-discovery/0.2",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            response = _open_request(request, self.timeout, self.proxy_url)
            with response:
                body = json.load(response)
        except Exception as exc:
            raise RuntimeError(f"AnySearch request failed for {query!r}: {exc}") from exc
        if body.get("error"):
            detail = body["error"].get("message") if isinstance(body["error"], dict) else body["error"]
            raise RuntimeError(f"AnySearch request failed for {query!r}: {detail}")
        content = body.get("result", {}).get("content", [])
        text = next(
            (str(item.get("text", "")) for item in content if item.get("type") == "text"),
            "",
        )
        return SearchOutcome(results=_parse_anysearch_markdown(query, text)[:limit])


class TavilyClient:
    name = "tavily"

    def __init__(
        self,
        api_key: str = "",
        proxy_url: str = "",
        timeout: float = 30,
        endpoint: str = TAVILY_SEARCH_ENDPOINT,
    ):
        self.api_key = api_key.strip()
        if not self.api_key:
            raise ValueError("Tavily API key is required")
        self.proxy_url = proxy_url.strip()
        self.timeout = timeout
        self.endpoint = endpoint

    def search(self, query: str, limit: int = 5) -> SearchOutcome:
        max_results = min(max(limit, 1), 20)
        payload = {
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_usage": True,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "key-person-discovery/0.2",
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            response = _open_request(request, self.timeout, self.proxy_url)
            with response:
                body = json.load(response)
        except Exception as exc:
            detail = str(exc).replace(self.api_key, "[redacted]")
            raise RuntimeError(f"Tavily request failed for {query!r}: {detail}") from exc
        if not isinstance(body, dict):
            raise RuntimeError(f"Tavily request failed for {query!r}: invalid response")
        if body.get("error") or body.get("detail"):
            detail = body.get("error") or body["detail"]
            if isinstance(detail, dict):
                detail = detail.get("error") or detail.get("message") or "provider error"
            detail = str(detail).replace(self.api_key, "[redacted]")
            raise RuntimeError(f"Tavily request failed for {query!r}: {detail}")

        retrieved_at = _utc_now()
        results: list[SearchResult] = []
        for rank, item in enumerate(body.get("results", []), start=1):
            if not isinstance(item, dict):
                continue
            target = str(item.get("url", "")).strip()
            parsed = urlparse(target)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            results.append(
                SearchResult(
                    query=query,
                    title=str(item.get("title", "")).strip(),
                    url=target,
                    snippet=str(item.get("content", "")).strip(),
                    provider=self.name,
                    rank=rank,
                    retrieved_at=retrieved_at,
                )
            )
            if len(results) >= max_results:
                break
        return SearchOutcome(results=results)


class Crawl4aiCrawler:
    def __init__(self, proxy_url: str, timeout_ms: int = 45_000):
        if not proxy_url.strip():
            raise ValueError("Crawl4AI proxy URL is required")
        self.proxy_url = proxy_url.strip()
        self.timeout_ms = timeout_ms

    async def crawl(
        self,
        urls: list[str],
        allow_failed_content: bool = False,
    ) -> list[CrawledPage]:
        try:
            from crawl4ai import (
                AsyncWebCrawler,
                BrowserConfig,
                CacheMode,
                CrawlerRunConfig,
                ProxyConfig,
            )
        except ImportError as exc:
            raise RuntimeError("crawl4ai is not installed; run: pip install -e .") from exc

        try:
            proxy_config = ProxyConfig.from_string(self.proxy_url)
        except Exception as exc:
            raise RuntimeError("KEY_PERSON_PROXY_URL is invalid") from exc
        browser_config = BrowserConfig(headless=True, proxy_config=proxy_config)
        run_config = CrawlerRunConfig(
            cache_mode=CacheMode.ENABLED,
            check_robots_txt=True,
            page_timeout=self.timeout_ms,
            semaphore_count=4,
        )
        async with AsyncWebCrawler(config=browser_config) as crawler:
            try:
                results = await crawler.arun_many(urls=urls, config=run_config)
            except Exception as exc:
                return [
                    CrawledPage(url=url, markdown="", error=str(exc), provider="crawl4ai")
                    for url in urls
                ]
        pages = []
        for result in results:
            url = str(getattr(result, "url", ""))
            if not result.success:
                content = str(result.html or "") if allow_failed_content else ""
                pages.append(
                    CrawledPage(
                        url=url,
                        markdown=content,
                        error=str(result.error_message),
                        provider="crawl4ai",
                    )
                )
                continue
            markdown = result.markdown
            text = getattr(markdown, "raw_markdown", None) or str(markdown or "")
            pages.append(CrawledPage(url=url, markdown=text, provider="crawl4ai"))
        return pages


def crawl_sync(
    crawler: Crawl4aiCrawler,
    urls: list[str],
    allow_failed_content: bool = False,
    followup_urls: Callable[[list[CrawledPage]], list[str]] | None = None,
) -> list[CrawledPage]:
    async def run() -> list[CrawledPage]:
        async def crawl_with_retry(targets: list[str]) -> list[CrawledPage]:
            pages = await crawler.crawl(
                targets, allow_failed_content=allow_failed_content
            )
            failed_urls = [page.url for page in pages if page.error and page.url]
            if not failed_urls:
                return pages
            retried = await crawler.crawl(
                failed_urls, allow_failed_content=allow_failed_content
            )
            retry_by_url = {page.url: page for page in retried}
            return [
                retry_by_url.get(page.url, page) if page.error else page
                for page in pages
            ]

        pages = await crawl_with_retry(urls)
        if followup_urls:
            extra_urls = followup_urls(pages)
            if extra_urls:
                pages.extend(await crawl_with_retry(extra_urls))
        return pages

    return asyncio.run(run())


def discover_official_urls(
    company: CompanyProfile,
    proxy_url: str,
    limit: int = 12,
) -> tuple[list[str], list[str]]:
    parsed = urlparse(company.website)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return [], []
    root = f"{parsed.scheme}://{parsed.netloc}"
    sitemap_urls = [f"{root}/sitemap.xml"]
    warnings: list[str] = []
    try:
        robots = _fetch_text(f"{root}/robots.txt", proxy_url)
        sitemap_urls[:0] = re.findall(r"(?im)^\s*sitemap:\s*(https?://\S+)", robots)
    except Exception:
        pass

    locations: list[str] = []
    fetched: set[str] = set()
    for sitemap_url in sitemap_urls[:4]:
        if sitemap_url in fetched:
            continue
        fetched.add(sitemap_url)
        try:
            found = _sitemap_locations(_fetch_text(sitemap_url, proxy_url))
        except Exception as exc:
            warnings.append(f"{sitemap_url}: {exc}")
            continue
        nested = [url for url in found if urlparse(url).path.casefold().endswith(".xml")]
        locations.extend(url for url in found if url not in nested)
        for nested_url in nested[:3]:
            try:
                locations.extend(_sitemap_locations(_fetch_text(nested_url, proxy_url)))
            except Exception as exc:
                warnings.append(f"{nested_url}: {exc}")

    host = parsed.hostname.casefold().removeprefix("www.")
    selected = [company.website]
    for url in locations:
        target = urlparse(url)
        target_host = (target.hostname or "").casefold().removeprefix("www.")
        path = unquote(target.path).casefold()
        if target_host == host and (
            path.endswith(".pdf") or any(term in path for term in _OFFICIAL_PATH_TERMS)
        ):
            selected.append(url)
    return _dedupe_urls(selected)[:limit], warnings


def discover_official_links(
    company: CompanyProfile,
    pages: list[CrawledPage],
) -> list[str]:
    official_host = (urlparse(company.website).hostname or "").casefold().removeprefix("www.")
    if not official_host:
        return []
    urls = []
    for page in pages:
        for _, target in re.findall(r"\[([^\]]*)\]\(([^)\s]+)", page.markdown):
            url = urljoin(page.url, target)
            parsed = urlparse(url)
            host = (parsed.hostname or "").casefold().removeprefix("www.")
            path = unquote(parsed.path).casefold()
            if host == official_host and (
                path.endswith(".pdf") or any(term in path for term in _OFFICIAL_PATH_TERMS)
            ):
                urls.append(url)
    return _dedupe_urls(urls)


def _parse_anysearch_markdown(query: str, value: str) -> list[SearchResult]:
    headings = list(re.finditer(r"(?m)^###\s+(\d+)\.\s+(.+?)\s*$", value))
    retrieved_at = _utc_now()
    results: list[SearchResult] = []
    for index, heading in enumerate(headings):
        block_end = headings[index + 1].start() if index + 1 < len(headings) else len(value)
        block = value[heading.end() : block_end]
        url_match = re.search(r"(?m)^- \*\*URL\*\*:\s*(\S+)\s*$", block)
        if not url_match or urlparse(url_match.group(1)).scheme not in {"http", "https"}:
            continue
        snippet = next(
            (
                line[2:].strip()
                for line in block.splitlines()
                if line.startswith("- ") and not line.startswith("- **URL**:")
            ),
            "",
        )
        results.append(
            SearchResult(
                query=query,
                title=heading.group(2).strip(),
                url=url_match.group(1),
                snippet=snippet,
                provider="anysearch",
                rank=int(heading.group(1)),
                retrieved_at=retrieved_at,
            )
        )
    return results


def _sitemap_locations(value: str) -> list[str]:
    root = ElementTree.fromstring(value)
    urls = []
    for element in root.iter():
        if not element.tag.casefold().endswith("loc") or not element.text:
            continue
        url = element.text.strip()
        if urlparse(url).scheme in {"http", "https"}:
            urls.append(url)
    return _dedupe_urls(urls)


def _fetch_text(url: str, proxy_url: str, timeout: float = 12) -> str:
    request = Request(url, headers={"User-Agent": "key-person-discovery/0.2"})
    response = _open_request(request, timeout, proxy_url)
    with response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise RuntimeError("response exceeds 2 MB")
    return data.decode("utf-8", errors="replace")


def _open_request(request: Request, timeout: float, proxy_url: str):
    if urlparse(proxy_url).scheme in {"http", "https"}:
        opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
        return opener.open(request, timeout=timeout)
    return urlopen(request, timeout=timeout)


def _dedupe_urls(urls: list[str]) -> list[str]:
    return list(
        dict.fromkeys(
            urlparse(url.strip())._replace(fragment="").geturl()
            for url in urls
            if url.strip()
        )
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _unresponsive_engines(value: object) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        return []
    output: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, list) or len(item) < 2:
            continue
        name, reason = str(item[0]).strip(), str(item[1]).strip()
        if name:
            output.append((name, reason))
    return output
