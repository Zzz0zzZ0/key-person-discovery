import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from key_person_discovery.models import CompanyProfile, CrawledPage
from key_person_discovery.sources import Crawl4aiCrawler, crawl_sync, discover_official_links, discover_official_urls, resolve_official_site
from key_person_discovery.pipeline import discover


class OfficialCoverageTests(unittest.TestCase):
    def test_only_official_entry_bypasses_redirect_losing_disk_cache(self):
        from crawl4ai import CacheMode
        root = 'https://old.example/'
        team = root + 'team'
        def result(url):
            return SimpleNamespace(url=url, redirected_url=url, success=True, html='', markdown='Text')
        instance = AsyncMock()
        instance.arun_many.side_effect = [[result(root)], [result(team)]]
        crawler = Crawl4aiCrawler('http://127.0.0.1:7897')
        crawler.refresh_urls.add(root)
        with patch('crawl4ai.AsyncWebCrawler') as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=instance)
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            pages = asyncio.run(crawler.crawl([team, root]))
        calls = instance.arun_many.call_args_list
        self.assertEqual(calls[0].kwargs['config'].cache_mode, CacheMode.BYPASS)
        self.assertEqual(calls[1].kwargs['config'].cache_mode, CacheMode.ENABLED)
        self.assertEqual([p.url for p in pages], [team, root])

    def test_adapter_preserves_final_url_request_title_and_links(self):
        result = SimpleNamespace(url='https://old.example/start', redirected_url='https://new.example/de/',
            success=True, html='<h1>Nordlicht Minerals</h1>', markdown='# Nordlicht Minerals',
            metadata={'title': 'Nordlicht Minerals'}, links={'internal': [{'href': '/p/7', 'text': 'Ansprechpartner'}]})
        instance = AsyncMock()
        instance.arun_many.return_value = [result]
        with patch('crawl4ai.AsyncWebCrawler') as crawler:
            crawler.return_value.__aenter__ = AsyncMock(return_value=instance)
            crawler.return_value.__aexit__ = AsyncMock(return_value=False)
            page = asyncio.run(Crawl4aiCrawler('http://127.0.0.1:7897').crawl([result.url]))[0]
        self.assertEqual(page.url, result.redirected_url)
        self.assertEqual(page.requested_url, result.url)
        self.assertEqual(page.title, 'Nordlicht Minerals')
        self.assertEqual(page.links[0]['text'], 'Ansprechpartner')

    def test_migration_requires_heading_identity_not_menu_or_shared_group(self):
        company = CompanyProfile('Nordlicht Minerals (Orion Group)', website='https://old.example/')
        target = 'https://new.example/de/'
        for title, expected in [('Nordlicht Minerals — Kontakt', 'confirmed_redirect'), ('Orion Group', 'unverified_redirect')]:
            page = CrawledPage(target, '[Nordlicht Minerals](/companies/nordlicht)', requested_url=company.website, title=title)
            self.assertEqual(resolve_official_site(company, [page])['status'], expected)
        tls = CrawledPage('https://www.old.example/', 'Home', requested_url='http://old.example/')
        self.assertEqual(resolve_official_site(CompanyProfile('Nordlicht', website='http://old.example/'), [tls])['status'], 'confirmed_redirect')

    def test_named_subsidiary_link_requires_fetch_and_stays_within_subtree(self):
        company = CompanyProfile('Nordlicht Minerals (Orion Group)', website='https://old.example/')
        wrong = CrawledPage('https://group.example/companies/steel/', '# Orion Steel\n[Nordlicht Minerals](/companies/minerals/)', requested_url=company.website)
        first = resolve_official_site(company, [wrong])
        self.assertEqual(first['website'], company.website)
        self.assertEqual(first['followup_urls'], ['https://group.example/companies/minerals/'])
        right = CrawledPage(first['followup_urls'][0], '# Nordlicht Minerals\n[Kontakt](contact)\n[Team Steel](/companies/steel/team)')
        final = resolve_official_site(company, [wrong, right])
        self.assertEqual(final['status'], 'confirmed_target_page')
        scoped = CompanyProfile(company.name, website=final['website'])
        self.assertEqual(discover_official_links(scoped, [right]), ['https://group.example/companies/minerals/contact'])

    def test_link_labels_languages_and_sitemap_order_under_budget(self):
        company = CompanyProfile('Nordlicht', website='https://example.org')
        page = CrawledPage(company.website, '[News](/news)\n[Kontakt](/de/kontakt)\n[外部](/nothing)',
            links=[dict(href='/p/42', text='Ansprechpartner'), dict(href='/p/51', text='Contatti'), dict(href='https://foreign.example/contact', text='Contact')])
        self.assertEqual(discover_official_links(company, [page]), ['https://example.org/p/42', 'https://example.org/p/51', 'https://example.org/de/kontakt', 'https://example.org/news'])
        xml = '<urlset>' + ''.join('<url><loc>https://example.org/' + path + '</loc></url>' for path in ['news/1', 'news/2', 'de/ansprechpartner', 'de/impressum']) + '</urlset>'
        with patch('key_person_discovery.sources._fetch_text', side_effect=['', xml]):
            urls, warnings = discover_official_urls(company, '', limit=3)
        self.assertEqual(urls, [company.website, 'https://example.org/de/ansprechpartner', 'https://example.org/de/impressum'])
        self.assertEqual(warnings, [])

    def test_retry_is_mapped_by_requested_url_after_redirect(self):
        old = 'https://old.example/'; new = 'https://new.example/'
        crawler = SimpleNamespace(crawl=AsyncMock(side_effect=[[CrawledPage(old, '', error='timeout')], [CrawledPage(new, 'content', requested_url=old)]]))
        pages = crawl_sync(crawler, [old])
        self.assertEqual(pages[0].url, new)
        self.assertFalse(pages[0].error)

    def test_pipeline_uses_new_domain_and_opaque_contact_page_with_same_budget(self):
        old = 'https://old.example/'; new = 'https://new.example/de/'
        home = CrawledPage(new, '# Nordlicht Minerals', requested_url=old, links=[dict(href='/p/9', text='Ansprechpartner')])
        contact = CrawledPage('https://new.example/p/9', 'Alex Smith Production Engineer alex@new.example')
        requests = []
        class Crawler:
            async def crawl(self, urls, **kwargs):
                requests.extend(urls)
                return [home if url == old else contact for url in urls]
        class Extractor:
            def extract(self, company, pages, signals, usage_path):
                self.website = company.website
                self.emails = [s['value'] for s in signals if s['channel'] == 'email']
                return dict(company_name=company.name, candidates=[], review_required=True)
        extractor = Extractor()
        with tempfile.TemporaryDirectory() as directory:
            result = discover(CompanyProfile('Nordlicht Minerals', website=old), [], Crawler(), extractor,
                Path(directory), max_urls=2, anysearch_query_limit=0, people_search_limit=0,
                official_site_discovery=lambda *_: ([old], []))
        self.assertEqual(requests, [old, contact.url])
        self.assertEqual(extractor.website, new)
        self.assertIn('alex@new.example', extractor.emails)
        self.assertEqual(result['run_summary']['website_resolution'], 'confirmed_redirect')

    def test_pipeline_blocks_wrong_company_redirect_until_target_page_is_fetched(self):
        old = 'https://old.example/'; group = 'https://group.example/companies/'
        wrong = CrawledPage(group + 'steel/', '# Orion Steel\n[Nordlicht Minerals](/companies/minerals/)\nwrong@group.example', requested_url=old)
        right = CrawledPage(group + 'minerals/', '# Nordlicht Minerals\ncorrect@group.example')
        class Crawler:
            async def crawl(self, urls, **kwargs):
                return [wrong if url == old else right for url in urls]
        class Extractor:
            def extract(self, company, pages, signals, usage_path):
                self.website = company.website
                self.emails = [s['value'] for s in signals if s['channel'] == 'email']
                return dict(company_name=company.name, candidates=[], review_required=True)
        extractor = Extractor()
        with tempfile.TemporaryDirectory() as directory:
            result = discover(CompanyProfile('Nordlicht Minerals (Orion Group)', website=old), [], Crawler(), extractor,
                Path(directory), max_urls=2, anysearch_query_limit=0, people_search_limit=0,
                official_site_discovery=lambda *_: ([old], []))
        self.assertEqual(extractor.website, right.url)
        self.assertEqual(extractor.emails, ['correct@group.example'])
        self.assertEqual(result['run_summary']['website_resolution'], 'confirmed_target_page')


if __name__ == '__main__':
    unittest.main()
