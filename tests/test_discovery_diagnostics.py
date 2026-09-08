import unittest
import test_people_recall as recall_tests


class DiscoveryDiagnosticsTests(unittest.TestCase):
    def test_optional_model_failure_identifies_stage_and_preserves_people(self):
        result, _, _, _ = recall_tests.PeopleRecallTests().run_discovery(model_fail=True)
        self.assertEqual(result['run_summary']['new_contactable_people'], 1)
        self.assertIn('diagnostics', result)
        self.assertEqual(result['diagnostics']['topup']['status'], 'failed')
        self.assertEqual(result['diagnostics']['topup']['error']['stage'], 'analysis')

    def test_failure_reasons_choose_different_recovery_queries(self):
        from key_person_discovery.diagnostics import diagnose, recovery_queries
        from key_person_discovery.models import CompanyProfile, CrawledPage
        company = CompanyProfile('Example Minerals', website='https://example.com')
        good = CrawledPage(company.website, 'Example Minerals company overview')
        bad = CrawledPage(company.website, '', error='timeout')
        person = dict(full_name='Jane Doe', company_match='verified', emails=[])
        scenarios = [
            ({}, [bad], '', 'crawl_failed', False),
            ({}, [], '', 'no_evidence', False),
            ({}, [good], 'unverified_redirect', 'website_unverified', False),
            ({}, [good], '', 'no_target_people', True),
            (dict(candidates=[person]), [good, bad], '', 'missing_channels', True),
            (dict(unverified_candidates=[dict(full_name='John Roe', company_match='uncertain')]), [good], '', 'employment_uncertain', True),
            (dict(crm_duplicates=[dict(full_name='Existing Person', kind='candidate')]), [good], '', 'crm_duplicate', True),
        ]
        for result, pages, status, reason, should_search in scenarios:
            with self.subTest(reason=reason):
                diag = diagnose(result, pages, website=company.website, website_status=status)
                self.assertEqual(diag['primary'], reason)
                queries = recovery_queries(company, result, diag, 'DE')
                self.assertEqual(bool(queries), should_search)
                if reason == 'missing_channels':
                    self.assertTrue(all('"Jane Doe"' in q for q in queries))
                if reason == 'employment_uncertain':
                    self.assertTrue(all('"John Roe"' in q for q in queries))
                    self.assertTrue(all('current' in q for q in queries))
                if reason == 'crm_duplicate':
                    self.assertTrue(all('-"Existing Person"' in q for q in queries))
        guessed = dict(candidates=[dict(person, emails=[dict(value='jane@example.com', status='guessed')])])
        diag = diagnose(guessed, [good], website=company.website)
        self.assertEqual(diag['primary'], 'missing_channels')
        self.assertTrue(all('"Jane Doe"' in q for q in recovery_queries(company, guessed, diag)))
        reached = diagnose(dict(candidates=[person]), [good, bad], website=company.website, contactable_people=1)
        self.assertEqual(recovery_queries(company, {}, reached), [])
        self.assertIn('crawl_failed', reached['reasons'])

    def test_no_evidence_skips_model_and_topup_but_returns_source_diagnosis(self):
        import tempfile
        import json
        from pathlib import Path
        from unittest.mock import patch, Mock
        from key_person_discovery.models import CompanyProfile, CrawledPage
        from key_person_discovery.pipeline import discover
        from key_person_discovery.job_runner import _output_stage
        company = CompanyProfile('Example Minerals', website='https://example.com')
        model = Mock()
        with tempfile.TemporaryDirectory() as directory, patch('key_person_discovery.pipeline.crawl_sync', return_value=[CrawledPage(company.website, '', error='timeout')]):
            root = Path(directory)
            result = discover(company, [], object(), model, root/'result.json.artifacts', people_search_limit=3,
                              anysearch_query_limit=0, official_site_discovery=lambda *_: ([], []))
            model.extract.assert_not_called()
            self.assertEqual(result['diagnostics']['primary'], 'crawl_failed')
            self.assertEqual(result['diagnostics']['topup']['queries'], [])
            (root/'result.json').write_text(json.dumps(result))
            self.assertEqual(_output_stage(root/'result.json'), 'source_limited')
            self.assertEqual(json.loads((root/'result.json.artifacts/diagnostics.json').read_text()), result['diagnostics'])

    def test_initial_analysis_error_is_saved_safely_for_failed_job(self):
        import tempfile
        import json
        from pathlib import Path
        from unittest.mock import patch, Mock
        from key_person_discovery.models import CompanyProfile, CrawledPage
        from key_person_discovery.pipeline import discover
        from key_person_discovery.jobs import JobStore
        from key_person_discovery.diagnostics import extraction_failure
        company = CompanyProfile('Example Minerals', website='https://example.com')
        model = Mock()
        model.extract.side_effect = RuntimeError('Every candidate must have evidence')
        with tempfile.TemporaryDirectory() as directory, patch('key_person_discovery.pipeline.crawl_sync', return_value=[CrawledPage(company.website, 'Example Minerals')]):
            output = Path(directory)/'result.json'
            artifacts = Path(str(output)+'.artifacts')
            with self.assertRaisesRegex(RuntimeError, 'Every candidate'):
                discover(company, [], object(), model, artifacts, people_search_limit=3,
                         official_site_discovery=lambda *_: ([], []))
            diag = json.loads((artifacts/'diagnostics.json').read_text())
            self.assertEqual(diag['primary'], 'analysis_failed')
            self.assertEqual(diag['error']['code'], 'missing_evidence')
            public = JobStore._public(dict(id='test', status='failed', created_at='2026-09-08T00:00:00+00:00', output_path=str(output)))
            self.assertEqual(public['diagnostics'], diag)
            (artifacts/'diagnostics.json').write_text('{')
            self.assertNotIn('diagnostics', JobStore._public(dict(id='test', status='failed', created_at='2026-09-08T00:00:00+00:00', output_path=str(output))))
        safe = extraction_failure(RuntimeError('token=PRIVATE_MODEL_OUTPUT'), 'analysis')
        self.assertNotIn('PRIVATE_MODEL_OUTPUT', json.dumps(safe))
        self.assertEqual(safe['code'], 'processing_error')

    def test_already_fetched_profile_meets_target_before_extra_queries(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from key_person_discovery.models import CompanyProfile, CrawledPage, SearchResult, SearchOutcome
        from key_person_discovery.pipeline import discover
        company = CompanyProfile('Example Minerals', website='https://example.com', target_contact_count=1)
        home = CrawledPage(company.website, 'Jane Doe, current Director at Example Minerals')
        class Search:
            name = 'searxng'
            def search(self, query, limit=10):
                return SearchOutcome([SearchResult(query, 'Jane Doe – Director at Example Minerals',
                                     'https://linkedin.com/in/jane-doe', 'Current director at Example Minerals')])
        class Model:
            def extract(self, *args):
                return dict(company_name=company.name, review_required=True, candidates=[dict(full_name='Jane Doe', company_match='verified',
                    current_title='Director', emails=[], phones=[], linkedin=[], evidence=[dict(source_url=home.url, quote=home.markdown)], review_required=True)])
        with tempfile.TemporaryDirectory() as directory, patch('key_person_discovery.pipeline.crawl_sync', return_value=[home]):
            result = discover(company, [Search()], object(), Model(), Path(directory), people_search_limit=3,
                anysearch_query_limit=0, official_site_discovery=lambda *_: ([], []))
        self.assertEqual(result['run_summary']['people_search_queries'], 0)
        self.assertEqual(result['run_summary']['people_before_topup'], 1)
        self.assertEqual(result['diagnostics']['topup']['status'], 'target_met')
        self.assertEqual(result['diagnostics']['counts']['new_contactable_people'], 1)

    def test_failed_search_is_not_diagnosed_as_absence_of_contacts(self):
        result, _, _, model = recall_tests.PeopleRecallTests().run_discovery(fail=True)
        self.assertEqual(result['diagnostics']['topup']['status'], 'source_limited')
        self.assertGreater(result['diagnostics']['topup']['source_warnings'], 0)
        self.assertEqual(model.calls, 1)
        self.assertEqual(result['run_summary']['new_contactable_people'], 1)

    def test_usable_pdf_prevents_web_failures_being_reported_as_total_source_failure(self):
        import json
        import tempfile
        from pathlib import Path
        from key_person_discovery.diagnostics import diagnose
        from key_person_discovery.models import CrawledPage
        from key_person_discovery.job_runner import _output_stage
        pages = [CrawledPage('https://example.com', '', error='timeout'),
                 CrawledPage('https://example.com/info.pdf', 'Example Minerals product data', source_type='pdf_extract')]
        result = dict(candidates=[],run_summary=dict(urls_crawled=1,crawl_failures=1,search_results=0))
        result['diagnostics'] = diagnose(result,pages,website='https://example.com')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'result.json'
            path.write_text(json.dumps(result))
            self.assertEqual(_output_stage(path), 'no_new_contact')
            del result['diagnostics']
            path.write_text(json.dumps(result))
            self.assertEqual(_output_stage(path), 'source_limited')
