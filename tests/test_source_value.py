import unittest
from dataclasses import replace

from key_person_discovery.models import CompanyProfile, SearchResult
from key_person_discovery.search_quality import classify_search_result, select_search_results, search_result_value, engine_quality_report
from key_person_discovery.pipeline import _relevant_urls
import test_search_quality as quality_tests


class SourceValueTests(unittest.TestCase):
    def hit(self, url, title, snippet='', query='"Atlas" procurement', engines=None):
        return SearchResult(query, title, url, snippet, provider='searxng', engines=engines or [])

    def test_one_word_name_requires_business_context_or_official_scope(self):
        company = CompanyProfile('Atlas')
        cases = [
            ('Atlas mountains', 'Travel routes and historical facts', 'pending'),
            ('Atlas', 'Contact our mountain visitor team', 'pending'),
            ('Atlas', 'Manufacturer of industrial components', 'eligible'),
            ('Atlas', '采购负责人和制造企业简介', 'eligible'),
        ]
        for title, snippet, status in cases:
            with self.subTest(title=title, snippet=snippet):
                self.assertEqual(classify_search_result(company, self.hit('https://new.test/article', title, snippet))[0], status)
        official = replace(company, website='https://atlas.test/en/index.html')
        self.assertEqual(classify_search_result(company, self.hit('https://atlas.test/contact', 'Contact - Atlas'))[0], 'eligible')
        self.assertEqual(classify_search_result(company, self.hit('https://travel.test/atlas/contact', 'Contact - Atlas'))[0], 'pending')
        self.assertEqual(classify_search_result(official, self.hit('https://atlas.test/en/contact', 'Contact'))[0], 'eligible')
        self.assertEqual(classify_search_result(official, self.hit('https://atlas.test/other', 'Travel'))[0], 'pending')

    def test_page_value_prioritizes_contact_evidence_over_jobs_and_directories(self):
        company = CompanyProfile('Atlas', website='https://atlas.test')
        hits = [
            self.hit('https://atlas.test/careers/job', 'Atlas hiring Purchasing Manager'),
            self.hit('https://directory.test/atlas', 'Atlas Employee Directory', 'Company employees'),
            self.hit('https://atlas.test/team', 'Atlas Team'),
            self.hit('https://de.linkedin.com/in/jane-doe', 'Jane Doe — Director at Atlas'),
            self.hit('https://conference.test/speakers/jane', 'Jane Doe — Procurement Director', 'Atlas manufacturer'),
            self.hit('https://atlas.test/about', 'About Atlas'),
        ]
        selected, _, decisions = select_search_results(company, hits, 5)
        self.assertEqual([h.url for h in selected], [h.url for h in [hits[2], hits[3], hits[4], hits[5], hits[1]]])
        self.assertEqual(decisions[0]['page_type'], 'recruitment')
        receipt=self.hit('https://atlas.test/contact/thank-you','Atlas Contact Thank You')
        self.assertEqual(search_result_value(company,receipt),(7,'form_confirmation'))
        self.assertNotIn(receipt,select_search_results(company,[receipt,*hits],5)[0])
        # All-job pools remain usable for background; demotion is not deletion.
        self.assertEqual(select_search_results(company, hits[:1], 5)[0], hits[:1])

    def test_stable_ties_and_duplicates_do_not_consume_slots(self):
        company = CompanyProfile('Example Minerals')
        a=self.hit('https://a.test/article', 'Example Minerals', query='q')
        b=self.hit('https://b.test/article', 'Example Minerals', query='q')
        self.assertEqual(select_search_results(company, [a, a, b], 2)[0], [a,b])

    def test_named_external_evidence_enters_crawl_but_generic_or_former_does_not(self):
        company=CompanyProfile('Example Minerals',website='https://example.test')
        good=self.hit('https://conference.test/jane','Jane Doe — Procurement Director','Current at Example Minerals',query='"Example Minerals" procurement')
        self.assertIn(good.url,_relevant_urls(company,[good]))
        former=replace(good,snippet='Former director at Example Minerals; now at Other Company')
        self.assertNotIn(former.url,_relevant_urls(company,[former]))
        generic=replace(good,title='Supplier Spotlight',snippet='Example Minerals purchasing story')
        self.assertEqual(search_result_value(company,generic)[1],'company_evidence')
        self.assertNotIn(generic.url,_relevant_urls(company,[generic]))
        article=replace(generic,url='https://journal.test/blog/example-minerals-procurement',title='Example Minerals procurement transformation')
        self.assertEqual(search_result_value(company,article)[1],'company_article')
        self.assertIn(article.url,_relevant_urls(company,[article]))
        generic_article=replace(article,url='https://journal.test/blog/general-procurement')
        self.assertNotIn(generic_article.url,_relevant_urls(company,[generic_article]))
        # Lowercase quoted role text must not be mistaken for a person's name.
        role=replace(generic,query='"Example Minerals" "sales contact"',snippet='Example Minerals sales contact and purchasing')
        self.assertEqual(search_result_value(company,role)[1],'company_evidence')

    def test_pipeline_applies_value_sort_before_cutoff_and_writes_report(self):
        helper=quality_tests.SearchQualityTests()
        helper.company=CompanyProfile('Example Minerals',website='https://example.test')
        def primary(q):
            return [helper.hit(f'https://jobs.test/jobs/{i}',title='Example Minerals hiring Director',query=q) for i in range(5)] + [helper.hit('https://example.test/team',query=q)]
        _, _, result, files=helper.run_pipeline(primary,lambda q:[])
        self.assertEqual(files['search-results.json'][0]['url'],'https://example.test/team')
        self.assertIn('source_quality',result)
        self.assertIn('page_type',files['search-quality.json'][0]['decisions'][0])

    def test_engine_shared_credit_unknown_provenance_and_crm_exclusion(self):
        company=CompanyProfile('Example Minerals')
        hit=self.hit('https://linkedin.com/in/jane','Jane Doe — Director at Example Minerals',engines=['bing','qwant'])
        decisions=select_search_results(company,[hit,hit],5)[2]
        person={'full_name':'Jane Doe','company_match':'verified','linkedin':[{'value':hit.url,'status':'probable','source_url':hit.url}]}
        audit=[{'query':hit.query,'provider':'searxng','decisions':decisions}]
        report=engine_quality_report(audit,[hit],[person],[])
        self.assertIsNone(engine_quality_report(audit,[hit],None,[])['engines']['bing']['retained_people_with_source_match'])
        for name in ['bing','qwant']:
            self.assertEqual(report['engines'][name]['observations'],1)
            self.assertEqual(report['engines'][name]['retained_people_with_source_match'],1)
            self.assertEqual(report['engines'][name]['exclusive_selected_unique_urls'],0)
        # Deduplication may drop origins on the selected copy; audit still proves them.
        self.assertEqual(engine_quality_report(audit,[replace(hit,engines=['bing'])],[person],[])['engines']['qwant']['retained_people_with_source_match'],1)
        unknown_audit=[{'query':hit.query,'provider':'searxng','decisions':select_search_results(company,[replace(hit,engines=[])],5)[2]}]
        report=engine_quality_report(unknown_audit,[replace(hit,engines=[])],[person],[])
        self.assertEqual(report['unattributed_retained_channels'],1)
        self.assertEqual(report['engines']['searxng:unattributed']['retained_people_with_source_match'],0)
        person['crm_existing_match']=['linkedin']
        self.assertEqual(engine_quality_report(audit,[hit],[person],[])['engines']['qwant']['retained_people_with_source_match'],0)


if __name__=='__main__':
    unittest.main()
