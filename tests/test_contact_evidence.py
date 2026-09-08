import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from key_person_discovery.contact_evidence import contact_metadata, contact_block_pages
from key_person_discovery.hermes import _build_prompt, _validate_output
from key_person_discovery.models import CompanyProfile, CrawledPage
from key_person_discovery.pipeline import discover, _attach_evidence_bound_contacts, _remove_conflicting_contacts, _exclude_unrelated_roles, _demote_navigation_only_company_matches
from key_person_discovery.signals import extract_contact_signals, normalize_observed_emails
from key_person_discovery.sources import Crawl4aiCrawler

URL = 'https://example.com/team'
COMPANY = CompanyProfile('Example Minerals', website='https://example.com')
HTML = '''<main><h2>Technical contacts</h2>
<article><header><h3>Jane Doe</h3></header><p>Production manager
<a href="mailto:jane@example.com">jane (at) example.com</a>
<a href="tel:+12025550123">+1 202 555 0123</a></p></article>
<div><h3>John Roe</h3><p>Application engineer
<a href="mailto:john@example.com">Email</a></p></div>
<footer><div><h3>Office</h3><a href="mailto:info@example.com">info@example.com</a></div></footer></main>'''


def page_from_html(html=HTML):
    blocks, conflicts = contact_metadata(html)
    return CrawledPage(URL, 'Example Minerals', contact_blocks=blocks, contact_conflicts=conflicts)


def candidate(name='Jane Doe', email='jane@example.com', quote='Jane Doe jane (at) example.com'):
    return {'full_name': name, 'current_title': 'Production manager', 'company_match': 'verified',
            'influence_type': 'technical_influencer', 'review_required': True,
            'emails': [{'value': email, 'status': 'observed', 'source_url': URL}],
            'phones': [], 'linkedin': [], 'evidence': [{'source_url': URL, 'quote': quote}]}


class ContactEvidenceTests(unittest.TestCase):
    def test_model_cannot_join_legal_representative_to_separate_office_section(self):
        for boundary in ['\n**Contact:**\n', '\n## Contact\n', '\n\n']:
            with self.subTest(boundary=boundary):
                page = CrawledPage(URL, 'Example Minerals\nRepresented by: Jane Doe' + boundary
                                   + 'office@example.com\nPhone: +1 202 555 0123')
                signals = extract_contact_signals([page], 'US')
                for signal in signals:
                    signal['company_public'] = True
                p = candidate(email='office@example.com', quote='Jane Doe office@example.com +1 202 555 0123')
                p['phones'] = [dict(value='+12025550123', source_url=URL, status='valid_format')]
                output = dict(company_name=COMPANY.name, candidates=[p], review_required=True)
                _validate_output(output, COMPANY, signals, {URL})
                _attach_evidence_bound_contacts(output)
                self.assertFalse(p['emails'])
                self.assertFalse(p['phones'])
                self.assertEqual(len(output['unassigned_contacts']), 2)

    def test_fetched_person_section_and_card_keep_explicit_channels(self):
        for page in [CrawledPage(URL, '## Jane Doe\nProduction manager\njane@example.com'),
                     CrawledPage(URL, 'Jane Doe\n\nProduction manager\n\njane@example.com', source_type='contact_card')]:
            signals = extract_contact_signals([page])
            p = candidate()
            output = dict(company_name=COMPANY.name, candidates=[p], review_required=True)
            _validate_output(output, COMPANY, signals, {URL})
            self.assertEqual(len(p['emails']), 1)

    def test_channel_values_are_not_people_and_remain_public_contacts(self):
        signals = [dict(channel='email', value='sales@example.com', source_url=URL, status='observed', company_public=True)]
        for name in [' sales@example.com ', 'https://example.com/contact', '+1 202 555 0123']:
            with self.subTest(name=name):
                p = candidate(name, 'sales@example.com', name + ' sales@example.com')
                output = dict(company_name=COMPANY.name, candidates=[p], review_required=True)
                _validate_output(output, COMPANY, signals, {URL})
                self.assertEqual(output['candidates'], [])
                self.assertEqual(output['unassigned_contacts'][0]['value'], 'sales@example.com')
                self.assertEqual(output['validation_rejections'][0]['reasons'], ['contact channel is not a person name'])
        for name in ['李明', 'José García', 'Jean-Luc Martin']:
            output = dict(company_name=COMPANY.name, candidates=[candidate(name, 'sales@example.com', name + ' sales@example.com')], review_required=True)
            _validate_output(output, COMPANY, signals, {URL})
            self.assertEqual(len(output['candidates']), 1)

    def test_group_navigation_does_not_verify_subsidiary_employment(self):
        company = CompanyProfile('Example Minerals (Example Group)', website='https://example.com')
        menu = '[Example Minerals](https://example.com/companies/minerals)\nExample Group\n'
        for body, independent, expected in [
            ('Example Steel\nAlex Smith, Commercial Director', False, 'unverified_candidates'),
            ('Example Minerals\nAlex Smith, Commercial Director', False, 'candidates'),
            ('Example Steel\nAlex Smith, Commercial Director', True, 'candidates'),
        ]:
            with self.subTest(body=body, independent=independent):
                p = candidate('Alex Smith')
                if independent:
                    p['evidence'].append(dict(source_url='https://evidence.org/article', quote='Alex Smith leads Example Minerals'))
                result = dict(candidates=[p])
                _demote_navigation_only_company_matches(result, company, [CrawledPage(URL, menu + body)])
                self.assertEqual(result[expected], [p])
                if expected == 'unverified_candidates':
                    self.assertEqual(p['company_match'], 'uncertain')
                    self.assertEqual(p['discovery_tier'], 'unverified')
                    self.assertFalse(result['candidates'])

    def test_partial_cards_allow_uncovered_prose_but_not_other_card_values(self):
        page = page_from_html('<div><h3>Office</h3><a href="mailto:office@example.com">Email</a></div>')
        page = CrawledPage(URL, 'Alex Smith, Process Engineer, alex@example.com', contact_blocks=page.contact_blocks)
        signals = extract_contact_signals([page, *contact_block_pages(COMPANY, [page])])
        for email, expected in [('alex@example.com', True), ('office@example.com', False)]:
            with self.subTest(email=email):
                p = candidate('Alex Smith', email, 'Alex Smith, Process Engineer, ' + email)
                output = dict(company_name=COMPANY.name, candidates=[p], review_required=True)
                _validate_output(output, COMPANY, signals, {URL}, contact_blocks={URL: page.contact_blocks})
                self.assertEqual(bool(p['emails']), expected)

    def test_personnel_title_variants_and_mixed_roles(self):
        excluded = ['Personalreferent', 'Senior Personalreferentin', 'Personalsachbearbeiterin',
                    'Personalleiterin', 'Vice President Human Resources']
        retained = ['Personnel procurement / Einkauf', 'Personalreferentin / Produktionsleitung',
                    'CEO and HR Director', 'Personal assistant to CEO', 'Technical sales']
        people = []
        for i, title in enumerate(excluded + retained):
            p = candidate(name=f'Person {i}')
            p['current_title'] = title
            people.append(p)
        result = {'unverified_candidates': people}
        _exclude_unrelated_roles(result)
        self.assertEqual([p['current_title'] for p in result['excluded_candidates']], excluded)
        self.assertEqual([p['current_title'] for p in result['unverified_candidates']], retained)

    def test_named_whatsapp_anchor_survives_split_quotes_without_promoting_employment(self):
        phone = '+12025550187'
        link = 'https://wa.me/12025550187'
        page = CrawledPage(URL, f'[Alex Smith, Process Engineer]({link})\n[Robin West](https://wa.me/12025550188)')
        signals = extract_contact_signals([page], 'US')
        for s in signals:
            s['company_public'] = True
        for employer in ['verified', 'uncertain']:
            for include_phone in [True, False]:
                with self.subTest(employer=employer, include_phone=include_phone):
                    p = candidate('Alex Smith')
                    p.update(company_match=employer, emails=[], phones=[dict(value=phone, status='valid_format', whatsapp_status='verified', source_url=URL)] if include_phone else [],
                             evidence=[dict(source_url=URL, quote='Alex Smith, Process Engineer'), dict(source_url=URL, quote=link)])
                    output = dict(company_name=COMPANY.name, candidates=[p], review_required=True)
                    _validate_output(output, COMPANY, signals, {URL})
                    _attach_evidence_bound_contacts(output)
                    group = 'candidates' if employer == 'verified' else 'unverified_candidates'
                    self.assertEqual([c['value'] for c in output[group][0]['phones']], [phone])
                    self.assertEqual(p['company_match'], employer)
                    self.assertEqual(p['phones'][0]['whatsapp_status'], 'verified')
                    self.assertFalse(any(c['value'] == phone for c in output['unassigned_contacts']))

    def test_whatsapp_rescue_requires_fetched_matching_named_anchor_and_source(self):
        phone = '+12025550187'
        link = 'https://api.whatsapp.com/send?phone=12025550187'
        for text, source_type, evidence_url, name in [
            (f'[Robin West]({link})', 'crawl', URL, 'Alex Smith'),
            (f'[Alex Smith]({link})', 'search_excerpt', URL, 'Alex Smith'),
            (f'[Alex Smith]({link})', 'crawl', URL + '/other', 'Alex Smith'),
            (f'[Alex Smithson]({link})', 'crawl', URL, 'Alex Smith'),
            (f'Alex Smith\n\n[Contact us]({link})', 'crawl', URL, 'Alex Smith'),
        ]:
            with self.subTest(text=text, source_type=source_type, evidence_url=evidence_url):
                signals = extract_contact_signals([CrawledPage(URL, text, source_type=source_type)], 'US')
                p = candidate(name)
                p.update(emails=[], phones=[dict(value=phone, status='valid_format', source_url=URL)],
                         evidence=[dict(source_url=evidence_url, quote=name), dict(source_url=evidence_url, quote=link)])
                output = dict(company_name=COMPANY.name, candidates=[p], review_required=True)
                _validate_output(output, COMPANY, signals, {URL, evidence_url})
                self.assertEqual(p['phones'], [])

    def test_structure_keeps_separate_cards_and_label_only_mailto(self):
        page = page_from_html()
        self.assertEqual(len(page.contact_blocks), 2)
        self.assertNotIn('John Roe', page.contact_blocks[0])
        self.assertIn('Technical contacts', page.contact_blocks[0])
        cards = contact_block_pages(COMPANY, [page])
        emails = {s['value'] for s in extract_contact_signals(cards) if s['channel'] == 'email'}
        self.assertEqual(emails, {'jane@example.com', 'john@example.com'})
        self.assertEqual(contact_block_pages(CompanyProfile('Other', website='https://other.com'), [page]), [])
        failed = CrawledPage(URL, 'error page', error='denied', contact_blocks=page.contact_blocks)
        self.assertEqual(contact_block_pages(COMPANY, [failed]), [])

    def test_explicit_obfuscation_only_and_fax_regression(self):
        text = 'Jane jane[at]example.com; john (AT) example.org; meet at example.com\nFax: +1 202 555 0198'
        signals = extract_contact_signals([CrawledPage(URL, text)], 'US')
        self.assertEqual({s['value'] for s in signals}, {'jane@example.com', 'john@example.org'})
        self.assertIn('meet at example.com', normalize_observed_emails(text))
        fax = page_from_html('<div><h3>Jane Doe</h3><p>Fax: <a href="tel:+12025550198">+1 202 555 0198</a></p></div>')
        self.assertEqual(extract_contact_signals(contact_block_pages(COMPANY, [fax])), [])

    def test_conflicts_quarantine_both_values_including_unicode_and_phone(self):
        healthy = page_from_html('<div><h3>Jane Doe</h3><a href="MAILTO:jane@example.com">E-Mail: jane@example.com</a></div>')
        self.assertEqual(healthy.contact_conflicts, [])
        self.assertEqual(len(healthy.contact_blocks), 1)
        html = '''<div><h3>Jane Doe</h3><p>
        <a href="mailto:jane@example.org">jane@example.com</a>
        <a href="mailto:reiß@example.com">reiss@example.com</a>
        <a href="tel:0012025550123">+1 202 555 0199</a></p></div>'''
        page = page_from_html(html)
        self.assertEqual(len(page.contact_conflicts), 3)
        excerpt = CrawledPage(URL + '/', 'jane@example.com +1 202 555 0199', source_type='contact_excerpt')
        signals = extract_contact_signals([page, *contact_block_pages(COMPANY, [page]), excerpt], 'US')
        self.assertTrue(signals)
        self.assertTrue(all(s['status'] == 'conflicting' for s in signals))
        for signal in signals:
            signal['company_public'] = True
        p = candidate(quote='Jane Doe jane@example.com')
        p['emails'][0]['status'] = 'guessed'
        output = dict(company_name=COMPANY.name, candidates=[p], review_required=True)
        _validate_output(output, COMPANY, signals, {URL})
        self.assertEqual(output['candidates'][0]['emails'], [])
        self.assertEqual(output['unassigned_contacts'], [])
        # Neither top-up merging nor later guessed-email enrichment can reintroduce it.
        output['candidates'][0]['emails'] = [dict(value='jane@example.com', status='guessed')]
        output['candidates'][0]['inferred_emails'] = [dict(value='jane@example.com', status='inferred')]
        _remove_conflicting_contacts(output, signals)
        self.assertEqual(output['candidates'][0]['emails'], [])
        self.assertEqual(output['candidates'][0]['inferred_emails'], [])

    def test_explicit_unrelated_roles_are_excluded_but_mixed_buying_roles_survive(self):
        people = []
        for index, title in enumerate(['HR Manager', 'Leitung Personal | Öffentlichkeitsarbeit', 'Website Administrator', 'CEO / HR Director', 'Procurement and HR manager', 'Technical sales', 'Personal assistant to CEO']):
            p = candidate(name='Person ' + str(index))
            p['current_title'] = title
            people.append(p)
        result = {'candidates': people}
        _exclude_unrelated_roles(result)
        _exclude_unrelated_roles(result)
        self.assertEqual(len(result['excluded_candidates']), 3)
        self.assertEqual(len(result['candidates']), 4)

    def test_wrong_person_card_and_footer_channels_are_not_assigned(self):
        page = page_from_html()
        signals = extract_contact_signals(contact_block_pages(COMPANY, [page]))
        for s in signals:
            s['company_public'] = True
        p = candidate(email='john@example.com', quote='Jane Doe john@example.com')
        output = dict(company_name=COMPANY.name, candidates=[p], review_required=True)
        blocks = {URL: page.contact_blocks}
        _validate_output(output, COMPANY, signals, {URL}, contact_blocks=blocks)
        self.assertEqual(p['emails'], [])
        _attach_evidence_bound_contacts(output, blocks)
        self.assertEqual(p['emails'], [])
        # A covered person cannot borrow an unrepresented footer channel.
        p = candidate(email='info@example.com', quote='Jane Doe info@example.com')
        footer_signals = signals + [dict(channel='email', value='info@example.com', source_url=URL, status='observed')]
        output['candidates'] = [p]
        _validate_output(output, COMPANY, footer_signals, {URL}, contact_blocks=blocks)
        self.assertEqual(p['emails'], [])
        p = candidate()
        output['candidates'] = [p]
        _validate_output(output, COMPANY, signals, {URL}, contact_blocks=blocks)
        self.assertEqual(p['emails'][0]['value'], 'jane@example.com')
        # Inline name spans must not break a genuine name-to-channel association.
        split_page = page_from_html(HTML.replace('Jane Doe', '<span>Jane</span><span>Doe</span>'))
        _validate_output(output, COMPANY, signals, {URL}, contact_blocks={URL: split_page.contact_blocks})
        self.assertEqual(p['emails'][0]['value'], 'jane@example.com')
        mixed = page_from_html('<section><h3>Jane Doe</h3><p>Founder</p><footer><a href="mailto:info@example.com">Contact</a></footer></section>')
        self.assertEqual(mixed.contact_blocks, [])

    def test_contact_cards_survive_prose_budget_without_expanding_it(self):
        page = page_from_html()
        prose = [CrawledPage('https://example.com/news/' + str(i), 'x' * 30000) for i in range(8)]
        prompt = _build_prompt(COMPANY, prose + contact_block_pages(COMPANY, [page]), [])
        payload = json.loads(prompt.split('INPUT JSON:\n', 1)[1])
        self.assertEqual(payload['sources'][0]['source_type'], 'contact_card')
        self.assertIn('John Roe', payload['sources'][1]['content'])
        self.assertLessEqual(sum(len(p['content']) for p in payload['sources']), 100000)

    def test_actual_crawler_adapter_carries_metadata(self):
        result = SimpleNamespace(url=URL, success=True, html=HTML, markdown='Example Minerals')
        instance = AsyncMock()
        instance.arun_many.return_value = [result]
        with patch('crawl4ai.AsyncWebCrawler') as crawler:
            crawler.return_value.__aenter__ = AsyncMock(return_value=instance)
            crawler.return_value.__aexit__ = AsyncMock(return_value=False)
            pages = asyncio.run(Crawl4aiCrawler('http://127.0.0.1:7897').crawl([URL]))
        self.assertEqual(len(pages[0].contact_blocks), 2)

    def test_discovery_uses_cards_before_topup_and_deduplicates_current_crm(self):
        class Extractor:
            def extract(self, company, pages, signals, usage_path):
                self.assertions = [p for p in pages if p.source_type == 'contact_card']
                output = dict(company_name=company.name, review_required=True, candidates=[
                    candidate(), candidate('John Roe', 'john@example.com', 'John Roe john@example.com')])
                _validate_output(output, company, signals, {URL}, contact_blocks={URL: page.contact_blocks})
                return output
        page = page_from_html()
        extractor = Extractor()
        with tempfile.TemporaryDirectory() as directory, patch('key_person_discovery.pipeline.crawl_sync', return_value=[page]):
            result = discover(COMPANY, [], object(), extractor, Path(directory),
                              crm_contacts=[dict(name='John Roe', email='john@example.com')],
                              people_search_limit=3, official_site_discovery=lambda *_: ([], []))
        self.assertEqual(len(extractor.assertions), 2)
        self.assertEqual(result['run_summary']['new_contactable_people'], 1)
        self.assertEqual(result['run_summary']['people_search_queries'], 0)
        self.assertEqual(result['run_summary']['contact_card_blocks'], 2)


if __name__ == '__main__':
    unittest.main()
