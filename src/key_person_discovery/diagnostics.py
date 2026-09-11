"""Choose one bounded recovery action from observed discovery gaps."""
from __future__ import annotations

import re

from .models import normalize_company_name, company_name_aliases
from .sources import build_people_queries, person_query_name, person_search_name


def diagnose(result, pages, *, website_status='', website='', target=1, contactable_people=0):
    people = [p for p in result.get('candidates', [])
              if p.get('company_match') == 'verified' and not p.get('crm_existing_match')]
    pending = result.get('unverified_candidates', [])
    missing = [p for p in people if not any(c.get('value') and c.get('status') != 'guessed'
               for field in ('emails', 'phones', 'linkedin') for c in p.get(field, []))]
    direct = [p for p in pages if p.source_type not in {'search_excerpt', 'contact_excerpt', 'contact_card'}]
    successful = sum(bool(p.markdown.strip()) and not p.error for p in direct)
    usable = any(p.markdown.strip() and not p.error for p in pages)
    duplicates = len({normalize_company_name(p.get('full_name', ''))
                      for p in result.get('crm_duplicates', []) if p.get('full_name')})
    reasons = []
    if website_status == 'unverified_redirect' or not website:
        reasons.append('website_unverified')
    if any(p.error for p in direct):
        reasons.append('crawl_failed')
    if not usable:
        reasons.append('no_evidence')
    if missing:
        reasons.append('missing_channels')
    if pending:
        reasons.append('employment_uncertain')
    if duplicates:
        reasons.append('crm_duplicate')
    if not people and not pending and not duplicates:
        reasons.append('no_target_people')
    if contactable_people >= target:
        primary = 'target_met'
    elif not usable:
        primary = 'website_unverified' if 'website_unverified' in reasons else 'crawl_failed' if 'crawl_failed' in reasons else 'no_evidence'
    elif website_status == 'unverified_redirect' and not people:
        primary = 'website_unverified'
    elif missing:
        primary = 'missing_channels'
    elif pending:
        primary = 'employment_uncertain'
    elif not people and duplicates:
        primary = 'crm_duplicate'
    else:
        primary = 'no_target_people' if not people else 'insufficient_people'
    return dict(primary=primary, reasons=reasons, counts=dict(
        fetched_documents=len(direct), successful_documents=successful,
        failed_documents=sum(bool(p.error) for p in direct), verified_people=len(people),
        people_without_channels=len(missing), pending_people=len(pending),
        crm_duplicate_people=duplicates, new_contactable_people=contactable_people, target=target))


def recovery_queries(company, result, diagnostic, phone_region=None):
    reason = diagnostic['primary']
    if reason in {'target_met', 'website_unverified', 'crawl_failed', 'no_evidence'}:
        # Repeating person searches cannot repair an unverified website or an outage.
        return []
    full_name_pending = [p for p in result.get('unverified_candidates', [])
                         if len(person_query_name(str(p.get('full_name', ''))).split()) >= 2
                         and len(person_query_name(str(p.get('full_name', ''))).split()[0].strip('.')) > 1
                         and not p.get('crm_existing_match')]
    initial_only = result.get('candidates') and all(
        person_search_name(str(p.get('full_name', ''))) != person_query_name(str(p.get('full_name', '')))
        for p in result['candidates'] if not p.get('crm_existing_match'))
    if reason == 'employment_uncertain' or (reason == 'missing_channels' and initial_only and full_name_pending):
        names = list(dict.fromkeys(person_search_name(str(p.get('full_name', '')))
                    for p in (full_name_pending or result.get('unverified_candidates', [])) if not p.get('crm_existing_match')))
        brand = min(company_name_aliases(re.sub(r'\([^)]*\)', '', company.name)) or {normalize_company_name(company.name)}, key=lambda alias: (len(alias), alias))
        return [f'"{name}" "{brand}" (current OR present OR director OR manager OR aktuell OR Geschäftsführer)'
                for name in names[:3] if name]
    if reason == 'missing_channels':
        return build_people_queries(company, result, phone_region)
    queries = build_people_queries(company, {}, phone_region)
    if reason == 'crm_duplicate':
        names = list(dict.fromkeys(person_query_name(str(p.get('full_name', '')))
                    for p in result.get('crm_duplicates', []) if p.get('full_name')))[:5]
        exclusions = ' '.join(f'-"{name}"' for name in names if name)
        if exclusions:
            queries = [f'{query} {exclusions}' for query in queries]
    return queries


def extraction_failure(exc, stage):
    # Never serialize arbitrary model output, subprocess stderr, tokens or URLs.
    messages = {
        'Hermes output does not contain a JSON object': 'invalid_json',
        'Hermes output company_name does not match the input company': 'company_mismatch',
        'Hermes output candidates must be an array': 'invalid_candidates',
        'Hermes output must require human review': 'missing_review_flag',
        'Every candidate must have a full_name': 'missing_person_name',
        'Every candidate must require human review': 'missing_person_review_flag',
        'Candidate influence_type is invalid': 'invalid_role_type',
        'Every candidate must have evidence': 'missing_evidence',
        'Every evidence item must have a source_url': 'missing_evidence_url',
        'Candidate email status is invalid': 'invalid_email_status',
    }
    code = messages.get(str(exc), 'timeout' if isinstance(exc, TimeoutError) or type(exc).__name__ == 'TimeoutExpired' else 'processing_error')
    return dict(stage=stage, error_type=type(exc).__name__, code=code)
