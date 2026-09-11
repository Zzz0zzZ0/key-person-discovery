import io
import json
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from key_person_discovery import pipeline
from key_person_discovery.models import CompanyProfile,CrawledPage,SearchOutcome,SearchResult
from key_person_discovery.sources import SearxngClient,official_link_priority,discover_official_links
from key_person_discovery.diagnostics import recovery_queries

class RetrievalRepairTests(unittest.TestCase):
    def test_full_name_pending_is_verified_before_repeating_initial_queries(self):
        company=CompanyProfile('Atlas Minerals B.V.')
        result=dict(candidates=[dict(full_name='J. Doe')],unverified_candidates=[dict(full_name='Jane Doe')])
        queries=recovery_queries(company,result,dict(primary='missing_channels'))
        self.assertIn('"Jane Doe" "atlas minerals"',queries[0])
        self.assertNotIn('"J. Doe"',queries[0])

    def test_repeated_engine_failures_stop_network_for_this_client(self):
        client=SearxngClient('http://localhost:18080',engines=['google cse'])
        body=dict(results=[],unresponsive_engines=[['google cse','too many requests']])
        with patch('key_person_discovery.sources.urlopen',side_effect=lambda *a,**k:io.BytesIO(json.dumps(body).encode())) as request:
            for _ in range(3):
                with self.assertRaises(RuntimeError):client.search('"Atlas Minerals"')
        self.assertEqual(request.call_count,2)

    def test_official_publications_outrank_confirmation_and_privacy(self):
        company=CompanyProfile('Atlas Minerals','https://atlas.example')
        page=CrawledPage(company.website,'[Publications](/media/publications/)\n[Contact](/contact/thank-you/)\n[Suppliers](/privacy-suppliers/)')
        self.assertIn(company.website+'/media/publications/',discover_official_links(company,[page]))
        self.assertGreater(official_link_priority(company.website+'/contact/contact-us-submitted/'),official_link_priority(company.website+'/media/publications/'))
        self.assertGreater(official_link_priority(company.website+'/privacy-suppliers/'),official_link_priority(company.website+'/media/publications/'))

    def run_budget_case(self,failed_search=False,target_met=False,external_results=False,redirect=False):
        company=CompanyProfile('Atlas Minerals','https://atlas.example',target_contact_count=1)
        new='https://atlas.example/media/interview-jane'
        urls=[company.website]+[company.website+'/contact/'+str(i) for i in range(15)]
        batches=[]
        class Search:
            name='searxng'
            def search(self,query,limit=5):
                if failed_search:raise RuntimeError('source unavailable')
                if external_results and '"Jane Doe"' in query:
                    return SearchOutcome([SearchResult(query,'Jane Doe - Director at Atlas Minerals',f'https://directory.example/person/{i}','Jane Doe, current director at Atlas Minerals') for i in range(5)])
                return SearchOutcome([SearchResult(query,'Jane Doe - Director at Atlas Minerals',new,'Jane Doe, current director at Atlas Minerals')]) if '"Jane Doe"' in query else SearchOutcome([])
        def crawl(crawler,selected,followup_urls=None):
            pages=[CrawledPage(u,'Atlas Minerals: Jane Doe, Director. [Publication]('+new+')' if u==company.website else 'Atlas Minerals contact information') for u in selected]
            if redirect:
                pages=[CrawledPage('https://atlas.example/contact/canonical',p.markdown,requested_url=p.url)
                       if p.url==urls[1] else p for p in pages]
                pages=[CrawledPage(p.url,p.markdown+' [Contact]('+urls[1]+')',requested_url=p.requested_url) for p in pages]
            batches.append(list(selected))
            if followup_urls:
                extra=followup_urls(pages);batches.append(extra)
                pages += [CrawledPage(u,'Atlas Minerals: Jane Doe, Director') for u in extra]
            return pages
        class Extractor:
            calls=0
            def extract(self,company,pages,signals,usage_path):
                self.calls+=1
                p=dict(full_name='Jane Doe',current_title='Director',company_match='verified',influence_type='decision_maker',review_required=True,evidence=[dict(source_url=company.website,quote='Jane Doe, Director at Atlas Minerals')],emails=[],phones=[],linkedin=[])
                if target_met:p['linkedin']=[dict(value='https://linkedin.com/in/jane-doe',status='observed',source_url=company.website)]
                return dict(company_name=company.name,candidates=[p],review_required=True)
        extractor=Extractor()
        with tempfile.TemporaryDirectory() as directory,patch.object(pipeline,'crawl_sync',side_effect=crawl):
            result=pipeline.discover(company,[Search()],object(),extractor,Path(directory),max_urls=10,people_search_limit=3,anysearch_query_limit=0,broad_discovery=True,official_site_discovery=lambda *_:(urls,[]))
        return batches,extractor,result,new

    def test_topup_can_fetch_new_evidence_within_total_budget(self):
        batches,extractor,result,new=self.run_budget_case()
        self.assertLessEqual(sum(map(len,batches[:2])),7)
        self.assertLessEqual(sum(map(len,batches)),10)
        self.assertTrue(any(new in batch for batch in batches[2:]),batches)
        self.assertEqual(extractor.calls,2)

    def test_official_followup_survives_search_failure(self):
        batches,extractor,result,new=self.run_budget_case(failed_search=True)
        self.assertTrue(any(new in b for b in batches[2:]),batches)
        self.assertLessEqual(sum(map(len,batches)),10)
        self.assertEqual(extractor.calls,2)

    def test_target_met_does_not_spend_more_model_calls(self):
        batches,extractor,result,new=self.run_budget_case(target_met=True)
        self.assertEqual(extractor.calls,1)

    def test_official_followup_is_not_crowded_out_by_person_directories(self):
        batches,extractor,result,new=self.run_budget_case(external_results=True)
        self.assertEqual(batches[2][0],new)
        self.assertLessEqual(sum(map(len,batches)),10)

    def test_topup_does_not_refetch_known_redirect_alias(self):
        batches,extractor,result,new=self.run_budget_case(redirect=True)
        self.assertEqual(sum(batch.count('https://atlas.example/contact/0') for batch in batches),1)

    def test_late_profile_attachment_does_not_double_count_one_person(self):
        def attach(result,*args):
            duplicate=copy.deepcopy(result['candidates'][0])
            duplicate['full_name']='J. Doe'
            result['candidates'].append(duplicate)
        with patch.object(pipeline,'_attach_named_linkedin_profiles',side_effect=attach):
            batches,extractor,result,new=self.run_budget_case(target_met=True)
        self.assertEqual(result['run_summary']['new_contactable_people'],1)
        self.assertEqual(len(result['candidates']),1)
