import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from key_person_discovery.models import CompanyProfile, CrawledPage, SearchOutcome, SearchResult
from key_person_discovery.pipeline import discover
from key_person_discovery.search_quality import classify_search_result
from key_person_discovery.sources import SearxngClient


class SearchQualityTests(unittest.TestCase):
    company = CompanyProfile('Example Minerals Ltd', website='https://example.test')

    def hit(self, url, title='Example Minerals', snippet='', query='"Example Minerals" contact'):
        return SearchResult(query, title, url, snippet)

    def test_operator_scope_uses_actual_host_and_path(self):
        query = 'site:linkedin.com/in "Example Minerals"'
        for url, expected in [
            ('https://de.linkedin.com/in/jane', 'eligible'),
            ('https://linkedin.com.evil.test/in/jane', 'rejected'),
            ('https://linkedin.com@evil.test/in/jane', 'rejected'),
            ('https://linkedin.com/invented/jane', 'rejected'),
            ('https://linkedin.com/pub/dir/Jane', 'rejected'),
            ('https://[broken', 'rejected'),
        ]:
            with self.subTest(url=url):
                self.assertEqual(classify_search_result(self.company, self.hit(url, query=query))[0], expected)
        # Do not interpret OR and negative site filters as one mandatory host.
        for query in ['site:one.test OR site:two.test "Example Minerals"', '-site:one.test "Example Minerals"']:
            self.assertEqual(classify_search_result(self.company, self.hit('https://new.test/page', query=query))[0], 'eligible')

    def test_echo_and_authentication_are_rejected_but_unknown_domains_survive(self):
        query = 'site:linkedin.com/in "Jane Doe" "Example Minerals"'
        echo = self.hit('https://unknown.test/page', title='Abc ' + query + ' xyz', query=query)
        self.assertEqual(classify_search_result(self.company, echo), ('rejected', 'query_echo'))
        for url in ['https://www.facebook.com/r.php/', 'https://unknown.test/login']:
            self.assertEqual(classify_search_result(self.company, self.hit(url))[1], 'authentication_page')
        self.assertEqual(classify_search_result(self.company, self.hit('https://association.test/speaker', snippet='Jane Doe, Example Minerals'))[0], 'eligible')
        thin = self.hit('https://unknown.test/jane', title='Jane Doe', snippet='Director')
        self.assertEqual(classify_search_result(self.company, thin)[0], 'pending')
        self.assertEqual(classify_search_result(self.company, self.hit('https://example.test/team', title='Team'))[0], 'eligible')

    def test_shared_host_does_not_prove_company_identity(self):
        company = CompanyProfile('Example Minerals Ltd', website='https://directory.test/example-minerals')
        self.assertEqual(classify_search_result(company, self.hit('https://directory.test/other', title='Other Firm'))[0], 'pending')

    def test_client_retains_full_response_and_actual_engine_attribution(self):
        payload = {'results': [dict(title='Example Minerals', url=f'https://example.test/{i}', content='Team', engines=['qwant', 'google cse']) for i in range(8)]}
        payload['results'].append({'url': 'https://[broken'})
        with patch('key_person_discovery.sources.urlopen', return_value=io.BytesIO(json.dumps(payload).encode())) as request:
            outcome = SearxngClient('http://localhost:18080').search('"Example Minerals"', limit=5)
        self.assertEqual(len(outcome.results), 5)  # Public API stays compatible.
        self.assertEqual(len(outcome.raw_results), 8)
        self.assertEqual(outcome.raw_results[-1].rank, 8)
        self.assertEqual(outcome.results[0].as_dict()['engines'], ['qwant', 'google cse'])
        self.assertEqual(outcome.raw_response, payload)
        self.assertEqual(request.call_count, 1)

    def run_pipeline(self, primary_results, fallback_results):
        class Search:
            def __init__(self, name, factory):
                self.name, self.factory, self.calls = name, factory, []
            def search(self, query):
                self.calls.append(query)
                results = self.factory(query)
                return SearchOutcome(results[:5], raw_results=results)
        class Extractor:
            def extract(self, company, pages, signals, usage_path):
                return {'company_name': company.name, 'candidates': [], 'review_required': True}
        primary, fallback = Search('anysearch', primary_results), Search('searxng', fallback_results)
        with tempfile.TemporaryDirectory() as directory, patch('key_person_discovery.pipeline.crawl_sync', return_value=[CrawledPage('https://example.test', 'Example Minerals Ltd. Official company page.')]):
            output = discover(self.company, [primary, fallback], object(), Extractor(), Path(directory),
                              anysearch_query_limit=1, official_site_discovery=lambda *_: ([], []))
            artifacts = {name: json.loads((Path(directory) / name).read_text()) for name in ['search-results.json', 'search-quality.json', 'search-responses.json']}
        return primary, fallback, output, artifacts

    def test_quality_filter_refills_from_same_response_before_cutoff(self):
        def primary(query):
            return [self.hit('https://facebook.com/r.php', query=query) for _ in range(5)] + [self.hit('https://example.test/team', query=query)]
        p, f, result, files = self.run_pipeline(primary, lambda q: [])
        self.assertNotIn(p.calls[0], f.calls)
        self.assertEqual(files['search-results.json'][0]['url'], 'https://example.test/team')
        self.assertEqual(files['search-quality.json'][0]['rejected'], 5)
        self.assertEqual(len(files['search-responses.json'][0]['parsed_results']), 6)
        self.assertEqual(result['run_summary']['anysearch_queries'], 1)

    def test_rejected_and_pending_hits_do_not_suppress_fallback(self):
        for primary_hit in [self.hit('https://facebook.com/r.php'), self.hit('https://unknown.test/jane', title='Jane Doe')]:
            with self.subTest(hit=primary_hit):
                p, f, result, files = self.run_pipeline(lambda q: [primary_hit], lambda q: [self.hit('https://example.test/team', query=q)])
                self.assertIn(p.calls[0], f.calls)
                self.assertEqual(files['search-results.json'][0]['url'], 'https://example.test/team')
                self.assertEqual(result['run_summary']['anysearch_queries'], 1)
                self.assertEqual(len(f.calls), len(set(f.calls)))

    def test_pending_survives_when_all_other_sources_are_empty(self):
        hit = self.hit('https://unknown.test/jane', title='Jane Doe')
        p, f, _, files = self.run_pipeline(lambda q: [hit], lambda q: [])
        self.assertIn(p.calls[0], f.calls)
        self.assertEqual(files['search-results.json'][0]['url'], hit.url)
        self.assertEqual(files['search-quality.json'][0]['pending'], 1)


if __name__ == '__main__':
    unittest.main()
