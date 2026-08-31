from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import CompanyProfile, canonical_contact_value, normalize_company_name, normalize_linkedin


_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+$")


def merge_topeasy_export(
    result: dict[str, Any], company: CompanyProfile, export_path: Path
) -> dict[str, Any]:
    """Merge a TopEasy decision-maker CSV export as explicitly unverified data."""

    domain = (urlparse(company.website).hostname or "").casefold().removeprefix("www.")
    if not domain:
        raise ValueError("TopEasy import requires company.website")

    with export_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"名称", "领英地址", "职位", "Email"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("TopEasy export is missing required columns")
        rows = list(reader)

    candidates = [
        candidate
        for group in ("candidates", "unverified_candidates")
        for candidate in result.get(group, []) or []
        if isinstance(candidate, dict)
    ]
    unverified = result.setdefault("unverified_candidates", [])
    public_contacts = result.setdefault("unassigned_contacts", [])
    summary: dict[str, Any] = {
        "source_file": export_path.name,
        "rows_total": len(rows),
        "candidates_added": 0,
        "candidates_enriched": 0,
        "public_contacts_added": 0,
        "foreign_emails_dropped": 0,
        "invalid_rows": 0,
    }

    def existing_candidate(name: str, linkedin: str) -> dict[str, Any] | None:
        normalized_name = normalize_company_name(name)
        for candidate in candidates:
            if normalized_name and normalize_company_name(
                str(candidate.get("full_name", ""))
            ) == normalized_name:
                return candidate
            if linkedin and any(
                normalize_linkedin(str(item.get("value", ""))) == linkedin
                for item in candidate.get("linkedin", []) or []
                if isinstance(item, dict)
            ):
                return candidate
        return None

    def add_contact(candidate: dict[str, Any], field: str, channel: str, value: str) -> None:
        existing = {
            canonical_contact_value(channel, str(item.get("value", "")))
            for item in candidate.get(field, []) or []
            if isinstance(item, dict)
        }
        if canonical_contact_value(channel, value) not in existing:
            candidate.setdefault(field, []).append(
                {
                    "value": value,
                    "status": "reported_unverified",
                    "verification_status": "ownership_unverified",
                    "source_url": company.website,
                    "provider": "topeasy",
                }
            )

    public_values = {
        canonical_contact_value(str(item.get("channel", "")), str(item.get("value", "")))
        for item in public_contacts
        if isinstance(item, dict)
    }
    for row in rows:
        name = str(row.get("名称", "")).strip()
        title = str(row.get("职位", "")).strip()
        linkedin = normalize_linkedin(str(row.get("领英地址", "")).strip())
        email = str(row.get("Email", "")).strip().casefold()
        if email and not _EMAIL.fullmatch(email):
            email = ""
            summary["invalid_rows"] += 1
        email_is_company = bool(email and email.rsplit("@", 1)[-1] == domain)
        if email and not email_is_company:
            summary["foreign_emails_dropped"] += 1

        if name and (linkedin or email_is_company):
            candidate = existing_candidate(name, linkedin)
            if candidate is None:
                candidate = {
                    "full_name": name,
                    "current_title": title,
                    "company_match": "probable",
                    "discovery_tier": "third_party_unverified",
                    "influence_type": "other",
                    "influence_score": 0,
                    "linkedin": [],
                    "emails": [],
                    "phones": [],
                    "evidence": [],
                    "confidence": 0.45,
                    "review_required": True,
                    "validation_reasons": [
                        "TopEasy associates this person with the company; current employment is not independently verified"
                    ],
                }
                unverified.append(candidate)
                candidates.append(candidate)
                summary["candidates_added"] += 1
            else:
                summary["candidates_enriched"] += 1
                if title and not str(candidate.get("current_title", "")).strip():
                    candidate["current_title"] = title
            if linkedin:
                add_contact(candidate, "linkedin", "linkedin", linkedin)
            if email_is_company:
                add_contact(candidate, "emails", "email", email)
            candidate.setdefault("evidence", []).append(
                {
                    "source_url": company.website,
                    "quote": "TopEasy decision-maker export",
                    "supports": "third-party person/company association; manual review required",
                    "provider": "topeasy",
                }
            )
            continue

        if email_is_company:
            key = canonical_contact_value("email", email)
            if key not in public_values:
                public_contacts.append(
                    {
                        "channel": "email",
                        "value": email,
                        "status": "reported_unverified",
                        "verification_status": "ownership_unverified",
                        "source_url": company.website,
                        "provider": "topeasy",
                        "company_public": True,
                        "binding_status": "unbound",
                    }
                )
                public_values.add(key)
                summary["public_contacts_added"] += 1

    if summary["candidates_added"] or summary["candidates_enriched"]:
        result["review_required"] = True
    return summary
