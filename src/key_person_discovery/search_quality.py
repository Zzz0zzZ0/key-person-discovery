"""Conservative search triage, before a response consumes result slots."""

import re
from urllib.parse import unquote, urlsplit, urldefrag

from .models import CompanyProfile, SearchResult, company_name_aliases, normalize_company_name, normalize_linkedin
from .sources import official_link_priority


def classify_search_result(company: CompanyProfile, result: SearchResult) -> tuple[str, str]:
    try:
        parsed = urlsplit(result.url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
    except ValueError:
        return "rejected", "invalid_url"
    if parsed.scheme not in {"http", "https"} or not host or parsed.username:
        return "rejected", "invalid_url"
    path = unquote(parsed.path).rstrip("/")
    if path.lower() in {"/login", "/signin", "/sign-in", "/signup", "/sign-up"} or (
        (host == "facebook.com" or host.endswith(".facebook.com"))
        and path.lower() in {"/r.php", "/login.php"}
    ):
        return "rejected", "authentication_page"

    # An exact query echoed as a title is not independent company evidence.
    query = result.query.strip().casefold()
    if len(query) >= 20 and re.search(r'site:|filetype:|"|\bor\b', query) and query in result.title.casefold():
        return "rejected", "query_echo"

    # Enforce only one positive site restriction. Do not guess OR/negative syntax.
    sites = re.findall(r'(?<![\w-])site:([^\s()]+)', result.query, flags=re.I)
    if len(sites) == 1:
        try:
            site = urlsplit("https://" + sites[0].strip('"').removeprefix("https://").removeprefix("http://"))
        except ValueError:
            return "pending", "unsupported_site_syntax"
        expected = (site.hostname or "").lower().removeprefix("www.")
        prefix = unquote(site.path).rstrip("/")
        if expected and not (host == expected or host.endswith("." + expected)):
            return "rejected", "site_host_mismatch"
        if prefix and not (path == prefix or path.startswith(prefix + "/")):
            return "rejected", "site_path_mismatch"

    if _official_scope(company, result.url):
        return "eligible", "official_scope"
    text = " " + normalize_company_name(unquote(f"{result.title} {result.snippet} {result.url}")) + " "
    matches = [alias for alias in company_name_aliases(company.name) if " " + alias + " " in text]
    if matches:
        context = f"{result.title} {result.snippet}"
        # A short contact excerpt on the matching brand domain still has an
        # independent identity signal. This is initial relevance, not verification.
        brand_contact = (host.split(".")[0] in {alias.replace(" ", "") for alias in matches}
                         and official_link_priority(result.url, result.title) == 0)
        if any(len(alias.split()) > 1 for alias in matches) or _BUSINESS.search(context) or brand_contact:
            return "eligible", "company_mentioned"
        return "pending", "ambiguous_company_name"
    return "pending", "company_not_supported"


_BUSINESS = re.compile(
    r"\b(company|corporation|manufacturer|supplier|procurement|purchasing|sourcing|"
    r"engineering|industrial|director|manager|executive|employees?|"
    r"gmbh|ltd|plc|inc|unternehmen|hersteller|geschaftsfuhrer|geschäftsführer|einkauf|"
    r"fabricant|entreprise|achats|fabricante|empresa|compras)\b|公司|采购|制造|企业|供应商",
    re.I,
)
_ROLE = re.compile(
    r"\b(procurement|purchasing|director|manager|officer|engineer|president|ceo|cpo|"
    r"geschäftsführer|geschaftsfuhrer|leiter|einkauf|directeur|responsable|gerente)\b|采购|经理|总监|工程师",
    re.I,
)


def _official_scope(company: CompanyProfile, url: str) -> bool:
    if not company.website:
        return False
    try:
        official, target = urlsplit(company.website), urlsplit(url)
        host = (official.hostname or "").lower().removeprefix("www.")
        other = (target.hostname or "").lower().removeprefix("www.")
    except ValueError:
        return False
    scope = unquote(official.path).rstrip("/")
    if re.search(r"/index\.(?:html?|php|aspx?)$", scope, re.I):
        scope = scope.rsplit("/", 1)[0]
    path = unquote(target.path).rstrip("/")
    return bool(host and (other == host or other.endswith("." + host)) and (
        not scope or path == scope or path.startswith(scope + "/")))


def _named_role(company: CompanyProfile, result: SearchResult) -> bool:
    text = f"{result.title} {result.snippet}"
    if not _ROLE.search(text):
        return False
    aliases = company_name_aliases(company.name)
    # An exact person seed in our query can identify non-Latin names as well.
    seeds = [name for name in re.findall(r'"([^"()]+)"', result.query)
             if (2 <= len(name.split()) <= 4 and name[0].isupper())
             or re.fullmatch(r"[\u3400-\u9fff]{2,6}", name)]
    title_parts = re.split(r"\s+[-–—|]\s+", result.title, maxsplit=1)
    title_name = title_parts[0].strip()
    # Independent conference/profile titles often begin with the person's name.
    words = title_name.split()
    if (len(title_parts) == 2 and _ROLE.match(title_parts[1])
            and 2 <= len(words) <= 4
            and all(w[0].isupper() and w.replace("-", "").isalpha() for w in words)):
        seeds.append(title_name)
    normalized_text = " " + normalize_company_name(text) + " "
    for seed in seeds:
        name = normalize_company_name(seed)
        if (name and name not in aliases and not _ROLE.search(seed)
                and " " + name + " " in normalized_text
                and not any(name in alias or alias in name for alias in aliases)):
            return True
    return False


def search_result_value(company: CompanyProfile, result: SearchResult) -> tuple[int, str]:
    """Stable tiers, not an inferred probability or a contact verification."""
    parsed = urlsplit(result.url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = unquote(parsed.path).lower()
    if path.rstrip('/').rsplit('/', 1)[-1] in {'thank-you', 'thankyou', 'thanks', 'confirmation'}:
        return 7, "form_confirmation"
    if re.search(r"/(?:jobs?|careers?|vacancies)(?:/|$)", path) or re.search(
        r"\b(hiring|vacancy|vacancies|apply for|job opening)\b", result.title, re.I
    ):
        return 6, "recruitment"
    if _official_scope(company, result.url) and official_link_priority(result.url, result.title) == 0:
        return 0, "official_contact"
    if (host == "linkedin.com" or host.endswith(".linkedin.com")) and re.match(r"/in/[^/]+", path):
        return 1, "personal_profile"
    if _named_role(company, result):
        return 2, "named_role_evidence"
    if _official_scope(company, result.url):
        return 3, "official_page"
    normalized_path = " " + normalize_company_name(path) + " "
    if (re.search(r"/(?:blog|news|interviews?|events?|case-studies|customer-stories)/", path)
            and any(" " + alias + " " in normalized_path for alias in company_name_aliases(company.name))
            and _ROLE.search(result.title + " " + result.snippet)):
        return 4, "company_article"
    if re.search(r"\b(directory|headcount|company profile|org chart)\b|employee[- ]directory|staff[- ]directory|/(?:companies|company)/", result.title + " " + path, re.I):
        return 5, "directory"
    return 4, "company_evidence"


def select_search_results(company: CompanyProfile, pool: list[SearchResult], limit: int):
    eligible, pending, decisions = [], [], []
    for item in pool:
        status, reason = classify_search_result(company, item)
        priority, kind = search_result_value(company, item) if status != "rejected" else (9, "rejected")
        decisions.append({"result": item.as_dict(), "status": status, "reason": reason, "page_type": kind})
        if status == "eligible":
            eligible.append((priority, item))
        elif status == "pending":
            pending.append(item)
    # Stable ties preserve provider order; duplicates cannot consume result slots.
    selected, seen = [], set()
    for _, item in sorted(eligible, key=lambda pair: pair[0]):
        key = normalize_linkedin(item.url) or urldefrag(item.url)[0].rstrip("/")
        if key not in seen:
            seen.add(key)
            selected.append(item)
    return selected[:limit], pending[:limit], decisions


def engine_quality_report(audits, selected, candidates, warnings):
    """Observed attribution only; shared origins are not additive contact gains."""
    from collections import Counter, defaultdict

    counts = defaultdict(Counter)
    observations = set()
    urls, people, channels = defaultdict(set), defaultdict(set), defaultdict(set)
    origins = defaultdict(set)
    audited_origins = defaultdict(set)
    key = lambda url: normalize_linkedin(url) or urldefrag(url)[0].rstrip('/')
    for audit in audits:
        for decision in audit['decisions']:
            hit = decision['result']
            engines = hit.get('engines') or [audit['provider'] + ':unattributed' if audit['provider'] == 'searxng' else audit['provider']]
            if decision['status'] != 'rejected':
                audited_origins[key(hit['url'])].update(engines)
            for engine in set(engines):
                identity = (engine, audit['query'], key(hit['url']))
                if identity not in observations:
                    observations.add(identity)
                    counts[engine][decision['status']] += 1
    for hit in selected:
        engines = audited_origins[key(hit.url)] or hit.engines or [hit.provider + ':unattributed' if hit.provider == 'searxng' else hit.provider or 'unknown']
        for engine in set(engines):
            urls[engine].add(key(hit.url))
            counts[engine]  # Include engines found only in legacy selected results.
            if engine != 'unknown' and not engine.endswith(':unattributed'):
                origins[key(hit.url)].add(engine)
    unattributed, attributed = set(), set()
    for person in candidates or []:
        name = normalize_company_name(str(person.get('full_name', '')))
        if not name or person.get('company_match') != 'verified' or person.get('crm_existing_match'):
            continue
        for field in ('emails', 'phones', 'linkedin'):
            for contact in person.get(field, []):
                if not contact.get('value') or contact.get('status') == 'guessed':
                    continue
                identity = (name, field, contact['value'])
                engines = origins.get(key(str(contact.get('source_url', ''))), set())
                if not engines:
                    unattributed.add(identity)
                else:
                    attributed.add(identity)
                for engine in engines:
                    people[engine].add(name)
                    channels[engine].add(identity)
    for warning in warnings:
        if warning.get('provider') in {'searxng', 'anysearch', 'tavily'}:
            counts[warning['engine']]['warnings'] += 1
    return {'engines': {engine: {'observations': sum(c[s] for s in ('eligible', 'pending', 'rejected')),
                                **{s: c[s] for s in ('eligible', 'pending', 'rejected', 'warnings')},
                                'selected_unique_urls': len(urls[engine]),
                                'exclusive_selected_unique_urls': sum(origins[url] == {engine} for url in urls[engine]),
                                'retained_people_with_source_match': len(people[engine]) if candidates is not None else None,
                                'retained_channels_with_source_match': len(channels[engine]) if candidates is not None else None}
                        for engine, c in sorted(counts.items())},
            'channels_evaluated': candidates is not None,
            'unattributed_retained_channels': len(unattributed - attributed) if candidates is not None else None,
            'policy': 'Observe only. Shared engine credit cannot be summed as incremental people. '
                      'Channels use exact normalized source URL matches; missing provenance is unknown. '
                      'Warnings count local observations, including suspension; no automatic weight changes.'}
