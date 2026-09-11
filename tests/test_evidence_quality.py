import copy
import json
import unittest
import tempfile

from key_person_discovery.hermes import _build_prompt, _validate_output, HermesExtractor
from key_person_discovery.models import CompanyProfile, CrawledPage
from key_person_discovery.signals import extract_contact_signals
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace


COMPANY = CompanyProfile('Example Minerals', 'https://example.com')
URL = 'https://example.com/contact.pdf'


def person(title='Technical Manager'):
    return dict(full_name='Alice Green', current_title=title, company_match='verified',
        influence_type='technical_influencer', review_required=True,
        evidence=[dict(source_url=URL, quote='Alice Green, Technical Manager, alice.green@example.com',
                       supports='current manager at Example Minerals')],
        emails=[dict(value='alice.green@example.com', status='observed', source_url=URL)],
        linkedin=[], phones=[])


def output(candidate):
    return dict(company_name=COMPANY.name, candidates=[candidate], review_required=True)


class EvidenceQualityTests(unittest.TestCase):
    def extract(self, candidate, text, source_type='pdf_extract'):
        page = CrawledPage(URL, text, source_type=source_type)
        signals = extract_contact_signals([page])
        with patch('key_person_discovery.hermes.os.access', return_value=True), patch(
            'key_person_discovery.hermes.subprocess.run',
            return_value=SimpleNamespace(returncode=0, stdout=json.dumps(output(candidate)), stderr='')):
            return HermesExtractor('/fake-hermes').extract(COMPANY, [page], signals, Path('/nonexistent/usage.json'))

    def test_long_navigation_does_not_hide_body_and_late_pdf(self):
        nav = ''.join(f'* [Section {i}](https://example.com/section/{i})\n' for i in range(700))
        text = nav + '# Contact\nAlice Green Technical Manager alice.green@example.com\nFooter contact: service@example.com'
        pages = [CrawledPage('https://example.com/' + str(i), text) for i in range(8)]
        pages.append(CrawledPage(URL, 'Published 2025\nSpecial PDF person', source_type='pdf_extract'))
        original = copy.deepcopy(pages)
        payload = json.loads(_build_prompt(COMPANY, pages, []).split('INPUT JSON:\n', 1)[1])
        self.assertTrue(all('# Contact' in s['content'] for s in payload['sources'] if s['source_type'] == 'crawl'))
        self.assertTrue(any('Special PDF person' in s['content'] for s in payload['sources']))
        self.assertTrue(any('Footer contact' in s['content'] for s in payload['sources']))
        self.assertLessEqual(sum(len(s['content']) for s in payload['sources']), 100000)
        self.assertEqual(pages, original)

    def test_prose_with_few_links_is_not_reordered(self):
        text = 'Important introductory evidence. ' * 100 + '\n# Details\nManager'
        payload = json.loads(_build_prompt(COMPANY, [CrawledPage(URL, text)], []).split('INPUT JSON:\n', 1)[1])
        self.assertEqual(payload['sources'][0]['content'], text)

    def test_bad_channel_does_not_discard_supported_person_or_email(self):
        candidate = person()
        candidate['linkedin'] = [dict(value='https://linkedin.com/in/alice', status='probable', source_url='https://unknown.example/alice')]
        value = output(candidate)
        signals = [dict(channel='email', value='alice.green@example.com', status='observed', source_url=URL)]
        _validate_output(value, COMPANY, signals, {URL})
        self.assertEqual(len(value['candidates']), 1)
        self.assertEqual(len(value['candidates'][0]['emails']), 1)
        self.assertEqual(value['candidates'][0]['linkedin'], [])
        self.assertTrue(value['contact_validation_rejections'])

    def test_invalid_person_evidence_still_rejects_entire_person(self):
        candidate = person()
        candidate['evidence'][0]['source_url'] = 'https://not-fetched.example'
        value = output(candidate)
        _validate_output(value, COMPANY, [], {URL})
        self.assertEqual(value['candidates'], [])

    def test_pdf_publication_date_is_checked_even_if_model_omits_it(self):
        for stamp in ['Published February 2020 Version 1', 'Published: 12 February 2020', 'Updated 12/02/2020']:
            with self.subTest(stamp=stamp):
                value = self.extract(person(), 'Example Minerals\nAlice Green Technical Manager alice.green@example.com\n' + stamp)
                self.assertEqual(value['candidates'], [])
                self.assertEqual(len(value['unverified_candidates'][0]['emails']), 1)
                self.assertIn('stale PDF', ' '.join(value['unverified_candidates'][0]['validation_reasons']))

    def test_standard_and_company_foundation_year_are_not_publication_dates(self):
        for text in ['Founded 1974. ISO 9001:2015 certified.', 'Published 2020\nUpdated September 2026']:
            with self.subTest(text=text):
                value = self.extract(person(), 'Example Minerals\nAlice Green Technical Manager alice.green@example.com\n' + text)
                self.assertEqual(len(value['candidates']), 1)

    def test_historical_title_is_pending_but_current_founder_is_retained(self):
        for title, expected in [('Founder (Historical)', 0), ('Former Technical Manager', 0), ('Founder and CEO', 1)]:
            with self.subTest(title=title):
                value = self.extract(person(title), 'Example Minerals\nAlice Green Technical Manager alice.green@example.com\nFounded 1974.', 'crawl')
                self.assertEqual(len(value['candidates']), expected)
                if not expected:
                    self.assertEqual(value['unverified_candidates'][0]['company_match'], 'probable')

    def test_pipeline_does_not_count_old_role_as_new_contactable_person(self):
        from key_person_discovery.pipeline import discover
        page = CrawledPage(URL, '# Example Minerals\nAlice Green Technical Manager alice.green@example.com\nPublished 2020', source_type='pdf_extract')
        extractor = SimpleNamespace(extract=lambda *args: output(person()))
        with tempfile.TemporaryDirectory() as directory, patch('key_person_discovery.pipeline.crawl_sync', return_value=[page]):
            value = discover(COMPANY, [], object(), extractor, Path(directory), anysearch_query_limit=0,
                             official_site_discovery=lambda *args: ([], []))
        self.assertEqual(value['run_summary']['new_contactable_people'], 0)
        self.assertEqual(len(value['unverified_candidates'][0]['emails']), 1)

    def test_current_independent_role_evidence_is_not_overridden_by_old_pdf(self):
        candidate = person()
        current_url = 'https://example.com/team'
        candidate['evidence'].append(dict(source_url=current_url, quote='Alice Green is our current Technical Manager'))
        pages = [CrawledPage(URL, 'Published 2020', source_type='pdf_extract'),
                 CrawledPage(current_url, 'Example Minerals: Alice Green is our current Technical Manager')]
        value = output(candidate)
        _validate_output(value, COMPANY, [], {p.url for p in pages}, source_pages=pages)
        self.assertEqual(len(value['candidates']), 1)


if __name__ == '__main__':
    unittest.main()
