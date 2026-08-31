from __future__ import annotations

import re
from typing import Any

import phonenumbers

from .models import CrawledPage, canonical_contact_value


EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I)
LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%./?=&-]+", re.I)
WHATSAPP_RE = re.compile(
    r"https?://(?:wa\.me/|api\.whatsapp\.com/send\?phone=)(\+?\d[\d -]{5,18})",
    re.I,
)
WHATSAPP_LABEL_RE = re.compile(
    r"\bwhats\s*app\b(?:(?!\n\s*\n).){0,160}?(\+\s*\d[\d\s()./-]{5,24}\d)",
    re.I | re.S,
)


def extract_contact_signals(
    pages: list[CrawledPage], default_region: str | None = None
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for page in pages:
        if not page.markdown:
            continue
        text = page.markdown
        emails = [email.lower() for email in EMAIL_RE.findall(text)]
        email_set = set(emails)
        for email in emails:
            local, domain = email.split("@", 1)
            if any(
                local.startswith(prefix)
                and f"{local.removeprefix(prefix)}@{domain}" in email_set
                for prefix in ("mailto", "to")
            ):
                continue
            status = "probable" if page.source_type == "contact_excerpt" else "observed"
            _append(signals, seen, "email", email, page.url, status)
        for linkedin in LINKEDIN_RE.findall(text):
            clean = linkedin.rstrip(".,;:)]}")
            status = "probable" if page.source_type == "search_excerpt" else "observed"
            _append(signals, seen, "linkedin", clean, page.url, status)

        whatsapp_numbers: set[str] = set()
        for raw in WHATSAPP_RE.findall(text):
            raw = raw if raw.lstrip().startswith("+") else f"+{raw.strip()}"
            number = _normalize_number(raw, default_region)
            if number:
                whatsapp_numbers.add(number)
                _append(signals, seen, "whatsapp", number, page.url, "verified")
                parsed = phonenumbers.parse(number, None)
                _append(
                    signals,
                    seen,
                    "phone",
                    number,
                    page.url,
                    "valid_format" if phonenumbers.is_valid_number(parsed) else "possible_format",
                    whatsapp_status="verified",
                )
        for raw in WHATSAPP_LABEL_RE.findall(text):
            number = _normalize_number(raw, default_region)
            if number:
                whatsapp_numbers.add(number)
                _append(signals, seen, "whatsapp", number, page.url, "verified")

        for match in phonenumbers.PhoneNumberMatcher(text, default_region):
            if _is_fax_match(text, match.start):
                continue
            number = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
            status = "valid_format" if phonenumbers.is_valid_number(match.number) else "possible_format"
            _append(
                signals,
                seen,
                "phone",
                number,
                page.url,
                status,
                whatsapp_status="verified" if number in whatsapp_numbers else "unknown",
                evidence_status=(
                    "probable" if page.source_type == "contact_excerpt" else "observed"
                ),
            )
    return signals


def _is_fax_match(text: str, start: int) -> bool:
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start]
    return bool(
        re.search(
            r"\b(?:telefax|fax|facsimile|t[ée]l[ée]copie)\b\s*[:：-]?\s*$",
            prefix,
            re.I,
        )
    )


def _normalize_number(raw: str, default_region: str | None) -> str | None:
    try:
        number = phonenumbers.parse(raw.replace(" ", "").replace("-", ""), default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(number):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def _append(
    output: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    channel: str,
    value: str,
    source_url: str,
    status: str,
    **extra: Any,
) -> None:
    value = canonical_contact_value(channel, value)
    key = (channel, value, source_url)
    if key in seen:
        return
    seen.add(key)
    output.append(
        {
            "channel": channel,
            "value": value,
            "status": status,
            "source_url": source_url,
            **extra,
        }
    )
