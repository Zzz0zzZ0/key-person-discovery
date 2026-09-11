from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .hermes import HermesExtractor
from .jobs import PROJECT_DIR, project_storage_path
from .models import CompanyProfile
from .pipeline import discover
from .preflight import verify_proxy_setup
from .sources import AnySearchClient, Crawl4aiCrawler, SearxngClient


_GLOBAL_SEARXNG_ENGINES = ("google cse",)
_LOCAL_SEARXNG_ENGINE_BY_REGION = {
    "BY": "yandex",
    "CN": "baidu",
    "KG": "yandex",
    "KR": "naver",
    "KZ": "yandex",
    "RU": "yandex",
    "UZ": "yandex",
}


def _parse_searxng_engines(value: str) -> list[str]:
    return [engine.strip() for engine in value.split(",") if engine.strip()]


def _searxng_engines_for_region(phone_region: str | None) -> list[str]:
    local_engine = _LOCAL_SEARXNG_ENGINE_BY_REGION.get(
        str(phone_region or "").strip().upper()
    )
    if not local_engine:
        return []
    return [*_GLOBAL_SEARXNG_ENGINES, local_engine]


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover public-web key persons for one company")
    parser.add_argument("--company", type=Path, required=True, help="Company profile JSON")
    parser.add_argument("--output", type=Path, required=True, help="Result JSON")
    parser.add_argument("--phone-region", help="ISO country code for local phone numbers, e.g. CN")
    parser.add_argument("--max-urls", type=int, default=20)
    parser.add_argument(
        "--topeasy-export",
        type=Path,
        help="Optional TopEasy decision-maker CSV export (.xls filename is supported)",
    )
    args = parser.parse_args()
    if not 1 <= args.max_urls <= 100:
        parser.error("--max-urls must be between 1 and 100")
    if args.topeasy_export is not None and not args.topeasy_export.is_file():
        parser.error("--topeasy-export must be an existing file")
    try:
        output_path = project_storage_path(args.output, "--output")
    except ValueError as exc:
        parser.error(str(exc))

    company_input = json.loads(args.company.read_text(encoding="utf-8"))
    company = CompanyProfile.from_dict(company_input)
    crm_contacts = company_input.get("crm_contacts", [])
    if not isinstance(crm_contacts, list) or not all(
        isinstance(contact, dict) for contact in crm_contacts
    ):
        parser.error("company.crm_contacts must be an array of objects")
    searxng_url = os.getenv("SEARXNG_URL", "http://127.0.0.1:18080").strip()
    proxy_url = (
        os.getenv("KEY_PERSON_PROXY_URL", "").strip()
        or os.getenv("HTTPS_PROXY", "").strip()
        or os.getenv("HTTP_PROXY", "").strip()
    )
    if not proxy_url:
        parser.error("KEY_PERSON_PROXY_URL is required")
    try:
        timeout = int(os.getenv("KEY_PERSON_TIMEOUT_SECONDS", "600"))
    except ValueError:
        parser.error("KEY_PERSON_TIMEOUT_SECONDS must be an integer")
    try:
        anysearch_query_limit = int(os.getenv("KEY_PERSON_ANYSEARCH_MAX_QUERIES", "5"))
    except ValueError:
        parser.error("KEY_PERSON_ANYSEARCH_MAX_QUERIES must be an integer")
    if not 0 <= anysearch_query_limit <= 100:
        parser.error("KEY_PERSON_ANYSEARCH_MAX_QUERIES must be between 0 and 100")
    try:
        people_search_limit = int(os.getenv("KEY_PERSON_PEOPLE_SEARCH_MAX_QUERIES", "3"))
    except ValueError:
        parser.error("KEY_PERSON_PEOPLE_SEARCH_MAX_QUERIES must be an integer")
    if not 0 <= people_search_limit <= 6:
        parser.error("KEY_PERSON_PEOPLE_SEARCH_MAX_QUERIES must be between 0 and 6")
    try:
        customs_anysearch_query_limit = int(
            os.getenv("KEY_PERSON_CUSTOMS_ANYSEARCH_MAX_QUERIES", "2")
        )
    except ValueError:
        parser.error("KEY_PERSON_CUSTOMS_ANYSEARCH_MAX_QUERIES must be an integer")
    if not 0 <= customs_anysearch_query_limit <= 3:
        parser.error("KEY_PERSON_CUSTOMS_ANYSEARCH_MAX_QUERIES must be between 0 and 3")
    broad_discovery = os.getenv(
        "KEY_PERSON_EXPERIMENTAL_BROAD_DISCOVERY", ""
    ).strip().casefold() in {"1", "true", "yes", "on"}
    experimental_searxng_engines = (
        _parse_searxng_engines(
            os.getenv("KEY_PERSON_EXPERIMENTAL_SEARXNG_ENGINES", "")
        )
        if broad_discovery
        else []
    )
    searxng_engines = experimental_searxng_engines or _searxng_engines_for_region(
        args.phone_region
    )

    crawler = Crawl4aiCrawler(proxy_url=proxy_url)
    egress_fingerprint = verify_proxy_setup(
        proxy_url=proxy_url,
        searxng_url=searxng_url,
        crawler=crawler,
        searxng_settings=Path(
            os.getenv(
                "SEARXNG_SETTINGS_PATH",
                str(PROJECT_DIR / "deploy" / "searxng" / "settings.yml"),
            )
        ),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    artifacts_dir = output_path.with_suffix(output_path.suffix + ".artifacts")
    searxng_client = (
        SearxngClient(searxng_url, engines=searxng_engines)
        if searxng_engines
        else SearxngClient(searxng_url)
    )
    result = discover(
        company=company,
        search_clients=[
            AnySearchClient(
                api_key=os.getenv("ANYSEARCH_API_KEY", ""),
                proxy_url=proxy_url,
            ),
            searxng_client,
        ],
        crawler=crawler,
        hermes=HermesExtractor(
            os.getenv("HERMES_COMMAND", "/Users/acelerzbw/.local/bin/hermes"),
            timeout=timeout,
        ),
        artifacts_dir=artifacts_dir,
        phone_region=args.phone_region,
        max_urls=args.max_urls,
        proxy_url=proxy_url,
        crm_contacts=crm_contacts,
        anysearch_query_limit=anysearch_query_limit,
        people_search_limit=people_search_limit,
        customs_anysearch_query_limit=customs_anysearch_query_limit,
        broad_discovery=broad_discovery,
        topeasy_export=args.topeasy_export,
    )
    result["run_summary"]["egress_fingerprint"] = egress_fingerprint
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_path.chmod(0o600)
    print(output_path)


if __name__ == "__main__":
    main()
