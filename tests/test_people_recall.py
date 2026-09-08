import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from key_person_discovery.hermes import _validate_output
from key_person_discovery.models import CompanyProfile, CrawledPage, SearchOutcome, SearchResult
from key_person_discovery.pipeline import discover, _new_contactable_people, _merge_people_results, _named_person_source_urls, _attach_named_linkedin_profiles
from key_person_discovery.sources import build_people_queries


class PeopleRecallTests(unittest.TestCase):
    def test_known_person_keeps_unique_current_profile_without_model_assignment(self):
        company = CompanyProfile('Example Minerals')
        url = 'https://de.linkedin.com/in/jane-doe-123'
        hit = SearchResult('query', 'Jane Doe – Director at Example Minerals', url, 'Current director at Example Minerals')
        result = dict(candidates=[dict(full_name='Dr. Jane Doe', company_match='verified', linkedin=[], evidence=[])])
        _attach_named_linkedin_profiles(result, company, [hit, hit])
        person = result['candidates'][0]
        self.assertEqual(person['linkedin'][0]['value'], 'https://www.linkedin.com/in/jane-doe-123')
        self.assertEqual(person['linkedin'][0]['status'], 'probable')
        self.assertEqual(person['evidence'][0]['source_url'], url)
        person['linkedin'] = []
        _attach_named_linkedin_profiles(result, company, [hit], [dict(linkedin=url)])
        self.assertFalse(person['linkedin'])

    def test_known_profile_requires_current_unique_full_name_and_preserves_existing(self):
        company = CompanyProfile('Example Minerals')
        hit = SearchResult('q', 'Jane Doe – Director at Example Minerals', 'https://linkedin.com/in/jane-doe', 'Current director at Example Minerals')
        cases = [
            [SearchResult('q', 'Jane Doer – Director at Example Minerals', hit.url, hit.snippet)],
            [SearchResult('q', 'Jane Doe Smith – Director at Example Minerals', hit.url, hit.snippet)],
            [SearchResult('q', 'Jane Doe', hit.url, 'Experience: Example Minerals')],
            [SearchResult('q', hit.title, hit.url, 'Former director at Example Minerals')],
            [SearchResult('q', hit.title, 'https://linkedin.com/pub/dir/Jane/Doe', hit.snippet)],
            [hit, SearchResult('q', hit.title, hit.url + '-other', hit.snippet)],
        ]
        for hits in cases:
            result = dict(candidates=[dict(full_name='Jane Doe', company_match='verified', linkedin=[])])
            _attach_named_linkedin_profiles(result, company, hits)
            self.assertFalse(result['candidates'][0]['linkedin'])
        for changes in [dict(company_match='uncertain'), dict(crm_existing_match=['name']),
                        dict(linkedin=[dict(value='https://linkedin.com/in/existing')])]:
            person = dict(full_name='Jane Doe', company_match='verified', linkedin=[])
            person.update(changes)
            before = copy.deepcopy(person)
            _attach_named_linkedin_profiles(dict(candidates=[person]), company, [hit])
            self.assertEqual(person, before)

    def test_profile_survives_optional_model_failure_and_counts_only_non_crm(self):
        company = CompanyProfile('Example Minerals', website='https://example.com', target_contact_count=1)
        page = CrawledPage(company.website, 'Jane Doe, Director at Example Minerals')
        url = 'https://linkedin.com/in/jane-doe'
        class Search:
            name = 'searxng'
            def search(self, query, limit=10):
                return SearchOutcome([SearchResult(query, 'Jane Doe – Director at Example Minerals', url, 'Currently at Example Minerals')]) if '"Jane Doe"' in query else SearchOutcome([])
        class Model:
            calls = 0
            def extract(self, *args):
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError('optional model failed')
                return dict(company_name=company.name, review_required=True, candidates=[dict(
                    full_name='Jane Doe', current_title='Director', company_match='verified', linkedin=[], emails=[], phones=[],
                    evidence=[dict(source_url=page.url, quote=page.markdown)], review_required=True)])
        for crm, expected in [([], 1), ([dict(linkedin=url)], 0)]:
            with tempfile.TemporaryDirectory() as directory, patch('key_person_discovery.pipeline.crawl_sync', return_value=[page]):
                result = discover(company, [Search()], object(), Model(), Path(directory), max_urls=1,
                    people_search_limit=3, anysearch_query_limit=0, crm_contacts=crm,
                    official_site_discovery=lambda *_: ([], []))
            self.assertEqual(result['run_summary']['new_contactable_people'], expected)
            self.assertEqual(result['run_summary']['people_topup_gain'], expected)

    def test_verified_name_without_channels_precedes_uncertain_people_and_broad_roles(self):
        company = CompanyProfile('Example Minerals GmbH')
        result = dict(candidates=[dict(full_name='Dr. Jane Doe', company_match='verified', influence_score='high')],
                      unverified_candidates=[dict(full_name='John Roe', linkedin=[dict(value='https://linkedin.com/in/john')])])
        queries = build_people_queries(company, result, 'DE')[:3]
        self.assertTrue(all('"Jane Doe"' in q for q in queries))
        self.assertNotIn('"Dr. Jane Doe"', queries[0])
        self.assertTrue(queries[1].startswith('site:linkedin.com/in'))
        self.assertIn('filetype:pdf', queries[2])
        result['candidates'][0]['crm_existing_match'] = ['name']
        self.assertIn('"John Roe"', build_people_queries(company, result)[0])

    def test_external_person_page_requires_exact_seed_and_target_not_group(self):
        company = CompanyProfile('Example Minerals (Holding Group)', website='https://example.com')
        result = dict(candidates=[dict(full_name='Dr. Jane Doe')])
        snippets = ['Jane Doe at Example Minerals, technical director',
                    'Jane Doer at Example Minerals', 'Jane Doe at Holding Group',
                    'John Roe at Example Minerals', 'Jane Doe, former employee at Example Minerals']
        hits = [SearchResult('query', '', f'https://conference.example/p/{i}', text) for i,text in enumerate(snippets)]
        self.assertEqual(_named_person_source_urls(company, result, hits), ['https://conference.example/p/0'])

    def test_person_pdf_is_fetched_and_public_footer_is_not_assigned_to_target(self):
        company = CompanyProfile('Example Minerals', website='https://example.com', target_contact_count=1)
        home = CrawledPage('https://example.com/team', 'Jane Doe, current Technical Director at Example Minerals')
        pdf_url = 'https://conference.example/speakers.pdf'
        pdf = CrawledPage(pdf_url, 'Jane Doe, Technical Director, Example Minerals, jane@example.com\nConference office: office@conference.example', source_type='pdf_extract')
        class Search:
            name = 'searxng'
            def search(self, query, limit=10):
                return SearchOutcome([SearchResult(query, 'Jane Doe at Example Minerals', pdf_url, 'Technical director speaker contact')]) if '"Jane Doe"' in query else SearchOutcome([])
        class Extractor:
            calls = 0
            def extract(self, company, pages, signals, usage_path):
                self.calls += 1
                p = dict(full_name='Jane Doe', current_title='Technical Director', company_match='verified',
                    review_required=True, influence_type='technical_influencer', linkedin=[], phones=[], emails=[],
                    evidence=[dict(source_url=home.url, quote=home.markdown)])
                if self.calls > 1:
                    p['emails'] = [dict(value='jane@example.com', status='observed', source_url=pdf_url)]
                    p['evidence'].append(dict(source_url=pdf_url, quote='Jane Doe, Technical Director, Example Minerals, jane@example.com'))
                    self_signals = [s for s in signals if s['source_url']==pdf_url]
                    assert all(not s.get('company_public') and not s.get('company_association') for s in self_signals)
                result = dict(company_name=company.name, candidates=[p], review_required=True)
                _validate_output(result, company, signals, {p.url for p in pages})
                return result
        extractor = Extractor()
        def fetch_pdf(urls, **kwargs):
            return [pdf] if urls else []
        with tempfile.TemporaryDirectory() as directory, patch('key_person_discovery.pipeline.crawl_sync', return_value=[home]), patch('key_person_discovery.pipeline.extract_pdf_urls', side_effect=fetch_pdf) as fetch:
            result = discover(company, [Search()], object(), extractor, Path(directory), max_urls=1,
                anysearch_query_limit=0, people_search_limit=3, official_site_discovery=lambda *_: ([], []))
        self.assertEqual(extractor.calls, 2)
        self.assertEqual(result['run_summary']['people_topup_gain'], 1)
        self.assertEqual(result['run_summary']['pdf_documents_processed'], 1)
        self.assertEqual(fetch.call_args_list[-1].args[0], [pdf_url])
        self.assertFalse(any(c['value']=='office@conference.example' for c in result['unassigned_contacts']))

    def test_queries_use_existing_aliases_local_roles_and_named_leads(self):
        company = CompanyProfile(name="Example Minerals GmbH")
        queries = build_people_queries(company, {"candidates": [], "unverified_candidates": []}, "DE")
        self.assertIn('"example minerals"', queries[0])
        self.assertIn("Einkauf", queries[0])
        self.assertTrue(any("technical" in query for query in queries))
        self.assertTrue(any("director" in query for query in queries))
        seeded = build_people_queries(company, {"unverified_candidates": [{"full_name": "Jane Doe"}]}, "DE")
        self.assertIn('"Jane Doe"', seeded[0])
        self.assertIn('"example minerals"', seeded[0])

    def run_discovery(self, *, limit=3, budget=1, target=2, fail=False, former=False, model_fail=False, broad=False):
        company = CompanyProfile(name="Example Minerals Ltd", website="https://example.com", target_contact_count=target)
        page = CrawledPage(url="https://example.com/team", markdown="Example Minerals. Jane Doe, Purchasing Manager. jane@example.com\nBob Smith, Director. bob@example.com\ninfo@example.com\nsales@example.com\ncontact@example.com\noffice@example.com")

        def person(name, email):
            return {"full_name": name, "company_match": "verified", "influence_type": "decision_maker", "review_required": True,
                    "emails": [{"value": email, "status": "observed", "source_url": page.url}], "phones": [], "linkedin": [],
                    "evidence": [{"source_url": page.url, "quote": f"{name}, Purchasing Manager. {email}"}]}

        class Search:
            def __init__(self, name):
                self.name, self.calls = name, []

            def search(self, query, limit=5):
                self.calls.append(query)
                if '"example minerals"' not in query:
                    return SearchOutcome([])
                if fail:
                    raise RuntimeError("Search unavailable")
                return SearchOutcome([SearchResult(query, "John Roe - Technical Manager", "https://linkedin.com/in/john-roe",
                    "Former employee at Example Minerals; now at Other Company" if former else "John Roe, Technical Manager. Current at Example Minerals.", self.name)])

        class Hermes:
            calls = 0

            def extract(self, company, pages, signals, usage_path):
                self.calls += 1
                if self.calls > 1 and model_fail:
                    raise RuntimeError("Analysis failed")
                candidates = [person("Jane Doe", "jane@example.com"), person("Bob Smith", "bob@example.com")]
                if self.calls > 1:
                    candidates = [{"full_name": "John Roe", "company_match": "verified", "influence_type": "technical_influencer", "review_required": True,
                        "linkedin": [{"value": "https://linkedin.com/in/john-roe", "status": "probable", "source_url": "https://linkedin.com/in/john-roe"}],
                        "emails": [], "phones": [], "evidence": [{"source_url": "https://linkedin.com/in/john-roe", "quote": "John Roe, Technical Manager. Current at Example Minerals."}]}]
                result = {"company_name": company.name, "review_required": True, "candidates": copy.deepcopy(candidates)}
                _validate_output(result, company, signals, {p.url for p in pages}, {p.url for p in pages if p.source_type == "search_excerpt"})
                return result

        search, fallback, hermes = Search("anysearch"), Search("searxng"), Hermes()
        with tempfile.TemporaryDirectory() as directory, patch("key_person_discovery.pipeline.crawl_sync", return_value=[page]):
            result = discover(company, [search, fallback], object(), hermes, Path(directory),
                official_site_discovery=lambda *_: ([], []), crm_contacts=[{"name": "Bob Smith", "email": "bob@example.com"}],
                anysearch_query_limit=budget, people_search_limit=limit, broad_discovery=broad)
        return result, search, fallback, hermes

    def test_topup_adds_new_person_after_crm_dedup_and_preserves_first_pass(self):
        result, search, _, hermes = self.run_discovery()
        self.assertEqual({p["full_name"] for p in result["candidates"]}, {"Jane Doe", "John Roe"})
        self.assertEqual(result["run_summary"]["new_contactable_people"], 2)
        self.assertEqual(result["run_summary"]["people_before_topup"], 1)
        self.assertEqual(result["run_summary"]["people_topup_gain"], 1)
        self.assertEqual(result["run_summary"]["people_search_queries"], 3)
        self.assertEqual(len(search.calls), 4)
        self.assertEqual(hermes.calls, 2)
        self.assertGreaterEqual(len(result["unassigned_contacts"]), 4)

    def test_enough_people_or_disabled_topup_does_not_spend_more(self):
        for options in ({"target": 1}, {"limit": 0}):
            with self.subTest(options=options):
                result, search, _, hermes = self.run_discovery(**options)
                self.assertEqual(result["run_summary"]["people_search_queries"], 0)
                self.assertEqual(len(search.calls), 1)
                self.assertEqual(hermes.calls, 1)

    def test_disabled_anysearch_stays_disabled_during_topup(self):
        result, search, _, _ = self.run_discovery(budget=0)
        self.assertEqual(search.calls, [])
        self.assertEqual(result["run_summary"]["new_contactable_people"], 2)

    def test_failed_topup_or_former_employee_preserves_baseline(self):
        for options in ({"fail": True}, {"former": True}):
            with self.subTest(options=options):
                result, _, _, hermes = self.run_discovery(**options)
                self.assertEqual([p["full_name"] for p in result["candidates"]], ["Jane Doe"])
                self.assertEqual(hermes.calls, 1)
                self.assertEqual(result["run_summary"]["people_topup_gain"], 0)

    def test_optional_model_failure_keeps_first_pass_contacts(self):
        result, _, _, hermes = self.run_discovery(model_fail=True)
        self.assertEqual(result["run_summary"]["new_contactable_people"], 1)
        self.assertEqual(hermes.calls, 2)
        self.assertEqual(result["candidates"][0]["emails"][0]["value"], "jane@example.com")

    def test_metric_excludes_guesses_pending_and_existing_crm_people(self):
        result = {"candidates": [
            {"full_name": "A", "company_match": "verified", "emails": [{"value": "a@example.com", "status": "guessed"}]},
            {"full_name": "B", "company_match": "verified", "crm_existing_match": ["name"], "phones": [{"value": "+12025550123"}]},
            {"full_name": "C", "company_match": "verified", "emails": [{"value": "c@example.com", "status": "observed"}]},
        ], "unverified_candidates": [{"full_name": "D", "linkedin": [{"value": "https://linkedin.com/in/d"}]}]}
        self.assertEqual(_new_contactable_people(result), 1)

    def test_merging_does_not_promote_unverified_channels(self):
        first = {"candidates": [], "unverified_candidates": [{"full_name": "Jane Doe", "emails": [{"value": "pending@example.com"}]}]}
        second = {"candidates": [{"full_name": "Jane Doe", "company_match": "verified", "linkedin": [{"value": "https://linkedin.com/in/jane-doe"}]}]}
        merged = _merge_people_results(first, second)
        self.assertEqual(merged["unverified_candidates"], [])
        self.assertFalse(merged["candidates"][0].get("emails"))
        self.assertEqual(len(first["unverified_candidates"]), 1)

    def test_same_name_with_conflicting_profiles_is_not_merged(self):
        first = {"candidates": [{"full_name": "Jane Doe", "linkedin": [{"value": "https://linkedin.com/in/jane-one"}]}]}
        second = {"candidates": [{"full_name": "Jane Doe", "linkedin": [{"value": "https://linkedin.com/in/jane-two"}]}]}
        self.assertEqual(len(_merge_people_results(first, second)["candidates"]), 2)

    def test_fusion_mode_keeps_topup_gain_with_the_same_evidence(self):
        result, search, fallback, _ = self.run_discovery(broad=True)
        self.assertEqual(result["run_summary"]["people_topup_gain"], 1)
        self.assertEqual(result["run_summary"]["new_contactable_people"], 2)
        self.assertEqual(len(search.calls), 4)
        self.assertTrue(fallback.calls)
