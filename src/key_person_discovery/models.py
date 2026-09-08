from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote, unquote, urlparse


_LEGAL_SUFFIXES = (
    ("corporation",),
    ("incorporated",),
    ("company",),
    ("limited",),
    ("corp",),
    ("inc",),
    ("llc",),
    ("ltd",),
    ("co", "ltd"),
    ("s", "a"),
    ("s", "l"),
    ("s", "p", "a"),
    ("sa",),
    ("sl",),
    ("spa",),
    ("gmbh",),
    ("plc",),
    ("pty",),
    ("pte",),
    ("bv",),
    ("nv",),
    ("ag",),
    ("ab",),
    ("oy",),
)


@dataclass(frozen=True)
class CompanyProfile:
    name: str
    website: str = ""
    linkedin_url: str = ""
    industries: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    crm_contact_count: int | None = None
    target_contact_count: int = 0
    customs_search_enabled: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CompanyProfile":
        name = str(value.get("name", "")).strip()
        if not name:
            raise ValueError("company.name is required")
        return cls(
            name=name,
            website=str(value.get("website", "")).strip(),
            linkedin_url=str(value.get("crm_linkedin_url") or value.get("linkedin_url") or "").strip(),
            industries=_string_list(value.get("industries")),
            products=_string_list(value.get("products")),
            crm_contact_count=_optional_nonnegative_int(value.get("crm_contact_count")),
            target_contact_count=_nonnegative_int(value.get("target_contact_count", 0)),
            customs_search_enabled=_boolean(value.get("customs_search_enabled", False)),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchResult:
    query: str
    title: str
    url: str
    snippet: str
    provider: str = ""
    rank: int = 0
    source_type: str = "web"
    retrieved_at: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class SearchOutcome:
    results: list[SearchResult]
    unresponsive_engines: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class CrawledPage:
    url: str
    markdown: str
    error: str = ""
    provider: str = ""
    source_type: str = "crawl"
    page_count: int = 0
    contact_blocks: list[str] = field(default_factory=list)
    contact_conflicts: list[dict[str, Any]] = field(default_factory=list)
    requested_url: str = ""
    title: str = ""
    links: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("industries and products must be arrays")
    return [str(item).strip() for item in value if str(item).strip()]


def _optional_nonnegative_int(value: Any) -> int | None:
    return None if value is None else _nonnegative_int(value)


def _nonnegative_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("contact counts must be integers") from exc
    if result < 0:
        raise ValueError("contact counts cannot be negative")
    return result


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("customs_search_enabled must be a boolean")
    return value


def normalize_company_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def normalize_linkedin(value: str) -> str:
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return ""
    parts = [part for part in unquote(parsed.path).split("/") if part]
    if len(parts) >= 2 and parts[0].casefold() in {"in", "company", "school"}:
        parts = parts[:2]
    path = quote("/" + "/".join(parts).casefold(), safe="/-._~")
    return f"https://www.linkedin.com{path}"


def canonical_contact_value(channel: str, value: str) -> str:
    value = value.strip()
    if channel == "linkedin":
        return normalize_linkedin(value) or value
    if channel == "email":
        return value.casefold()
    return value


def company_name_aliases(value: str) -> set[str]:
    aliases = {normalize_company_name(value)}
    for item in re.findall(r"\(([^()]*)\)", value):
        alias = normalize_company_name(item)
        if len(alias.replace(" ", "")) >= 4:
            aliases.add(alias)

    base = normalize_company_name(re.sub(r"\([^()]*\)", " ", value))
    tokens = base.split()
    stripped = True
    while stripped and tokens:
        stripped = False
        for suffix in _LEGAL_SUFFIXES:
            if tuple(tokens[-len(suffix) :]) == suffix:
                del tokens[-len(suffix) :]
                stripped = True
                break
    if tokens:
        aliases.add(" ".join(tokens))
    return {alias for alias in aliases if alias}


def same_site(first_url: str, second_url: str) -> bool:
    return bool(_site_key(first_url) and _site_key(first_url) == _site_key(second_url))


def _site_key(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    parts = host.split(".")
    if len(parts) < 2:
        return host
    suffix = 3 if len(parts[-1]) == 2 and parts[-2] in {"ac", "co", "com", "gov", "net", "org"} else 2
    return ".".join(parts[-suffix:])
