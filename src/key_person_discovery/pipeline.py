from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from .hermes import HermesExtractor, _evidence_binds_contact
from .models import (
    CompanyProfile,
    CrawledPage,
    SearchResult,
    canonical_contact_value,
    company_name_aliases,
    normalize_company_name,
    normalize_linkedin,
    same_site,
)
from .pdf_source import extract_pdf_urls
from .signals import extract_contact_signals
from .contact_evidence import contact_block_pages
from .diagnostics import diagnose, recovery_queries, extraction_failure
from .topeasy import merge_topeasy_export
from .sources import (
    Crawl4aiCrawler,
    SearchClient,
    build_customs_queries,
    build_official_query,
    build_queries,
    crawl_sync,
    discover_official_links,
    discover_official_urls,
    CONTACT_PATH_TERMS,
    official_link_priority,
    official_url_in_scope,
    page_links,
    resolve_official_site,
    person_query_name,
)


_TRUSTED_EXTERNAL_HOSTS = {
    "alcircle.com",
    "all.biz",
    "bloomberg.com",
    "cnpj.biz",
    "companieshouse.gov.uk",
    "crunchbase.com",
    "dnb.com",
    "ec21.com",
    "federalcompass.com",
    "glassglobal.com",
    "iranindustrial.com",
    "linkedin.com",
    "opencorporates.com",
    "rocketreach.co",
    "theorg.com",
    "tradekorea.com",
    "trademo.com",
    "zoominfo.com",
}

_CUSTOMS_DATA_HOSTS = {
    "importgenius.com",
    "panjiva.com",
    "trademo.com",
    "volza.com",
}
_CUSTOMS_TERMS = (
    "bill of lading",
    "consignee",
    "customs",
    "import",
    "shipment",
    "trade data",
)

_CONTACT_SOURCE_PATH_TERMS = CONTACT_PATH_TERMS

_EXTERNAL_SERVICE_MARKERS = (
    ("daro webdesign & entwicklung", "DARO Webdesign & Entwicklung"),
    ("kiwa international cert gmbh", "Kiwa International Cert GmbH"),
    (
        "certitut gesellschaft für compliance und datenschutz mbh",
        "certitut Gesellschaft für Compliance und Datenschutz mbH",
    ),
    ("compliance officer services legal", "Compliance Officer Services Legal"),
)


def discover(
    company: CompanyProfile,
    search_clients: list[SearchClient],
    crawler: Crawl4aiCrawler,
    hermes: HermesExtractor,
    artifacts_dir: Path,
    phone_region: str | None = None,
    max_urls: int = 20,
    proxy_url: str = "",
    crm_contacts: list[dict[str, Any]] | None = None,
    anysearch_query_limit: int | None = None,
    customs_anysearch_query_limit: int = 0,
    official_site_discovery: Callable[
        [CompanyProfile, str, int], tuple[list[str], list[str]]
    ] = discover_official_urls,
    broad_discovery: bool = False,
    topeasy_export: Path | None = None,
    people_search_limit: int = 0,
) -> dict[str, Any]:
    if anysearch_query_limit is not None and anysearch_query_limit < 0:
        raise ValueError("AnySearch query limit cannot be negative")
    if not 0 <= customs_anysearch_query_limit <= 3:
        raise ValueError("Customs AnySearch query limit must be between 0 and 3")
    if not 0 <= people_search_limit <= 6:
        raise ValueError("People search limit must be between 0 and 6")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.chmod(0o700)
    search_results = []
    search_warnings = []
    anysearch_queries = 0
    effective_anysearch_limit = anysearch_query_limit
    customs_anysearch_queries = 0
    executed_queries: list[str] = []
    executed_query_keys: set[str] = set()
    crm_website = company.website
    website_resolution_events: list[dict[str, Any]] = []
    routing_pages: list[CrawledPage] = []
    external_person_urls: set[str] = set()

    def run_search(client: SearchClient, query: str) -> Any:
        provider = getattr(client, "name", client.__class__.__name__)
        if broad_discovery and provider == "searxng":
            return client.search(query, limit=10)
        return client.search(query)

    def search(query: str) -> list[SearchResult]:
        nonlocal anysearch_queries
        query_key = query.strip()
        if not query_key or query_key in executed_query_keys:
            return []
        for client in search_clients:
            provider = getattr(client, "name", client.__class__.__name__)
            if provider == "anysearch":
                if (
                    effective_anysearch_limit is not None
                    and anysearch_queries >= effective_anysearch_limit
                ):
                    continue
                anysearch_queries += 1
            if query_key not in executed_query_keys:
                executed_query_keys.add(query_key)
                executed_queries.append(query)
            try:
                outcome = run_search(client, query)
            except RuntimeError as exc:
                search_warnings.append(
                    {
                        "query": query,
                        "provider": provider,
                        "engine": provider,
                        "reason": str(exc),
                    }
                )
                continue
            search_warnings.extend(
                {
                    "query": query,
                    "provider": provider,
                    "engine": engine,
                    "reason": reason,
                }
                for engine, reason in outcome.unresponsive_engines
            )
            if outcome.results:
                if not broad_discovery:
                    return outcome.results
                # Keep the first provider's ordering while retaining unique
                # URLs from later providers for the isolated experiment.
                merged: list[SearchResult] = []
                seen: set[str] = set()
                for item in outcome.results:
                    key = normalize_linkedin(item.url) or _url_key(item.url)
                    if key and key not in seen:
                        seen.add(key)
                        merged.append(item)
                for remaining_client in search_clients[search_clients.index(client) + 1 :]:
                    remaining_provider = getattr(
                        remaining_client,
                        "name",
                        remaining_client.__class__.__name__,
                    )
                    if remaining_provider == "anysearch":
                        if (
                            effective_anysearch_limit is not None
                            and anysearch_queries >= effective_anysearch_limit
                        ):
                            continue
                        anysearch_queries += 1
                    try:
                        remaining_outcome = run_search(remaining_client, query)
                    except RuntimeError as exc:
                        search_warnings.append(
                            {
                                "query": query,
                                "provider": remaining_provider,
                                "engine": remaining_provider,
                                "reason": str(exc),
                            }
                        )
                        continue
                    search_warnings.extend(
                        {
                            "query": query,
                            "provider": remaining_provider,
                            "engine": engine,
                            "reason": reason,
                        }
                        for engine, reason in remaining_outcome.unresponsive_engines
                    )
                    for item in remaining_outcome.results:
                        key = normalize_linkedin(item.url) or _url_key(item.url)
                        if key and key not in seen:
                            seen.add(key)
                            merged.append(item)
                return merged
        return []

    official_query = build_official_query(company)
    official_search_results = search(official_query)
    search_results.extend(official_search_results)
    if verified_website := _official_website_from_search(company, official_search_results):
        company = replace(company, website=verified_website)

    base_queries = build_queries(company)
    queries = [official_query, *base_queries]
    official_urls, official_warnings = official_site_discovery(company, proxy_url, max_urls)
    search_warnings.extend(
        {
            "query": company.website,
            "provider": "official_site",
            "engine": "sitemap",
            "reason": warning,
        }
        for warning in official_warnings
    )
    def crawl_sources(
        current_results: list[SearchResult],
    ) -> tuple[list[CrawledPage], list[CrawledPage], list[str]]:
        nonlocal company
        acquisition_company = company
        if isinstance(crawler, Crawl4aiCrawler):
            crawler.refresh_urls.add(company.website)
        all_candidate_urls = _unique_urls(
            company.website,
            _linkedin_company_urls(company.linkedin_url)
            + (
                official_urls
                if _distinctive_company_url(company, company.website)
                else []
            )
            + _relevant_urls(company, current_results),
        )
        current_pdf_urls = sorted(
            (url for url in all_candidate_urls if _is_pdf_url(url)),
            key=lambda url: _contact_source_priority(company, url),
        )
        candidate_urls = sorted(
            (
                url
                for url in all_candidate_urls
                if not _is_pdf_url(url) and not _is_linkedin_profile(url)
            ),
            key=lambda url: _contact_source_priority(company, url),
        )
        reserved = min(5, max_urls // 2)
        first_urls = candidate_urls[: max_urls - reserved]
        first_keys = {_url_key(url) for url in first_urls}
        linked_pdf_urls: list[str] = []

        def followup_urls(first_pages):
            nonlocal company
            resolution = resolve_official_site(acquisition_company, first_pages)
            if resolution['status'].startswith('confirmed_'):
                company = replace(company, website=resolution['website'])
            elif resolution['status'] == 'unverified_redirect':
                return resolution['followup_urls'][:max_urls - len(first_urls)]
            # Redirected final URLs count as already fetched too.
            first_keys.update(_url_key(page.url) for page in first_pages)
            link_priorities = {url: official_link_priority(url, label)
                               for page in first_pages for label, url in page_links(page)}
            second_candidates = sorted(
                discover_official_links(company, first_pages)
                + [url for url in candidate_urls[len(first_urls):]
                   if company == acquisition_company or official_url_in_scope(company, url)],
                key=lambda url: (_contact_source_priority(company, url)[0],
                                 min(_contact_source_priority(company, url)[1], link_priorities.get(url, 2))),
            )
            linked_pdf_urls.extend(
                url for url in second_candidates if _is_pdf_url(url)
            )
            return [
                url
                for url in _unique_urls("", second_candidates)
                if _url_key(url) not in first_keys
                and not _is_pdf_url(url)
            ][: max_urls - len(first_urls)]

        current_pages = (
            crawl_sync(crawler, first_urls, followup_urls=followup_urls)
            if first_urls
            else []
        )
        resolution = resolve_official_site(acquisition_company, current_pages)
        if resolution['status'] != 'unchanged':
            website_resolution_events.append(resolution)
            if resolution['status'].startswith('confirmed_'):
                company = replace(company, website=resolution['website'])
            blocked = set(resolution['blocked_urls'])
            routing_pages.extend(page for page in current_pages if page.url in blocked)
            current_pages = [replace(page, markdown='', contact_blocks=[], contact_conflicts=[],
                                     error='target company not verified on redirected page')
                             if page.url in blocked else page for page in current_pages]
            current_pdf_urls = [url for url in current_pdf_urls if official_url_in_scope(company, url)]
        current_pdf_urls = sorted(
            _unique_urls("", current_pdf_urls + linked_pdf_urls),
            key=lambda url: _contact_source_priority(company, url),
        )
        return (
            current_pages,
            extract_pdf_urls(current_pdf_urls, proxy_url=proxy_url),
            current_pdf_urls,
        )

    def evidence_context(
        current_pages: list[CrawledPage],
        current_pdf_pages: list[CrawledPage],
        current_results: list[SearchResult],
    ) -> tuple[
        list[CrawledPage],
        list[CrawledPage],
        list[dict[str, Any]],
        list[CrawledPage],
    ]:
        current_search_evidence = _search_evidence_pages(
            company, current_results
        )
        current_contact_evidence = _contact_evidence_pages(
            company, current_results
        )
        current_evidence_pages = (
            contact_block_pages(company, current_pages)
            + current_pages
            + current_pdf_pages
            + current_search_evidence
            + current_contact_evidence
        )
        current_signals = extract_contact_signals(
            current_evidence_pages, phone_region
        )
        contact_source_pages = current_pages + current_pdf_pages + current_contact_evidence
        company_public_urls = {
            _url_key(page.url)
            for page in contact_source_pages
            if page.markdown and _url_key(page.url) not in external_person_urls and _page_is_company_public(company, page)
        }
        company_associated_urls = {
            _url_key(page.url)
            for page in contact_source_pages
            if page.markdown
            and _url_key(page.url) not in external_person_urls
            and not _is_trusted_external(page.url)
            and _page_matches_company(company, page)
        }
        for signal in current_signals:
            source_key = _url_key(str(signal.get("source_url", "")))
            if source_key in company_public_urls:
                signal["company_public"] = True
            elif source_key in company_associated_urls:
                signal["company_association"] = "probable"
        return (
            current_search_evidence,
            current_contact_evidence,
            current_signals,
            current_evidence_pages,
        )

    pages: list[CrawledPage]
    pdf_pages: list[CrawledPage]
    pdf_urls: list[str]
    search_evidence: list[CrawledPage]
    contact_evidence: list[CrawledPage]
    signals: list[dict[str, Any]]
    evidence_pages: list[CrawledPage]
    if anysearch_query_limit == 7:
        for query in base_queries[:4]:
            search_results.extend(search(query))
        pages, pdf_pages, pdf_urls = crawl_sources(search_results)
        (
            search_evidence,
            contact_evidence,
            signals,
            evidence_pages,
        ) = evidence_context(pages, pdf_pages, search_results)

        seed_probe: dict[str, Any] = {
            "candidates": [],
            "unassigned_contacts": [],
        }
        _add_linkedin_signal_candidates(seed_probe, company, signals)
        seeds = [
            candidate
            for candidate in seed_probe.get("unverified_candidates", [])
            if isinstance(candidate, dict)
        ][:2]
        dynamic_queries: list[str] = []
        for seed in seeds:
            name = str(seed.get("full_name", "")).strip()
            if not name:
                continue
            dynamic_queries.append(
                f'site:linkedin.com/in "{name}" "{company.name}"'
            )
        if len(seeds) == 1:
            name = str(seeds[0].get("full_name", "")).strip()
            dynamic_queries.append(
                f'"{name}" "{company.name}" '
                "(current OR present OR director OR owner)"
            )
        if seeds:
            effective_anysearch_limit = 7
            for query in dynamic_queries[:2]:
                if query not in queries:
                    queries.append(query)
                search_results.extend(search(query))
        elif (
            not _search_evidence_pages(company, search_results)
            and not _contact_evidence_pages(company, search_results)
        ):
            effective_anysearch_limit = 5

        for query in base_queries[4:]:
            search_results.extend(search(query))
        (
            search_evidence,
            contact_evidence,
            signals,
            evidence_pages,
        ) = evidence_context(pages, pdf_pages, search_results)
    else:
        for query_number, query in enumerate(queries[1:], start=2):
            search_results.extend(search(query))
            if query_number == 5 and anysearch_query_limit == 7:
                if (
                    not _search_evidence_pages(company, search_results)
                    and not _contact_evidence_pages(company, search_results)
                ):
                    effective_anysearch_limit = 5
        pages, pdf_pages, pdf_urls = crawl_sources(search_results)
        (
            search_evidence,
            contact_evidence,
            signals,
            evidence_pages,
        ) = evidence_context(pages, pdf_pages, search_results)

    customs_leads: list[dict[str, Any]] = []
    customs_client = next(
        (
            client
            for client in search_clients
            if getattr(client, "name", client.__class__.__name__) == "anysearch"
        ),
        None,
    )
    if company.customs_search_enabled and customs_client and customs_anysearch_query_limit:
        for query in build_customs_queries(company)[:customs_anysearch_query_limit]:
            customs_anysearch_queries += 1
            try:
                outcome = customs_client.search(query)
            except RuntimeError as exc:
                search_warnings.append(
                    {
                        "query": query,
                        "provider": "anysearch",
                        "engine": "anysearch",
                        "purpose": "customs",
                        "reason": str(exc),
                    }
                )
                break
            current_leads = _customs_leads(company, outcome.results)
            if not current_leads:
                break
            customs_leads = _merge_customs_leads(customs_leads, current_leads)

    def save_evidence():
        _write_json(artifacts_dir / 'website-resolution.json', website_resolution_events)
        _write_json(artifacts_dir / 'website-routing-pages.json', [page.as_dict() for page in routing_pages])
        _write_json(artifacts_dir / "search-results.json", [item.as_dict() for item in search_results])
        _write_json(artifacts_dir / "customs-search-results.json", customs_leads)
        _write_json(artifacts_dir / "search-warnings.json", search_warnings)
        _write_json(artifacts_dir / "pages.json", [page.as_dict() for page in pages])
        _write_json(artifacts_dir / "pdf-pages.json", [page.as_dict() for page in pdf_pages])
        _write_json(
            artifacts_dir / "search-evidence.json",
            [page.as_dict() for page in search_evidence],
        )
        _write_json(
            artifacts_dir / "contact-evidence.json",
            [page.as_dict() for page in contact_evidence],
        )
        _write_json(artifacts_dir / "contact-signals.json", signals)
        _write_json(artifacts_dir / "contact-conflicts.json", [
            dict(item, source_url=page.url) for page in pages for item in page.contact_conflicts
        ])

    save_evidence()

    def diagnostic_for(value):
        return diagnose(value, pages + pdf_pages + search_evidence + contact_evidence,
            website=company.website, website_status=website_resolution_events[-1]['status'] if website_resolution_events else '',
            target=max(1, company.target_contact_count), contactable_people=_new_contactable_people(value))

    result = dict(company_name=company.name, candidates=[], unassigned_contacts=[], review_required=True)
    if any(page.markdown.strip() and not page.error for page in evidence_pages):
        try:
            result = hermes.extract(company, evidence_pages, signals, artifacts_dir / "usage.json")
        except Exception as exc:
            diagnostic = diagnostic_for(result)
            diagnostic['primary'] = 'analysis_failed'
            diagnostic['reasons'] = [reason for reason in diagnostic['reasons'] if reason != 'no_target_people']
            diagnostic['error'] = extraction_failure(exc, 'initial_analysis')
            _write_json(artifacts_dir / 'diagnostics.json', diagnostic)
            raise
    topeasy_summary: dict[str, Any] = {}

    def finalize_people(result, include_topeasy=False):
        nonlocal topeasy_summary
        _add_linkedin_employee_candidates(result, company, pages)
        _verify_linkedin_anchored_candidates(result, company, search_results)
        _add_linkedin_signal_candidates(result, company, signals)
        if broad_discovery:
            _add_linkedin_search_candidates(result, company, search_results)
        _promote_linkedin_signal_candidates(result, company, search_results)
        if broad_discovery:
            _normalize_broad_discovery_candidates(result, company, search_results)
        if include_topeasy and topeasy_export is not None:
            topeasy_summary = merge_topeasy_export(result, company, topeasy_export)
            _write_json(artifacts_dir / "topeasy-import.json", topeasy_summary)
        _demote_navigation_only_company_matches(result, company, pages)
        _exclude_unrelated_roles(result)
        _attach_evidence_bound_contacts(result, {
            page.url.rstrip('/'): page.contact_blocks for page in pages
            if page.contact_blocks and same_site(company.website, page.url)
        })
        _attach_crm_matched_emails(result, crm_contacts or [])
        _deduplicate_crm_contacts(result, crm_contacts or [])
        if broad_discovery:
            _add_inferred_email_candidates(result, company, signals, crm_contacts or [])
        _filter_directory_footer_contacts(
            result,
            pages + contact_evidence,
            [
                url
                for url in official_urls + [company.website] + [page.url for page in pages]
                if same_site(company.website, url) or _distinctive_company_host(company, url)
            ],
            company,
        )
        _remove_conflicting_contacts(result, signals)

    finalize_people(result, include_topeasy=True)
    if people_search_limit:
        _attach_named_linkedin_profiles(result, company, search_results, crm_contacts or [])
    people_before_topup = _new_contactable_people(result)
    people_queries: list[str] = []
    people_anysearch_queries = 0
    people_target = max(1, company.target_contact_count)
    initial_diagnostic = diagnostic_for(result)
    topup_plan = [q for q in recovery_queries(company, result, initial_diagnostic, phone_region)
                  if q.strip() not in executed_query_keys][:people_search_limit]
    topup = dict(reason=initial_diagnostic['primary'], budget=people_search_limit, queries=[],
                 status='disabled' if not people_search_limit else 'target_met' if people_before_topup >= people_target else 'skipped')
    if people_search_limit and people_before_topup < people_target and topup_plan:
        _write_json(artifacts_dir / "people-baseline.json", result)
        # A separate, bounded budget; explicitly disabling AnySearch also disables it here.
        if anysearch_query_limit is not None and anysearch_query_limit > 0:
            effective_anysearch_limit = anysearch_queries + people_search_limit
        previous_anysearch_queries = anysearch_queries
        previous_results = len(search_results)
        topup_stage = 'search'
        try:
            for query in topup_plan:
                if query not in executed_query_keys:
                    try:
                        search_results.extend(search(query))
                    finally:
                        if query.strip() in executed_query_keys:
                            people_queries.append(query)
            extra_results = search_results[previous_results:]
            topup_stage = 'crawl'
            existing_urls = {_url_key(page.url) for page in pages + pdf_pages}
            named_urls = _named_person_source_urls(company, result, extra_results)
            external_person_urls.update(_url_key(url) for url in named_urls if not same_site(company.website, url))
            selected_urls = _unique_urls('', named_urls + _relevant_urls(company, extra_results))
            extra_pdf_urls = [url for url in selected_urls if _is_pdf_url(url) and _url_key(url) not in existing_urls][:max(0, 3 - len(pdf_pages))]
            extra_urls = [
                url for url in selected_urls
                if _url_key(url) not in existing_urls
                and not _is_linkedin_profile(url) and not _is_pdf_url(url)
            ][:min(6, max(0, max_urls - len(pages)))]
            extra_pages = crawl_sync(crawler, extra_urls) if extra_urls else []
            previous_evidence = {(page.url, page.markdown) for page in evidence_pages}
            # A redirect does not make an external person source a company-wide directory.
            external_person_urls.update(_url_key(page.url) for page in extra_pages
                                        if _url_key(page.requested_url or page.url) in external_person_urls)
            pages.extend(extra_pages)
            if extra_pdf_urls:
                pdf_urls.extend(extra_pdf_urls)
                pdf_pages.extend(extract_pdf_urls(extra_pdf_urls, proxy_url=proxy_url))
            search_evidence, contact_evidence, signals, evidence_pages = evidence_context(pages, pdf_pages, extra_results + search_results[:previous_results])
            # Repeated search hits alone do not justify another model call.
            if any(page.markdown.strip() and not page.error and (page.url, page.markdown) not in previous_evidence for page in evidence_pages):
                save_evidence()
                # New evidence goes first so the existing prompt size cap cannot hide it.
                ordered_pages = sorted(evidence_pages, key=lambda page: (page.url, page.markdown) in previous_evidence)
                topup_stage = 'analysis'
                extra_result = hermes.extract(company, ordered_pages, signals, artifacts_dir / "usage-people-topup.json")
                topup_stage = 'validation'
                finalize_people(extra_result)
                result = _merge_people_results(result, extra_result)
                topup['status'] = 'completed'
            else:
                topup['status'] = 'no_new_evidence'
        except Exception as exc:
            # Optional enrichment must not discard a usable, validated first pass.
            search_warnings.append({"provider": "people_topup", "engine": "people_topup", "reason": type(exc).__name__})
            topup['status'] = 'failed'
            topup['error'] = extraction_failure(exc, topup_stage)
        people_anysearch_queries = anysearch_queries - previous_anysearch_queries
        save_evidence()
    if people_search_limit:
        _attach_named_linkedin_profiles(result, company, search_results, crm_contacts or [])
    topup['queries'] = people_queries
    topup['new_results'] = len(search_results) - previous_results if people_queries else 0
    topup['source_warnings'] = sum(w.get('query') in people_queries for w in search_warnings)
    if topup['status'] == 'no_new_evidence' and not topup['new_results'] and topup['source_warnings']:
        topup['status'] = 'source_limited'
    # A later fetch can expose conflicts in channels retained by the first pass.
    _remove_conflicting_contacts(result, signals)
    result['diagnostics'] = diagnostic_for(result)
    result['diagnostics'].update(before=initial_diagnostic, topup=topup)
    _write_json(artifacts_dir / 'diagnostics.json', result['diagnostics'])
    _write_json(artifacts_dir / "people-search.json", {
        "queries": people_queries, "people_before": people_before_topup,
        "people_after": _new_contactable_people(result), "target": people_target,
    })
    _write_json(artifacts_dir / "crm-duplicates.json", result.get("crm_duplicates", []))
    contactable_items = _contactable_items(result)
    contact_methods = _contact_method_count(result)
    customs_has_product = any(item["matched_products"] for item in customs_leads)
    result["customs"] = {
        "status": (
            "evidence_found"
            if customs_has_product
            else "lead_requires_review"
            if customs_leads
            else "no_public_lead"
            if customs_anysearch_queries
            else "not_selected"
            if not company.customs_search_enabled
            else "disabled"
        ),
        "tags": (
            ["海关采购证据"]
            if customs_has_product
            else ["海关记录待核实"]
            if customs_leads
            else []
        ),
        "leads": customs_leads,
    }
    result["run_summary"] = {
        "queries": len(executed_queries),
        "anysearch_queries": anysearch_queries,
        "anysearch_query_limit": anysearch_query_limit,
        "people_search_limit": people_search_limit,
        "people_search_queries": len(people_queries),
        "people_anysearch_queries": people_anysearch_queries,
        "people_before_topup": people_before_topup,
        "new_contactable_people": _new_contactable_people(result),
        "people_topup_gain": _new_contactable_people(result) - people_before_topup,
        "target_new_people": people_target,
        "anysearch_extended": anysearch_queries > 5,
        "customs_anysearch_queries": customs_anysearch_queries,
        "customs_search_selected": company.customs_search_enabled,
        "customs_leads": len(customs_leads),
        "search_results": len(search_results),
        "search_engine_warnings": len(search_warnings),
        "urls_crawled": len(pages),
        "crawl_failures": sum(bool(page.error) for page in pages),
        "contact_signals": len(signals),
        "contact_card_blocks": sum(len(page.contact_blocks) for page in pages),
        "contact_channel_conflicts": sum(len(page.contact_conflicts) for page in pages),
        "excluded_role_candidates": len(result.get('excluded_candidates', [])),
        "official_urls_discovered": len(official_urls),
        "crm_website": crm_website,
        "official_website": company.website,
        "website_recovered": bool(company.website and company.website != crm_website),
        "website_resolution": website_resolution_events[-1]['status'] if website_resolution_events else 'unchanged',
        "pdf_documents_discovered": len(pdf_urls),
        "pdf_documents_processed": len(pdf_pages),
        "pdf_documents_extracted": sum(bool(page.markdown) for page in pdf_pages),
        "pdf_pages_extracted": sum(page.page_count for page in pdf_pages),
        "pdf_failures": sum(bool(page.error) for page in pdf_pages),
        "search_evidence_pages": len(search_evidence),
        "contact_evidence_pages": len(contact_evidence),
        "search_providers": [
            getattr(client, "name", client.__class__.__name__) for client in search_clients
        ],
        "validation_rejections": len(result.get("validation_rejections", [])),
        "unverified_candidates": len(result.get("unverified_candidates", [])),
        "crm_contact_count": company.crm_contact_count,
        "crm_duplicates_removed": result.get("crm_duplicates_removed", 0),
        "target_contactable_items": company.target_contact_count,
        "contactable_items": contactable_items,
        "target_contact_methods": company.target_contact_count,
        "contact_methods": contact_methods,
        "contact_target_met": not company.target_contact_count or contact_methods >= company.target_contact_count,
        "broad_discovery": broad_discovery,
        "topeasy_import": topeasy_summary,
    }
    return result


_LINKEDIN_NAME_PARTICLES = {
    "da",
    "de",
    "del",
    "den",
    "der",
    "di",
    "do",
    "dos",
    "du",
    "la",
    "le",
    "van",
    "von",
}
_LINKEDIN_NAME_GENERIC_TOKENS = {
    "admin",
    "buyer",
    "company",
    "contact",
    "director",
    "employee",
    "employees",
    "engineer",
    "manager",
    "member",
    "members",
    "official",
    "people",
    "person",
    "profile",
    "procurement",
    "purchasing",
    "quality",
    "search",
    "sales",
    "sourcing",
    "technical",
    "unknown",
    "user",
}


def _add_linkedin_signal_candidates(
    result: dict[str, Any],
    company: CompanyProfile,
    signals: list[dict[str, Any]],
) -> None:
    """Add conservative website-linked LinkedIn profiles for manual review."""

    existing_names: set[str] = set()
    existing_linkedin: set[str] = set()
    for group in ("candidates", "unverified_candidates"):
        for candidate in result.get(group, []) or []:
            if not isinstance(candidate, dict):
                continue
            normalized_name = normalize_company_name(
                str(candidate.get("full_name", ""))
            )
            if normalized_name:
                existing_names.add(normalized_name)
            for contact in candidate.get("linkedin", []) or []:
                if isinstance(contact, dict):
                    normalized_linkedin = normalize_linkedin(
                        str(contact.get("value", ""))
                    )
                    if normalized_linkedin:
                        existing_linkedin.add(normalized_linkedin)

    added: list[dict[str, Any]] = []
    for signal in signals:
        if not isinstance(signal, dict) or signal.get("channel") != "linkedin":
            continue
        if signal.get("company_public") is not True:
            continue
        source_url = str(signal.get("source_url", "")).strip()
        if (
            not source_url
            or not same_site(company.website, source_url)
            or _is_linkedin_host(source_url)
        ):
            continue
        profile_url = normalize_linkedin(str(signal.get("value", "")))
        if not profile_url or not _is_linkedin_profile(profile_url):
            continue
        name = _linkedin_name_from_profile(profile_url)
        normalized_name = normalize_company_name(name)
        if (
            not name
            or normalized_name in company_name_aliases(company.name)
            or normalized_name in existing_names
            or profile_url in existing_linkedin
        ):
            continue
        candidate = {
            "full_name": name,
            "current_title": "",
            "company_match": "probable",
            "influence_type": "other",
            "influence_score": 0,
            "linkedin": [
                {
                    "value": profile_url,
                    "status": "observed",
                    "source_url": source_url,
                }
            ],
            "emails": [],
            "phones": [],
            "evidence": [
                {
                    "source_url": source_url,
                    "quote": f"Official website links to {profile_url}",
                    "supports": (
                        "官网链接该个人主页，但当前任职未验证 "
                        "(official website links to this LinkedIn profile; "
                        "current employment not verified)"
                    ),
                }
            ],
            "confidence": 0.3,
            "review_required": True,
            "validation_reasons": ["current employment not verified"],
        }
        added.append(candidate)
        existing_names.add(normalized_name)
        existing_linkedin.add(profile_url)

    if added:
        result.setdefault("unverified_candidates", []).extend(added)
    assigned_linkedin = {
        normalize_linkedin(str(contact.get("value", "")))
        for group in ("candidates", "unverified_candidates")
        for candidate in result.get(group, []) or []
        if isinstance(candidate, dict)
        for contact in candidate.get("linkedin", []) or []
        if isinstance(contact, dict)
        and normalize_linkedin(str(contact.get("value", "")))
    }
    if "unassigned_contacts" not in result:
        return
    result["unassigned_contacts"] = [
        contact
        for contact in result.get("unassigned_contacts", []) or []
        if not (
            isinstance(contact, dict)
            and contact.get("channel") == "linkedin"
            and normalize_linkedin(str(contact.get("value", "")))
            in assigned_linkedin
        )
    ]


_BROAD_DISCOVERY_ROLE_EVIDENCE = re.compile(
    r"\b(?:procurement|purchasing|buyer|sourcing|supply\s+chain|"
    r"owner|founder|chief|ceo|cfo|coo|cto|executive|director|president|"
    r"general\s+manager|plant|site|works|operations?|smelter|mine|"
    r"production|quality|maintenance|technical|engineering|engineer|"
    r"metallurg(?:y|ist)|refractory|foundry|ceramic)\b",
    re.I,
)
_FORMER_EMPLOYMENT_TERMS = re.compile(
    r"\b(?:former|ex|previous|past|prior|formerly)\b", re.I
)
_PUBLIC_EMAIL_LOCALS = {
    "accounts",
    "admin",
    "billing",
    "career",
    "careers",
    "contact",
    "customerservice",
    "enquiries",
    "export",
    "general",
    "hello",
    "hr",
    "info",
    "inquiry",
    "jobs",
    "logistics",
    "mail",
    "marketing",
    "office",
    "orders",
    "purchasing",
    "reception",
    "sales",
    "service",
    "shipping",
    "support",
    "team",
}


def _add_linkedin_search_candidates(
    result: dict[str, Any],
    company: CompanyProfile,
    search_results: list[SearchResult],
) -> None:
    """Keep target-role LinkedIn hits; current-employment proof only raises rank."""

    existing_names: set[str] = set()
    existing_linkedin: set[str] = set()
    for group in ("candidates", "unverified_candidates"):
        for candidate in result.get(group, []) or []:
            if not isinstance(candidate, dict):
                continue
            name = normalize_company_name(str(candidate.get("full_name", "")))
            if name:
                existing_names.add(name)
            for contact in candidate.get("linkedin", []) or []:
                if isinstance(contact, dict):
                    value = normalize_linkedin(str(contact.get("value", "")))
                    if value:
                        existing_linkedin.add(value)

    added: list[dict[str, Any]] = []
    for item in search_results:
        if not _is_linkedin_profile(item.url):
            continue
        if _has_conflicting_labeled_employer(company, item.snippet) or (
            _company_mention_only_in_related_profiles(
                company, item.title, item.snippet
            )
        ):
            continue
        text = f"{item.title} {item.snippet}"
        probable_current = _has_probable_current_employment(
            company, item.title, item.snippet
        )
        if not probable_current and not _has_exact_company_association(
            company, item.title, item.snippet
        ):
            continue
        if _former_employment_near_company(company, text):
            continue
        profile_url = normalize_linkedin(item.url)
        name = _linkedin_name_from_profile(profile_url) or _linkedin_search_title_name(item.title)
        normalized_name = normalize_company_name(name)
        if (
            not profile_url
            or not name
            or not normalized_name
            or any(
                normalized_name == alias or normalized_name.startswith(f"{alias} ")
                for alias in company_name_aliases(company.name)
            )
            or normalized_name in existing_names
            or profile_url in existing_linkedin
        ):
            continue
        title = _search_result_role_title(company, item)
        role_text = title or item.title
        evidence_quote = text.strip()[:500]
        added.append(
            {
                "full_name": name,
                "current_title": title,
                "company_match": "probable" if probable_current else "uncertain",
                "discovery_tier": "probable_current" if probable_current else "unverified",
                "influence_type": "technical_influencer"
                if re.search(
                    r"\b(?:technical|engineering|engineer|metallurg|refractory|foundry|ceramic|production|quality|maintenance)\b",
                    role_text,
                    re.I,
                )
                else "decision_maker"
                if _BROAD_DISCOVERY_ROLE_EVIDENCE.search(role_text)
                else "other",
                "influence_score": 0,
                "linkedin": [
                    {
                        "value": profile_url,
                        "status": "probable",
                        "source_url": item.url,
                    }
                ],
                "emails": [],
                "phones": [],
                "evidence": [
                    {
                        "source_url": item.url,
                        "quote": evidence_quote,
                        "supports": (
                            "Search excerpt names the person and exact target company; "
                            "current employment, role, and contact ownership still "
                            "require independent verification"
                        ),
                    }
                ],
                "confidence": 0.6 if probable_current else 0.35,
                "review_required": True,
                "validation_reasons": [
                    (
                        "search summary requires independent current-employment verification"
                        if probable_current
                        else "target-company association is visible but current employment is unverified"
                    ),
                    "contact ownership is not verified",
                ],
            }
        )
        existing_names.add(normalized_name)
        existing_linkedin.add(profile_url)
    if added:
        result.setdefault("unverified_candidates", []).extend(added)


def _normalize_broad_discovery_candidates(
    result: dict[str, Any],
    company: CompanyProfile,
    search_results: list[SearchResult],
) -> None:
    """Remove non-people and apply consistent review tiers after broad discovery."""

    retained_verified: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    for candidate in result.get("candidates", []) or []:
        if not isinstance(candidate, dict):
            continue
        if _has_only_stale_pdf_employment_evidence(candidate):
            candidate["company_match"] = "probable"
            candidate["review_required"] = True
            candidate.setdefault("validation_reasons", []).append(
                "only stale PDF role evidence is available; current employment requires review"
            )
            demoted.append(candidate)
        else:
            retained_verified.append(candidate)
    result["candidates"] = retained_verified
    if demoted:
        result.setdefault("unverified_candidates", []).extend(demoted)

    seen_linkedin_urls: set[str] = set()
    seen_linkedin_ids: set[str] = set()
    for group in ("candidates", "unverified_candidates"):
        kept: list[dict[str, Any]] = []
        for candidate in result.get(group, []) or []:
            if not isinstance(candidate, dict):
                continue
            name = str(candidate.get("full_name", "")).strip()
            evidence_text = " ".join(
                f"{item.get('quote', '')} {item.get('supports', '')}"
                for item in candidate.get("evidence", []) or []
                if isinstance(item, dict)
            )
            if name.casefold().startswith("unknown") or re.search(
                r"\b(?:job[_ -]?posting|current vacancy|listed as (?:a )?vacancy)\b",
                evidence_text,
                re.I,
            ):
                continue

            linkedin_urls = {
                normalize_linkedin(str(contact.get("value", "")))
                for contact in candidate.get("linkedin", []) or []
                if isinstance(contact, dict)
                and normalize_linkedin(str(contact.get("value", "")))
            }
            linkedin_ids = {
                stable_id
                for url in linkedin_urls
                if (stable_id := _linkedin_profile_stable_id(url))
            }
            if linkedin_urls & seen_linkedin_urls or linkedin_ids & seen_linkedin_ids:
                continue

            if group == "candidates" and candidate.get("company_match") == "verified":
                candidate["discovery_tier"] = "verified_current"
                kept.append(candidate)
                seen_linkedin_urls.update(linkedin_urls)
                seen_linkedin_ids.update(linkedin_ids)
                continue

            matched_result = _candidate_linkedin_search_result(
                candidate, company, search_results
            )
            if matched_result is not None and (
                _has_conflicting_labeled_employer(company, matched_result.snippet)
                or _company_mention_only_in_related_profiles(
                    company, matched_result.title, matched_result.snippet
                )
            ):
                continue
            if (
                matched_result is not None
                and _has_probable_current_employment(
                    company, matched_result.title, matched_result.snippet
                )
                and not _former_employment_near_company(
                    company, f"{matched_result.title} {matched_result.snippet}"
                )
                and _BROAD_DISCOVERY_ROLE_EVIDENCE.search(
                    f"{matched_result.title} {matched_result.snippet}"
                )
            ):
                candidate["company_match"] = "probable"
                candidate["discovery_tier"] = "probable_current"
                current_title = str(candidate.get("current_title", "")).strip()
                if not current_title or current_title.casefold().startswith("unknown"):
                    title = _search_result_role_title(company, matched_result)
                    if title:
                        candidate["current_title"] = title
            else:
                candidate["discovery_tier"] = "unverified"
            kept.append(candidate)
            seen_linkedin_urls.update(linkedin_urls)
            seen_linkedin_ids.update(linkedin_ids)
        result[group] = kept


def _has_only_stale_pdf_employment_evidence(candidate: dict[str, Any]) -> bool:
    evidence = [
        item
        for item in candidate.get("evidence", []) or []
        if isinstance(item, dict)
    ]
    if not evidence or any(
        ".pdf" not in str(item.get("source_url", "")).casefold()
        for item in evidence
    ):
        return False
    evidence_text = " ".join(
        f"{item.get('source_url', '')} {item.get('quote', '')} {item.get('supports', '')}"
        for item in evidence
    )
    years = [int(value) for value in re.findall(r"\b20\d{2}\b", evidence_text)]
    if not years or max(years) > date.today().year - 2:
        return False
    return not re.search(
        r"\b(?:current|currently|present|since|appointed)\b",
        evidence_text,
        re.I,
    )


def _candidate_linkedin_search_result(
    candidate: dict[str, Any],
    company: CompanyProfile,
    search_results: list[SearchResult],
) -> SearchResult | None:
    profile_urls = {
        normalize_linkedin(str(contact.get("value", "")))
        for contact in candidate.get("linkedin", []) or []
        if isinstance(contact, dict)
        and normalize_linkedin(str(contact.get("value", "")))
    }
    if not profile_urls:
        return None
    matches = [
        item
        for item in search_results
        if normalize_linkedin(item.url) in profile_urls
    ]
    return next(
        (
            item
            for item in matches
            if _has_probable_current_employment(company, item.title, item.snippet)
            and not _former_employment_near_company(
                company, f"{item.title} {item.snippet}"
            )
            and _BROAD_DISCOVERY_ROLE_EVIDENCE.search(
                f"{item.title} {item.snippet}"
            )
        ),
        matches[0] if matches else None,
    )


def _former_employment_near_company(company: CompanyProfile, text: str) -> bool:
    normalized = normalize_company_name(text)
    for alias in company_name_aliases(company.name):
        for match in re.finditer(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized):
            window = normalized[max(0, match.start() - 120) : match.end() + 120]
            if _FORMER_EMPLOYMENT_TERMS.search(window):
                return True
    return False


def _has_conflicting_labeled_employer(
    company: CompanyProfile,
    snippet: str,
) -> bool:
    """Reject an explicit employer label that expands into another company."""

    for alias in company_name_aliases(company.name):
        alias_pattern = r"[\W_]+".join(
            re.escape(token) for token in alias.split()
        )
        for match in re.finditer(
            rf"\b(?:experience|employer|company)\s*:?\s*(?:at\s+)?{alias_pattern}\b",
            snippet,
            re.I,
        ):
            remainder = re.split(r"[·•]", snippet[match.end() :], maxsplit=1)[0]
            if not _candidate_company_remainder_is_exact(remainder):
                return True
    return False


def _company_mention_only_in_related_profiles(
    company: CompanyProfile,
    title: str,
    snippet: str,
) -> bool:
    marker = re.search(r"\b(?:other|view all)\s+similar profiles\b", snippet, re.I)
    if marker is None:
        return False
    before = normalize_company_name(f"{title} {snippet[:marker.start()]}")
    after = normalize_company_name(snippet[marker.end() :])
    aliases = company_name_aliases(company.name)
    return not any(
        re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", before)
        for alias in aliases
    ) and any(
        re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", after)
        for alias in aliases
    )


def _has_probable_current_employment(
    company: CompanyProfile,
    title: str,
    snippet: str,
) -> bool:
    """Accept an exact LinkedIn headline without weakening verified evidence."""

    if _has_exact_current_employment(company, title, snippet):
        return True
    normalized_title = normalize_company_name(title)
    suffixes = "|".join(
        sorted(
            (re.escape(word) for word in _CURRENT_EMPLOYMENT_LEGAL_SUFFIX_WORDS),
            key=len,
            reverse=True,
        )
    )
    return any(
        re.search(
            rf"\b(?:at|with|for|of)\s+{re.escape(alias)}"
            rf"(?:\s+(?:{suffixes}))*"
            rf"(?:\s+linkedin)?$",
            normalized_title,
        )
        for alias in company_name_aliases(company.name)
    )


def _has_exact_company_association(
    company: CompanyProfile,
    title: str,
    snippet: str,
) -> bool:
    """Accept an exact company identity without treating it as current employment."""

    suffixes = "|".join(
        sorted(
            (re.escape(word) for word in _CURRENT_EMPLOYMENT_LEGAL_SUFFIX_WORDS),
            key=len,
            reverse=True,
        )
    )
    for value in (title, snippet):
        normalized = normalize_company_name(value)
        if any(
            re.search(
                rf"(?<!\w){re.escape(alias)}"
                rf"(?:\s+(?:{suffixes}))*"
                rf"(?:\s+linkedin)?$",
                normalized,
            )
            for alias in company_name_aliases(company.name)
        ):
            return True
    return False


def _linkedin_search_title_name(title: str) -> str:
    raw = title.split(" - ", 1)[0].split(" | ", 1)[0].strip()
    normalized = normalize_company_name(raw)
    parts = normalized.split()
    if not 2 <= len(parts) <= 6 or any(
        not re.fullmatch(r"[^\W\d_]+", part, flags=re.UNICODE) for part in parts
    ):
        return ""
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def _linkedin_name_from_profile(profile_url: str) -> str:
    parts = [
        part
        for part in unquote(urlparse(profile_url).path).split("/")
        if part
    ]
    if len(parts) != 2 or parts[0].casefold() != "in":
        return ""
    slug = parts[1].casefold()
    raw_tokens = [token for token in re.split(r"[-_]+", slug) if token]
    if len(raw_tokens) < 2 or not re.search(r"[-_]", slug):
        return ""
    if any(char.isdigit() for char in raw_tokens[-1]):
        raw_tokens.pop()
    if not 2 <= len(raw_tokens) <= 6:
        return ""
    if any(
        not re.fullmatch(r"[^\W\d_]+", token, flags=re.UNICODE)
        or len(token) < 2
        or token in _LINKEDIN_NAME_GENERIC_TOKENS
        for token in raw_tokens
    ):
        return ""
    return " ".join(
        token
        if token in _LINKEDIN_NAME_PARTICLES and index
        else token[:1].upper() + token[1:]
        for index, token in enumerate(raw_tokens)
    )


def _promote_linkedin_signal_candidates(
    result: dict[str, Any],
    company: CompanyProfile,
    search_results: list[SearchResult],
) -> None:
    pending = result.get("unverified_candidates")
    if not isinstance(pending, list):
        return
    existing_names = {
        normalize_company_name(str(candidate.get("full_name", "")))
        for candidate in result.get("candidates", [])
        if isinstance(candidate, dict)
    }
    existing_linkedin = {
        normalize_linkedin(str(contact.get("value", "")))
        for candidate in result.get("candidates", [])
        if isinstance(candidate, dict)
        for contact in candidate.get("linkedin", [])
        if isinstance(contact, dict)
    }
    promoted: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for candidate in pending:
        if not isinstance(candidate, dict) or not _is_official_linkedin_seed(
            candidate, company
        ):
            remaining.append(candidate)
            continue
        seed_url = next(
            (
                normalize_linkedin(str(contact.get("value", "")))
                for contact in candidate.get("linkedin", [])
                if isinstance(contact, dict)
                and _is_linkedin_profile(str(contact.get("value", "")))
            ),
            "",
        )
        seed_id = _linkedin_profile_stable_id(seed_url)
        matched_result = next(
            (
                item
                for item in search_results
                if _is_linkedin_profile(item.url)
                and seed_id
                and _linkedin_profile_stable_id(item.url) == seed_id
                and _has_exact_current_employment(
                    company,
                    item.title,
                    item.snippet,
                )
            ),
            None,
        )
        if matched_result is None:
            remaining.append(candidate)
            continue
        current_url = normalize_linkedin(matched_result.url)
        normalized_name = normalize_company_name(
            str(candidate.get("full_name", ""))
        )
        if normalized_name in existing_names or current_url in existing_linkedin:
            continue
        candidate["company_match"] = "verified"
        candidate["linkedin"] = [
            {
                "value": current_url,
                "status": (
                    "observed"
                    if matched_result.source_type == "direct"
                    else "probable"
                ),
                "source_url": matched_result.url,
            }
        ]
        current_title = str(candidate.get("current_title", "")).strip()
        if not current_title or current_title.casefold().startswith("unknown"):
            extracted_title = _search_result_role_title(company, matched_result)
            if extracted_title:
                candidate["current_title"] = extracted_title
        candidate.setdefault("evidence", []).append(
            {
                "source_url": matched_result.url,
                "quote": (
                    f"{matched_result.title} {matched_result.snippet}"
                ).strip()[:500],
                "supports": (
                    "LinkedIn search evidence matches the official profile ID "
                    "and shows current employment at the target company"
                ),
            }
        )
        candidate.pop("validation_reasons", None)
        candidate["confidence"] = max(
            float(candidate.get("confidence", 0.0) or 0.0), 0.55
        )
        promoted.append(candidate)
        existing_names.add(normalized_name)
        existing_linkedin.add(current_url)
    if remaining:
        result["unverified_candidates"] = remaining
    else:
        result.pop("unverified_candidates", None)
    if promoted:
        result.setdefault("candidates", []).extend(promoted)


def _search_result_role_title(
    company: CompanyProfile,
    result: SearchResult,
) -> str:
    suffixes = "|".join(
        sorted(
            (re.escape(word) for word in _CURRENT_EMPLOYMENT_LEGAL_SUFFIX_WORDS),
            key=len,
            reverse=True,
        )
    )
    for text in (result.snippet, result.title):
        for alias in company_name_aliases(company.name):
            alias_pattern = r"[\W_]+".join(
                re.escape(token) for token in alias.split()
            )
            match = re.search(
                rf"(?P<role>[^|\n]{{2,160}}?)\s+"
                rf"(?:at|of|for|with|bei)\s+{alias_pattern}"
                rf"(?:\s+(?:{suffixes}))*\b",
                text,
                re.I,
            )
            if not match:
                continue
            parts = [
                part.strip(" -#,:;()")
                for part in re.split(r"\.{3,}", match.group("role"))
                if part.strip(" -#,:;()")
            ]
            role = parts[-1] if parts else match.group("role").strip()
            role = re.sub(
                r"^(?:current|currently|present)\s+",
                "",
                role,
                flags=re.I,
            ).strip(" -#,:;()")
            if role:
                return role[:120]
    return ""


def _is_official_linkedin_seed(
    candidate: dict[str, Any],
    company: CompanyProfile,
) -> bool:
    if candidate.get("company_match") != "probable":
        return False
    if not any(
        "current employment" in str(reason)
        and "not verified" in str(reason)
        for reason in candidate.get("validation_reasons", [])
    ):
        return False
    return any(
        isinstance(evidence, dict)
        and same_site(company.website, str(evidence.get("source_url", "")))
        and not _is_linkedin_host(str(evidence.get("source_url", "")))
        for evidence in candidate.get("evidence", [])
    )


def _linkedin_profile_stable_id(profile_url: str) -> str:
    path_parts = [
        part
        for part in unquote(urlparse(profile_url).path).split("/")
        if part
    ]
    if len(path_parts) != 2 or path_parts[0].casefold() != "in":
        return ""
    slug_parts = [
        part for part in re.split(r"[-_]+", path_parts[1].casefold()) if part
    ]
    if not slug_parts or not any(char.isdigit() for char in slug_parts[-1]):
        return ""
    return slug_parts[-1]


_CURRENT_EMPLOYMENT_CONTINUATION_WORDS = {
    "a",
    "ab",
    "ag",
    "aps",
    "as",
    "at",
    "bv",
    "co",
    "company",
    "corp",
    "corporation",
    "current",
    "currently",
    "director",
    "en",
    "engineer",
    "est",
    "established",
    "experience",
    "from",
    "gmbh",
    "inc",
    "incorporated",
    "in",
    "linkedin",
    "l",
    "limited",
    "llc",
    "ltd",
    "manager",
    "nv",
    "operations",
    "oy",
    "p",
    "plc",
    "pte",
    "pty",
    "present",
    "production",
    "quality",
    "role",
    "s",
    "s.a",
    "s.l",
    "sa",
    "since",
    "sl",
    "spa",
    "s.p.a",
    "to",
    "with",
}

_CURRENT_EMPLOYMENT_LEGAL_SUFFIX_WORDS = {
    "a",
    "ab",
    "ag",
    "aps",
    "as",
    "bv",
    "co",
    "company",
    "corp",
    "corporation",
    "gmbh",
    "inc",
    "incorporated",
    "l",
    "limited",
    "llc",
    "ltd",
    "nv",
    "oy",
    "p",
    "plc",
    "pte",
    "pty",
    "s",
    "sa",
    "sl",
    "spa",
}


def _has_headline_exact_company_reference(
    company: CompanyProfile,
    snippet: str,
) -> bool:
    headline = normalize_company_name(snippet)[:240]
    if not headline:
        return False
    suffixes = "|".join(
        sorted(
            (re.escape(word) for word in _CURRENT_EMPLOYMENT_LEGAL_SUFFIX_WORDS),
            key=len,
            reverse=True,
        )
    )
    boundary = r"(?:est|established|current|currently|present|linkedin)"
    for alias in company_name_aliases(company.name):
        if re.search(
            rf"(?<!\w)(?:at|bei|of|for|with)\s+{re.escape(alias)}"
            rf"(?:\s+(?:{suffixes}))*"
            rf"(?=\s+{boundary}\b|$)",
            headline,
        ):
            return True
    return False


def _has_exact_current_employment(
    company: CompanyProfile,
    title: str,
    snippet: str,
) -> bool:
    title = normalize_company_name(title)
    snippet = normalize_company_name(snippet)
    aliases = company_name_aliases(company.name)
    if not any(
        _has_current_employment(alias, title, snippet) for alias in aliases
    ) and not _has_headline_exact_company_reference(
        company, f"{title} {snippet}"
    ):
        return False
    found_exact = False
    for alias in aliases:
        combined = f"{title} {snippet}"
        for match in re.finditer(
            rf"(?<!\w){re.escape(alias)}(?!\w)", combined
        ):
            window = combined[max(0, match.start() - 90) : match.end() + 120]
            if not re.search(
                r"\b(?:at|bei|of|for|with|na|en|current|currently|present|atual|presente)\b",
                window,
            ):
                continue
            remainder = combined[match.end() :].strip().split()
            while (
                remainder
                and remainder[0] in _CURRENT_EMPLOYMENT_LEGAL_SUFFIX_WORDS
            ):
                remainder.pop(0)
            if (
                remainder
                and remainder[0] not in _CURRENT_EMPLOYMENT_CONTINUATION_WORDS
            ):
                return False
            found_exact = True
    return found_exact


def _is_linkedin_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _add_linkedin_employee_candidates(
    result: dict[str, Any],
    company: CompanyProfile,
    pages: list[CrawledPage],
) -> None:
    linkedin_key = normalize_linkedin(company.linkedin_url)
    if not linkedin_key:
        return
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        return
    existing_names = {
        normalize_company_name(str(candidate.get("full_name", "")))
        for candidate in candidates
        if isinstance(candidate, dict)
    }
    existing_linkedin = {
        normalize_linkedin(str(contact.get("value", "")))
        for candidate in candidates
        if isinstance(candidate, dict)
        for contact in candidate.get("linkedin", [])
        if isinstance(contact, dict)
    }
    for page in pages:
        if normalize_linkedin(page.url) != linkedin_key or not page.markdown:
            continue
        for match in re.finditer(r"\[\s*([^\]\n]+?)\s*\]\((https?://[^)\s]+)\)", page.markdown):
            name = " ".join(match.group(1).split())
            profile_url = match.group(2)
            tracking = parse_qs(urlparse(profile_url).query).get("trk", [""])[0]
            canonical_url = normalize_linkedin(profile_url)
            normalized_name = normalize_company_name(name)
            if (
                not tracking.startswith("org-employees")
                or not _is_linkedin_profile(profile_url)
                or not normalized_name
                or normalized_name in existing_names
                or canonical_url in existing_linkedin
            ):
                continue
            candidates.append(
                {
                    "full_name": name,
                    "current_title": "",
                    "company_match": "verified",
                    "influence_type": "other",
                    "influence_score": 0,
                    "linkedin": [
                        {
                            "value": canonical_url,
                            "status": "observed",
                            "source_url": page.url,
                        }
                    ],
                    "emails": [],
                    "phones": [],
                    "evidence": [
                        {
                            "source_url": page.url,
                            "quote": name,
                            "supports": "listed in the company's LinkedIn Employees section",
                        }
                    ],
                    "confidence": 0.7,
                    "review_required": True,
                }
            )
            existing_names.add(normalized_name)
            existing_linkedin.add(canonical_url)
    result["unassigned_contacts"] = [
        contact
        for contact in result.get("unassigned_contacts", [])
        if not (
            contact.get("channel") == "linkedin"
            and normalize_linkedin(str(contact.get("value", ""))) in existing_linkedin
        )
    ]


_CANDIDATE_ROLE_EVIDENCE = re.compile(
    r"\b(?:owner|director|board|member|chairman|chairwoman|ceo|chief|"
    r"executive|manager|head|president|founder|contact|engineer|"
    r"procurement|purchasing|maintenance|technical|operations|quality|role)\b"
)


def _candidate_query_matches(
    company: CompanyProfile,
    candidate: dict[str, Any],
    item: SearchResult,
) -> bool:
    query = normalize_company_name(item.query)
    name = normalize_company_name(str(candidate.get("full_name", "")))
    company_name = normalize_company_name(company.name)
    if not query or not name or not company_name:
        return False
    padded_query = f" {query} "
    if f" {name} " not in padded_query or f" {company_name} " not in padded_query:
        return False
    if _is_linkedin_host(item.url):
        return False
    return (
        same_site(company.website, item.url)
        or _is_trusted_external(item.url)
        or _distinctive_company_url(company, item.url)
    )


def _candidate_role_evidence_matches(
    company: CompanyProfile,
    candidate: dict[str, Any],
    item: SearchResult,
) -> bool:
    if not _candidate_query_matches(company, candidate, item):
        return False
    text = f"{item.title} {item.snippet}"
    normalized_text = normalize_company_name(text)
    candidate_name = normalize_company_name(str(candidate.get("full_name", "")))
    if not candidate_name or f" {candidate_name} " not in f" {normalized_text} ":
        return False
    former_terms = re.compile(
        r"\b(?:former|ex|previous|past|prior|formerly)\b",
        re.I,
    )
    for alias in company_name_aliases(company.name):
        alias_pattern = r"[\W_]+".join(
            re.escape(token) for token in alias.split()
        )
        for match in re.finditer(
            rf"\b(?:at|of|for|with|bei)\s+{alias_pattern}\b",
            text,
            re.I,
        ):
            window = text[max(0, match.start() - 140) : match.end() + 140]
            if (
                former_terms.search(window)
                or not _candidate_company_remainder_is_exact(
                    text[match.end() :]
                )
                or not _CANDIDATE_ROLE_EVIDENCE.search(
                    normalize_company_name(window)
                )
            ):
                continue
            return True
    return False


def _candidate_company_remainder_is_exact(remainder: str) -> bool:
    rest = remainder.lstrip()
    while rest:
        match = re.match(r"[^\W\d_]+", rest, re.UNICODE)
        if not match or normalize_company_name(match.group()) not in _CURRENT_EMPLOYMENT_LEGAL_SUFFIX_WORDS:
            break
        rest = rest[match.end() :].lstrip()
    return not rest or rest[0] in ",.;:!?)]}-—|({"


def _has_official_linkedin_anchor(
    candidate: dict[str, Any],
    company: CompanyProfile,
) -> bool:
    profile_paths = {
        urlparse(normalize_linkedin(str(contact.get("value", "")))).path.casefold().rstrip("/")
        for contact in candidate.get("linkedin", []) or []
        if isinstance(contact, dict)
        and _is_linkedin_profile(str(contact.get("value", "")))
    }
    if not profile_paths:
        return False
    for evidence in candidate.get("evidence", []) or []:
        if not isinstance(evidence, dict):
            continue
        source_url = str(evidence.get("source_url", ""))
        supports = str(evidence.get("supports", "")).casefold()
        if _is_linkedin_company(source_url) and "employees section" in supports:
            return True
        if not same_site(company.website, source_url) or _is_linkedin_host(source_url):
            continue
        evidence_text = (
            f"{evidence.get('quote', '')} {evidence.get('supports', '')}"
        ).casefold()
        if any(path and path in evidence_text for path in profile_paths):
            return True
    return False


def _verify_linkedin_anchored_candidates(
    result: dict[str, Any],
    company: CompanyProfile,
    search_results: list[SearchResult],
) -> None:
    for candidate in result.get("candidates", []) or []:
        if not isinstance(candidate, dict) or candidate.get("company_match") != "verified":
            continue
        if not _has_official_linkedin_anchor(candidate, company):
            continue
        for matched_result in search_results:
            if not _candidate_role_evidence_matches(
                company, candidate, matched_result
            ):
                continue
            source_key = _url_key(matched_result.url)
            if any(
                _url_key(str(evidence.get("source_url", ""))) == source_key
                for evidence in candidate.get("evidence", []) or []
                if isinstance(evidence, dict)
            ):
                continue
            current_title = str(candidate.get("current_title", "")).strip()
            if not current_title or current_title.casefold().startswith("unknown"):
                title_source = matched_result.title
                if not _CANDIDATE_ROLE_EVIDENCE.search(
                    normalize_company_name(title_source)
                ):
                    title_source = matched_result.snippet
                candidate["current_title"] = str(title_source).strip()[:240]
            candidate.setdefault("evidence", []).append(
                {
                    "source_url": matched_result.url,
                    "quote": (
                        f"{matched_result.title} {matched_result.snippet}"
                    ).strip()[:500],
                    "supports": (
                        "Candidate-specific search evidence matches the exact name "
                        "and target company and explicitly shows a current role/"
                        "owner/director/board relationship; original LinkedIn seed "
                        "retained"
                    ),
                }
            )


def _customs_leads(
    company: CompanyProfile,
    results: list[SearchResult],
) -> list[dict[str, Any]]:
    aliases = company_name_aliases(company.name)
    product_names = {
        product: normalize_company_name(product)
        for product in company.products
        if normalize_company_name(product)
    }
    leads = []
    for item in results:
        text = normalize_company_name(
            " ".join((item.title, item.snippet, unquote(item.url)))
        )
        host = (urlparse(item.url).hostname or "").casefold().removeprefix("www.")
        if not any(alias in text for alias in aliases):
            continue
        trusted_host = any(
            host == item or host.endswith(f".{item}") for item in _CUSTOMS_DATA_HOSTS
        )
        if not trusted_host and not any(term in text for term in _CUSTOMS_TERMS):
            continue
        lead = item.as_dict()
        lead["matched_products"] = [
            product for product, normalized in product_names.items() if normalized in text
        ]
        lead["review_required"] = True
        leads.append(lead)
    return leads


def _merge_customs_leads(
    existing: list[dict[str, Any]],
    additional: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = {item["url"]: item for item in existing}
    for item in additional:
        merged.setdefault(item["url"], item)
    return list(merged.values())


def _new_contactable_people(result: dict[str, Any]) -> int:
    return len({
        normalize_company_name(str(candidate.get("full_name", "")))
        for candidate in result.get("candidates", [])
        if candidate.get("company_match") == "verified"
        and not candidate.get("crm_existing_match")
        and any(
            contact.get("value") and contact.get("status") != "guessed"
            for field in ("linkedin", "emails", "phones")
            for contact in candidate.get(field, [])
        )
    } - {""})


def _merge_people_results(first: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(first)

    def same_person(left, right):
        left_urls = {normalize_linkedin(item["value"]) for item in left.get("linkedin", []) if item.get("value")}
        right_urls = {normalize_linkedin(item["value"]) for item in right.get("linkedin", []) if item.get("value")}
        if left_urls - {""} and right_urls - {""} and not (left_urls & right_urls) - {""}:
            return False
        return bool((left_urls & right_urls) - {""}) or (
            bool(left.get("full_name"))
            and normalize_company_name(left["full_name"]) == normalize_company_name(right.get("full_name", ""))
        )

    for group in ("candidates", "unverified_candidates"):
        for person in extra.get(group, []):
            if group == "unverified_candidates" and any(same_person(person, known) for known in merged.get("candidates", [])):
                continue
            if group == "candidates":
                merged["unverified_candidates"] = [known for known in merged.get("unverified_candidates", []) if not same_person(person, known)]
            people = merged.setdefault(group, [])
            known = next((known for known in people if same_person(person, known)), None)
            if known is None:
                people.append(deepcopy(person))
            else:
                for field in ("linkedin", "emails", "phones", "evidence"):
                    for item in person.get(field, []):
                        if item not in known.setdefault(field, []):
                            known[field].append(deepcopy(item))
    for field in ("unassigned_contacts", "validation_rejections", "crm_duplicates", "excluded_candidates"):
        for item in extra.get(field, []):
            if item not in merged.setdefault(field, []):
                merged[field].append(deepcopy(item))
    assigned = {
        (channel, canonical_contact_value(channel, contact["value"]))
        for group in ("candidates", "unverified_candidates")
        for person in merged.get(group, [])
        for field, channel in (("linkedin", "linkedin"), ("emails", "email"), ("phones", "phone"))
        for contact in person.get(field, []) if contact.get("value")
    }
    public = {}
    for item in merged.get("unassigned_contacts", []):
        key = (item.get("channel", ""), canonical_contact_value(item.get("channel", ""), item.get("value", "")))
        if key not in assigned:
            public.setdefault(key, item)
    merged["unassigned_contacts"] = list(public.values())
    merged["crm_duplicates_removed"] = len(merged.get("crm_duplicates", []))
    return merged


def _contactable_items(result: dict[str, Any]) -> int:
    people = 0
    for group in ("candidates", "unverified_candidates"):
        for candidate in result.get(group, []):
            if isinstance(candidate, dict) and any(
                candidate.get(field) for field in ("linkedin", "emails", "phones")
            ):
                people += 1
    public_values = {
        str(contact.get("value", "")).strip().casefold()
        for contact in result.get("unassigned_contacts", [])
        if isinstance(contact, dict) and str(contact.get("value", "")).strip()
    }
    return people + len(public_values)


def _contact_method_count(result: dict[str, Any]) -> int:
    methods: set[tuple[str, str]] = set()
    for group in ("candidates", "unverified_candidates"):
        for candidate in result.get(group, []):
            if not isinstance(candidate, dict):
                continue
            for field, channel in (("linkedin", "linkedin"), ("emails", "email"), ("phones", "phone")):
                for contact in candidate.get(field, []) or []:
                    if not isinstance(contact, dict):
                        continue
                    value = canonical_contact_value(channel, str(contact.get("value", "")))
                    if value:
                        methods.add((channel, value))
                        if channel == "phone" and contact.get("whatsapp_status") == "verified":
                            methods.add(("whatsapp", value))
    for contact in result.get("unassigned_contacts", []):
        if not isinstance(contact, dict):
            continue
        channel = str(contact.get("channel", ""))
        value = canonical_contact_value(channel, str(contact.get("value", "")))
        if channel and value:
            methods.add((channel, value))
    return len(methods)


def _demote_navigation_only_company_matches(
    result: dict[str, Any], company: CompanyProfile, pages: list[CrawledPage],
) -> None:
    """A target linked from a group menu does not establish staff employment there."""
    # Parenthetical group names are context, not interchangeable subsidiary employers.
    aliases = company_name_aliases(re.sub(r'\([^)]*\)', '', company.name))
    def mentions(text):
        normalized = ' ' + normalize_company_name(text) + ' '
        return any(' ' + alias + ' ' in normalized for alias in aliases)

    texts: dict[str, str] = {}
    for page in pages:
        if page.markdown and not page.error:
            key = _url_key(page.url)
            texts[key] = texts.get(key, '') + '\n' + page.markdown
    linked_only = {url for url, text in texts.items()
                   if mentions(text) and not mentions(re.sub(r'!?\[[^\]]*\]\([^)]*\)', '', text))}
    kept = []
    for candidate in result.get('candidates', []):
        urls = {_url_key(str(e.get('source_url', ''))) for e in candidate.get('evidence', [])}
        if candidate.get('company_match') == 'verified' and urls and urls <= linked_only:
            candidate['company_match'] = 'uncertain'
            candidate['discovery_tier'] = 'unverified'
            candidate['review_required'] = True
            candidate.setdefault('validation_reasons', []).append(
                'target company appears only in page links; current employment at the exact entity is unverified')
            result.setdefault('unverified_candidates', []).append(candidate)
        else:
            kept.append(candidate)
    result['candidates'] = kept


def _exclude_unrelated_roles(result: dict[str, Any]) -> None:
    """Keep explicit HR/web-only roles reviewable without counting them as target people."""
    excluded = result.setdefault('excluded_candidates', [])
    excluded_names = {normalize_company_name(p['full_name']) for p in excluded}
    for group in ('candidates', 'unverified_candidates'):
        kept = []
        for person in result.get(group, []):
            title = str(person.get('current_title', ''))
            unrelated = re.search(r'\b(?:HR|human resources|recruit\w*|webmaster|website administrator|ressources humaines|leitung personal|personal(?:leit|referent|sachbearbeit)\w*|personalabteilung)\b|人事|人力资源', title, re.I)
            relevant = re.search(r'\b(?:CEO|chief executive|owner|geschäftsführ\w*|procurement|purchas\w*|einkauf\w*|supply chain|operations|production|produktionsleit\w*|plant|foundry|metallurg\w*|engineer\w*|technical|research)\b|采购|生产|技术|总经理', title, re.I)
            if unrelated and not relevant:
                person['exclusion_reason'] = 'explicit HR or website role outside target purchase/technical scope'
                name = normalize_company_name(person['full_name'])
                if name not in excluded_names:
                    excluded.append(person)
                    excluded_names.add(name)
            else:
                kept.append(person)
        if group in result:
            result[group] = kept


def _remove_conflicting_contacts(result: dict[str, Any], signals: list[dict[str, Any]]) -> None:
    blocked = {(s['channel'], canonical_contact_value(s['channel'], s['value']))
               for s in signals if s.get('status') == 'conflicting'}
    for group in ('candidates', 'unverified_candidates', 'excluded_candidates'):
        for person in result.get(group, []):
            for field, channel in (('emails', 'email'), ('inferred_emails', 'email'), ('phones', 'phone')):
                person[field] = [c for c in person.get(field, [])
                                 if (channel, canonical_contact_value(channel, c['value'])) not in blocked]
    result['unassigned_contacts'] = [c for c in result.get('unassigned_contacts', [])
                                     if (c['channel'], canonical_contact_value(c['channel'], c['value'])) not in blocked]


def _attach_evidence_bound_contacts(
    result: dict[str, Any], contact_blocks: dict[str, list[str]] | None = None,
) -> None:
    candidates = [
        candidate
        for group in ("candidates", "unverified_candidates")
        for candidate in result.get(group, [])
        if isinstance(candidate, dict)
    ]
    remaining = []
    for contact in result.get("unassigned_contacts", []):
        channel = str(contact.get("channel", ""))
        if channel not in {"email", "phone"}:
            remaining.append(contact)
            continue
        source_key = _url_key(str(contact.get("source_url", "")))
        matches = []
        for candidate in candidates:
            value = str(contact.get("value", ""))
            if _evidence_binds_contact(candidate, channel, value, source_key, contact_blocks,
                                       contact.get('binding_quotes'), contact.get('binding_sections')):
                matches.append(candidate)
        if len(matches) != 1:
            remaining.append(contact)
            continue
        field = "emails" if channel == "email" else "phones"
        entry = {
            key: value
            for key, value in contact.items()
            if key not in {"channel", "company_public", "evidence_status", "binding_sections"}
        }
        matches[0].setdefault(field, []).append(entry)
    assigned_whatsapp = {c['value'] for person in candidates for c in person.get('phones', [])
                         if c.get('whatsapp_status') == 'verified'}
    result["unassigned_contacts"] = [c for c in remaining
                                     if not (c.get('channel') == 'whatsapp' and c['value'] in assigned_whatsapp)]


def _attach_crm_matched_emails(
    result: dict[str, Any],
    crm_contacts: list[dict[str, Any]],
) -> None:
    surname_matches: dict[str, dict[str, str]] = {}
    for contact in crm_contacts:
        name = str(contact.get("name", "")).strip()
        normalized = normalize_company_name(name)
        if not normalized:
            continue
        surname_matches.setdefault(normalized.split()[-1], {})[normalized] = name
    candidates = result.get("candidates", [])
    by_name = {
        normalize_company_name(str(candidate.get("full_name", ""))): candidate
        for candidate in candidates
        if isinstance(candidate, dict)
    }
    remaining = []
    for contact in result.get("unassigned_contacts", []):
        value = str(contact.get("value", ""))
        if contact.get("channel") != "email" or "@" not in value:
            remaining.append(contact)
            continue
        local_part = normalize_company_name(value.split("@", 1)[0]).replace(" ", "")
        matches = surname_matches.get(local_part, {})
        if len(matches) != 1:
            remaining.append(contact)
            continue
        normalized_name, full_name = next(iter(matches.items()))
        candidate = by_name.get(normalized_name)
        if candidate is None:
            candidate = {
                "full_name": full_name,
                "current_title": "",
                "company_match": "probable",
                "influence_type": "other",
                "influence_score": 0,
                "linkedin": [],
                "emails": [],
                "phones": [],
                "evidence": [
                    {
                        "source_url": str(contact.get("source_url", "")),
                        "quote": value,
                        "supports": "email local part uniquely matches an existing CRM contact surname",
                    }
                ],
                "confidence": 0.6,
                "review_required": True,
            }
            candidates.append(candidate)
            by_name[normalized_name] = candidate
        candidate.setdefault("emails", []).append(
            {
                "value": value.casefold(),
                "status": "guessed",
                "source_url": str(contact.get("source_url", "")),
            }
        )
    result["unassigned_contacts"] = remaining


def _filter_directory_footer_contacts(
    result: dict[str, Any],
    pages: list[CrawledPage],
    official_urls: list[str],
    company: CompanyProfile | None = None,
) -> None:
    company_domains = {_hostname(url) for url in official_urls if _hostname(url)}
    if not company_domains:
        return
    page_by_url: dict[str, str] = {}
    for page in pages:
        if page.markdown:
            key = _url_key(page.url)
            page_by_url[key] = "\n".join(filter(None, (page_by_url.get(key), page.markdown)))
    kept = []
    excluded = list(result.get("excluded_contacts", []))
    for contact in result.get("unassigned_contacts", []):
        channel = str(contact.get("channel", ""))
        value = str(contact.get("value", ""))
        source_url = str(contact.get("source_url", ""))
        markdown = page_by_url.get(_url_key(source_url), "")
        external_owner = _explicit_external_service_owner(markdown, channel, value)
        if external_owner:
            quarantined = dict(contact)
            quarantined.update(
                {
                    "relationship": "external_service",
                    "owner_name": external_owner,
                    "exclusion_reason": "explicit_external_service_context",
                }
            )
            excluded.append(quarantined)
            continue
        relationship = _explicit_network_relationship(markdown, channel, value)
        if relationship:
            contact = dict(contact)
            contact["relationship"] = relationship
            if channel == "email" and "@" in value:
                contact["owner_domain"] = value.rsplit("@", 1)[-1].casefold()
        if _hostname(source_url) in company_domains:
            kept.append(contact)
            continue
        if channel == "email":
            domain = value.rsplit("@", 1)[-1].casefold()
            if domain not in company_domains and not (
                company and _distinctive_company_host(company, f"https://{domain}")
            ):
                continue
        elif channel in {"phone", "whatsapp"} and markdown:
            digits = re.sub(r"\D", "", value)[-7:]
            match = re.search(r"\D{0,6}".join(digits), markdown) if digits else None
            if match:
                start = max(0, match.start() - 180)
                context = markdown[start : match.end() + 180]
                email_matches = list(
                    re.finditer(
                        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
                        context,
                        re.I,
                    )
                )
                nearest_domain = (
                    min(
                        email_matches,
                        key=lambda item: abs((start + item.start()) - match.start()),
                    ).group().rsplit("@", 1)[-1].casefold()
                    if email_matches
                    else ""
                )
                if nearest_domain and nearest_domain not in company_domains and not (
                    company and _distinctive_company_host(company, f"https://{nearest_domain}")
                ):
                    continue
        kept.append(contact)
    result["unassigned_contacts"] = kept
    if excluded:
        result["excluded_contacts"] = excluded


def _explicit_external_service_owner(markdown: str, channel: str, value: str) -> str:
    context = " ".join(
        _contact_context(markdown, channel, value, radius=600).casefold().split()
    )
    for marker, owner in _EXTERNAL_SERVICE_MARKERS:
        if " ".join(marker.split()) in context:
            return owner
    return ""


def _explicit_network_relationship(markdown: str, channel: str, value: str) -> str:
    span = _contact_span(markdown, channel, value)
    if span is None:
        return ""
    prefix = markdown[: span[0]]
    headings = {
        "subsidiary": max(
            (match.start() for match in re.finditer(r"^##\s+subsidiaries\s*$", prefix, re.I | re.M)),
            default=-1,
        ),
        "agent": max(
            (match.start() for match in re.finditer(r"^##\s+agencies\s*$", prefix, re.I | re.M)),
            default=-1,
        ),
    }
    relationship, position = max(headings.items(), key=lambda item: item[1])
    return relationship if position >= 0 else ""


def _contact_context(markdown: str, channel: str, value: str, radius: int) -> str:
    span = _contact_span(markdown, channel, value)
    if span is None:
        return ""
    return markdown[max(0, span[0] - radius) : min(len(markdown), span[1] + radius)]


def _contact_span(markdown: str, channel: str, value: str) -> tuple[int, int] | None:
    if channel == "email":
        start = markdown.casefold().find(value.casefold())
        return (start, start + len(value)) if start >= 0 else None
    if channel not in {"phone", "whatsapp"}:
        return None
    digits = re.sub(r"\D", "", value)[-7:]
    match = re.search(r"\D{0,6}".join(digits), markdown) if digits else None
    return match.span() if match else None


def _deduplicate_crm_contacts(
    result: dict[str, Any],
    crm_contacts: list[dict[str, Any]],
) -> None:
    names = {
        normalize_company_name(str(contact.get("name", "")))
        for contact in crm_contacts
        if str(contact.get("name", "")).strip()
    }
    emails = {
        value.casefold()
        for contact in crm_contacts
        for value in _nested_strings((contact.get("email"), contact.get("additional_emails")))
        if "@" in value
    }
    phones = {
        normalized
        for contact in crm_contacts
        for value in _nested_strings((contact.get("phone"), contact.get("additional_phones")))
        if (normalized := _normalize_phone(value))
    }
    linkedin = {
        normalized
        for contact in crm_contacts
        for value in _nested_strings((contact.get("linkedin"), contact.get("additional_linkedin")))
        if (normalized := normalize_linkedin(value))
    }

    duplicates = []
    for group, duplicate_kind in (
        ("candidates", "candidate"),
        ("unverified_candidates", "unverified_candidate"),
    ):
        if group not in result:
            continue
        kept_candidates = []
        for candidate in result.get(group, []) or []:
            matched_by = []
            if normalize_company_name(str(candidate.get("full_name", ""))) in names:
                matched_by.append("name")
            if any(
                str(item.get("value", "")).casefold() in emails
                for item in candidate.get("emails", [])
            ):
                matched_by.append("email")
            if any(
                _normalize_phone(str(item.get("value", ""))) in phones
                for item in candidate.get("phones", [])
            ):
                matched_by.append("phone")
            if any(
                normalize_linkedin(str(item.get("value", ""))) in linkedin
                for item in candidate.get("linkedin", [])
            ):
                matched_by.append("linkedin")
            if matched_by:
                candidate["emails"] = [
                    item
                    for item in candidate.get("emails", [])
                    if str(item.get("value", "")).casefold() not in emails
                ]
                candidate["phones"] = [
                    item
                    for item in candidate.get("phones", [])
                    if _normalize_phone(str(item.get("value", ""))) not in phones
                ]
                candidate["linkedin"] = [
                    item
                    for item in candidate.get("linkedin", [])
                    if normalize_linkedin(str(item.get("value", ""))) not in linkedin
                ]
                duplicates.append(
                    {
                        "kind": duplicate_kind,
                        "full_name": str(candidate.get("full_name", "")),
                        "matched_by": sorted(set(matched_by)),
                    }
                )
                if any(candidate.get(field) for field in ("linkedin", "emails", "phones")):
                    candidate["crm_existing_match"] = sorted(set(matched_by))
                    kept_candidates.append(candidate)
            else:
                kept_candidates.append(candidate)
        result[group] = kept_candidates

    public_contacts = []
    for contact in result.get("unassigned_contacts", []):
        channel = str(contact.get("channel", ""))
        value = str(contact.get("value", ""))
        duplicate = (
            (channel == "email" and value.casefold() in emails)
            or (channel in {"phone", "whatsapp"} and _normalize_phone(value) in phones)
            or (channel == "linkedin" and normalize_linkedin(value) in linkedin)
        )
        if duplicate:
            duplicates.append(
                {"kind": "unassigned_contact", "channel": channel, "matched_by": [channel]}
            )
        else:
            public_contacts.append(contact)

    result["unassigned_contacts"] = public_contacts
    result["crm_duplicates"] = duplicates
    result["crm_duplicates_removed"] = len(duplicates)


def _add_inferred_email_candidates(
    result: dict[str, Any],
    company: CompanyProfile,
    signals: list[dict[str, Any]] | None = None,
    crm_contacts: list[dict[str, Any]] | None = None,
) -> None:
    """Add separately-labelled email guesses without making people contactable."""

    compatibility_mode = signals is None
    signals = signals or []
    crm_contacts = crm_contacts or []
    if compatibility_mode:
        for group in ("candidates", "unverified_candidates"):
            for candidate in result.get(group, []) or []:
                for email in candidate.get("emails", []) or []:
                    if not isinstance(email, dict):
                        continue
                    signals.append(
                        {
                            "channel": "email",
                            "value": str(email.get("value", "")),
                            "status": "observed",
                            "source_url": str(email.get("source_url", "")),
                            "company_public": True,
                        }
                    )
    domain = _hostname(company.website)
    if not domain:
        return
    observed: dict[str, dict[str, Any]] = {}
    for signal in signals:
        if (
            not isinstance(signal, dict)
            or signal.get("channel") != "email"
            or signal.get("company_public") is not True
            or signal.get("status") != "observed"
        ):
            continue
        value = str(signal.get("value", "")).strip().casefold()
        source_url = str(signal.get("source_url", "")).strip()
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9._%+-]*@[a-z0-9.-]+\.[a-z]{2,}", value)
            or value.rsplit("@", 1)[-1] != domain
            or not same_site(company.website, source_url)
        ):
            continue
        local = value.split("@", 1)[0]
        if local in _PUBLIC_EMAIL_LOCALS or any(
            token in local for token in ("contact", "sales", "info", "support")
        ):
            continue
        observed[value] = signal
    if not observed:
        return

    crm_values = {
        value.casefold()
        for contact in crm_contacts
        for value in _nested_strings(
            (contact.get("email"), contact.get("additional_emails"))
        )
        if "@" in value
    }
    existing_values = set(crm_values) | set(observed)
    format_bases: dict[str, dict[str, Any]] = {}
    candidates = [
        candidate
        for group in ("candidates", "unverified_candidates")
        for candidate in result.get(group, []) or []
        if isinstance(candidate, dict)
        and (
            candidate.get("company_match") in {"verified", "probable"}
            or (compatibility_mode and "company_match" not in candidate)
        )
    ]
    for candidate in candidates:
        name = str(candidate.get("full_name", "")).strip()
        for email in candidate.get("emails", []) or []:
            if not isinstance(email, dict):
                continue
            value = str(email.get("value", "")).strip().casefold()
            signal = observed.get(value)
            if signal is None:
                continue
            pattern = _email_pattern_for_name(value.split("@", 1)[0], name)
            if pattern:
                format_bases.setdefault(
                    pattern,
                    {"value": value, "source_url": str(signal.get("source_url", ""))},
                )
        # Hermes evidence can carry the same observed email even when it did
        # not bind the signal into candidate.emails.
        evidence_text = " ".join(
            f"{item.get('quote', '')} {item.get('supports', '')}"
            for item in candidate.get("evidence", []) or []
            if isinstance(item, dict)
        ).casefold()
        for value, signal in observed.items():
            if value in evidence_text and normalize_company_name(name) in normalize_company_name(evidence_text):
                pattern = _email_pattern_for_name(value.split("@", 1)[0], name)
                if pattern:
                    format_bases.setdefault(
                        pattern,
                        {"value": value, "source_url": str(signal.get("source_url", ""))},
                    )
    if not format_bases:
        return

    for candidate in candidates:
        name = str(candidate.get("full_name", "")).strip()
        parts = normalize_company_name(name).split()
        if len(parts) < 2:
            continue
        inferred = candidate.setdefault("inferred_emails", [])
        if not isinstance(inferred, list):
            inferred = []
            candidate["inferred_emails"] = inferred
        existing_email_items = (candidate.get("emails", []) or []) + inferred
        already = {
            str(item.get("value", "")).casefold()
            for item in existing_email_items
            if isinstance(item, dict)
        }
        for pattern, basis in format_bases.items():
            if len(inferred) >= 3:
                break
            local = _render_email_pattern(pattern, parts)
            value = f"{local}@{domain}".casefold()
            if value in existing_values or value in already:
                continue
            inferred.append(
                {
                    "value": value,
                    "status": "inferred",
                    "verification_status": "ownership_unverified",
                    "basis": (
                        f"Observed personal-format email {basis['value']} on verified official page; "
                        f"matched {pattern} format"
                    ),
                    "source_url": basis["source_url"],
                }
            )
            already.add(value)
            existing_values.add(value)


def _email_pattern_for_name(local: str, full_name: str) -> str:
    parts = normalize_company_name(full_name).split()
    if len(parts) < 2:
        return ""
    first, last = parts[0], parts[-1]
    patterns = {
        f"{first}.{last}": "first.last",
        first: "first",
        f"{first[0]}{last}": "flast",
        f"{first[0]}.{last}": "f.last",
        f"{first}_{last}": "first_last",
        f"{first}{last}": "firstlast",
    }
    return patterns.get(local, "")


def _render_email_pattern(pattern: str, parts: list[str]) -> str:
    first, last = parts[0], parts[-1]
    return {
        "first.last": f"{first}.{last}",
        "first": first,
        "flast": f"{first[0]}{last}",
        "f.last": f"{first[0]}.{last}",
        "first_last": f"{first}_{last}",
        "firstlast": f"{first}{last}",
    }[pattern]


def _nested_strings(value: Any):
    if isinstance(value, str):
        if value.strip():
            yield value.strip()
    elif isinstance(value, dict):
        for item in value.values():
            yield from _nested_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _nested_strings(item)


def _normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return digits[2:] if digits.startswith("00") else digits


def _unique_urls(website: str, urls: list[str]) -> list[str]:
    ordered = ([website] if urlparse(website).scheme in {"http", "https"} else []) + urls
    seen: set[str] = set()
    output: list[str] = []
    for url in ordered:
        clean = urlparse(url.strip())._replace(fragment="").geturl()
        key = _url_key(clean)
        if not clean or key in seen:
            continue
        seen.add(key)
        output.append(clean)
    return output


def _relevant_urls(company: CompanyProfile, results: list[SearchResult]) -> list[str]:
    official_host = _hostname(company.website)
    aliases = company_name_aliases(company.name)
    official_url_is_distinctive = _distinctive_company_url(company, company.website)
    relevant: list[str] = []
    for result in results:
        host = _hostname(result.url)
        text = normalize_company_name(
            unquote(" ".join((result.title, result.url, result.snippet)))
        )
        url_text = normalize_company_name(unquote(result.url))
        text_matches = any(f" {alias} " in f" {text} " for alias in aliases)
        url_matches = any(f" {alias} " in f" {url_text} " for alias in aliases)
        same_official_host = official_host and (
            host == official_host or host.endswith(f".{official_host}")
        )
        if same_official_host and not official_url_in_scope(company, result.url):
            continue
        if same_official_host and (official_url_is_distinctive or text_matches):
            relevant.append(result.url)
            continue
        trusted_host = any(
            host == trusted or host.endswith(f".{trusted}")
            for trusted in _TRUSTED_EXTERNAL_HOSTS
        )
        if text_matches and (
            url_matches
            or trusted_host
            or same_site(company.website, result.url)
            or _distinctive_company_url(company, result.url)
        ) and (trusted_host or _website_identity_continues(company, result.url)):
            relevant.append(result.url)
    return _unique_urls(company.website, relevant)


def _official_website_from_search(
    company: CompanyProfile,
    results: list[SearchResult],
) -> str:
    aliases = company_name_aliases(company.name)
    shortest_alias = min(aliases, key=lambda alias: (len(alias.split()), len(alias)))
    company_tokens = {token for token in shortest_alias.split() if len(token) >= 3}
    candidates = []
    old_website_is_external = _is_trusted_external(company.website)
    for result in results:
        text_tokens = set(
            normalize_company_name(
                unquote(" ".join((result.title, result.url, result.snippet)))
            ).split()
        )
        matched = company_tokens.intersection(text_tokens)
        if not matched or (len(company_tokens) > 1 and len(matched) < 2):
            continue
        explicit_websites = re.findall(
            r"(?i)\bwebsite\b[^\n]{0,40}(https?://[^\s|)]+)",
            result.snippet,
        )
        for candidate_url, is_explicit in [
            (result.url, False),
            *((url, True) for url in explicit_websites),
        ]:
            parsed = urlparse(candidate_url.rstrip(".,;"))
            host = _hostname(candidate_url)
            if parsed.scheme not in {"http", "https"} or not host:
                continue
            if old_website_is_external and not is_explicit:
                continue
            host_tokens = set(normalize_company_name(host).split())
            if candidate_url == result.url and not company_tokens.intersection(host_tokens):
                continue
            if _is_trusted_external(candidate_url):
                continue
            if not _website_identity_continues(company, candidate_url):
                continue
            score = (
                candidate_url != result.url,
                len(matched) / len(company_tokens),
                len(matched),
                -result.rank,
            )
            candidates.append((score, f"{parsed.scheme}://{parsed.netloc}"))
    return max(candidates, default=((), ""))[1]


def _website_identity_continues(company: CompanyProfile, candidate_url: str) -> bool:
    old_host = _hostname(company.website)
    if not old_host or same_site(company.website, candidate_url):
        return True
    if _is_trusted_external(company.website):
        return True
    company_tokens = {
        token
        for alias in company_name_aliases(company.name)
        for token in alias.split()
        if len(token) >= 3
    }
    old_compact = re.sub(r"[^a-z0-9]", "", old_host)
    new_compact = re.sub(r"[^a-z0-9]", "", _hostname(candidate_url))
    old_identity = {token for token in company_tokens if token in old_compact}
    if not old_identity:
        return len({token for token in company_tokens if token in new_compact}) >= 2
    return len(old_identity) >= 2 and any(
        len(token) >= 5 and token in new_compact for token in old_identity
    )


def _named_person_source_urls(company: CompanyProfile, result: dict, results: list[SearchResult]) -> list[str]:
    """Allow bounded external evidence only when the snippet names both seed and employer."""
    names = {normalize_company_name(person_query_name(str(person.get('full_name', ''))))
             for group in ('candidates', 'unverified_candidates') for person in result.get(group, [])
             if not person.get('crm_existing_match')}
    names.discard('')
    aliases = company_name_aliases(re.sub(r'\([^)]*\)', '', company.name))
    urls = []
    for item in results:
        text = f'{item.title} {item.snippet}'
        normalized = ' ' + normalize_company_name(text) + ' '
        if (urlparse(item.url).scheme in {'http', 'https'}
            and any(' ' + name + ' ' in normalized for name in names)
            and any(' ' + alias + ' ' in normalized for alias in aliases)
            and not _has_conflicting_labeled_employer(company, text)
            and not _former_employment_near_company(company, text)):
            urls.append(item.url)
    return _unique_urls('', urls)


def _hostname(url: str) -> str:
    return (urlparse(url).hostname or "").casefold().removeprefix("www.")


def _contact_source_priority(company: CompanyProfile, url: str) -> tuple[int, int]:
    path = unquote(urlparse(url).path).casefold()
    return (
        0 if _url_key(url) == _url_key(company.website) else 1,
        0
        if same_site(company.website, url)
        and any(term in path for term in _CONTACT_SOURCE_PATH_TERMS)
        else 1,
    )


def _is_trusted_external(url: str) -> bool:
    host = _hostname(url)
    return any(
        host == external or host.endswith(f".{external}")
        for external in _TRUSTED_EXTERNAL_HOSTS
    )


def _search_evidence_pages(
    company: CompanyProfile,
    results: list[SearchResult],
) -> list[CrawledPage]:
    aliases = company_name_aliases(company.name)
    pages = []
    seen = set()
    for result in results:
        if not _is_linkedin_profile(result.url) or _url_key(result.url) in seen:
            continue
        title = normalize_company_name(result.title)
        snippet = normalize_company_name(result.snippet)
        if not any(_has_current_employment(alias, title, snippet) for alias in aliases):
            continue
        seen.add(_url_key(result.url))
        pages.append(
            CrawledPage(
                url=result.url,
                markdown=f"{result.title}\n{result.snippet}\nLinkedIn: {result.url}",
                provider=result.provider,
                source_type="search_excerpt",
            )
        )
    return pages


def _attach_named_linkedin_profiles(result: dict, company: CompanyProfile, results: list[SearchResult],
                                    crm_contacts: list[dict] | None = None) -> None:
    """Keep a unique exact-name/current-employer hit even if the model omits it."""
    eligible = {_url_key(page.url) for page in _search_evidence_pages(company, results)}
    crm_profiles = {normalize_linkedin(value) for contact in crm_contacts or []
                    for value in _nested_strings((contact.get('linkedin'), contact.get('additional_linkedin')))}
    for person in result.get('candidates', []):
        if person.get('company_match') != 'verified' or person.get('crm_existing_match') or person.get('linkedin'):
            continue
        name = normalize_company_name(person_query_name(str(person.get('full_name', ''))))
        if not name:
            continue
        matches = {}
        for hit in results:
            title = normalize_company_name(re.split(r'\s+[-–—|]\s+', person_query_name(hit.title), maxsplit=1)[0])
            text = f'{hit.title} {hit.snippet}'
            if (_url_key(hit.url) in eligible and title == name
                and not _has_conflicting_labeled_employer(company, text)
                and not _company_mention_only_in_related_profiles(company, hit.title, hit.snippet)
                and not _former_employment_near_company(company, text)):
                matches.setdefault(normalize_linkedin(hit.url), hit)
        if len(matches) == 1:
            url, hit = next(iter(matches.items()))
            if url in crm_profiles:
                continue
            person['linkedin'] = [dict(value=url, status='probable', source_url=hit.url)]
            person.setdefault('evidence', []).append(dict(source_url=hit.url,
                quote=f'{hit.title}\n{hit.snippet}',
                supports='Exact name and current target employer in search excerpt; profile ownership requires review'))
            person['review_required'] = True


def _contact_evidence_pages(
    company: CompanyProfile,
    results: list[SearchResult],
) -> list[CrawledPage]:
    aliases = company_name_aliases(company.name)
    relevant = {_url_key(url) for url in _relevant_urls(company, results)}
    pages = []
    seen = set()
    for result in results:
        key = _url_key(result.url)
        text = normalize_company_name(f"{result.title} {result.snippet}")
        contact_hint = re.search(
            r"(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b(?:tel|phone|whatsapp)\b|wa\.me)",
            f"{result.title} {result.snippet}",
            re.I,
        )
        if (
            key in relevant
            and key not in seen
            and same_site(company.website, result.url)
            and contact_hint
            and any(f" {alias} " in f" {text} " for alias in aliases)
        ):
            seen.add(key)
            pages.append(
                CrawledPage(
                    url=result.url,
                    markdown=f"{result.title}\n{result.snippet}",
                    provider=result.provider,
                    source_type="contact_excerpt",
                )
            )
    return pages


def _page_matches_company(company: CompanyProfile, page: CrawledPage) -> bool:
    text = normalize_company_name(page.markdown)
    return any(
        f" {alias} " in f" {text} "
        for alias in company_name_aliases(company.name)
    )


def _page_is_company_public(company: CompanyProfile, page: CrawledPage) -> bool:
    if company.linkedin_url and _is_linkedin_company(page.url):
        return same_site(company.linkedin_url, page.url)
    if page.source_type == "contact_excerpt":
        return same_site(company.website, page.url) and _page_matches_company(company, page)
    if same_site(company.website, page.url):
        return official_url_in_scope(company, page.url) and not _is_trusted_external(page.url)
    if _is_trusted_external(page.url) or not _page_matches_company(company, page):
        return False
    return _website_identity_continues(company, page.url) and _distinctive_company_url(
        company, page.url
    )


def _distinctive_company_url(company: CompanyProfile, url: str) -> bool:
    aliases = company_name_aliases(company.name)
    if not aliases:
        return False
    shortest_alias = min(aliases, key=lambda alias: (len(alias.split()), len(alias)))
    compact_url = re.sub(r"[^a-z0-9]", "", unquote(url).casefold())
    return any(
        len(token) >= 3 and token in compact_url
        for token in shortest_alias.split()
    )


def _distinctive_company_host(company: CompanyProfile, url: str) -> bool:
    aliases = company_name_aliases(company.name)
    tokens = {
        token
        for alias in aliases
        for token in alias.split()
        if len(token) >= 3
    }
    compact_host = re.sub(r"[^a-z0-9]", "", _hostname(url))
    return any(token in compact_host for token in tokens)


def _has_current_employment(alias: str, title: str, snippet: str) -> bool:
    if any(f" {word} {alias} " in f" {title} " for word in ("at", "na", "en")):
        return True
    if f" bei {alias} " in f" {title} {snippet[:160]} ":
        return True
    current = re.compile(r"\b(current|currently|present|atual|presente)\b")
    for match in re.finditer(rf"(?<!\w){re.escape(alias)}(?!\w)", snippet):
        window = snippet[max(0, match.start() - 80) : match.end() + 120]
        if current.search(window) and len(re.findall(r"\b(?:19|20)\d{2}\b", window)) < 2:
            return True
    return False


def _is_linkedin_profile(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    return (host == "linkedin.com" or host.endswith(".linkedin.com")) and parsed.path.startswith(
        "/in/"
    )


def _is_linkedin_company(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    return (host == "linkedin.com" or host.endswith(".linkedin.com")) and parsed.path.startswith(
        "/company/"
    )


def _linkedin_company_urls(url: str) -> list[str]:
    if not _is_linkedin_company(url):
        return []
    parsed = urlparse(url.strip())
    public_url = parsed._replace(netloc="tt.linkedin.com", query="", fragment="").geturl()
    return _unique_urls("", [url, public_url])


def _is_pdf_url(url: str) -> bool:
    # ponytail: extension routing covers discovered PDFs; add HEAD sniffing only if
    # extensionless PDF links prove common in live samples.
    return urlparse(url).path.casefold().endswith(".pdf")


def _url_key(url: str) -> str:
    return urlparse(url.strip())._replace(fragment="").geturl().rstrip("/")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
