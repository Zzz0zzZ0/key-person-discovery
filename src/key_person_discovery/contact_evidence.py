"""Keep bounded HTML contact sections and explicit link conflicts as evidence.

This extracts page structure, not identities or employment. Hermes still makes
the person/role decision; the normal validator and CRM dedup remain mandatory.
"""
from __future__ import annotations

import copy
import re
from urllib.parse import unquote

from bs4 import BeautifulSoup
import phonenumbers

from .models import CompanyProfile, CrawledPage, same_site
from .signals import _normalize_number, normalize_observed_emails


def contact_metadata(html: str) -> tuple[list[str], list[dict]]:
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup(['script', 'style', 'noscript', 'svg']):
        node.decompose()
    conflicts = []
    for a in soup.select('a[href]'):
        href = unquote(a['href'])
        scheme, _, target = href.partition(':')
        visible = normalize_observed_emails(a.get_text(' ', strip=True))
        target = target.split('?', 1)[0].strip()
        if scheme.lower() == 'mailto':
            # lower(), not casefold(): an address containing ß is not an ss alias.
            addresses = re.findall(r"[\w.+%'-]+@[\w.-]+\.[\w-]+", visible)
            visible_value = addresses[0].lower() if len(addresses) == 1 else ''
            linked_value = target.lower() if re.fullmatch(r'[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+', target) else ''
            channel = 'email'
        elif scheme.lower() == 'tel':
            def number(value, region=None):
                if value.startswith('00'):
                    value = '+' + value[2:]
                return _normalize_number(value, region)
            linked_value = number(target)
            region = phonenumbers.region_code_for_number(phonenumbers.parse(linked_value, None)) if linked_value else None
            visible_value = number(visible, region)
            channel = 'phone'
        else:
            continue
        if visible_value and linked_value and visible_value != linked_value:
            conflicts.append({'channel': channel, 'visible': visible, 'href': href,
                              'values': [visible_value, linked_value]})

    blocks = []
    seen = set()
    for heading in soup.find_all(['h2', 'h3', 'h4', 'h5', 'h6']):
        if heading.find_parent(['nav', 'footer', 'blockquote']):
            continue
        # ponytail: bounded single-heading containers; other layouts keep normal prose extraction.
        for parent in list(heading.parents)[:4]:
            if parent.name in {'body', 'html', 'main', '[document]'}:
                break
            if parent.find(['nav', 'footer', 'blockquote']):
                break
            if len(parent.find_all(['h2', 'h3', 'h4', 'h5', 'h6'])) != 1:
                break
            text = parent.get_text(' ', strip=True)
            if len(text) > 2500:
                break
            if not any(a['href'].lower().startswith(('mailto:', 'tel:')) for a in parent.find_all('a', href=True)):
                continue
            block = copy.copy(parent)
            for a in block.find_all('a', href=True):
                if a['href'].lower().startswith(('mailto:', 'tel:')):
                    visible = a.get_text(' ', strip=True)
                    href = unquote(a['href'])
                    # A second rendered phone would lose an adjacent Fax label.
                    if href.lower().startswith('tel:') and any(c.isdigit() for c in visible):
                        a.replace_with(visible)
                    else:
                        a.replace_with(f"{visible} [{href}]")
            content = normalize_observed_emails(block.get_text('\n', strip=True))
            section = heading.find_previous('h2')
            if section and section is not heading and section not in parent.descendants:
                content = section.get_text(' ', strip=True)[:200] + '\n' + content
            if content not in seen:
                blocks.append(content)
                seen.add(content)
            break
    return blocks, conflicts


def contact_block_pages(company: CompanyProfile, pages: list[CrawledPage]) -> list[CrawledPage]:
    """Prioritize only fetched same-site blocks; third-party roles need full context."""
    return [
        CrawledPage(page.url, block, provider=page.provider, source_type='contact_card')
        for page in pages if not page.error and same_site(company.website, page.url)
        for block in page.contact_blocks
    ]
