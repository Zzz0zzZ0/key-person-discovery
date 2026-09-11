import copy
import unittest

from key_person_discovery.models import CompanyProfile, SearchResult, company_name_aliases
from key_person_discovery.sources import build_people_queries
from key_person_discovery.pipeline import _attach_named_linkedin_profiles, _deduplicate_crm_contacts, _add_linkedin_search_candidates


class PersonIdentityQueryTests(unittest.TestCase):
    def test_job_and_employer_heading_is_not_a_person_name(self):
        company = CompanyProfile('Atlas Minerals B.V.')
        result = dict(candidates=[])
        hit = SearchResult('q', 'Teamleider Bedrijfsbureau bij Atlas Minerals - LinkedIn',
            'https://nl.linkedin.com/in/abc123', 'Ervaring: Atlas Minerals BV')
        _add_linkedin_search_candidates(result, company, [hit])
        self.assertFalse(result.get('candidates'))
        self.assertFalse(result.get('unverified_candidates'))

    def test_dotted_dutch_legal_suffixes_keep_brand_alias(self):
        for suffix in ('B.V.', 'N.V.', 'BV', 'NV'):
            self.assertIn('atlas minerals', company_name_aliases('Atlas Minerals ' + suffix))
        self.assertIn('atlas b v services', company_name_aliases('Atlas B V Services'))

    def test_initials_relax_retrieval_without_changing_candidate_identity(self):
        company = CompanyProfile('Atlas Minerals B.V.')
        for name, surname in [('Mr M. van der Green', 'van der Green'), ('A.B. Jones', 'Jones'), ('M Green', 'Green')]:
            result = dict(candidates=[dict(full_name=name, company_match='verified')])
            original = copy.deepcopy(result)
            queries = build_people_queries(company, result)[:3]
            self.assertEqual(result, original)
            self.assertTrue(all('"' + surname + '"' in q for q in queries), queries)
            self.assertTrue(all('"atlas minerals"' in q for q in queries), queries)
            self.assertTrue(queries[1].startswith('site:linkedin.com/in'))

    def test_full_given_names_and_apostrophes_stay_exact(self):
        company = CompanyProfile('Atlas Minerals')
        for name in ('Mark A. Green', "Anne O'Neill", '李 明', 'J. R.'):
            result = dict(candidates=[dict(full_name=name, company_match='verified')])
            self.assertIn('"' + name + '"', build_people_queries(company, result)[0])

    def test_surname_hit_does_not_auto_expand_seed_or_merge_crm_identity(self):
        company = CompanyProfile('Atlas Minerals')
        person = dict(full_name='M. Green', company_match='verified', linkedin=[])
        result = dict(candidates=[person])
        hits = [SearchResult('"Green" "Atlas Minerals"', 'Mark Green - Director at Atlas Minerals',
            'https://linkedin.com/in/mark-green', 'Current director at Atlas Minerals')]
        _attach_named_linkedin_profiles(result, company, hits)
        self.assertEqual(person['linkedin'], [])
        _deduplicate_crm_contacts(result, [dict(name='Mark Green')])
        self.assertEqual(len(result['candidates']), 1)
