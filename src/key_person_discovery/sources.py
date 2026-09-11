from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Callable, Protocol
from urllib.parse import unquote, urlencode, urljoin, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from xml.etree import ElementTree

from .models import CompanyProfile, CrawledPage, SearchOutcome, SearchResult, company_name_aliases, normalize_company_name, same_site
from .contact_evidence import contact_metadata


ANYSEARCH_ENDPOINT = "https://api.anysearch.com/mcp"
TAVILY_SEARCH_ENDPOINT = "https://api.tavily.com/search"
CONTACT_PATH_TERMS = (
    "brochure", "company-profile", "contact", "directory", "leadership", "management",
    "manual", "paia", "people", "popi", "profile", "staff", "supplier", "team",
    "kontakt", "ansprechpartner", "geschaeftsfuehr", "geschäftsführ", "einkauf",
    "vertrieb", "impressum", "standorte", "contato", "equipe", "contatti",
    "contacter", "einkaufs", "purchasing", "procurement", "yhteystiedot",
    "联系我们", "联系方式", "采购", "контакты",
)
_OFFICIAL_PATH_TERMS = CONTACT_PATH_TERMS + (
    "publication", "interview", "media-centre", "media-center", "press", "events",
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


def person_query_name(value: str) -> str:
    # Honorifics are not part of an exact-name search; keep particles and Unicode.
    return re.sub(r'^(?:(?:mr|mrs|ms|miss|herr|frau|dr|prof|dipl[.-]*ing)\.?\s+)+', '', value.replace('"', ' ').strip(), flags=re.I)


def normalize_person_name(value: str) -> str:
    return normalize_company_name(person_query_name(value))


def person_search_name(value: str) -> str:
    """Relax leading Latin initials for retrieval only; never use as an identity key."""
    name = person_query_name(value)
    surname = re.sub(r'^(?:[A-Za-z](?:\.\s*|\s+))+', '', name)
    return surname if len(normalize_company_name(surname).replace(' ', '')) > 1 else name


def build_people_queries(company: CompanyProfile, result: dict, phone_region: str | None = None) -> list[str]:
    # Reuse identity aliases: retrieval may use a short legal name, validation stays unchanged.
    aliases = sorted(company_name_aliases(company.name), key=lambda name: (len(name), name))[:3]
    name = "(" + " OR ".join(f'"{alias}"' for alias in aliases) + ")"
    local_roles = {
        "DE": ("Einkauf OR Beschaffung", "Technischer Leiter OR Betriebsleiter", "Geschäftsführer OR Inhaber"),
        "AT": ("Einkauf OR Beschaffung", "Technischer Leiter OR Betriebsleiter", "Geschäftsführer OR Inhaber"),
        "FR": ("achats OR acheteur", "directeur technique OR production", "directeur OR dirigeant"),
        "ES": ("compras OR comprador", "director técnico OR producción", "gerente OR director"),
        "BR": ("compras OR comprador", "engenheiro OR produção", "diretor OR proprietário"),
        "CN": ("采购 OR 供应链", "技术经理 OR 生产经理", "总经理 OR 董事长"),
        "JP": ("購買 OR 調達", "技術 OR 生産", "代表取締役 OR 社長"),
        "KR": ("구매 OR 조달", "기술 OR 생산", "대표 OR 사장"),
    }.get((phone_region or "").upper(), ("", "", ""))
    queries = []
    confirmed_leads = [
        candidate for candidate in result.get("candidates", [])
        if candidate.get('company_match') == 'verified' and not candidate.get('crm_existing_match')
        and not any(contact.get('value') and contact.get('status') != 'guessed'
                    for field in ("linkedin", "emails", "phones") for contact in candidate.get(field, []))
    ]
    if confirmed_leads:
        names = list(dict.fromkeys(person_search_name(str(p.get('full_name', ''))) for p in confirmed_leads))[:2]
        names = [person for person in names if person]
        # Spend the bounded budget on known people before broad role discovery.
        return ([f'"{person}" {name}' for person in names]
                + [f'site:linkedin.com/in "{person}" {name}' for person in names]
                + [f'"{person}" {name} filetype:pdf' for person in names])
    leads = result.get("unverified_candidates", [])
    for candidate in leads:
        person = person_search_name(str(candidate.get("full_name", "")))
        query = f'"{person}" {name} (current OR present OR email OR contact)'
        if person and query not in queries:
            queries.append(query)
        if len(queries) == 2:
            break
    for roles, local in zip((
        "procurement OR purchasing OR sourcing OR buyer",
        'engineer OR "technical manager" OR "plant manager" OR production',
        "owner OR director OR managing OR founder",
    ), local_roles):
        queries.append(f'site:linkedin.com/in {name} ({roles}{" OR " + local if local else ""})')
    return queries


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
        self._engine_failures: dict[str, int] = {}
        self._suspended_engines: set[str] = set()

    def search(self, query: str, limit: int = 5) -> SearchOutcome:
        if not self.base_url:
            raise RuntimeError("SEARXNG_URL is required")
        params = {"q": query, "format": "json"}
        if self.engines or self._suspended_engines:
            available = self.active_engines() - self._suspended_engines
            if not available:
                raise RuntimeError("All SearXNG engines suspended after repeated failures in this task")
            params["engines"] = ",".join(sorted(available))
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
            if not isinstance(item, dict):
                continue
            target = str(item.get("url", "")).strip()
            try:
                parsed = urlparse(target)
            except ValueError:
                continue
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
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
                    engines=[name for name in item.get("engines", []) if isinstance(name, str)]
                    if isinstance(item.get("engines"), list) else [],
                )
            )
        unresponsive = _unresponsive_engines(payload.get("unresponsive_engines"))
        failed = {name for name, _ in unresponsive}
        for name in failed:
            self._engine_failures[name] = self._engine_failures.get(name, 0) + 1
            if self._engine_failures[name] >= 2:
                self._suspended_engines.add(name)
        for item in results:
            for name in set(item.engines) - failed:
                self._engine_failures.pop(name, None)
        if not results and unresponsive:
            failed = {name for name, _ in unresponsive}
            active = self.active_engines()
            if active and active.issubset(failed):
                details = ", ".join(f"{name}: {reason}" for name, reason in unresponsive)
                raise RuntimeError(f"All active SearXNG engines failed for {query!r}: {details}")
        return SearchOutcome(results=results[:limit], unresponsive_engines=unresponsive,
                             raw_results=results, raw_response=payload)

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
        results = _parse_anysearch_markdown(query, text)
        return SearchOutcome(results=results[:limit], raw_results=results)


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
        return SearchOutcome(results=results[:max_results], raw_results=results)


class Crawl4aiCrawler:
    def __init__(self, proxy_url: str, timeout_ms: int = 45_000):
        if not proxy_url.strip():
            raise ValueError("Crawl4AI proxy URL is required")
        self.proxy_url = proxy_url.strip()
        self.timeout_ms = timeout_ms
        self.refresh_urls: set[str] = set()

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
                # Crawl4AI's current disk cache omits redirected_url. Refresh official entry
                # pages to establish the actual destination; keep ordinary-page caching.
                fresh = [url for url in urls if url in self.refresh_urls]
                cached = [url for url in urls if url not in self.refresh_urls]
                results = []
                if fresh:
                    results.extend(await crawler.arun_many(urls=fresh, config=run_config.clone(cache_mode=CacheMode.BYPASS)))
                if cached:
                    results.extend(await crawler.arun_many(urls=cached, config=run_config))
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
            blocks, conflicts = contact_metadata(str(result.html or ""))
            final_url = str(getattr(result, 'redirected_url', '') or url)
            if urlparse(final_url).scheme not in {'http', 'https'}:
                final_url = url
            links = [dict(href=str(item.get('href', '')), text=str(item.get('text', '')))
                     for group in (getattr(result, 'links', None) or {}).values()
                     for item in group if isinstance(item, dict) and item.get('href')]
            pages.append(CrawledPage(url=final_url, requested_url=url, markdown=text, provider="crawl4ai",
                                     title=str((getattr(result, 'metadata', None) or {}).get('title', '')),
                                     links=links, contact_blocks=blocks, contact_conflicts=conflicts))
        by_request = {page.requested_url or page.url: page for page in pages}
        return [by_request[url] for url in urls if url in by_request]


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
            retry_by_url = {page.requested_url or page.url: page for page in retried}
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
        if target_host == host and official_url_in_scope(company, url) and (
            path.endswith(".pdf") or any(term in path for term in _OFFICIAL_PATH_TERMS)
        ):
            selected.append(url)
    selected = sorted(_dedupe_urls(selected), key=lambda url: (url != company.website, official_link_priority(url)))
    return selected[:limit], warnings


def official_link_priority(url: str, text: str = '') -> int:
    value = unquote(urlparse(url).path + ' ' + text).casefold()
    if any(term in value for term in ('privacy', 'cookie', 'terms-and-conditions', 'thank-you', 'thankyou', 'submitted', 'confirmation')):
        return 3
    if any(term in value for term in CONTACT_PATH_TERMS):
        return 0
    return 1 if any(term in value for term in _OFFICIAL_PATH_TERMS) else 2


def page_links(page: CrawledPage) -> list[tuple[str, str]]:
    links = [(item.get('text', ''), item.get('href', '')) for item in page.links]
    # Remove inline image targets first so nested icons retain their outer page link.
    markdown = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', page.markdown)
    links += re.findall(r'\[([^\]]*)\]\(([^)\s]+)', markdown)
    return [(text, urljoin(page.url, href)) for text, href in links
            if urlparse(urljoin(page.url, href)).scheme in {'http', 'https'}
            and not unquote(urlparse(href).path).casefold().endswith(
                ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.css', '.js', '.woff', '.woff2'))]


def official_url_in_scope(company: CompanyProfile, url: str) -> bool:
    host = lambda value: (urlparse(value).hostname or '').casefold().removeprefix('www.')
    if not host(company.website) or host(company.website) != host(url):
        return False
    # A company page on a group site is a subtree, not permission to crawl siblings.
    path = urlparse(company.website).path.rstrip('/')
    if re.search(r'/(?:companies|empresas|subsidiaries)/[^/]+', path, re.I):
        target = urlparse(url).path.rstrip('/')
        return target == path or target.startswith(path + '/')
    return True


def resolve_official_site(company: CompanyProfile, pages: list[CrawledPage]) -> dict:
    """Resolve only observed redirects or explicitly named links, with heading evidence."""
    def normalized(text):
        return normalize_company_name(text.casefold().replace('ä', 'ae').replace('ö', 'oe').replace('ü', 'ue'))

    primary_name = re.sub(r'\([^)]*\)', '', company.name)
    aliases = {normalized(alias) for alias in company_name_aliases(primary_name)}
    # Keep the original spelling too; legal suffix stripping uses transliteration.
    aliases |= {normalized(primary_name)}
    aliases |= company_name_aliases(primary_name)
    def matches_heading(page):
        headings = page.title + '\n' + '\n'.join(re.findall(r'^#{1,2}\s+(.+)$', page.markdown, re.M))
        heading = ' ' + normalized(headings) + ' '
        if any(' ' + alias + ' ' in heading for alias in aliases):
            return True
        old_host = normalized(urlparse(company.website).hostname or '').replace(' ', '')
        new_host = normalized(urlparse(page.url).hostname or '').replace(' ', '')
        # A changed legal name may retain a distinctive brand in both hosts and heading.
        return any(len(token) >= 5 and token in old_host and token in new_host and
                   ' ' + token + ' ' in heading
                   for token in normalized(primary_name).split()
                   if token not in {'international', 'group', 'holding', 'limited', 'company'})

    start = next((p for p in pages if (p.requested_url or p.url).rstrip('/') == company.website.rstrip('/') and not p.error), None)
    result = dict(status='unchanged', website=company.website, followup_urls=[], blocked_urls=[])
    if not start or start.url.rstrip('/') == company.website.rstrip('/'):
        return result
    result.update(requested_url=company.website, final_url=start.url)
    same_location = official_url_in_scope(company, start.url) and (
        urlparse(company.website).path.rstrip('/') == urlparse(start.url).path.rstrip('/'))
    if same_location or matches_heading(start):
        result.update(status='confirmed_redirect', website=start.url)
        return result
    result.update(status='unverified_redirect', blocked_urls=[start.url])
    targets = [url for label, url in page_links(start)
               if normalized(label) in aliases and same_site(start.url, url)]
    result['followup_urls'] = _dedupe_urls(targets)[:2]
    for page in pages:
        if (page.requested_url or page.url) in targets and not page.error and matches_heading(page):
            result.update(status='confirmed_target_page', website=page.url)
            return result
    return result


def discover_official_links(
    company: CompanyProfile,
    pages: list[CrawledPage],
) -> list[str]:
    if not company.website:
        return []
    urls = []
    for page in pages:
        if page.error or not official_url_in_scope(company, page.url):
            continue
        for label, url in page_links(page):
            parsed = urlparse(url)
            path = unquote(parsed.path).casefold()
            if official_url_in_scope(company, url) and (
                path.endswith(".pdf") or official_link_priority(url, label) < 2
            ):
                urls.append((official_link_priority(url, label), url))
    return _dedupe_urls([url for _, url in sorted(urls, key=lambda item: item[0])])


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
