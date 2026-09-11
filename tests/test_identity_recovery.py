import copy
import unittest

from key_person_discovery.models import CompanyProfile, SearchResult, CrawledPage
from key_person_discovery.pipeline import _official_website_from_search, _deduplicate_crm_contacts, _new_contactable_people
from key_person_discovery.sources import person_query_name, build_people_queries, discover_official_links


def hit(url, title='Atlas Minerals', rank=1, snippet='Atlas Minerals manufacturer'):
    return SearchResult('official website', title, url, snippet, rank=rank)


class IdentityRecoveryTests(unittest.TestCase):
    def test_nested_image_keeps_contact_page_link_without_crawling_icon(self):
        company = CompanyProfile('Atlas Minerals', 'https://atlasminerals.example')
        page = CrawledPage(company.website, '[Contact ![icon next](/assets/contact.PNG?v=1)](/contact)\n[Directory](/people.pdf)',
            links=[dict(text='Team icon', href='/team.svg'), dict(text='Staff', href='/staff')])
        self.assertEqual(set(discover_official_links(company, [page])), {
            company.website + '/contact', company.website + '/people.pdf', company.website + '/staff'})

    def test_existing_compact_brand_domain_wins_over_same_name_competitor(self):
        company = CompanyProfile('Atlas Minerals', 'https://atlasminerals.example')
        results = [hit('https://atlas-global.example/contact', rank=1), hit(company.website, rank=8)]
        self.assertEqual(_official_website_from_search(company, results), company.website)
        self.assertEqual(_official_website_from_search(company, results[:1]), '')

    def test_compact_brand_domain_can_be_found_without_an_input_website(self):
        url = 'https://atlasminerals.example'
        self.assertEqual(_official_website_from_search(CompanyProfile('Atlas Minerals'), [hit(url)]), url)

    def test_matching_search_does_not_widen_existing_subsidiary_scope(self):
        company = CompanyProfile('Atlas Minerals', 'https://atlas-group.example/companies/minerals/')
        self.assertEqual(_official_website_from_search(company, [hit(company.website)]), company.website)

    def test_honorifics_are_shared_between_crm_dedup_and_search(self):
        for crm_name, candidate_name in [('M. Green', 'Mr M. Green'), ('Dr. Alice Green', 'Ms Alice Green')]:
            with self.subTest(candidate=candidate_name):
                candidate = dict(full_name=candidate_name, company_match='verified', emails=[], phones=[], linkedin=[])
                result = dict(candidates=[candidate], unassigned_contacts=[])
                _deduplicate_crm_contacts(result, [dict(name=crm_name)])
                self.assertEqual(result['candidates'], [])
                queries = build_people_queries(CompanyProfile('Atlas Minerals'), dict(candidates=[candidate]))
                self.assertTrue(queries)
                self.assertTrue(all(candidate_name not in q for q in queries))
        self.assertEqual(person_query_name('Mr Dr. M. van der Green'), 'M. van der Green')
        self.assertEqual(person_query_name('Mrsmith Jones'), 'Mrsmith Jones')

    def test_initials_are_not_expanded_to_different_people(self):
        candidate = dict(full_name='Mr M. Green', company_match='verified', emails=[], phones=[], linkedin=[])
        for crm_name in ['Mark Green', 'Mary Green', 'J. Green']:
            value = dict(candidates=[copy.deepcopy(candidate)], unassigned_contacts=[])
            _deduplicate_crm_contacts(value, [dict(name=crm_name)])
            self.assertEqual(len(value['candidates']), 1)

    def test_existing_person_new_channel_is_retained_but_not_new_person(self):
        value = dict(candidates=[dict(full_name='Mr M. Green', company_match='verified',
            emails=[dict(value='m.green@atlasminerals.example', status='observed')], phones=[], linkedin=[])], unassigned_contacts=[])
        _deduplicate_crm_contacts(value, [dict(name='M. Green')])
        self.assertEqual(len(value['candidates'][0]['emails']), 1)
        self.assertEqual(_new_contactable_people(value), 0)


if __name__ == '__main__':
    unittest.main()
