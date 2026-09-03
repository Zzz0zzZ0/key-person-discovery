import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from key_person_discovery.batch import _active_delta, run_batch
from key_person_discovery.crm import list_companies, search_companies
from key_person_discovery.dashboard import DiscoveryJobs, create_app, load_results
from key_person_discovery.hermes import _validate_output, parse_json_object
from key_person_discovery.models import (
    CompanyProfile,
    CrawledPage,
    SearchOutcome,
    SearchResult,
    normalize_linkedin,
)
from key_person_discovery.jobs import JobStore, PROJECT_DIR, project_storage_path, spawn_runner
from key_person_discovery.pdf_source import extract_pdf_urls
from key_person_discovery.pipeline import (
    _contact_source_priority,
    _contact_method_count,
    _contactable_items,
    _official_website_from_search,
    _relevant_urls,
    _search_evidence_pages,
    discover,
)
from key_person_discovery.preflight import (
    EGRESS_CHECK_URLS,
    _trace_fingerprint,
    _validate_local_searxng_proxy,
)
from key_person_discovery.signals import extract_contact_signals
from key_person_discovery.sources import (
    SearxngClient,
    TavilyClient,
    _parse_anysearch_markdown,
    build_customs_queries,
    build_official_query,
    discover_official_links,
    _sitemap_locations,
    build_queries,
    crawl_sync,
)
from key_person_discovery.topeasy import merge_topeasy_export


class CoreTests(unittest.TestCase):
    def test_fastapi_dashboard_preserves_routes_and_security_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = root / "index.html"
            index.write_text("<h1>Dashboard</h1>", encoding="utf-8")
            jobs = MagicMock()
            jobs.list.return_value = [{"id": "job-1"}]
            jobs.start.return_value = {"id": "job-2", "status": "queued"}
            jobs.store.noncompleted_output_paths.return_value = set()
            client = TestClient(create_app(root / "outputs", index, jobs))

            page = client.get("/")
            listed = client.get("/api/jobs?limit=5")
            blocked = client.post(
                "/api/discover",
                headers={"Origin": "http://other.example"},
                json={"name": "Example"},
            )
            started = client.post("/api/discover", json={"name": "Example"})

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.text, "<h1>Dashboard</h1>")
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertEqual(listed.json(), [{"id": "job-1"}])
        jobs.list.assert_called_once_with(5)
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.headers["cache-control"], "no-store")
        self.assertEqual(started.status_code, 202)
        jobs.start.assert_called_once_with({"name": "Example", "website": None})

    def test_topeasy_export_keeps_people_and_same_domain_contacts_only(self):
        company = CompanyProfile(name="Heatmasters", website="https://heatmasters.net")
        result = {"candidates": [], "unverified_candidates": [], "unassigned_contacts": []}
        csv_text = (
            "头像,公司名称,名称,领英地址,职位,Email\n"
            ",Heatmasters,Ilkka Mujunen,https://de.linkedin.com/in/mujunenilkka?trk=x,CEO,ilkka.mujunen@heatmasters.net\n"
            ",Heatmasters,,,,info@heatmasters.net\n"
            ",Heatmasters,,,,partner@example.com\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "用户数据表.xls"
            path.write_text(csv_text, encoding="utf-8-sig")
            summary = merge_topeasy_export(result, company, path)

        self.assertEqual(summary["rows_total"], 3)
        self.assertEqual(summary["candidates_added"], 1)
        self.assertEqual(summary["public_contacts_added"], 1)
        self.assertEqual(summary["foreign_emails_dropped"], 1)
        candidate = result["unverified_candidates"][0]
        self.assertEqual(candidate["discovery_tier"], "third_party_unverified")
        self.assertEqual(
            candidate["linkedin"][0]["value"],
            "https://www.linkedin.com/in/mujunenilkka",
        )
        self.assertEqual(candidate["emails"][0]["value"], "ilkka.mujunen@heatmasters.net")
        self.assertEqual(result["unassigned_contacts"][0]["value"], "info@heatmasters.net")

    def test_topeasy_export_enriches_existing_candidate_without_duplication(self):
        company = CompanyProfile(name="Heatmasters", website="https://heatmasters.net")
        result = {
            "candidates": [],
            "unverified_candidates": [
                {
                    "full_name": "Ilkka Mujunen",
                    "current_title": "",
                    "linkedin": [{"value": "https://www.linkedin.com/in/mujunenilkka"}],
                    "emails": [],
                    "phones": [],
                    "evidence": [],
                }
            ],
            "unassigned_contacts": [],
        }
        csv_text = (
            "头像,公司名称,名称,领英地址,职位,Email\n"
            ",Heatmasters,Ilkka Mujunen,https://www.linkedin.com/in/mujunenilkka,CEO,ilkka.mujunen@heatmasters.net\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.xls"
            path.write_text(csv_text, encoding="utf-8-sig")
            summary = merge_topeasy_export(result, company, path)

        self.assertEqual(summary["candidates_added"], 0)
        self.assertEqual(summary["candidates_enriched"], 1)
        self.assertEqual(len(result["unverified_candidates"]), 1)
        self.assertEqual(result["unverified_candidates"][0]["current_title"], "CEO")
        self.assertEqual(
            result["unverified_candidates"][0]["emails"][0]["value"],
            "ilkka.mujunen@heatmasters.net",
        )

    def test_topeasy_export_requires_expected_columns(self):
        company = CompanyProfile(name="Heatmasters", website="https://heatmasters.net")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.xls"
            path.write_text("Email\ninfo@heatmasters.net\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing required columns"):
                merge_topeasy_export({}, company, path)

    def test_localized_crm_country_names_map_to_phone_regions(self):
        from key_person_discovery.jobs import _phone_region

        self.assertEqual(_phone_region("日本"), "JP")
        self.assertEqual(_phone_region("越南"), "VN")
        self.assertEqual(_phone_region("韩国"), "KR")
        self.assertEqual(_phone_region("russian federation"), "RU")
        self.assertEqual(_phone_region("俄罗斯"), "RU")
        self.assertEqual(_phone_region("Spain"), "ES")
        self.assertEqual(_phone_region("us"), "US")
        self.assertIsNone(_phone_region("unknown"))
        signals = extract_contact_signals(
            [CrawledPage(url="https://example.vn/contact", markdown="Contact: 0913.858.349")],
            _phone_region("越南"),
        )
        self.assertEqual(signals[0]["value"], "+84913858349")

    def test_spanish_sl_legal_suffix_is_removed_from_company_alias(self):
        from key_person_discovery.models import company_name_aliases

        self.assertIn("servifund mol", company_name_aliases("SERVIFUND&MOL, S.L."))

    def test_project_storage_cannot_escape_key_search_root(self):
        self.assertEqual(
            project_storage_path(PROJECT_DIR / "outputs", "outputs"),
            (PROJECT_DIR / "outputs").resolve(),
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "inside the Key Search project"):
                project_storage_path(Path(directory), "outputs")

    def test_crm_search_is_read_only_and_normalizes_company(self):
        response = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "id": "id",
                    "name": "Example Minerals",
                    "website": "example.com",
                    "linkedin_url": "",
                    "country": "US",
                    "updated_at": "2026-08-14T00:00:00+00:00",
                }
            ),
        )
        environment = {
            "TWENTY_DB_HOST": "db.example",
            "TWENTY_DB_NAME": "twenty",
            "TWENTY_DB_USER": "readonly",
            "TWENTY_DB_PASSWORD": "secret",
            "TWENTY_WORKSPACE_SCHEMA": "workspace_test",
            "PSQL_BIN": "/usr/bin/psql",
        }
        with patch.dict("os.environ", environment, clear=True), patch(
            "key_person_discovery.crm.subprocess.run", return_value=response
        ) as run:
            results = search_companies("Example")
        self.assertEqual(results[0]["website"], "https://example.com")
        self.assertEqual(results[0]["updated_at"], "2026-08-14T00:00:00+00:00")
        command = run.call_args.args[0]
        self.assertIn("search=Example", command)
        sql = run.call_args.kwargs["input"]
        self.assertIn("BEGIN READ ONLY", sql)
        self.assertIn("COMMIT", sql)

    def test_discovery_job_saves_snapshot_and_hides_crm_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile.json"
            profile.write_text(json.dumps({"industries": ["Refractory"], "products": ["Silicon Carbide"]}))
            db = root / "state" / "jobs.sqlite3"
            with patch("key_person_discovery.dashboard.spawn_runner") as runner:
                jobs = DiscoveryJobs(root / "outputs", root / "inputs", profile, db)
                job = jobs.start(
                    {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "name": "Example Minerals",
                        "website": "example.com",
                        "country": "US",
                        "contact_count": 2,
                        "contacts": [
                            {
                                "name": "Existing Person",
                                "email": "existing@example.com",
                                "phone": "+12025550100",
                                "linkedin": "https://linkedin.com/in/existing",
                            }
                        ],
                    }
                )
            restored = DiscoveryJobs(root / "outputs", root / "inputs", profile, db).get(job["id"])
            snapshot_path = next((root / "inputs").glob("*/*.json"))
            snapshot = json.loads(snapshot_path.read_text())
            with patch("key_person_discovery.jobs.subprocess.Popen") as popen, patch.dict(
                "os.environ",
                {"TWENTY_DB_PASSWORD": "secret", "KEY_PERSON_PROXY_URL": "proxy"},
                clear=False,
            ):
                spawn_runner(jobs.store, job["id"])
        self.assertEqual(job["status"], "queued")
        self.assertEqual(restored["status"], "queued")
        runner.assert_called_once()
        self.assertEqual(snapshot["website"], "https://example.com")
        self.assertEqual(snapshot["products"], ["Silicon Carbide"])
        self.assertEqual(snapshot["crm_contact_count"], 2)
        self.assertEqual(snapshot["target_contact_count"], 4)
        self.assertFalse(snapshot["customs_search_enabled"])
        self.assertEqual(snapshot["crm_contacts"][0]["name"], "Existing Person")
        child_env = popen.call_args.kwargs["env"]
        self.assertNotIn("TWENTY_DB_PASSWORD", child_env)
        self.assertEqual(child_env["KEY_PERSON_PROXY_URL"], "proxy")

    def test_no_new_contact_is_completed_and_visible_in_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile.json"
            profile.write_text(json.dumps({"industries": [], "products": []}))
            with patch("key_person_discovery.dashboard.spawn_runner"):
                jobs = DiscoveryJobs(
                    root / "outputs", root / "inputs", profile, root / "jobs.sqlite3"
                )
                job = jobs.start({"name": "No New Contact", "website": "example.com"})
            output_path = Path(jobs.store.get_raw(job["id"])["output_path"])
            output_path.write_text("{}")
            jobs.store.finish(job["id"], completed=True, stage="no_new_contact")

            restored = jobs.get(job["id"])
            self.assertEqual(restored["status"], "completed")
            self.assertEqual(restored["stage"], "no_new_contact")
            self.assertNotIn(output_path.resolve(), jobs.store.noncompleted_output_paths())

    def test_runner_applies_output_substate(self):
        from key_person_discovery.job_runner import run_job

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile.json"
            profile.write_text(json.dumps({"industries": [], "products": []}))
            with patch("key_person_discovery.dashboard.spawn_runner"):
                jobs = DiscoveryJobs(
                    root / "outputs", root / "inputs", profile, root / "jobs.sqlite3"
                )
                no_new = jobs.start({"name": "No New", "website": "example.com"})
                limited = jobs.start({"name": "Limited", "website": "limited.example"})

            summaries = {
                no_new["id"]: {"search_results": 10, "urls_crawled": 2, "crawl_failures": 0},
                limited["id"]: {"search_results": 10, "urls_crawled": 2, "crawl_failures": 2},
            }
            process = MagicMock(returncode=0)
            process.poll.return_value = 0
            results = {}
            with patch("key_person_discovery.job_runner.subprocess.Popen", return_value=process):
                for job_id, summary in summaries.items():
                    output_path = Path(jobs.store.get_raw(job_id)["output_path"])
                    output_path.write_text(
                        json.dumps(
                            {
                                "candidates": [],
                                "unassigned_contacts": [],
                                "run_summary": summary,
                            }
                        )
                    )
                    results[job_id] = run_job(jobs.store, job_id)

            self.assertEqual(results[no_new["id"]], 0)
            self.assertEqual(jobs.get(no_new["id"])["stage"], "no_new_contact")
            self.assertEqual(results[limited["id"]], 1)
            self.assertEqual(jobs.get(limited["id"])["stage"], "source_limited")

    def test_crm_company_pages_use_stable_id_cursor(self):
        response = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "name": "Second Company",
                    "website": "second.example",
                    "linkedin_url": "",
                    "country": "US",
                    "updated_at": "2026-08-14T00:00:00+00:00",
                }
            ),
        )
        environment = {
            "TWENTY_DB_HOST": "db.example",
            "TWENTY_DB_NAME": "twenty",
            "TWENTY_DB_USER": "readonly",
            "TWENTY_WORKSPACE_SCHEMA": "workspace_test",
            "PSQL_BIN": "/usr/bin/psql",
        }
        cursor = "00000000-0000-0000-0000-000000000001"
        with patch.dict("os.environ", environment, clear=True), patch(
            "key_person_discovery.crm.subprocess.run", return_value=response
        ) as run:
            companies = list_companies(cursor, 25)
        self.assertEqual(companies[0]["website"], "https://second.example")
        self.assertIn(f"after_id={cursor}", run.call_args.args[0])
        self.assertIn("ORDER BY company.id", run.call_args.kwargs["input"])

    def test_batch_enqueues_persistent_crm_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile.json"
            profile.write_text(json.dumps({"industries": [], "products": []}))
            jobs = DiscoveryJobs(root / "outputs", root / "inputs", profile, root / "jobs.sqlite3")
            company = {
                "id": "00000000-0000-0000-0000-000000000010",
                "name": "Batch Company",
                "website": "https://batch.example",
                "country": "US",
            }
            with patch("key_person_discovery.batch.list_companies", return_value=[company]), patch(
                "key_person_discovery.batch.spawn_runner"
            ), patch("key_person_discovery.batch.time.sleep"):
                batch = run_batch(
                    store=jobs.store,
                    outputs_dir=root / "outputs",
                    inputs_dir=root / "inputs",
                    profile_path=profile,
                    duration_hours=1,
                    workers=1,
                    page_size=10,
                    max_companies=1,
                )
            self.assertEqual(batch["status"], "completed")
            self.assertEqual(batch["enqueued"], 1)
            self.assertEqual(batch["duration_seconds"], 3600)
            self.assertEqual(jobs.list()[0]["company_name"], "Batch Company")

    def test_batch_active_time_ignores_sleep_gap(self):
        start = datetime(2026, 8, 14, tzinfo=timezone.utc)
        self.assertEqual(_active_delta(start, start + timedelta(seconds=2)), 2)
        self.assertEqual(_active_delta(start, start + timedelta(hours=8)), 0)

        with tempfile.TemporaryDirectory() as directory:
            store = DiscoveryJobs(
                Path(directory) / "outputs",
                Path(directory) / "inputs",
                Path(directory) / "profile.json",
                Path(directory) / "jobs.sqlite3",
            ).store
            batch = store.create_batch(hours=48, workers=1)
            store.add_batch_active_seconds(batch["id"], 120)
            current = store.get_batch(batch["id"])
        self.assertEqual(current["active_seconds"], 120)
        self.assertEqual(current["remaining_seconds"], 48 * 3600 - 120)

    def test_dashboard_loads_results_but_not_summaries_invalid_or_excluded_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = Path(directory)
            run = outputs / "run-v1"
            run.mkdir()
            (run / "company.json").write_text(
                json.dumps({"company_name": "Example", "candidates": []}),
                encoding="utf-8",
            )
            (run / "summary.json").write_text(
                json.dumps({"companies": []}),
                encoding="utf-8",
            )
            excluded = run / "failed-job.json"
            excluded.write_text(
                json.dumps({"company_name": "Failed Company", "candidates": []}),
                encoding="utf-8",
            )
            (run / "broken.json").write_text("not json", encoding="utf-8")
            results = load_results(outputs, {excluded.resolve()})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["company_name"], "Example")
        self.assertEqual(results[0]["run"], "run-v1")

    def test_dashboard_keeps_latest_retry_per_company_and_run(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = Path(directory)
            run = outputs / "run-v1"
            run.mkdir()
            old = run / "company-old.json"
            new = run / "company-new.json"
            old.write_text(
                json.dumps({"company_name": "Example", "candidates": [{"full_name": "Old"}]}),
                encoding="utf-8",
            )
            new.write_text(
                json.dumps({"company_name": "Example", "candidates": [{"full_name": "New"}]}),
                encoding="utf-8",
            )
            os.utime(old, (1, 1))
            os.utime(new, (2, 2))

            results = load_results(outputs)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["file"], "company-new.json")

    def test_queries_cover_people_and_company_domain(self):
        company = CompanyProfile(name="Example Minerals", website="https://www.example.com")
        queries = build_queries(company)
        self.assertTrue(any("linkedin.com/in" in query for query in queries))
        self.assertTrue(any("refractory engineer" in query for query in queries))
        self.assertTrue(any("site:example.com" in query for query in queries))
        self.assertEqual(
            build_official_query(company),
            '"Example Minerals" official website',
        )

    def test_official_website_search_precedes_contact_search_and_recovers_domain(self):
        class SearchStub:
            name = "anysearch"

            def __init__(self):
                self.calls = []

            def search(self, query):
                self.calls.append(query)
                if query == '"Batts Kilns & Furnaces Ltd" official website':
                    return SearchOutcome(
                        results=[
                            SearchResult(
                                query=query,
                                title="Kilns & Furnaces Ltd",
                                url="https://www.kompass.com/z/gb/c/kilns-furnaces-ltd/gb80109752/",
                                snippet="Kilns and furnaces in Stoke-on-Trent. Website available.",
                                provider=self.name,
                                rank=1,
                            ),
                            SearchResult(
                                query=query,
                                title="KILNS AND FURNACES LIMITED",
                                url="https://bringo.co.uk/company/9912419",
                                snippet="Kilns & Furnaces Ltd. Website https://kilns.co.uk",
                                provider=self.name,
                                rank=2,
                            )
                        ]
                    )
                return SearchOutcome(results=[])

        class HermesStub:
            website = ""

            def extract(self, company, pages, signals, usage_path):
                self.website = company.website
                return {"company_name": company.name, "candidates": [], "review_required": True}

        search = SearchStub()
        hermes = HermesStub()
        discovered = []

        def official_discovery(company, *_):
            discovered.append(company.website)
            return [company.website], []

        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[],
        ):
            result = discover(
                company=CompanyProfile(
                    name="Batts Kilns & Furnaces Ltd",
                    website="https://battskilns.co.uk",
                    target_contact_count=4,
                ),
                search_clients=[search],
                crawler=object(),
                hermes=hermes,
                artifacts_dir=Path(directory) / "artifacts",
                official_site_discovery=official_discovery,
                anysearch_query_limit=100,
            )

        self.assertEqual(search.calls[0], '"Batts Kilns & Furnaces Ltd" official website')
        self.assertEqual(discovered, ["https://kilns.co.uk"])
        self.assertEqual(hermes.website, "https://kilns.co.uk")
        self.assertTrue(result["run_summary"]["website_recovered"])
        self.assertEqual(result["run_summary"]["anysearch_queries"], 9)

    def test_official_website_recovery_rejects_unrelated_same_name_domains(self):
        cases = [
            (
                CompanyProfile(
                    name="ECOACERO (Grupo Estrella)",
                    website="https://aceroestrella.com.do",
                ),
                SearchResult(
                    query='"ECOACERO (Grupo Estrella)" official website',
                    title="ECOACERO",
                    url="https://ecoacero.com/",
                    snippet="ECOACERO official website for circular steel",
                    provider="anysearch",
                    rank=1,
                ),
            ),
            (
                CompanyProfile(name="IMF Group S.p.A.", website="https://imf-group.com"),
                SearchResult(
                    query='"IMF Group S.p.A." official website',
                    title="IMF Group",
                    url="https://www.imf-i.com/",
                    snippet="IMF Group international offices",
                    provider="anysearch",
                    rank=1,
                ),
            ),
            (
                CompanyProfile(name="Heatmasters", website="https://heatmasters.net"),
                SearchResult(
                    query='"Heatmasters" official website',
                    title="Heatmasters Mechanical",
                    url="https://www.linkedin.com/company/heatmasters-mechanical",
                    snippet=(
                        "Heatmasters Mechanical HVAC contractor in Chicago. "
                        "Website https://www.heatmastersmechanical.com/"
                    ),
                    provider="anysearch",
                    rank=1,
                ),
            ),
            (
                CompanyProfile(
                    name="GARION INTERNATIONAL LTD",
                    website="https://c551701.tradekorea.com",
                ),
                SearchResult(
                    query='"GARION INTERNATIONAL LTD" official website',
                    title="Garion International Ltd.",
                    url="https://garion.en.ec21.com/",
                    snippet="EC21 supplier profile for Garion International Ltd.",
                    provider="anysearch",
                    rank=1,
                ),
            ),
        ]
        for company, result in cases:
            with self.subTest(company=company.name):
                self.assertEqual(_official_website_from_search(company, [result]), "")

    def test_current_batch_does_not_substitute_historical_reprocessed_results(self):
        html = (PROJECT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        current_scope = html.split('if (state.run === "__current__") {', 1)[1].split(
            "\n      }", 1
        )[0]
        self.assertIn("state.currentBatchFiles.has(item.file)", current_scope)
        self.assertNotIn("reprocessed_at", current_scope)

    def test_customs_queries_include_company_and_target_products(self):
        company = CompanyProfile(
            name="Example Minerals",
            products=["Silicon Carbide", "Tabular Alumina"],
        )
        queries = build_customs_queries(company)
        self.assertEqual(len(queries), 3)
        self.assertTrue(all('"Example Minerals"' in query for query in queries))
        self.assertTrue(all('"Silicon Carbide"' in query for query in queries))

    def test_low_contact_company_expands_queries_and_counts_public_channels(self):
        company = CompanyProfile(
            name="Example Minerals",
            website="https://example.com",
            crm_contact_count=1,
            target_contact_count=4,
        )
        queries = build_queries(company)
        self.assertTrue(any("sales contact" in query for query in queries))
        self.assertTrue(any('"mailto:"' in query for query in queries))
        self.assertEqual(
            _contactable_items(
                {
                    "candidates": [
                        {"full_name": "Jane", "linkedin": [{"value": "https://linkedin.com/in/jane"}]},
                        {"full_name": "No channel", "linkedin": [], "emails": [], "phones": []},
                    ],
                    "unassigned_contacts": [
                        {"channel": "email", "value": "sales@example.com"},
                        {"channel": "phone", "value": "+12025550100"},
                        {"channel": "whatsapp", "value": "+12025550100"},
                    ],
                }
            ),
            3,
        )

    def test_contact_method_count_counts_channels_instead_of_people(self):
        self.assertEqual(
            _contact_method_count(
                {
                    "candidates": [
                        {
                            "linkedin": [{"value": "https://linkedin.com/in/jane"}],
                            "emails": [{"value": "jane@example.com"}],
                            "phones": [
                                {
                                    "value": "+12025550100",
                                    "whatsapp_status": "verified",
                                }
                            ],
                        }
                    ],
                    "unassigned_contacts": [
                        {"channel": "email", "value": "sales@example.com"},
                        {"channel": "phone", "value": "+12025550100"},
                    ],
                }
            ),
            5,
        )

    def test_searxng_json_response_is_parsed(self):
        payload = {
            "results": [
                {
                    "title": "Jane Doe",
                    "url": "https://example.com/jane",
                    "content": "Purchasing Manager",
                }
            ]
        }
        with patch(
            "key_person_discovery.sources.urlopen",
            return_value=io.BytesIO(json.dumps(payload).encode()),
        ) as request:
            outcome = SearxngClient("http://127.0.0.1:8080").search("Jane Doe")
        self.assertEqual(outcome.results[0].url, "https://example.com/jane")
        self.assertEqual(outcome.unresponsive_engines, [])
        parsed = urlparse(request.call_args.args[0].full_url)
        self.assertEqual(parse_qs(parsed.query)["format"], ["json"])
        self.assertNotIn("engines", parse_qs(parsed.query))

    def test_tavily_json_response_is_parsed_with_request_parameters(self):
        payload = {
            "results": [
                {
                    "title": "Jane Doe",
                    "url": "https://example.com/jane",
                    "content": "Purchasing Manager",
                }
            ],
            "usage": {"credits": 1},
        }
        with patch(
            "key_person_discovery.sources._open_request",
            return_value=io.BytesIO(json.dumps(payload).encode()),
        ) as request:
            outcome = TavilyClient("test-key").search("Jane Doe", limit=5)
        self.assertEqual(outcome.results[0].url, "https://example.com/jane")
        self.assertEqual(outcome.results[0].snippet, "Purchasing Manager")
        sent_request = request.call_args.args[0]
        sent_payload = json.loads(sent_request.data)
        self.assertEqual(sent_payload["query"], "Jane Doe")
        self.assertEqual(sent_payload["search_depth"], "basic")
        self.assertEqual(sent_payload["max_results"], 5)
        self.assertFalse(sent_payload["include_answer"])
        self.assertFalse(sent_payload["include_raw_content"])
        self.assertTrue(sent_payload["include_usage"])
        self.assertTrue(sent_request.headers.get("Authorization"))

    def test_tavily_filters_invalid_urls_and_clamps_limit(self):
        payload = {
            "results": [
                {"title": "bad", "url": "javascript:alert(1)", "content": "bad"},
                {"title": "first", "url": "https://example.com/1", "content": "one"},
                {"title": "second", "url": "http://example.com/2", "content": "two"},
            ]
        }
        with patch(
            "key_person_discovery.sources._open_request",
            return_value=io.BytesIO(json.dumps(payload).encode()),
        ) as request:
            outcome = TavilyClient("test-key").search("query", limit=0)
        self.assertEqual([item.url for item in outcome.results], ["https://example.com/1"])
        sent_payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(sent_payload["max_results"], 1)

    def test_tavily_requires_api_key(self):
        with self.assertRaisesRegex(ValueError, "API key is required"):
            TavilyClient()

    def test_tavily_provider_error_does_not_expose_api_key(self):
        api_key = "test-secret-key"
        payload = {"detail": {"error": f"invalid key {api_key}"}}
        with patch(
            "key_person_discovery.sources._open_request",
            return_value=io.BytesIO(json.dumps(payload).encode()),
        ), self.assertRaises(RuntimeError) as raised:
            TavilyClient(api_key).search("query")
        self.assertNotIn(api_key, str(raised.exception))

    def test_searxng_explicit_engines_are_added_to_search_request(self):
        payload = {"results": []}
        with patch(
            "key_person_discovery.sources.urlopen",
            return_value=io.BytesIO(json.dumps(payload).encode()),
        ) as request:
            SearxngClient(
                "http://127.0.0.1:8080", engines=["bing", "yandex"]
            ).search("Jane Doe")
        parsed = urlparse(request.call_args.args[0].full_url)
        self.assertEqual(parse_qs(parsed.query)["engines"], ["bing,yandex"])

    def test_searxng_explicit_engine_failure_is_reported(self):
        payload = {
            "results": [],
            "unresponsive_engines": [["yandex", "timeout"]],
        }
        with patch(
            "key_person_discovery.sources.urlopen",
            return_value=io.BytesIO(json.dumps(payload).encode()),
        ), self.assertRaisesRegex(RuntimeError, "All active SearXNG engines failed"):
            SearxngClient(
                "http://127.0.0.1:8080", engines=["yandex"]
            ).search("Jane Doe")

    def test_cli_searxng_engine_parser_strips_empty_values(self):
        from key_person_discovery.cli import _parse_searxng_engines

        self.assertEqual(
            _parse_searxng_engines(" bing, ,yandex ,, naver "),
            ["bing", "yandex", "naver"],
        )

    def test_cli_searxng_engines_add_local_engine_for_supported_region(self):
        from key_person_discovery.cli import _searxng_engines_for_region

        global_engines = ["bing", "duckduckgo", "google cse", "qwant"]
        self.assertEqual(
            _searxng_engines_for_region("cn"),
            [*global_engines, "baidu"],
        )
        self.assertEqual(
            _searxng_engines_for_region("KR"),
            [*global_engines, "naver"],
        )
        self.assertEqual(
            _searxng_engines_for_region("RU"),
            [*global_engines, "yandex"],
        )
        self.assertEqual(_searxng_engines_for_region("DE"), [])
        self.assertEqual(_searxng_engines_for_region(None), [])

    def test_cli_selects_regional_engines_and_keeps_experimental_override(self):
        from key_person_discovery import cli

        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as directory:
            root = Path(directory)
            profile = root / "profile.json"
            output = root / "result.json"
            profile.write_text(
                json.dumps({"name": "Example Minerals", "website": "example.com"}),
                encoding="utf-8",
            )
            for broad_discovery, phone_region, expected_engines in (
                (False, None, None),
                (
                    False,
                    "CN",
                    ["bing", "duckduckgo", "google cse", "qwant", "baidu"],
                ),
                (True, "CN", ["bing", "yandex"]),
            ):
                argv = [
                    "key-person-discovery",
                    "--company",
                    str(profile),
                    "--output",
                    str(output),
                ]
                if phone_region:
                    argv.extend(["--phone-region", phone_region])
                with self.subTest(
                    broad_discovery=broad_discovery,
                    phone_region=phone_region,
                ), patch.dict(
                    os.environ,
                    {
                        "KEY_PERSON_PROXY_URL": "http://127.0.0.1:12001",
                        "KEY_PERSON_EXPERIMENTAL_BROAD_DISCOVERY": str(
                            broad_discovery
                        ),
                        "KEY_PERSON_EXPERIMENTAL_SEARXNG_ENGINES": " bing, ,yandex ",
                    },
                    clear=False,
                ), patch.object(
                    sys,
                    "argv",
                    argv,
                ), patch(
                    "key_person_discovery.cli.Crawl4aiCrawler"
                ), patch(
                    "key_person_discovery.cli.verify_proxy_setup",
                    return_value="fingerprint",
                ), patch(
                    "key_person_discovery.cli.discover",
                    return_value={"run_summary": {}},
                ), patch("key_person_discovery.cli.SearxngClient") as searxng:
                    cli.main()

                if expected_engines is None:
                    searxng.assert_called_once_with("http://127.0.0.1:18080")
                else:
                    searxng.assert_called_once_with(
                        "http://127.0.0.1:18080", engines=expected_engines
                    )

    def test_cli_defaults_anysearch_budget_to_five(self):
        from key_person_discovery import cli

        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as directory:
            root = Path(directory)
            profile = root / "profile.json"
            output = root / "result.json"
            profile.write_text(
                json.dumps({"name": "Example Minerals", "website": "example.com"}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"KEY_PERSON_PROXY_URL": "http://127.0.0.1:12001"},
                clear=True,
            ), patch.object(
                sys,
                "argv",
                ["key-person-discovery", "--company", str(profile), "--output", str(output)],
            ), patch(
                "key_person_discovery.cli.Crawl4aiCrawler"
            ), patch(
                "key_person_discovery.cli.verify_proxy_setup",
                return_value="fingerprint",
            ), patch(
                "key_person_discovery.cli.discover",
                return_value={"run_summary": {}},
            ) as discovery:
                cli.main()

            self.assertEqual(discovery.call_args.kwargs["anysearch_query_limit"], 5)

    def test_anysearch_markdown_is_parsed_with_provenance(self):
        results = _parse_anysearch_markdown(
            "Example Minerals purchasing",
            "## Search Results (1 result, 10ms)\n\n"
            "### 1. Jane Doe | LinkedIn\n"
            "- **URL**: https://www.linkedin.com/in/jane-doe\n"
            "- Purchasing Manager at Example Minerals\n",
        )
        self.assertEqual(results[0].title, "Jane Doe | LinkedIn")
        self.assertEqual(results[0].provider, "anysearch")
        self.assertEqual(results[0].rank, 1)

    def test_sitemap_locations_keep_http_urls_only(self):
        xml = (
            '<?xml version="1.0"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            '<url><loc>https://example.com/team</loc></url>'
            '<url><loc>javascript:alert(1)</loc></url>'
            '</urlset>'
        )
        self.assertEqual(_sitemap_locations(xml), ["https://example.com/team"])

    def test_official_links_are_discovered_from_crawled_homepage(self):
        company = CompanyProfile(name="Example", website="https://example.com")
        pages = [
            CrawledPage(
                url="https://example.com",
                markdown=(
                    "[Our team](/about/team) "
                    "[Our team details](/about/team#people) "
                    "[Management directory](/files/management.pdf) "
                    "[Products](/products) "
                    "[External](https://other.example/contact)"
                ),
            )
        ]
        self.assertEqual(
            discover_official_links(company, pages),
            [
                "https://example.com/about/team",
                "https://example.com/files/management.pdf",
            ],
        )

    def test_pdf_extractor_returns_bounded_text_with_provenance(self):
        class Response(io.BytesIO):
            headers = {"Content-Length": "20", "Content-Type": "application/pdf"}

        class Page:
            def extract_text(self, extraction_mode):
                self.extraction_mode = extraction_mode
                return "Jane Doe - Purchasing Manager\njane@example.com"

        class Reader:
            is_encrypted = False
            pages = [Page()]

        with patch(
            "key_person_discovery.pdf_source._open_request",
            return_value=Response(b"%PDF-1.7 test bytes"),
        ) as request, patch(
            "key_person_discovery.pdf_source.PdfReader",
            return_value=Reader(),
        ):
            pages = extract_pdf_urls(
                ["https://example.com/management.pdf"],
                proxy_url="http://127.0.0.1:12001",
            )
        self.assertIn("Jane Doe", pages[0].markdown)
        self.assertEqual(pages[0].source_type, "pdf_extract")
        self.assertEqual(pages[0].provider, "pypdf")
        self.assertEqual(pages[0].page_count, 1)
        self.assertEqual(request.call_args.args[2], "http://127.0.0.1:12001")

    def test_pdf_extractor_rejects_non_pdf_without_stopping_batch(self):
        class Response(io.BytesIO):
            headers = {"Content-Type": "text/html"}

        with patch(
            "key_person_discovery.pdf_source._open_request",
            return_value=Response(b"<html>not a pdf</html>"),
        ):
            pages = extract_pdf_urls(["https://example.com/fake.pdf"], proxy_url="proxy")
        self.assertEqual(pages[0].markdown, "")
        self.assertIn("not a PDF", pages[0].error)

    def test_pipeline_routes_pdf_into_evidence_and_artifacts(self):
        class SearchStub:
            name = "search"

            def search(self, query):
                return SearchOutcome(
                    results=[
                        SearchResult(
                            query=query,
                            title="Example management directory",
                            url="https://example.com/management.pdf",
                            snippet="Example purchasing team",
                            provider=self.name,
                        )
                    ]
                )

        class HermesStub:
            evidence_pages = []
            signals = []

            def extract(self, company, pages, signals, usage_path):
                self.evidence_pages = pages
                self.signals = signals
                return {"company_name": company.name, "candidates": [], "review_required": True}

        hermes = HermesStub()
        pdf_page = CrawledPage(
            url="https://example.com/management.pdf",
            markdown="Jane Doe - Purchasing Manager - jane@example.com",
            provider="pypdf",
            source_type="pdf_extract",
            page_count=1,
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[],
        ), patch(
            "key_person_discovery.pipeline.extract_pdf_urls",
            return_value=[pdf_page],
        ):
            artifacts = Path(directory) / "artifacts"
            result = discover(
                company=CompanyProfile(name="Example", website="https://example.com"),
                search_clients=[SearchStub()],
                crawler=object(),
                hermes=hermes,
                artifacts_dir=artifacts,
                official_site_discovery=lambda *_: ([], []),
                proxy_url="http://127.0.0.1:12001",
            )
            pdf_artifact = json.loads((artifacts / "pdf-pages.json").read_text())
        self.assertIn(pdf_page, hermes.evidence_pages)
        self.assertIn("jane@example.com", {item["value"] for item in hermes.signals})
        self.assertEqual(result["run_summary"]["pdf_documents_extracted"], 1)
        self.assertEqual(pdf_artifact[0]["source_type"], "pdf_extract")

    def test_contact_sources_prioritize_official_contact_pages_and_documents(self):
        company = CompanyProfile(name="Example", website="https://example.com")
        urls = [
            "https://example.com/news/latest",
            "https://example.com/files/csr.pdf",
            "https://example.com/files/paia-manual.pdf",
            "https://example.com/contact",
            "https://example.com",
        ]

        self.assertEqual(
            sorted(urls, key=lambda url: _contact_source_priority(company, url)),
            [
                "https://example.com",
                "https://example.com/files/paia-manual.pdf",
                "https://example.com/contact",
                "https://example.com/news/latest",
                "https://example.com/files/csr.pdf",
            ],
        )

    def test_searxng_reports_partial_engine_failure(self):
        payload = {
            "results": [
                {
                    "title": "Jane Doe",
                    "url": "https://example.com/jane",
                    "content": "Purchasing Manager",
                }
            ],
            "unresponsive_engines": [["duckduckgo", "timeout"]],
        }
        with patch(
            "key_person_discovery.sources.urlopen",
            return_value=io.BytesIO(json.dumps(payload).encode()),
        ):
            outcome = SearxngClient("http://127.0.0.1:8080").search("Jane Doe")
        self.assertEqual(outcome.unresponsive_engines, [("duckduckgo", "timeout")])

    def test_searxng_rejects_all_engines_failed(self):
        search_payload = {
            "results": [],
            "unresponsive_engines": [["bing", "timeout"], ["duckduckgo", "CAPTCHA"]],
        }
        config_payload = {
            "engines": [
                {"name": "bing", "enabled": True},
                {"name": "duckduckgo", "enabled": True},
            ]
        }
        with patch(
            "key_person_discovery.sources.urlopen",
            side_effect=[
                io.BytesIO(json.dumps(search_payload).encode()),
                io.BytesIO(json.dumps(config_payload).encode()),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "All active SearXNG engines failed"):
                SearxngClient("http://127.0.0.1:8080").search("Jane Doe")

    def test_local_searxng_proxy_matches_host_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.yml"
            settings.write_text(
                "outgoing:\n"
                "  proxies:\n"
                "    all://:\n"
                "      - http://host.docker.internal:12001\n",
                encoding="utf-8",
            )
            _validate_local_searxng_proxy("http://127.0.0.1:12001", settings)

    def test_egress_fingerprint_never_exposes_ip(self):
        fingerprint = _trace_fingerprint("fl=1\nip=203.0.113.7\nloc=US\n")
        self.assertEqual(len(fingerprint), 12)
        self.assertNotIn("203.0.113.7", fingerprint)

    def test_egress_checks_use_https_to_preserve_ip_response(self):
        self.assertTrue(all(url.startswith("https://") for url in EGRESS_CHECK_URLS))

    def test_pipeline_persists_partial_engine_warnings(self):
        class SearchStub:
            def search(self, query):
                return SearchOutcome(
                    results=[
                        SearchResult(
                            query=query,
                            title="Jane Doe",
                            url="https://example.com/jane",
                            snippet="Purchasing Manager",
                        )
                    ],
                    unresponsive_engines=[("duckduckgo", "timeout")],
                )

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                return {
                    "company_name": company.name,
                    "candidates": [],
                    "review_required": True,
                }

        company = CompanyProfile(name="Example Minerals")
        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[],
        ):
            artifacts = Path(directory) / "artifacts"
            result = discover(
                company=company,
                search_clients=[SearchStub()],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=artifacts,
                official_site_discovery=lambda *_: ([], []),
            )
            warnings = json.loads((artifacts / "search-warnings.json").read_text())
        self.assertEqual(result["run_summary"]["search_engine_warnings"], 5)
        self.assertEqual(warnings[0]["engine"], "duckduckgo")

    def test_pipeline_falls_back_when_primary_search_fails(self):
        class FailingSearch:
            name = "anysearch"

            def search(self, query):
                raise RuntimeError("temporary outage")

        class FallbackSearch:
            name = "searxng"

            def search(self, query):
                return SearchOutcome(
                    results=[
                        SearchResult(
                            query=query,
                            title="Example Minerals",
                            url="https://example.com/team",
                            snippet="Purchasing Manager",
                            provider=self.name,
                        )
                    ]
                )

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                return {"company_name": company.name, "candidates": [], "review_required": True}

        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[],
        ):
            artifacts = Path(directory) / "artifacts"
            result = discover(
                company=CompanyProfile(name="Example Minerals"),
                search_clients=[FailingSearch(), FallbackSearch()],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=artifacts,
                official_site_discovery=lambda *_: ([], []),
            )
            hits = json.loads((artifacts / "search-results.json").read_text())
        self.assertEqual(result["run_summary"]["search_engine_warnings"], 7)
        self.assertTrue(all(item["provider"] == "searxng" for item in hits))

    def test_pipeline_limits_anysearch_queries_and_uses_searxng_after_budget(self):
        class SearchStub:
            def __init__(self, name):
                self.name = name
                self.calls = []

            def search(self, query):
                self.calls.append(query)
                return SearchOutcome(
                    results=[
                        SearchResult(
                            query=query,
                            title="Example Minerals",
                            url="https://example.com/team",
                            snippet="Purchasing Manager",
                            provider=self.name,
                        )
                    ]
                )

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                return {"company_name": company.name, "candidates": [], "review_required": True}

        anysearch = SearchStub("anysearch")
        searxng = SearchStub("searxng")
        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[],
        ):
            result = discover(
                company=CompanyProfile(name="Example Minerals"),
                search_clients=[anysearch, searxng],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=Path(directory) / "artifacts",
                official_site_discovery=lambda *_: ([], []),
                anysearch_query_limit=2,
            )

        self.assertEqual(len(anysearch.calls), 2)
        self.assertEqual(len(searxng.calls), 5)
        self.assertEqual(result["run_summary"]["anysearch_queries"], 2)

    def test_default_anysearch_budget_stops_at_five_or_extends_to_seven(self):
        class SearchStub:
            name = "anysearch"

            def __init__(self, with_lead):
                self.with_lead = with_lead
                self.calls = []

            def search(self, query):
                self.calls.append(query)
                if self.with_lead and "site:linkedin.com/in" in query:
                    return SearchOutcome(
                        results=[
                            SearchResult(
                                query=query,
                                title="Jane Doe at Example Minerals",
                                url="https://www.linkedin.com/in/jane-doe",
                                snippet="Currently Purchasing Manager at Example Minerals",
                                provider=self.name,
                            ),
                        ]
                    )
                return SearchOutcome(results=[])

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                return {"company_name": company.name, "candidates": [], "review_required": True}

        for with_lead, expected_calls in ((False, 5), (True, 7)):
            with self.subTest(with_lead=with_lead), tempfile.TemporaryDirectory() as directory, patch(
                "key_person_discovery.pipeline.crawl_sync",
                return_value=[],
            ):
                search = SearchStub(with_lead)
                result = discover(
                    company=CompanyProfile(
                        name="Example Minerals",
                        website="https://example.com",
                        target_contact_count=4,
                    ),
                    search_clients=[search],
                    crawler=object(),
                    hermes=HermesStub(),
                    artifacts_dir=Path(directory) / "artifacts",
                    official_site_discovery=lambda *_: ([], []),
                    anysearch_query_limit=7,
                )

            self.assertEqual(len(search.calls), expected_calls)
            self.assertEqual(result["run_summary"]["anysearch_queries"], expected_calls)
            self.assertEqual(result["run_summary"]["anysearch_extended"], with_lead)

    def test_default_anysearch_budget_does_not_extend_for_generic_company_urls(self):
        class SearchStub:
            name = "anysearch"

            def __init__(self):
                self.calls = []

            def search(self, query):
                self.calls.append(query)
                return SearchOutcome(
                    results=[
                        SearchResult(
                            query=query,
                            title="Example Minerals supplier profile",
                            url="https://directory.test/example-minerals",
                            snippet="Company directory entry for Example Minerals",
                            provider=self.name,
                        )
                    ]
                )

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                return {"company_name": company.name, "candidates": [], "review_required": True}

        search = SearchStub()
        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[],
        ):
            result = discover(
                company=CompanyProfile(
                    name="Example Minerals",
                    website="https://example.com",
                    target_contact_count=4,
                ),
                search_clients=[search],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=Path(directory) / "artifacts",
                official_site_discovery=lambda *_: ([], []),
                anysearch_query_limit=7,
            )

        self.assertEqual(len(search.calls), 5)
        self.assertEqual(result["run_summary"]["anysearch_queries"], 5)
        self.assertFalse(result["run_summary"]["anysearch_extended"])

    def test_pipeline_customs_budget_is_separate_and_requires_a_lead_to_continue(self):
        class AnySearchStub:
            name = "anysearch"

            def __init__(self):
                self.calls = []

            def search(self, query):
                self.calls.append(query)
                return SearchOutcome(
                    results=[
                        SearchResult(
                            query=query,
                            title="Example Minerals silicon carbide import shipments",
                            url="https://www.volza.com/company-profile/example-minerals/",
                            snippet="Example Minerals imported Silicon Carbide shipments",
                            provider=self.name,
                        )
                    ]
                )

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                return {"company_name": company.name, "candidates": [], "review_required": True}

        anysearch = AnySearchStub()
        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[],
        ):
            result = discover(
                company=CompanyProfile(
                    name="Example Minerals",
                    products=["Silicon Carbide"],
                    customs_search_enabled=True,
                ),
                search_clients=[anysearch],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=Path(directory) / "artifacts",
                official_site_discovery=lambda *_: ([], []),
                anysearch_query_limit=0,
                customs_anysearch_query_limit=2,
            )

        self.assertEqual(len(anysearch.calls), 2)
        self.assertEqual(result["run_summary"]["anysearch_queries"], 0)
        self.assertEqual(result["run_summary"]["customs_anysearch_queries"], 2)
        self.assertEqual(result["customs"]["tags"], ["海关采购证据"])
        self.assertEqual(result["customs"]["leads"][0]["matched_products"], ["Silicon Carbide"])

    def test_pipeline_customs_search_stops_after_first_query_without_a_lead(self):
        class AnySearchStub:
            name = "anysearch"

            def __init__(self):
                self.calls = []

            def search(self, query):
                self.calls.append(query)
                return SearchOutcome(
                    results=[
                        SearchResult(
                            query=query,
                            title="Unrelated company",
                            url="https://example.net/",
                            snippet="No relevant trade record",
                            provider=self.name,
                        )
                    ]
                )

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                return {"company_name": company.name, "candidates": [], "review_required": True}

        anysearch = AnySearchStub()
        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[],
        ):
            result = discover(
                company=CompanyProfile(
                    name="Example Minerals",
                    customs_search_enabled=True,
                ),
                search_clients=[anysearch],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=Path(directory) / "artifacts",
                official_site_discovery=lambda *_: ([], []),
                anysearch_query_limit=0,
                customs_anysearch_query_limit=2,
            )

        self.assertEqual(len(anysearch.calls), 1)
        self.assertEqual(result["run_summary"]["customs_anysearch_queries"], 1)
        self.assertEqual(result["customs"], {"status": "no_public_lead", "tags": [], "leads": []})

    def test_pipeline_skips_customs_search_when_company_is_not_selected(self):
        class AnySearchStub:
            name = "anysearch"

            def __init__(self):
                self.calls = []

            def search(self, query):
                self.calls.append(query)
                return SearchOutcome(results=[])

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                return {"company_name": company.name, "candidates": [], "review_required": True}

        anysearch = AnySearchStub()
        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[],
        ):
            result = discover(
                company=CompanyProfile(name="Example Minerals"),
                search_clients=[anysearch],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=Path(directory) / "artifacts",
                official_site_discovery=lambda *_: ([], []),
                anysearch_query_limit=0,
                customs_anysearch_query_limit=2,
            )

        self.assertEqual(anysearch.calls, [])
        self.assertEqual(result["customs"]["status"], "not_selected")
        self.assertEqual(result["run_summary"]["customs_anysearch_queries"], 0)

    def test_relevant_urls_require_company_or_official_domain_match(self):
        company = CompanyProfile(
            name="Harbor Castings Inc.",
            website="https://www.harborinvestmentcastings.com",
        )
        results = [
            SearchResult(
                query="q",
                title="About Harbor Castings",
                url="https://www.harborinvestmentcastings.com/about",
                snippet="Investment casting foundry",
            ),
            SearchResult(
                query="q",
                title="Harbor Castings Inc. company profile",
                url="https://www.zoominfo.com/c/harbor-castings-inc/122700868",
                snippet="Harbor Castings Inc.",
            ),
            SearchResult(
                query="q",
                title="Harbor Freight Tools",
                url="https://www.harborfreight.com/",
                snippet="Tools and equipment",
            ),
            SearchResult(
                query="q",
                title="Harbor container registry",
                url="https://goharbor.io/",
                snippet="Cloud native registry",
            ),
            SearchResult(
                query="q",
                title="Jane Doe",
                url="https://www.linkedin.com/in/jane-doe",
                snippet="Purchasing Manager at Harbor Castings Inc.",
            ),
            SearchResult(
                query="q",
                title="Harbor Castings Inc.",
                url="https://random-example.invalid/page",
                snippet="Harbor Castings Inc. purchasing team",
            ),
        ]
        urls = _relevant_urls(company, results)
        self.assertIn("https://www.harborinvestmentcastings.com/about", urls)
        self.assertIn("https://www.zoominfo.com/c/harbor-castings-inc/122700868", urls)
        self.assertIn("https://www.linkedin.com/in/jane-doe", urls)
        self.assertNotIn("https://www.harborfreight.com/", urls)
        self.assertNotIn("https://goharbor.io/", urls)
        self.assertNotIn("https://random-example.invalid/page", urls)

    def test_generic_wrong_crm_website_does_not_admit_same_host_pages(self):
        company = CompanyProfile(name="POSCO Future M", website="https://refwin.com")
        results = [
            SearchResult(
                query="q",
                title="Refractory industry directory",
                url="https://refwin.com/company/contact-123",
                snippet="Global refractory supplier directory",
            ),
            SearchResult(
                query="q",
                title="POSCO Future M contact",
                url="https://www.poscofuturem.com/posco-future-m/contact",
                snippet="POSCO Future M official contact information",
            ),
        ]

        self.assertEqual(
            _relevant_urls(company, results),
            [company.website, "https://www.poscofuturem.com/posco-future-m/contact"],
        )

    def test_single_token_same_name_site_is_not_relevant(self):
        company = CompanyProfile(name="Heatmasters", website="https://heatmasters.net")
        result = SearchResult(
            query='"Heatmasters" official website',
            title="Heatmasters Mechanical",
            url="https://www.heatmastersmechanical.com/",
            snippet="Heatmasters Mechanical HVAC contractor in Chicago",
        )

        self.assertEqual(_relevant_urls(company, [result]), [company.website])

    def test_directory_sibling_and_exact_company_page_are_relevant(self):
        company = CompanyProfile(
            name="JSJ Jodeit GmbH",
            website="https://apps.glassglobal.com/profile/documents3274.html",
        )
        results = [
            SearchResult(
                query="JSJ Jodeit contact",
                title="Components for glass melting plants",
                url="https://fr.glassglobal.com/directory/profile.asp?id=3274",
                snippet="JSJ Jodeit GmbH. Tel: +49-3641-622920 Email: jodeit@JSJ.de",
            ),
            SearchResult(
                query="JSJ Jodeit contact",
                title="JSJ Speciality Glass",
                url="https://www.hornglass.com/jsjspecialityglass",
                snippet="JSJ Jodeit GmbH has been a HORN subsidiary since 2021.",
            ),
            SearchResult(
                query="JSJ Jodeit contact",
                title="Unrelated glass article",
                url="https://news.example/article",
                snippet="HORN takes over JSJ Jodeit GmbH.",
            ),
        ]
        self.assertEqual(
            _relevant_urls(company, results),
            [
                company.website,
                "https://fr.glassglobal.com/directory/profile.asp?id=3274",
                "https://www.hornglass.com/jsjspecialityglass",
            ],
        )

    def test_exact_company_contact_excerpt_becomes_unassigned_contact(self):
        class SearchStub:
            name = "search"

            def search(self, query):
                return SearchOutcome(
                    results=[
                        SearchResult(
                            query=query,
                            title="Components for glass melting plants",
                            url="https://fr.glassglobal.com/directory/profile.asp?id=3274",
                            snippet=(
                                "JSJ Jodeit GmbH. Tel: +49-3641-622920 "
                                "Email: jodeit@JSJ.de"
                            ),
                            provider=self.name,
                        )
                    ]
                )

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                value = {
                    "company_name": company.name,
                    "candidates": [],
                    "unassigned_contacts": [],
                    "review_required": True,
                }
                _validate_output(
                    value,
                    company,
                    signals,
                    source_urls={page.url for page in pages if page.markdown},
                )
                return value

        company = CompanyProfile(
            name="JSJ Jodeit GmbH",
            website="https://apps.glassglobal.com/profile/documents3274.html",
            crm_contact_count=8,
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[
                CrawledPage(
                    url=company.website,
                    markdown="",
                    error="connection closed",
                )
            ],
        ):
            result = discover(
                company=company,
                search_clients=[SearchStub()],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=Path(directory),
                phone_region="DE",
                official_site_discovery=lambda *_: ([company.website], []),
            )
        contacts = {
            (item["channel"], item["value"])
            for item in result["unassigned_contacts"]
        }
        self.assertIn(("email", "jodeit@jsj.de"), contacts)
        self.assertIn(("phone", "+493641622920"), contacts)

    def test_exact_company_crawled_page_marks_public_contacts(self):
        class SearchStub:
            name = "search"

            def search(self, query):
                return SearchOutcome(
                    results=[
                        SearchResult(
                            query=query,
                            title="JSJ Speciality Glass",
                            url="https://www.hornglass.com/jsjspecialityglass",
                            snippet="JSJ Jodeit GmbH has been a HORN subsidiary since 2021.",
                            provider=self.name,
                        )
                    ]
                )

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                value = {
                    "company_name": company.name,
                    "candidates": [],
                    "unassigned_contacts": [],
                    "review_required": True,
                }
                _validate_output(
                    value,
                    company,
                    signals,
                    source_urls={page.url for page in pages if page.markdown},
                )
                return value

        company = CompanyProfile(
            name="JSJ Jodeit GmbH",
            website="https://apps.glassglobal.com/profile/documents3274.html",
        )
        horn_page = CrawledPage(
            url="https://www.hornglass.com/jsjspecialityglass",
            markdown="JSJ Jodeit GmbH\nTelephone +49 9636 9204861",
            provider="crawl4ai",
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[horn_page],
        ):
            result = discover(
                company=company,
                search_clients=[SearchStub()],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=Path(directory),
                phone_region="DE",
                official_site_discovery=lambda *_: ([company.website], []),
            )
        self.assertIn(
            ("phone", "+4996369204861"),
            {(item["channel"], item["value"]) for item in result["unassigned_contacts"]},
        )

    def test_verified_company_domain_marks_public_contacts_without_stale_name_prefix(self):
        from key_person_discovery.pipeline import _page_is_company_public

        company = CompanyProfile(
            name="Batts Kilns & Furnaces Ltd",
            website="https://kilns.co.uk",
        )
        page = CrawledPage(
            url="https://www.kilns.co.uk/contact-us",
            markdown="Kilns & Furnaces Ltd\nsales@kilns.co.uk\n01782 344270",
        )

        self.assertTrue(_page_is_company_public(company, page))

    def test_unrelated_same_name_domain_is_not_a_company_public_page(self):
        from key_person_discovery.pipeline import _page_is_company_public

        company = CompanyProfile(
            name="ECOACERO (Grupo Estrella)",
            website="https://aceroestrella.com.do",
        )
        page = CrawledPage(
            url="https://ecoacero.com/contacto/",
            markdown=(
                "ECOACERO Asociación Ecológica para el Reciclado de la Hojalata\n"
                "info@ecoacero.com\n+34 822 040 656"
            ),
        )

        self.assertFalse(_page_is_company_public(company, page))

    def test_third_party_directory_page_is_not_company_public(self):
        from key_person_discovery.pipeline import _page_is_company_public

        cases = [
            (
                CompanyProfile(
                    name="Ardakan Industrial Ceramics",
                    website="http://www.aic.ir",
                ),
                CrawledPage(
                    url=(
                        "https://www.alcircle.com/directory/bauxite/detail/60465/"
                        "ardakan-industrial-ceramics-solutions"
                    ),
                    markdown=(
                        "Ardakan Industrial Ceramics Solutions\n"
                        "WhatsApp +91 8910106540"
                    ),
                ),
            ),
            (
                CompanyProfile(
                    name="GARION INTERNATIONAL LTD",
                    website="https://c551701.tradekorea.com",
                ),
                CrawledPage(
                    url="https://c551701.tradekorea.com",
                    markdown="GARION INTERNATIONAL LTD supplier profile",
                ),
            ),
        ]

        for company, page in cases:
            with self.subTest(company=company.name):
                self.assertFalse(_page_is_company_public(company, page))

    def test_crm_linkedin_employee_links_become_review_candidates(self):
        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                value = {
                    "company_name": company.name,
                    "candidates": [],
                    "unassigned_contacts": [],
                    "review_required": True,
                }
                _validate_output(
                    value,
                    company,
                    signals,
                    source_urls={page.url for page in pages if page.markdown},
                )
                return value

        linkedin_url = "https://www.linkedin.com/company/refko-feuerfest-gmbh"
        company = CompanyProfile.from_dict(
            {
                "name": "REFKO Feuerfest GmbH",
                "crm_linkedin_url": linkedin_url,
            }
        )
        public_linkedin_url = "https://tt.linkedin.com/company/refko-feuerfest-gmbh"
        linkedin_page = CrawledPage(
            url=public_linkedin_url,
            markdown=(
                "# REFKO Feuerfest GmbH\n"
                "## Employees at REFKO Feuerfest GmbH\n"
                "[ Peter Weirich ](https://de.linkedin.com/in/peter-weirich-a81855231?trk=org-employees)\n"
                "## Updates\n"
                "[ Jinwook Kim ](https://kr.linkedin.com/in/jinwook-kim?trk=organization_guest_main-feed-card_feed-actor-name)"
            ),
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[linkedin_page],
        ) as crawl:
            result = discover(
                company=company,
                search_clients=[],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=Path(directory),
                official_site_discovery=lambda *_: ([], []),
            )

        self.assertIn(linkedin_url, crawl.call_args.args[1])
        self.assertIn(public_linkedin_url, crawl.call_args.args[1])
        self.assertEqual(
            [(item["full_name"], item["linkedin"][0]["value"]) for item in result["candidates"]],
            [("Peter Weirich", "https://www.linkedin.com/in/peter-weirich-a81855231")],
        )
        self.assertEqual(
            [item["value"] for item in result["unassigned_contacts"]],
            ["https://www.linkedin.com/in/jinwook-kim"],
        )

    def test_official_linkedin_signals_create_one_unverified_slug_candidate(self):
        company = CompanyProfile(name="Sialon", website="https://sialon.example")
        profile_url = "https://www.linkedin.com/in/nico-van-dongen-3a6466165"
        pages = [
            CrawledPage(
                url=f"https://sialon.example/team-{index}",
                markdown=f"Team link: {profile_url}",
            )
            for index in range(20)
        ]

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                return {
                    "company_name": company.name,
                    "candidates": [],
                    "unassigned_contacts": [
                        {
                            "channel": "linkedin",
                            "value": profile_url,
                            "source_url": pages[0].url,
                        }
                    ],
                    "review_required": True,
                }

        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=pages,
        ):
            result = discover(
                company=company,
                search_clients=[],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=Path(directory),
                official_site_discovery=lambda *_: ([page.url for page in pages], []),
            )

        self.assertEqual(len(result["unverified_candidates"]), 1)
        candidate = result["unverified_candidates"][0]
        self.assertEqual(candidate["full_name"], "Nico van Dongen")
        self.assertEqual(candidate["company_match"], "probable")
        self.assertEqual(candidate["current_title"], "")
        self.assertEqual(candidate["influence_type"], "other")
        self.assertEqual(candidate["influence_score"], 0)
        self.assertEqual(candidate["linkedin"][0]["value"], profile_url)
        self.assertEqual(candidate["linkedin"][0]["status"], "observed")
        self.assertEqual(candidate["confidence"], 0.3)
        self.assertTrue(candidate["review_required"])
        self.assertIn("current employment not verified", candidate["validation_reasons"])
        self.assertIn("当前任职未验证", candidate["evidence"][0]["supports"])
        self.assertEqual(result["unassigned_contacts"], [])

    def test_employee_page_candidate_wins_over_duplicate_official_linkedin_signal(self):
        company_page_url = "https://tt.linkedin.com/company/refko-feuerfest-gmbh"
        official_page_url = "https://refko.example/team"
        profile_url = "https://www.linkedin.com/in/peter-weirich-a81855231"
        company = CompanyProfile(
            name="REFKO Feuerfest GmbH",
            website="https://refko.example",
            linkedin_url="https://www.linkedin.com/company/refko-feuerfest-gmbh",
        )
        pages = [
            CrawledPage(
                url=company_page_url,
                markdown=(
                    "[ Peter Weirich ]("
                    "https://de.linkedin.com/in/peter-weirich-a81855231"
                    "?trk=org-employees)"
                ),
            ),
            CrawledPage(
                url=official_page_url,
                markdown=f"Technical team: {profile_url}",
            ),
        ]

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                return {
                    "company_name": company.name,
                    "candidates": [],
                    "unassigned_contacts": [],
                    "review_required": True,
                }

        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=pages,
        ):
            result = discover(
                company=company,
                search_clients=[],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=Path(directory),
                official_site_discovery=lambda *_: ([official_page_url], []),
            )

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["full_name"], "Peter Weirich")
        self.assertEqual(result["candidates"][0]["company_match"], "verified")
        self.assertEqual(
            result["candidates"][0]["linkedin"][0]["value"],
            profile_url,
        )
        self.assertNotIn("unverified_candidates", result)

    def test_default_budget_promotes_only_matching_linkedin_stable_id(self):
        cases = [
            {
                "company": "Sialon",
                "website": "https://sialon.example",
                "old_slug": "nico-van-dongen-3a6466165",
                "new_slug": "nico-v-3a6466165",
                "name": "Nico van Dongen",
                "title": "Nico V Dongen - Maintenance Manager at Sialon | LinkedIn",
                "snippet": "Current Maintenance Manager at Sialon",
                "promote": True,
            },
            {
                "company": "Sialon",
                "website": "https://sialon.example",
                "old_slug": "nico-van-dongen-3a6466165",
                "new_slug": "nico-v-58311966",
                "name": "Nico van Dongen",
                "title": "Nico V Dongen - Director at NG Consultancy | LinkedIn",
                "snippet": "Current Director at NG Consultancy",
                "promote": False,
            },
            {
                "company": "Heatmasters",
                "website": "https://heatmasters.example",
                "old_slug": "niklas-ant-12345",
                "new_slug": "niklas-ant-12345",
                "name": "Niklas Ant",
                "title": (
                    "Niklas Ant - Purchasing Manager at "
                    "Heatmasters Mechanical | LinkedIn"
                ),
                "snippet": (
                    "Current Purchasing Manager at Heatmasters Mechanical"
                ),
                "promote": False,
            },
        ]

        for case in cases:
            with self.subTest(company=case["company"]):
                old_url = (
                    "https://www.linkedin.com/in/" + case["old_slug"]
                )
                new_url = (
                    "https://www.linkedin.com/in/" + case["new_slug"]
                )
                official_page_url = f'{case["website"]}/team'
                company = CompanyProfile(
                    name=case["company"],
                    website=case["website"],
                )
                pages = [
                    CrawledPage(
                        url=official_page_url,
                        markdown=f"Team member: {old_url}",
                    )
                ]

                class SearchStub:
                    name = "anysearch"

                    def __init__(self):
                        self.calls = []

                    def search(self, query):
                        self.calls.append(query)
                        if case["name"] not in query:
                            return SearchOutcome(results=[])
                        return SearchOutcome(
                            results=[
                                SearchResult(
                                    query=query,
                                    title=case["title"],
                                    url=new_url,
                                    snippet=case["snippet"],
                                    provider=self.name,
                                )
                            ]
                        )

                class HermesStub:
                    def extract(self, company, pages, signals, usage_path):
                        return {
                            "company_name": company.name,
                            "candidates": [],
                            "unassigned_contacts": [],
                            "review_required": True,
                        }

                search = SearchStub()
                with tempfile.TemporaryDirectory() as directory, patch(
                    "key_person_discovery.pipeline.crawl_sync",
                    return_value=pages,
                ):
                    result = discover(
                        company=company,
                        search_clients=[search],
                        crawler=object(),
                        hermes=HermesStub(),
                        artifacts_dir=Path(directory),
                        official_site_discovery=lambda *_: (
                            [official_page_url],
                            [],
                        ),
                        anysearch_query_limit=7,
                    )

                self.assertEqual(len(search.calls), 7)
                self.assertEqual(result["run_summary"]["anysearch_queries"], 7)
                self.assertEqual(result["run_summary"]["queries"], 7)
                if case["promote"]:
                    self.assertEqual(len(result["candidates"]), 1)
                    self.assertEqual(
                        result["candidates"][0]["company_match"], "verified"
                    )
                    self.assertEqual(
                        result["candidates"][0]["linkedin"][0]["value"],
                        new_url,
                    )
                    self.assertNotIn("unverified_candidates", result)
                else:
                    self.assertEqual(result["candidates"], [])
                    self.assertEqual(
                        len(result["unverified_candidates"]), 1
                    )

    def test_default_budget_accepts_sialon_ceramics_aps_but_not_other_entity(self):
        cases = (
            ("Sialon Ceramics ApS", True),
            ("Sialon Ceramics Consulting", False),
            ("Sialon Ceramics Mechanical", False),
            ("Sialon Ceramics ApS Consulting", False),
        )
        old_url = (
            "https://www.linkedin.com/in/"
            "nico-van-dongen-3a6466165"
        )
        new_url = "https://www.linkedin.com/in/nico-v-3a6466165"

        for employer, promote in cases:
            with self.subTest(employer=employer):
                company = CompanyProfile(
                    name="Sialon Ceramics",
                    website="https://sialon-ceramics.example",
                )
                official_page_url = f"{company.website}/team"
                pages = [
                    CrawledPage(
                        url=official_page_url,
                        markdown=f"Team member: {old_url}",
                    )
                ]

                class SearchStub:
                    name = "anysearch"

                    def __init__(self):
                        self.calls = []

                    def search(self, query):
                        self.calls.append(query)
                        if "Nico van Dongen" not in query:
                            return SearchOutcome(results=[])
                        return SearchOutcome(
                            results=[
                                SearchResult(
                                    query=query,
                                    title=(
                                        "Nico V Dongen - Maintenance Manager at "
                                        f"{employer} | LinkedIn"
                                    ),
                                    url=new_url,
                                    snippet=(
                                        "Current Maintenance Manager at "
                                        f"{employer}"
                                    ),
                                    provider=self.name,
                                )
                            ]
                        )

                class HermesStub:
                    def extract(self, company, pages, signals, usage_path):
                        return {
                            "company_name": company.name,
                            "candidates": [],
                            "unassigned_contacts": [],
                            "review_required": True,
                        }

                search = SearchStub()
                with tempfile.TemporaryDirectory() as directory, patch(
                    "key_person_discovery.pipeline.crawl_sync",
                    return_value=pages,
                ):
                    result = discover(
                        company=company,
                        search_clients=[search],
                        crawler=object(),
                        hermes=HermesStub(),
                        artifacts_dir=Path(directory),
                        official_site_discovery=lambda *_: (
                            [official_page_url],
                            [],
                        ),
                        anysearch_query_limit=7,
                    )

                self.assertEqual(len(search.calls), 7)
                self.assertEqual(result["run_summary"]["anysearch_queries"], 7)
                if promote:
                    self.assertEqual(len(result["candidates"]), 1)
                    self.assertEqual(
                        result["candidates"][0]["company_match"],
                        "verified",
                    )
                    self.assertNotIn("unverified_candidates", result)
                else:
                    self.assertEqual(result["candidates"], [])
                    self.assertEqual(
                        len(result["unverified_candidates"]),
                        1,
                    )

    def test_linkedin_promotion_fills_missing_title_without_overwriting_real_title(self):
        from key_person_discovery.pipeline import _promote_linkedin_signal_candidates

        company = CompanyProfile(
            name="Sialon Ceramics",
            website="https://sialon-ceramics.example",
        )
        seed_url = (
            "https://www.linkedin.com/in/"
            "nico-van-dongen-3a6466165"
        )
        matched_url = "https://www.linkedin.com/in/nico-v-3a6466165"
        matched_result = SearchResult(
            query='"Nico van Dongen" "Sialon Ceramics" current',
            title="Nico V.",
            url=matched_url,
            snippet=(
                "Sales & Product Development ... at Sialon Ceramics ApS "
                "(est. 1986)"
            ),
            provider="anysearch",
        )

        for initial_title in (
            "",
            "Unknown (LinkedIn profile associated with company)",
            "Existing Director",
        ):
            with self.subTest(initial_title=initial_title):
                result = {
                    "candidates": [],
                    "unverified_candidates": [
                        {
                            "full_name": "Nico van Dongen",
                            "current_title": initial_title,
                            "company_match": "probable",
                            "influence_type": "other",
                            "influence_score": 0,
                            "linkedin": [
                                {
                                    "value": seed_url,
                                    "status": "observed",
                                    "source_url": f"{company.website}/team",
                                }
                            ],
                            "emails": [],
                            "phones": [],
                            "evidence": [
                                {
                                    "source_url": f"{company.website}/team",
                                    "quote": f"Official website links to {seed_url}",
                                    "supports": (
                                        "official website links to this LinkedIn "
                                        "profile; current employment not verified"
                                    ),
                                }
                            ],
                            "confidence": 0.3,
                            "review_required": True,
                            "validation_reasons": [
                                "current employment not verified"
                            ],
                        }
                    ],
                }

                _promote_linkedin_signal_candidates(
                    result,
                    company,
                    [matched_result],
                )

                candidate = result["candidates"][0]
                if initial_title.casefold().startswith("unknown") or not initial_title:
                    self.assertIn("Sales & Product Development", candidate["current_title"])
                    self.assertNotEqual(
                        candidate["current_title"].casefold(),
                        "unknown (linkedin profile associated with company)",
                    )
                else:
                    self.assertEqual(candidate["current_title"], initial_title)

    def test_default_budget_promotes_real_sialon_headline_only_for_exact_entity(self):
        cases = (
            ("Sialon Ceramics ApS", True),
            ("Sialon Ceramics Consulting", False),
            ("Sialon Ceramics Mechanical", False),
        )
        old_url = (
            "https://www.linkedin.com/in/"
            "nico-van-dongen-3a6466165"
        )
        new_url = "https://www.linkedin.com/in/nico-v-3a6466165"

        for employer, promote in cases:
            with self.subTest(employer=employer):
                company = CompanyProfile(
                    name="Sialon Ceramics",
                    website="https://sialon-ceramics.example",
                )
                official_page_url = f"{company.website}/team"
                pages = [
                    CrawledPage(
                        url=official_page_url,
                        markdown=f"Team member: {old_url}",
                    )
                ]

                class SearchStub:
                    name = "anysearch"

                    def __init__(self):
                        self.calls = []

                    def search(self, query):
                        self.calls.append(query)
                        if "Nico van Dongen" not in query:
                            return SearchOutcome(results=[])
                        return SearchOutcome(
                            results=[
                                SearchResult(
                                    query=query,
                                    title="Nico V.",
                                    url=new_url,
                                    snippet=(
                                        "# Nico V. Ultrasonic ... at "
                                        f"{employer} (est. 1986) ..."
                                    ),
                                    provider=self.name,
                                )
                            ]
                        )

                class HermesStub:
                    def extract(self, company, pages, signals, usage_path):
                        return {
                            "company_name": company.name,
                            "candidates": [],
                            "unassigned_contacts": [],
                            "review_required": True,
                        }

                search = SearchStub()
                with tempfile.TemporaryDirectory() as directory, patch(
                    "key_person_discovery.pipeline.crawl_sync",
                    return_value=pages,
                ):
                    result = discover(
                        company=company,
                        search_clients=[search],
                        crawler=object(),
                        hermes=HermesStub(),
                        artifacts_dir=Path(directory),
                        official_site_discovery=lambda *_: (
                            [official_page_url],
                            [],
                        ),
                        anysearch_query_limit=7,
                    )

                self.assertEqual(len(search.calls), 7)
                if promote:
                    self.assertEqual(len(result["candidates"]), 1)
                    self.assertEqual(
                        result["candidates"][0]["company_match"],
                        "verified",
                    )
                    self.assertNotIn("unverified_candidates", result)
                else:
                    self.assertEqual(result["candidates"], [])
                    self.assertEqual(
                        len(result["unverified_candidates"]),
                        1,
                    )

    def test_employee_linkedin_seed_uses_candidate_query_external_role_evidence(self):
        cases = (
            ("Sialon Ceramics ApS", True),
            ("Sialon Ceramics Consulting", False),
            ("Sialon Ceramics Mechanical", False),
        )
        company_linkedin_url = (
            "https://www.linkedin.com/company/sialon-ceramics"
        )
        seed_url = (
            "https://www.linkedin.com/in/"
            "nico-van-dongen-3a6466165"
        )
        wrong_linkedin_url = (
            "https://www.linkedin.com/in/nico-v-58311966"
        )
        external_url = (
            "https://en.syna.se/companypublic/5593796419/"
            "sialon-ceramics-sweden-ab"
        )

        for employer, validates in cases:
            with self.subTest(employer=employer):
                company = CompanyProfile(
                    name="Sialon Ceramics",
                    website="https://sialon-ceramics.example",
                    linkedin_url=company_linkedin_url,
                )
                official_page_url = f"{company.website}/team"
                pages = [
                    CrawledPage(
                        url=company_linkedin_url,
                        markdown=(
                            "[ Nico van Dongen ]("
                            f"{seed_url}?trk=org-employees)"
                        ),
                    ),
                    CrawledPage(
                        url=official_page_url,
                        markdown=f"Contact person: {seed_url}",
                    ),
                ]

                class SearchStub:
                    name = "anysearch"

                    def __init__(self):
                        self.calls = []

                    def search(self, query):
                        self.calls.append(query)
                        if "Nico van Dongen" not in query:
                            return SearchOutcome(results=[])
                        results = [
                            SearchResult(
                                query=query,
                                title=(
                                    "Nico van Dongen - Director at "
                                    "NG Consultancy | LinkedIn"
                                ),
                                url=wrong_linkedin_url,
                                snippet="Current Director at NG Consultancy",
                                provider=self.name,
                            )
                        ]
                        results.append(
                            SearchResult(
                                query=query,
                                title=(
                                    "Nico van Dongen - Board Member at "
                                    f"{employer}"
                                ),
                                url=external_url,
                                snippet=(
                                    "Current board member and contact "
                                    f"person at {employer}"
                                ),
                                provider=self.name,
                            )
                        )
                        return SearchOutcome(results=results)

                class HermesStub:
                    def extract(self, company, pages, signals, usage_path):
                        return {
                            "company_name": company.name,
                            "candidates": [],
                            "unassigned_contacts": [],
                            "review_required": True,
                        }

                search = SearchStub()
                with tempfile.TemporaryDirectory() as directory, patch(
                    "key_person_discovery.pipeline.crawl_sync",
                    return_value=pages,
                ):
                    result = discover(
                        company=company,
                        search_clients=[search],
                        crawler=object(),
                        hermes=HermesStub(),
                        artifacts_dir=Path(directory),
                        official_site_discovery=lambda *_: (
                            [official_page_url],
                            [],
                        ),
                        anysearch_query_limit=7,
                    )

                self.assertEqual(len(result["candidates"]), 1)
                candidate = result["candidates"][0]
                self.assertEqual(
                    candidate["linkedin"][0]["value"],
                    seed_url,
                )
                evidence_sources = {
                    evidence["source_url"]
                    for evidence in candidate["evidence"]
                }
                self.assertNotIn(wrong_linkedin_url, {
                    contact["value"]
                    for contact in candidate["linkedin"]
                })
                if validates:
                    self.assertIn(external_url, evidence_sources)
                    self.assertTrue(
                        any(
                            "current role" in evidence["supports"]
                            for evidence in candidate["evidence"]
                            if evidence["source_url"] == external_url
                        )
                    )
                else:
                    self.assertNotIn(external_url, evidence_sources)

    def test_live_shape_official_linkedin_anchor_accepts_glassonline_role_evidence(self):
        cases = (
            (
                "Nico Van Dongen, Director of Sialon Ceramics, explained...",
                True,
                "Unknown (LinkedIn profile associated with company)",
            ),
            (
                "Nico Van Dongen, Director of Sialon Ceramics, explained...",
                True,
                "Existing Director",
            ),
            (
                "Nico Van Dongen, Director of Sialon Ceramics Consulting, explained...",
                False,
                "Unknown (LinkedIn profile associated with company)",
            ),
            (
                "Nico Van Dongen, Director of Sialon Ceramics Mechanical, explained...",
                False,
                "Unknown (LinkedIn profile associated with company)",
            ),
            (
                "Nico Van Dongen, former Director of Sialon Ceramics, explained...",
                False,
                "Unknown (LinkedIn profile associated with company)",
            ),
        )
        seed_url = (
            "https://www.linkedin.com/in/"
            "nico-van-dongen-3a6466165"
        )
        wrong_linkedin_url = "https://www.linkedin.com/in/nico-v-58311966"
        glassonline_url = (
            "https://www.glassonline.com/sialon-ceramics-ultrasonic"
        )
        ultracapacitor_url = "https://ultracapacitor.info/about-us"

        for snippet, validates, initial_title in cases:
            with self.subTest(snippet=snippet):
                company = CompanyProfile(
                    name="Sialon Ceramics",
                    website="https://sialon.com",
                )
                official_page_url = company.website
                pages = [
                    CrawledPage(
                        url=official_page_url,
                        markdown=f"Social link: {seed_url}",
                    )
                ]

                class SearchStub:
                    name = "anysearch"

                    def __init__(self):
                        self.calls = []

                    def search(self, query):
                        self.calls.append(query)
                        if "Nico van Dongen" not in query:
                            return SearchOutcome(results=[])
                        return SearchOutcome(
                            results=[
                                SearchResult(
                                    query=query,
                                    title=(
                                        "Nico van Dongen - Director at NG "
                                        "Consultancy | LinkedIn"
                                    ),
                                    url=wrong_linkedin_url,
                                    snippet="Current Director at NG Consultancy",
                                    provider=self.name,
                                ),
                                SearchResult(
                                    query=query,
                                    title="GlassOnline - Sialon Ceramics ultrasonic",
                                    url=glassonline_url,
                                    snippet=snippet,
                                    provider=self.name,
                                ),
                                SearchResult(
                                    query=query,
                                    title="Ultracapacitor company profile",
                                    url=ultracapacitor_url,
                                    snippet=(
                                        "Nico van Dongen, owner of Sialon "
                                        "Ceramics Ltd."
                                    ),
                                    provider=self.name,
                                ),
                            ]
                        )

                class HermesStub:
                    def extract(self, company, pages, signals, usage_path):
                        return {
                            "company_name": company.name,
                            "candidates": [
                                {
                                    "full_name": "Nico van Dongen",
                                    "current_title": initial_title,
                                    "company_match": "verified",
                                    "influence_type": "other",
                                    "influence_score": 0,
                                    "linkedin": [
                                        {
                                            "value": seed_url,
                                            "status": "observed",
                                            "source_url": official_page_url,
                                        }
                                    ],
                                    "emails": [],
                                    "phones": [],
                                    "evidence": [
                                        {
                                            "source_url": "https://sialon.com",
                                            "quote": seed_url,
                                            "supports": "Social link on company homepage",
                                        }
                                    ],
                                    "confidence": 0.45,
                                    "review_required": True,
                                }
                            ],
                            "unassigned_contacts": [],
                            "review_required": True,
                        }

                search = SearchStub()
                with tempfile.TemporaryDirectory() as directory, patch(
                    "key_person_discovery.pipeline.crawl_sync",
                    return_value=pages,
                ):
                    result = discover(
                        company=company,
                        search_clients=[search],
                        crawler=object(),
                        hermes=HermesStub(),
                        artifacts_dir=Path(directory),
                        official_site_discovery=lambda *_: (
                            [official_page_url],
                            [],
                        ),
                        anysearch_query_limit=7,
                    )

                candidate = result["candidates"][0]
                self.assertEqual(candidate["linkedin"][0]["value"], seed_url)
                evidence_sources = {
                    evidence["source_url"]
                    for evidence in candidate["evidence"]
                }
                self.assertNotIn(
                    wrong_linkedin_url,
                    {contact["value"] for contact in candidate["linkedin"]},
                )
                self.assertNotIn(ultracapacitor_url, evidence_sources)
                if validates:
                    self.assertIn(glassonline_url, evidence_sources)
                    self.assertTrue(candidate["current_title"])
                    self.assertIn("Director", candidate["current_title"])
                    if initial_title.casefold().startswith("unknown"):
                        self.assertEqual(candidate["current_title"], snippet)
                    else:
                        self.assertEqual(candidate["current_title"], initial_title)
                    self.assertTrue(
                        any(
                            "current role" in evidence["supports"]
                            for evidence in candidate["evidence"]
                            if evidence["source_url"] == glassonline_url
                        )
                    )
                else:
                    self.assertNotIn(glassonline_url, evidence_sources)

    def test_linkedin_signal_candidate_requires_official_non_linkedin_source(self):
        from key_person_discovery.pipeline import _add_linkedin_signal_candidates

        company = CompanyProfile(name="Heatmasters", website="https://heatmasters.net")
        profile_url = "https://www.linkedin.com/in/niklas-ant-12345"
        result = {"candidates": [], "unassigned_contacts": [], "review_required": True}
        signals = [
            {
                "channel": "linkedin",
                "value": profile_url,
                "source_url": profile_url,
                "company_public": True,
            },
            {
                "channel": "linkedin",
                "value": profile_url,
                "source_url": "https://heatmasters.net/search",
                "company_public": False,
            },
            {
                "channel": "linkedin",
                "value": profile_url,
                "source_url": "https://directory.example/heatmasters",
                "company_public": True,
            },
        ]

        _add_linkedin_signal_candidates(result, company, signals)

        self.assertNotIn("unverified_candidates", result)

    def test_default_budget_without_official_person_or_search_signal_stops_at_five(self):
        class SearchStub:
            name = "anysearch"

            def __init__(self):
                self.calls = []

            def search(self, query):
                self.calls.append(query)
                return SearchOutcome(results=[])

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                return {
                    "company_name": company.name,
                    "candidates": [],
                    "unassigned_contacts": [],
                    "review_required": True,
                }

        search = SearchStub()
        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[CrawledPage(
                url="https://acerias.example/team",
                markdown="Acerias ProcoMetal team",
            )],
        ):
            result = discover(
                company=CompanyProfile(
                    name="Acerias ProcoMetal",
                    website="https://acerias.example",
                ),
                search_clients=[search],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=Path(directory),
                official_site_discovery=lambda *_: (
                    ["https://acerias.example/team"],
                    [],
                ),
                anysearch_query_limit=7,
            )

        self.assertEqual(len(search.calls), 5)
        self.assertEqual(result["run_summary"]["anysearch_queries"], 5)
        self.assertFalse(result["run_summary"]["anysearch_extended"])

    def test_default_budget_keeps_old_official_contact_signal_extension(self):
        class SearchStub:
            name = "anysearch"

            def __init__(self):
                self.calls = []

            def search(self, query):
                self.calls.append(query)
                if (
                    "email OR phone" in query
                    or "site:example.com" in query
                ):
                    return SearchOutcome(
                        results=[
                            SearchResult(
                                query=query,
                                title="Example Minerals contact",
                                url="https://example.com/contact",
                                snippet="Example Minerals contact info@example.com",
                                provider=self.name,
                            )
                        ]
                    )
                return SearchOutcome(results=[])

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                return {
                    "company_name": company.name,
                    "candidates": [],
                    "unassigned_contacts": [],
                    "review_required": True,
                }

        search = SearchStub()
        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[],
        ):
            result = discover(
                company=CompanyProfile(
                    name="Example Minerals",
                    website="https://example.com",
                    target_contact_count=4,
                ),
                search_clients=[search],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=Path(directory),
                official_site_discovery=lambda *_: ([], []),
                anysearch_query_limit=7,
            )

        self.assertEqual(len(search.calls), 7)
        self.assertEqual(result["run_summary"]["anysearch_queries"], 7)
        self.assertTrue(result["run_summary"]["anysearch_extended"])

    def test_linkedin_signal_candidate_rejects_unparseable_slug_and_empty_company(self):
        from key_person_discovery.pipeline import _add_linkedin_signal_candidates

        profile_url = "https://www.linkedin.com/in/niklasant"
        invalid_slug_signals = [
            {
                "channel": "linkedin",
                "value": profile_url,
                "source_url": "https://acerias.example/team",
                "company_public": True,
            }
        ]
        cases = (
            (
                CompanyProfile(
                    name="Acerias ProcoMetal",
                    website="https://acerias.example",
                ),
                [],
            ),
            (
                CompanyProfile(
                    name="Acerias ProcoMetal",
                    website="https://acerias.example",
                ),
                invalid_slug_signals,
            ),
        )
        for company, signals in cases:
            with self.subTest(signals=signals):
                result = {"candidates": [], "unassigned_contacts": []}
                _add_linkedin_signal_candidates(result, company, signals)
                self.assertNotIn("unverified_candidates", result)

    def test_linkedin_signal_candidate_does_not_duplicate_existing_person(self):
        from key_person_discovery.pipeline import _add_linkedin_signal_candidates

        company = CompanyProfile(name="Sialon", website="https://sialon.example")
        profile_url = "https://www.linkedin.com/in/nico-van-dongen-3a6466165"
        result = {
            "candidates": [
                {
                    "full_name": "Nico van Dongen",
                    "linkedin": [{"value": profile_url}],
                    "emails": [],
                    "phones": [],
                }
            ],
            "unassigned_contacts": [
                {"channel": "linkedin", "value": profile_url}
            ],
        }
        signals = [
            {
                "channel": "linkedin",
                "value": profile_url,
                "source_url": "https://sialon.example/team",
                "company_public": True,
            }
        ]

        _add_linkedin_signal_candidates(result, company, signals)

        self.assertNotIn("unverified_candidates", result)
        self.assertEqual(result["unassigned_contacts"], [])

    def test_crm_linkedin_snapshot_deduplicates_unverified_candidate(self):
        from key_person_discovery.pipeline import _deduplicate_crm_contacts

        result = {
            "candidates": [],
            "unverified_candidates": [
                {
                    "full_name": "Nico van Dongen",
                    "linkedin": [
                        {
                            "value": (
                                "https://www.linkedin.com/in/"
                                "nico-van-dongen-3a6466165"
                            )
                        }
                    ],
                    "emails": [],
                    "phones": [],
                }
            ],
            "unassigned_contacts": [],
        }
        existing = [
            {
                "name": "",
                "email": "",
                "phone": "",
                "linkedin": (
                    "https://de.linkedin.com/in/"
                    "nico-van-dongen-3a6466165?trk=profile"
                ),
            }
        ]

        _deduplicate_crm_contacts(result, existing)

        self.assertEqual(result["unverified_candidates"], [])
        self.assertEqual(result["crm_duplicates_removed"], 1)
        self.assertEqual(result["crm_duplicates"][0]["kind"], "unverified_candidate")


    def test_directory_footer_contact_is_not_assigned_to_mentioned_company(self):
        class SearchStub:
            name = "search"

            def search(self, query):
                return SearchOutcome(
                    results=[
                        SearchResult(
                            query=query,
                            title="glasstec exhibitor directory",
                            url="https://www.glassglobal.com/glasstec/",
                            snippet="Exhibitors include JSJ Jodeit GmbH.",
                            provider=self.name,
                        )
                    ]
                )

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                value = {
                    "company_name": company.name,
                    "candidates": [],
                    "unassigned_contacts": [],
                    "review_required": True,
                }
                _validate_output(
                    value,
                    company,
                    signals,
                    source_urls={page.url for page in pages if page.markdown},
                )
                return value

        company = CompanyProfile(
            name="JSJ Jodeit GmbH",
            website="https://apps.glassglobal.com/profile/documents3274.html",
        )
        directory_page = CrawledPage(
            url="https://www.glassglobal.com/glasstec/",
            markdown=(
                "Exhibitor directory: JSJ Jodeit GmbH\n"
                "Site operator OGIS GmbH, Telephone +49 211 2807330"
            ),
            provider="crawl4ai",
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "key_person_discovery.pipeline.crawl_sync",
            return_value=[directory_page],
        ):
            result = discover(
                company=company,
                search_clients=[SearchStub()],
                crawler=object(),
                hermes=HermesStub(),
                artifacts_dir=Path(directory),
                phone_region="DE",
                official_site_discovery=lambda *_: ([company.website], []),
            )
        self.assertEqual(result["unassigned_contacts"], [])

    def test_zero_contact_output_is_not_usable(self):
        from key_person_discovery.job_runner import _output_is_usable

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            output.write_text(
                json.dumps(
                    {
                        "company_name": "Example",
                        "candidates": [],
                        "unassigned_contacts": [],
                        "run_summary": {"contactable_items": 0},
                    }
                )
            )
            self.assertFalse(_output_is_usable(output))
            output.write_text(
                json.dumps(
                    {
                        "company_name": "Example",
                        "candidates": [],
                        "unverified_candidates": [{"full_name": "Jane Doe"}],
                        "unassigned_contacts": [],
                        "run_summary": {"contactable_items": 0},
                    }
                )
            )
            self.assertTrue(_output_is_usable(output))
            output.write_text(
                json.dumps(
                    {
                        "company_name": "Example",
                        "candidates": [],
                        "unassigned_contacts": [
                            {"channel": "email", "value": "info@example.com"}
                        ],
                        "run_summary": {"contactable_items": 1},
                    }
                )
            )
            self.assertTrue(_output_is_usable(output))

    def test_output_stage_distinguishes_no_new_contact_and_source_failure(self):
        from key_person_discovery.job_runner import _output_stage

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            output.write_text(
                json.dumps(
                    {
                        "candidates": [],
                        "unassigned_contacts": [],
                        "run_summary": {
                            "search_results": 40,
                            "urls_crawled": 10,
                            "crawl_failures": 0,
                        },
                    }
                )
            )
            self.assertEqual(_output_stage(output), "no_new_contact")

            output.write_text(
                json.dumps(
                    {
                        "candidates": [],
                        "unassigned_contacts": [],
                        "run_summary": {
                            "search_results": 40,
                            "urls_crawled": 3,
                            "crawl_failures": 3,
                        },
                    }
                )
            )
            self.assertEqual(_output_stage(output), "source_limited")

            output.write_text("not json")
            self.assertIsNone(_output_stage(output))

    def test_crawl_sync_retries_failed_urls_once(self):
        class RetryCrawler:
            def __init__(self):
                self.calls = 0

            async def crawl(self, urls, allow_failed_content=False):
                self.calls += 1
                if self.calls == 1:
                    return [CrawledPage(url=urls[0], markdown="", error="connection closed")]
                return [CrawledPage(url=urls[0], markdown="contact page")]

        crawler = RetryCrawler()
        pages = crawl_sync(crawler, ["https://example.com/contact"])

        self.assertEqual(crawler.calls, 2)
        self.assertEqual(pages[0].markdown, "contact page")
        self.assertFalse(pages[0].error)

    def test_crm_contacts_are_removed_before_target_count(self):
        from key_person_discovery.pipeline import _deduplicate_crm_contacts

        result = {
            "company_name": "JSJ Jodeit GmbH",
            "candidates": [
                {
                    "full_name": "Steffen Schulze",
                    "linkedin": [],
                    "emails": [],
                    "phones": [{"value": "+49 9636 9204861"}],
                },
                {
                    "full_name": "New Person",
                    "linkedin": [{"value": "https://linkedin.com/in/new-person/"}],
                    "emails": [],
                    "phones": [],
                },
            ],
            "unassigned_contacts": [
                {"channel": "phone", "value": "+49 9636 9204861"},
                {"channel": "email", "value": "new@jsj.de"},
            ],
        }
        existing = [
            {
                "name": "Steffen  Schulze",
                "email": "",
                "additional_emails": [],
                "phone": "+4996369204861",
                "additional_phones": [],
                "linkedin": "",
            }
        ]
        _deduplicate_crm_contacts(result, existing)
        self.assertEqual(
            [candidate["full_name"] for candidate in result["candidates"]],
            ["New Person"],
        )
        self.assertEqual(
            result["unassigned_contacts"],
            [{"channel": "email", "value": "new@jsj.de"}],
        )
        self.assertEqual(result["crm_duplicates_removed"], 2)

    def test_new_email_is_attached_to_existing_crm_person(self):
        from key_person_discovery.pipeline import (
            _attach_crm_matched_emails,
            _deduplicate_crm_contacts,
        )

        source_url = "https://directory.example/refko"
        result = {
            "company_name": "REFKO Feuerfest GmbH",
            "candidates": [],
            "unassigned_contacts": [
                {
                    "channel": "email",
                    "value": "hoenl@refko.de",
                    "source_url": source_url,
                },
                {
                    "channel": "phone",
                    "value": "+4926231738",
                    "source_url": source_url,
                },
            ],
        }
        existing = [
            {
                "name": "Herbert Hoenl",
                "email": "",
                "phone": "",
                "linkedin": "https://linkedin.com/in/herbert-hoenl",
            }
        ]

        _attach_crm_matched_emails(result, existing)
        _deduplicate_crm_contacts(result, existing)

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["full_name"], "Herbert Hoenl")
        self.assertEqual(
            result["candidates"][0]["emails"],
            [
                {
                    "value": "hoenl@refko.de",
                    "status": "guessed",
                    "source_url": source_url,
                }
            ],
        )
        self.assertEqual(result["candidates"][0]["crm_existing_match"], ["name"])
        self.assertEqual(
            result["unassigned_contacts"],
            [{"channel": "phone", "value": "+4926231738", "source_url": source_url}],
        )

    def test_observed_phone_in_person_evidence_is_bound_to_that_person(self):
        from key_person_discovery.pipeline import _attach_evidence_bound_contacts

        source_url = "https://example.vn/procurement-notice"
        result = {
            "candidates": [
                {
                    "full_name": "Hồ Văn Ích Em",
                    "evidence": [
                        {
                            "source_url": source_url,
                            "quote": "Người liên hệ: Hồ Văn Ích Em. Điện thoại: 0913.858.349.",
                        }
                    ],
                    "phones": [],
                }
            ],
            "unassigned_contacts": [
                {
                    "channel": "phone",
                    "value": "+84913858349",
                    "status": "valid_format",
                    "source_url": source_url,
                    "whatsapp_status": "unknown",
                    "company_public": True,
                },
                {
                    "channel": "phone",
                    "value": "+842543922091",
                    "status": "valid_format",
                    "source_url": source_url,
                },
            ],
        }

        _attach_evidence_bound_contacts(result)

        self.assertEqual(
            result["candidates"][0]["phones"][0]["value"],
            "+84913858349",
        )
        self.assertEqual(
            [item["value"] for item in result["unassigned_contacts"]],
            ["+842543922091"],
        )

    def test_directory_operator_footer_contacts_are_removed(self):
        from key_person_discovery.pipeline import _filter_directory_footer_contacts

        source_url = "https://directory.example/refko"
        page = CrawledPage(
            url=source_url,
            markdown=(
                "REFKO Feuerfest GmbH\n"
                "+49 (0) 2623 1738\n"
                "hoenl@refko.de\n"
                "---\nDirectory Operator GmbH\n"
                "Phone: +49 7221 502 200\n"
                "Email: info@directory.example"
            ),
        )
        result = {
            "unassigned_contacts": [
                {"channel": "phone", "value": "+4926231738", "source_url": source_url},
                {"channel": "email", "value": "hoenl@refko.de", "source_url": source_url},
                {"channel": "phone", "value": "+497221502200", "source_url": source_url},
                {
                    "channel": "email",
                    "value": "info@directory.example",
                    "source_url": source_url,
                },
            ]
        }

        _filter_directory_footer_contacts(
            result,
            [page],
            ["https://www.refko.de/"],
        )

        self.assertEqual(
            [(item["channel"], item["value"]) for item in result["unassigned_contacts"]],
            [("phone", "+4926231738"), ("email", "hoenl@refko.de")],
        )

    def test_explicit_external_services_are_quarantined_and_network_contacts_retained(self):
        from key_person_discovery.pipeline import _filter_directory_footer_contacts

        locations_url = "https://www.ekw-refractories.com/en/about-us/locations/"
        pages = [
            CrawledPage(
                url="https://www.schwarzhaupt.de/en/impressum.html",
                markdown="### Webdesign & Realisierung\nDARO Webdesign & Entwicklung\nE-Mail: mail@daro.de",
            ),
            CrawledPage(
                url="https://www.ekw-refractories.com/certificate.pdf",
                markdown=(
                    "Kiwa International Cert GmbH certifies EKW GmbH\n"
                    "Telefon +49 (0)40 30 39 49 60\n"
                    "e-mail: info@kiwa.de"
                ),
            ),
            CrawledPage(
                url="https://www.hamag.de/en/privacy-policy/",
                markdown=(
                    "external data protection officer\ncertitut\nGesellschaft für Compliance "
                    "und Datenschutz mbH\nTelefon +49 89 21541600"
                ),
            ),
            CrawledPage(
                url="https://www.beinbauer-group.de/code-of-conduct.pdf",
                markdown=(
                    "Compliance Officer Services Legal\nRechtsanwalt Stephan Rheinwald\n"
                    "E-Mail: s.rheinwald@cos-legal.eu"
                ),
            ),
            CrawledPage(
                url=locations_url,
                markdown=(
                    "## Subsidiaries\n### Brazil\nsubsidiary@example.com\n"
                    "## Agencies\n### Argentina\nKIMIA3\nv.demonte@kimia3.com.ar"
                ),
            ),
        ]
        pages.extend(
            [
                CrawledPage(
                    url=pages[0].url,
                    markdown="Schwarzhaupt contact excerpt: mail@daro.de",
                    source_type="contact_excerpt",
                ),
                CrawledPage(
                    url=pages[2].url,
                    markdown="HAMAG privacy contact excerpt: +49 89 21541600",
                    source_type="contact_excerpt",
                ),
            ]
        )
        result = {
            "unassigned_contacts": [
                {"channel": "email", "value": "mail@daro.de", "source_url": pages[0].url},
                {"channel": "email", "value": "info@kiwa.de", "source_url": pages[1].url},
                {"channel": "phone", "value": "+494030394960", "source_url": pages[1].url},
                {"channel": "phone", "value": "+498921541600", "source_url": pages[2].url},
                {
                    "channel": "email",
                    "value": "s.rheinwald@cos-legal.eu",
                    "source_url": pages[3].url,
                },
                {
                    "channel": "email",
                    "value": "subsidiary@example.com",
                    "source_url": locations_url,
                },
                {
                    "channel": "email",
                    "value": "v.demonte@kimia3.com.ar",
                    "source_url": locations_url,
                },
            ]
        }

        _filter_directory_footer_contacts(
            result,
            pages,
            [
                "https://www.schwarzhaupt.de",
                "https://www.ekw-refractories.com",
                "https://www.hamag.de",
                "https://www.beinbauer-group.de",
            ],
        )

        self.assertEqual(
            [(item["value"], item.get("relationship")) for item in result["unassigned_contacts"]],
            [
                ("subsidiary@example.com", "subsidiary"),
                ("v.demonte@kimia3.com.ar", "agent"),
            ],
        )
        self.assertEqual(
            {item["value"] for item in result["excluded_contacts"]},
            {
                "mail@daro.de",
                "info@kiwa.de",
                "+494030394960",
                "+498921541600",
                "s.rheinwald@cos-legal.eu",
            },
        )

    def test_company_name_in_directory_path_does_not_make_directory_official(self):
        from key_person_discovery.pipeline import (
            _distinctive_company_host,
            _filter_directory_footer_contacts,
        )

        company = CompanyProfile(
            name="Công ty TNHH Một Thành Viên Thép Miền Nam - VNSTEEL",
            website="https://thepmiennam.com.vn",
        )
        source_url = (
            "https://insangtaotre.vn/doc/"
            "cong-ty-tnhh-mot-thanh-vien-thep-mien-nam-vnsteel/"
        )
        page = CrawledPage(
            url=source_url,
            markdown="VNSTEEL logo document\nHotline: 0933 991 768\nEmail: insangtaotre@gmail.com",
        )
        result = {
            "unassigned_contacts": [
                {"channel": "email", "value": "insangtaotre@gmail.com", "source_url": source_url},
                {"channel": "phone", "value": "+84933991768", "source_url": source_url},
            ]
        }

        self.assertFalse(_distinctive_company_host(company, source_url))
        _filter_directory_footer_contacts(result, [page], [company.website])

        self.assertEqual(result["unassigned_contacts"], [])

    def test_search_evidence_requires_exact_current_company(self):
        company = CompanyProfile(name="Harbor Castings Inc.")
        results = [
            SearchResult(
                query="q",
                title="Dan Mihovk",
                url="https://linkedin.com/in/dan-mihovk",
                snippet="Tooling Manager - HARBOR CASTINGS INC. (Current)",
                provider="anysearch",
            ),
            SearchResult(
                query="q",
                title="Chuck Lane - President at HARBOR CASTINGS INC.",
                url="https://linkedin.com/in/chuck-lane",
                snippet="Experience: HARBOR CASTINGS INC.",
                provider="anysearch",
            ),
            SearchResult(
                query="q",
                title="Former Employee",
                url="https://linkedin.com/in/former",
                snippet=(
                    "Current role at Another Company. "
                    "Harbor Castings Inc. from 2013 to 2015."
                ),
                provider="anysearch",
            ),
        ]
        pages = _search_evidence_pages(company, results)
        self.assertEqual(
            [page.url for page in pages],
            [
                "https://linkedin.com/in/dan-mihovk",
                "https://linkedin.com/in/chuck-lane",
            ],
        )
        self.assertTrue(all(page.source_type == "search_excerpt" for page in pages))
        signals = extract_contact_signals(pages)
        self.assertTrue(all(item["status"] == "probable" for item in signals))

    def test_search_evidence_accepts_german_bei_current_company(self):
        company = CompanyProfile(name="REFKO Feuerfest GmbH")
        results = [
            SearchResult(
                query="q",
                title="Peter Weirich - Prokurist bei REFKO Feuerfest GmbH",
                url="https://linkedin.com/in/peter-weirich-a81855231",
                snippet="Berufserfahrung: REFKO Feuerfest GmbH",
                provider="anysearch",
            ),
            SearchResult(
                query="q",
                title="Lukas Möhring - REFKO Feuerfest GmbH",
                url="https://linkedin.com/in/lukas-moehring",
                snippet="Technischer Vertrieb bei REFKO Feuerfest GmbH",
                provider="anysearch",
            ),
            SearchResult(
                query="q",
                title="Jinwook Kim",
                url="https://linkedin.com/in/jinwook-kim",
                snippet="Posted about REFKO Feuerfest GmbH",
                provider="anysearch",
            ),
        ]

        pages = _search_evidence_pages(company, results)

        self.assertEqual(
            [page.url for page in pages],
            [
                "https://linkedin.com/in/peter-weirich-a81855231",
                "https://linkedin.com/in/lukas-moehring",
            ],
        )

    def test_company_name_validation_accepts_explicit_aliases_only(self):
        company = CompanyProfile(name="Industrias Mineiras do Mondego S.A. (IMOSA®)")
        base = {
            "candidates": [],
            "review_required": True,
        }
        _validate_output({**base, "company_name": "Industrias Mineiras do Mondego S.A. (IMOSA)"}, company, [])
        _validate_output({**base, "company_name": "IMOSA"}, company, [])
        with self.assertRaisesRegex(RuntimeError, "company_name"):
            _validate_output({**base, "company_name": "IMOSA Fashion"}, company, [])

    def test_unverified_or_unfetched_candidate_is_rejected_not_returned(self):
        company = CompanyProfile(name="Example Minerals")
        output = {
            "company_name": company.name,
            "candidates": [
                {
                    "full_name": "Jane Doe",
                    "company_match": "probable",
                    "influence_type": "decision_maker",
                    "linkedin": [],
                    "emails": [],
                    "phones": [],
                    "evidence": [
                        {
                            "source_url": "https://unfetched.example/jane",
                            "quote": "Jane Doe, Purchasing Manager",
                            "supports": "employment",
                        }
                    ],
                    "review_required": True,
                }
            ],
            "review_required": True,
        }
        _validate_output(
            output,
            company,
            [],
            source_urls={"https://example.com/team"},
        )
        self.assertEqual(output["candidates"], [])
        self.assertNotIn("unverified_candidates", output)
        self.assertEqual(output["validation_rejections"][0]["full_name"], "Jane Doe")

    def test_unverified_employment_candidate_is_preserved_for_review(self):
        company = CompanyProfile(name="Example Minerals")
        profile_url = "https://linkedin.com/in/jane-doe"
        output = {
            "company_name": company.name,
            "candidates": [
                {
                    "full_name": "Jane Doe",
                    "current_title": "Purchasing Manager",
                    "company_match": "probable",
                    "influence_type": "decision_maker",
                    "linkedin": [
                        {
                            "value": profile_url,
                            "status": "probable",
                            "source_url": profile_url,
                        }
                    ],
                    "emails": [],
                    "phones": [],
                    "evidence": [{"source_url": profile_url, "quote": "Jane Doe, Purchasing Manager"}],
                    "review_required": True,
                }
            ],
            "review_required": True,
        }
        signals = [
            {
                "channel": "linkedin",
                "value": profile_url,
                "status": "probable",
                "source_url": profile_url,
            }
        ]

        _validate_output(output, company, signals, source_urls={profile_url})

        self.assertEqual(output["candidates"], [])
        self.assertEqual(output["unverified_candidates"][0]["full_name"], "Jane Doe")
        self.assertEqual(
            output["unverified_candidates"][0]["validation_reasons"],
            ["current employment at the target company is not verified"],
        )

    def test_probable_candidate_from_search_excerpt_stays_pending(self):
        company = CompanyProfile(name="Example Minerals")
        profile_url = "https://linkedin.com/in/jane-doe"
        output = {
            "company_name": company.name,
            "candidates": [
                {
                    "full_name": "Jane Doe",
                    "company_match": "probable",
                    "influence_type": "technical_influencer",
                    "linkedin": [
                        {
                            "value": profile_url,
                            "status": "probable",
                            "source_url": profile_url,
                        }
                    ],
                    "emails": [],
                    "phones": [],
                    "evidence": [{"source_url": profile_url, "quote": "Current at Example Minerals"}],
                    "review_required": True,
                }
            ],
            "review_required": True,
        }
        signals = [
            {
                "channel": "linkedin",
                "value": profile_url,
                "status": "probable",
                "source_url": profile_url,
            }
        ]
        _validate_output(
            output,
            company,
            signals,
            source_urls={profile_url},
            probable_source_urls={profile_url},
        )
        self.assertEqual(output["candidates"], [])
        self.assertEqual(output["unverified_candidates"][0]["full_name"], "Jane Doe")

    def test_contact_signals_preserve_three_required_channels(self):
        page = CrawledPage(
            url="https://example.com/team",
            markdown=(
                "Jane Doe jane.doe@example.com "
                "https://www.linkedin.com/in/jane-doe/ "
                "Phone +86 138 0013 8000 WhatsApp https://wa.me/8613800138000"
            ),
        )
        signals = extract_contact_signals([page], "CN")
        by_channel = {item["channel"]: item for item in signals}
        self.assertEqual(by_channel["email"]["value"], "jane.doe@example.com")
        self.assertEqual(by_channel["linkedin"]["value"], "https://www.linkedin.com/in/jane-doe")
        self.assertEqual(by_channel["whatsapp"]["status"], "verified")
        self.assertEqual(by_channel["phone"]["whatsapp_status"], "verified")

    def test_plain_mobile_is_not_assumed_to_be_whatsapp(self):
        page = CrawledPage(url="https://example.com", markdown="Mobile: +49 151 23456789")
        phone = extract_contact_signals([page], "DE")[0]
        self.assertEqual(phone["channel"], "phone")
        self.assertEqual(phone["whatsapp_status"], "unknown")

    def test_explicit_whatsapp_label_verifies_the_following_number(self):
        page = CrawledPage(
            url="https://cerablast.com/en/order/",
            markdown=(
                "### New: Order in WhatsApp\n"
                "You can easily place your order in WhatsApp using the following number: "
                "**+49 (0)172-6659576**"
            ),
        )
        signals = extract_contact_signals([page], "DE")
        by_channel = {item["channel"]: item for item in signals}
        self.assertEqual(by_channel["whatsapp"]["value"], "+491726659576")
        self.assertEqual(by_channel["phone"]["whatsapp_status"], "verified")

    def test_fax_number_is_not_extracted_as_phone(self):
        page = CrawledPage(
            url="https://example.com/contact",
            markdown=(
                "Phone: +49 9636 92 04 861\n"
                "Telefax: +49 9636 92 04 871"
            ),
        )
        signals = extract_contact_signals([page], "DE")
        self.assertEqual(
            [item["value"] for item in signals if item["channel"] == "phone"],
            ["+4996369204861"],
        )

    def test_wa_me_number_is_international_without_phone_region(self):
        page = CrawledPage(
            url="https://example.com/contact",
            markdown="WhatsApp https://wa.me/5519998641869",
        )
        signals = extract_contact_signals([page])
        self.assertIn(
            ("whatsapp", "+5519998641869"),
            {(item["channel"], item["value"]) for item in signals},
        )

    def test_contact_signals_use_canonical_email_and_linkedin_values(self):
        page = CrawledPage(
            url="https://example.com/team",
            markdown=(
                "Sales@Example.COM "
                "https://de.linkedin.com/in/Peter-Weirich-A81855231/?trk=org-employees\n"
                "https://de.linkedin.com/in/lukas-m%C3%B6hring-8a4085389/en?trk=org-employees"
            ),
        )

        signals = extract_contact_signals([page])

        self.assertIn(
            ("email", "sales@example.com"),
            {(item["channel"], item["value"]) for item in signals},
        )
        self.assertIn(
            ("linkedin", "https://www.linkedin.com/in/peter-weirich-a81855231"),
            {(item["channel"], item["value"]) for item in signals},
        )
        self.assertIn(
            ("linkedin", "https://www.linkedin.com/in/lukas-m%C3%B6hring-8a4085389"),
            {(item["channel"], item["value"]) for item in signals},
        )

    def test_contact_signals_drop_prefixed_duplicates_from_broken_mailto_text(self):
        page = CrawledPage(
            url="https://example.com/contact",
            markdown=(
                "e.gigova@example.com "
                "https://example.com/mailtoe.gigova@example.com\n"
                "sales@example.com send an email tosales@example.com"
            ),
        )

        emails = [
            item["value"]
            for item in extract_contact_signals([page])
            if item["channel"] == "email"
        ]

        self.assertEqual(emails, ["e.gigova@example.com", "sales@example.com"])

    def test_unassigned_contacts_keep_tiered_public_sources(self):
        company = CompanyProfile(name="Example", website="https://example.com")
        output = {"company_name": "Example", "candidates": [], "review_required": True}
        signals = [
            {
                "channel": "email",
                "value": "sales@example.com",
                "status": "observed",
                "source_url": "https://example.com/contact",
                "company_public": True,
            },
            {
                "channel": "email",
                "value": "directory@third-party.test",
                "status": "observed",
                "source_url": "https://third-party.test/example",
            },
            {
                "channel": "email",
                "value": "sales@example.com",
                "status": "observed",
                "source_url": "https://example.com/about",
                "company_public": True,
            },
            {
                "channel": "phone",
                "value": "+12026631282",
                "status": "valid_format",
                "source_url": "https://example.com/government-letter.pdf",
                "company_public": True,
            },
            {
                "channel": "phone",
                "value": "+12026631283",
                "status": "valid_format",
                "source_url": "https://directory.test/example",
                "company_association": "probable",
            },
        ]
        _validate_output(output, company, signals)
        self.assertEqual(
            [item["value"] for item in output["unassigned_contacts"]],
            ["sales@example.com", "+12026631282", "+12026631283"],
        )
        self.assertEqual(
            [item["verification_status"] for item in output["unassigned_contacts"]],
            ["company_source", "published_document_unverified", "ownership_unverified"],
        )

    def test_linkedin_tracking_variants_are_bound_and_deduplicated(self):
        company = CompanyProfile(name="REFKO Feuerfest GmbH")
        profile_url = "https://linkedin.com/in/peter-weirich-a81855231"
        tracked_profile_url = (
            "https://de.linkedin.com/in/peter-weirich-a81855231?trk=org-employees"
        )
        company_page = "https://linkedin.com/company/refko-feuerfest-gmbh"
        output = {
            "company_name": company.name,
            "candidates": [
                {
                    "full_name": "Peter Weirich",
                    "company_match": "probable",
                    "influence_type": "decision_maker",
                    "linkedin": [
                        {
                            "value": tracked_profile_url,
                            "status": "probable",
                            "source_url": profile_url,
                        }
                    ],
                    "emails": [],
                    "phones": [],
                    "evidence": [{"source_url": profile_url, "quote": "Prokurist bei REFKO"}],
                    "review_required": True,
                }
            ],
            "review_required": True,
        }
        signals = [
            {
                "channel": "linkedin",
                "value": profile_url,
                "status": "probable",
                "source_url": profile_url,
            },
            {
                "channel": "linkedin",
                "value": tracked_profile_url,
                "status": "observed",
                "source_url": company_page,
                "company_public": True,
            },
            {
                "channel": "linkedin",
                "value": "https://kr.linkedin.com/in/jinwook-kim?trk=actor-image",
                "status": "observed",
                "source_url": company_page,
                "company_public": True,
            },
            {
                "channel": "linkedin",
                "value": "https://kr.linkedin.com/in/jinwook-kim?trk=actor-name",
                "status": "observed",
                "source_url": company_page,
                "company_public": True,
            },
        ]

        _validate_output(
            output,
            company,
            signals,
            source_urls={profile_url, company_page},
            probable_source_urls={profile_url},
        )

        self.assertEqual(
            output["unverified_candidates"][0]["linkedin"][0]["value"],
            "https://www.linkedin.com/in/peter-weirich-a81855231",
        )
        self.assertEqual(
            [item["value"] for item in output["unassigned_contacts"]],
            ["https://www.linkedin.com/in/jinwook-kim"],
        )

    def test_company_phone_is_not_attached_to_person(self):
        company = CompanyProfile(name="Example", website="https://example.com")
        profile_url = "https://linkedin.com/in/jane-doe"
        output = {
            "company_name": "Example",
            "candidates": [
                {
                    "full_name": "Jane Doe",
                    "company_match": "verified",
                    "influence_type": "decision_maker",
                    "linkedin": [
                        {
                            "value": profile_url,
                            "status": "observed",
                            "source_url": profile_url,
                        }
                    ],
                    "emails": [],
                    "phones": [
                        {
                            "value": "+13304997178",
                            "status": "valid_format",
                            "whatsapp_status": "unknown",
                            "source_url": "https://example.com/contact",
                        }
                    ],
                    "evidence": [{"source_url": profile_url, "quote": "Current at Example"}],
                    "review_required": True,
                }
            ],
            "review_required": True,
        }
        signals = [
            {
                "channel": "linkedin",
                "value": profile_url,
                "status": "probable",
                "source_url": profile_url,
            },
            {
                "channel": "phone",
                "value": "+13304997178",
                "status": "valid_format",
                "whatsapp_status": "unknown",
                "source_url": "https://example.com/contact",
                "company_public": True,
            },
        ]
        _validate_output(
            output,
            company,
            signals,
            source_urls={profile_url, "https://example.com/contact"},
        )
        self.assertEqual(output["candidates"][0]["phones"], [])
        self.assertEqual(output["candidates"][0]["linkedin"][0]["status"], "probable")
        self.assertEqual(output["unassigned_contacts"][0]["value"], "+13304997178")

    def test_contact_value_cannot_be_reassigned_to_a_different_source(self):
        company = CompanyProfile(name="Example", website="https://example.com")
        pdf_url = "https://example.com/management.pdf"
        output = {
            "company_name": "Example",
            "candidates": [
                {
                    "full_name": "Jane Doe",
                    "company_match": "verified",
                    "influence_type": "decision_maker",
                    "linkedin": [],
                    "emails": [],
                    "phones": [
                        {
                            "value": "+13304997178",
                            "status": "valid_format",
                            "whatsapp_status": "unknown",
                            "source_url": pdf_url,
                        }
                    ],
                    "evidence": [{"source_url": pdf_url, "quote": "Jane Doe, President"}],
                    "review_required": True,
                }
            ],
            "review_required": True,
        }
        signals = [
            {
                "channel": "phone",
                "value": "+13304997178",
                "status": "valid_format",
                "whatsapp_status": "unknown",
                "source_url": "https://example.com/contact",
                "company_public": True,
            }
        ]
        _validate_output(output, company, signals, source_urls={pdf_url})
        self.assertEqual(output["candidates"][0]["phones"], [])
        self.assertEqual(output["unassigned_contacts"][0]["source_url"], "https://example.com/contact")

    def test_parse_hermes_json_ignores_surrounding_text(self):
        value = parse_json_object('prefix\n```json\n{"candidates": [], "review_required": true}\n```')
        self.assertEqual(value["candidates"], [])

    def test_downgrades_unsupported_verified_whatsapp_without_failing_company(self):
        company = CompanyProfile(name="Example Minerals")
        output = {
            "company_name": company.name,
            "candidates": [
                {
                    "full_name": "Jane Doe",
                    "company_match": "verified",
                    "influence_type": "decision_maker",
                    "linkedin": [],
                    "emails": [],
                    "phones": [
                        {
                            "value": "+4915123456789",
                            "whatsapp_status": "verified",
                            "source_url": "https://example.com",
                        }
                    ],
                    "evidence": [
                        {
                            "source_url": "https://example.com",
                            "quote": "Jane Doe, phone +49 151 23456789",
                        }
                    ],
                    "review_required": True,
                }
            ],
            "review_required": True,
        }
        signals = [
            {
                "channel": "phone",
                "value": "+4915123456789",
                "whatsapp_status": "unknown",
                "source_url": "https://example.com",
            }
        ]
        _validate_output(
            output,
            company,
            signals,
            source_urls={"https://example.com"},
        )
        self.assertEqual(output["candidates"][0]["phones"][0]["whatsapp_status"], "unknown")

    def test_company_whatsapp_stays_unassigned_without_person_name_association(self):
        from key_person_discovery.pipeline import _attach_evidence_bound_contacts

        company = CompanyProfile(name="Cerablast GmbH & Co.KG")
        source_url = "https://cerablast.com/en/order/"
        number = "+491726659576"
        output = {
            "company_name": company.name,
            "candidates": [
                {
                    "full_name": "Tobias Gast",
                    "company_match": "verified",
                    "influence_type": "decision_maker",
                    "linkedin": [],
                    "emails": [],
                    "phones": [
                        {
                            "value": number,
                            "status": "valid_format",
                            "whatsapp_status": "verified",
                            "source_url": source_url,
                        }
                    ],
                    "evidence": [
                        {
                            "source_url": source_url,
                            "quote": "Order in WhatsApp using the following number: +49 (0)172-6659576",
                            "supports": "Company order channel",
                        }
                    ],
                    "review_required": True,
                }
            ],
            "review_required": True,
        }
        signals = [
            {
                "channel": "whatsapp",
                "value": number,
                "status": "verified",
                "source_url": source_url,
                "company_public": True,
            },
            {
                "channel": "phone",
                "value": number,
                "status": "valid_format",
                "whatsapp_status": "verified",
                "source_url": source_url,
                "company_public": True,
            },
        ]

        _validate_output(output, company, signals, source_urls={source_url})
        _attach_evidence_bound_contacts(output)

        self.assertEqual(output["candidates"][0]["phones"], [])
        self.assertEqual(
            {(item["channel"], item["value"]) for item in output["unassigned_contacts"]},
            {("phone", number), ("whatsapp", number)},
        )

    def test_broad_discovery_fuses_searxng_without_changing_default_fallback(self):
        """The experiment widens a query's evidence, but keeps the AnySearch cap."""

        company = CompanyProfile(
            name="Example Minerals",
            website="https://example.com",
            target_contact_count=1,
        )
        official_query = '"Example Minerals" official website'
        anysearch_url = (
            "https://www.linkedin.com/in/jane-doe?trk=org-employees"
        )
        searxng_duplicate = "https://de.linkedin.com/in/jane-doe/"
        searxng_extra = "https://www.linkedin.com/in/john-roe"

        class SearchStub:
            def __init__(self, name):
                self.name = name
                self.calls = []
                self.limits = []

            def search(self, query, limit=5):
                self.calls.append(query)
                self.limits.append(limit)
                if query != official_query:
                    return SearchOutcome(results=[])
                if self.name == "anysearch":
                    return SearchOutcome(
                        results=[
                            SearchResult(
                                query=query,
                                title="Jane Doe | LinkedIn",
                                url=anysearch_url,
                                snippet="Purchasing Manager at Example Minerals",
                                provider=self.name,
                                rank=1,
                            )
                        ]
                    )
                return SearchOutcome(
                    results=[
                        SearchResult(
                            query=query,
                            title="Jane Doe | LinkedIn",
                            url=searxng_duplicate,
                            snippet="Purchasing Manager at Example Minerals",
                            provider=self.name,
                            rank=1,
                        ),
                        SearchResult(
                            query=query,
                            title="John Roe | LinkedIn",
                            url=searxng_extra,
                            snippet="Plant Manager at Example Minerals",
                            provider=self.name,
                            rank=2,
                        ),
                    ]
                )

        class HermesStub:
            def extract(self, company, pages, signals, usage_path):
                return {
                    "company_name": company.name,
                    "candidates": [],
                    "unassigned_contacts": [],
                    "review_required": True,
                }

        for broad_discovery in (False, True):
            with self.subTest(broad_discovery=broad_discovery), tempfile.TemporaryDirectory() as directory, patch(
                "key_person_discovery.pipeline.crawl_sync",
                return_value=[],
            ):
                anysearch = SearchStub("anysearch")
                searxng = SearchStub("searxng")
                result = discover(
                    company=company,
                    search_clients=[anysearch, searxng],
                    crawler=object(),
                    hermes=HermesStub(),
                    artifacts_dir=Path(directory),
                    official_site_discovery=lambda *_: ([], []),
                    anysearch_query_limit=7,
                    broad_discovery=broad_discovery,
                )
                hits = json.loads(
                    (Path(directory) / "search-results.json").read_text()
                )

            official_hits = [item for item in hits if item["query"] == official_query]
            by_linkedin = {}
            for item in official_hits:
                canonical = normalize_linkedin(item["url"])
                if canonical:
                    by_linkedin.setdefault(canonical, []).append(item)

            self.assertLessEqual(len(anysearch.calls), 7)
            self.assertTrue(anysearch.limits)
            self.assertTrue(all(limit == 5 for limit in anysearch.limits))
            self.assertTrue(searxng.limits)
            expected_searxng_limit = 10 if broad_discovery else 5
            self.assertTrue(
                all(limit == expected_searxng_limit for limit in searxng.limits)
            )
            if not broad_discovery:
                self.assertEqual(len(official_hits), 1)
                self.assertEqual(official_hits[0]["provider"], "anysearch")
                self.assertNotIn(normalize_linkedin(searxng_extra), by_linkedin)
            else:
                self.assertIn(normalize_linkedin(searxng_extra), by_linkedin)
                jane_hits = by_linkedin[normalize_linkedin(anysearch_url)]
                self.assertEqual(len(jane_hits), 1)
                self.assertEqual(jane_hits[0]["provider"], "anysearch")
                self.assertIn(official_query, searxng.calls)

    def test_broad_linkedin_search_candidates_keep_company_associated_people(self):
        from key_person_discovery.pipeline import _add_linkedin_search_candidates

        company = CompanyProfile(
            name="Example Minerals",
            website="https://example.com",
        )
        accepted = (
            ("Jane Doe", "Purchasing Manager"),
            ("John Roe", "Plant Manager"),
            ("Tina Tech", "Technical Manager"),
            ("Paul Pro", "Production Manager"),
            ("Quinn Alder", "Quality Manager"),
            ("Manny Maint", "Maintenance Manager"),
        )
        search_results = [
            SearchResult(
                query="q",
                title=f"{name} - {role} at Example Minerals | LinkedIn",
                url=f"https://www.linkedin.com/in/{name.lower().replace(' ', '-')}",
                snippet=f"Current {role} at Example Minerals",
                provider="searxng",
            )
            for name, role in accepted
        ] + [
            SearchResult(
                query="q",
                title="Una Ver - Purchasing Manager | Example Minerals",
                url="https://www.linkedin.com/in/una-ver",
                snippet="Purchasing and sourcing profile",
                provider="searxng",
            ),
            SearchResult(
                query="q",
                title="Former Employee - Purchasing Manager at Example Minerals",
                url="https://www.linkedin.com/in/former-employee",
                snippet="Former Purchasing Manager at Example Minerals",
                provider="searxng",
            ),
            SearchResult(
                query="q",
                title="Previous Employee - Plant Manager at Example Minerals",
                url="https://www.linkedin.com/in/previous-employee",
                snippet="Previous Plant Manager at Example Minerals",
                provider="searxng",
            ),
            SearchResult(
                query="q",
                title="Other Corp - Purchasing Manager",
                url="https://www.linkedin.com/in/other-corp-buyer",
                snippet="Current Purchasing Manager at Example Minerals Services",
                provider="searxng",
            ),
            SearchResult(
                query="q",
                title="Harbor HR - Human Resources Manager",
                url="https://www.linkedin.com/in/harbor-hr",
                snippet="Current Human Resources Manager at Example Minerals",
                provider="searxng",
            ),
        ]
        result = {"candidates": [], "unverified_candidates": []}

        _add_linkedin_search_candidates(result, company, search_results)

        candidates = result["unverified_candidates"]
        self.assertEqual(
            {candidate["full_name"] for candidate in candidates},
            {name for name, _ in accepted} | {"Una Ver", "Harbor Hr"},
        )
        for candidate in candidates:
            if candidate["full_name"] == "Una Ver":
                self.assertEqual(candidate["company_match"], "uncertain")
                self.assertEqual(candidate["discovery_tier"], "unverified")
                self.assertEqual(candidate["confidence"], 0.35)
            else:
                self.assertEqual(candidate["company_match"], "probable")
                self.assertEqual(candidate["discovery_tier"], "probable_current")
            if candidate["full_name"] == "Harbor Hr":
                self.assertEqual(candidate["influence_type"], "other")
            self.assertTrue(candidate["review_required"])
            self.assertEqual(candidate["emails"], [])
            self.assertEqual(candidate["phones"], [])

    def test_inferred_email_candidates_are_separate_bounded_and_not_contactable(self):
        from key_person_discovery.pipeline import _add_inferred_email_candidates

        company = CompanyProfile(name="Example Minerals", website="https://example.com")
        result = {
            "candidates": [
                {
                    "full_name": "Alice Smith",
                    "company_match": "verified",
                    "emails": [
                        {
                            "value": "alice.smith@example.com",
                            "status": "observed",
                            "source_url": "https://example.com/team",
                        }
                    ],
                    "linkedin": [],
                    "phones": [],
                },
                {
                    "full_name": "Bob Jones",
                    "company_match": "probable",
                    "emails": [],
                    "linkedin": [
                        {"value": "https://www.linkedin.com/in/bob-jones"}
                    ],
                    "phones": [],
                },
                {
                    "full_name": "CRM Person",
                    "company_match": "probable",
                    "emails": [],
                    "linkedin": [],
                    "phones": [],
                },
            ],
            "unassigned_contacts": [
                {
                    "channel": "email",
                    "value": "info@example.com",
                    "source_url": "https://example.com/contact",
                }
            ],
        }
        before = _contactable_items(result)

        _add_inferred_email_candidates(
            result,
            company,
            signals=[
                {
                    "channel": "email",
                    "value": "alice.smith@example.com",
                    "status": "observed",
                    "company_public": True,
                    "source_url": "https://example.com/team",
                }
            ],
            crm_contacts=[
                {
                    "name": "CRM Person",
                    "email": "crm.person@example.com",
                }
            ],
        )

        bob = next(item for item in result["candidates"] if item["full_name"] == "Bob Jones")
        self.assertLessEqual(len(bob.get("inferred_emails", [])), 3)
        self.assertIn(
            "bob.jones@example.com",
            {item["value"] for item in bob.get("inferred_emails", [])},
        )
        self.assertEqual(bob["emails"], [])
        self.assertNotIn(
            "crm.person@example.com",
            {item["value"] for item in bob.get("inferred_emails", [])},
        )
        self.assertNotIn(
            "info@example.com",
            {item["value"] for item in bob.get("inferred_emails", [])},
        )
        self.assertEqual(_contactable_items(result), before)

    def test_unverified_linkedin_candidate_counts_as_contactable(self):
        result = {
            "candidates": [],
            "unverified_candidates": [
                {
                    "full_name": "Jane Doe",
                    "linkedin": [
                        {"value": "https://www.linkedin.com/in/jane-doe"}
                    ],
                    "emails": [],
                    "phones": [],
                }
            ],
            "unassigned_contacts": [],
        }

        self.assertEqual(_contactable_items(result), 1)

    def test_broad_candidate_normalization_filters_vacancies_and_tags_existing_people(self):
        from key_person_discovery.pipeline import _normalize_broad_discovery_candidates

        company = CompanyProfile(name="Example Minerals", website="https://example.com")
        result = {
            "candidates": [
                {
                    "full_name": "Verified Leader",
                    "company_match": "verified",
                    "evidence": [],
                }
            ],
            "unverified_candidates": [
                {
                    "full_name": "Unknown Process Engineer",
                    "current_title": "Process Engineer",
                    "company_match": "uncertain",
                    "linkedin": [],
                    "evidence": [
                        {
                            "quote": "Process Engineer listed as current vacancy",
                            "supports": "job_posting_current_vacancy",
                        }
                    ],
                },
                {
                    "full_name": "Jane Doe",
                    "current_title": "Unknown",
                    "company_match": "probable",
                    "linkedin": [
                        {"value": "https://de.linkedin.com/in/jane-doe?trk=search"}
                    ],
                    "evidence": [],
                },
                {
                    "full_name": "Harbor HR",
                    "current_title": "Unknown",
                    "company_match": "probable",
                    "linkedin": [
                        {"value": "https://www.linkedin.com/in/harbor-hr"}
                    ],
                    "evidence": [],
                },
            ],
        }
        search_results = [
            SearchResult(
                query="q",
                title="Jane Doe - Purchasing Manager at Example Minerals",
                url="https://www.linkedin.com/in/jane-doe/",
                snippet="Current Purchasing Manager at Example Minerals",
                provider="searxng",
            ),
            SearchResult(
                query="q",
                title="Harbor HR - Human Resources Manager at Example Minerals",
                url="https://www.linkedin.com/in/harbor-hr",
                snippet="Current Human Resources Manager at Example Minerals",
                provider="searxng",
            ),
        ]

        _normalize_broad_discovery_candidates(result, company, search_results)

        self.assertEqual(result["candidates"][0]["discovery_tier"], "verified_current")
        self.assertEqual(
            {item["full_name"] for item in result["unverified_candidates"]},
            {"Jane Doe", "Harbor HR"},
        )
        jane = next(
            item for item in result["unverified_candidates"] if item["full_name"] == "Jane Doe"
        )
        self.assertEqual(jane["current_title"], "Purchasing Manager")
        self.assertEqual(jane["discovery_tier"], "probable_current")
        harbor = next(
            item for item in result["unverified_candidates"] if item["full_name"] == "Harbor HR"
        )
        self.assertEqual(harbor["discovery_tier"], "unverified")

    def test_broad_search_rejects_conflicting_expanded_company_identity(self):
        from key_person_discovery.pipeline import _add_linkedin_search_candidates

        company = CompanyProfile(name="Heatmasters", website="https://heatmasters.net")
        result = {"candidates": [], "unverified_candidates": []}
        _add_linkedin_search_candidates(
            result,
            company,
            [
                SearchResult(
                    query="q",
                    title="Tim Dowd - Project Manager at Heatmasters - LinkedIn",
                    url="https://www.linkedin.com/in/tim-dowd26",
                    snippet=(
                        "Project Manager at Heatmasters · Experience: "
                        "Heatmasters Mechanical · Education: Northern Illinois University"
                    ),
                    provider="searxng",
                ),
                SearchResult(
                    query="q",
                    title="Lasse Laakso - Industrial Supervisor at Heatmasters Oy | LinkedIn",
                    url="https://www.linkedin.com/in/lasse-laakso-1234",
                    snippet=(
                        "Current Industrial Supervisor at Heatmasters Oy · "
                        "Experience: Heatmasters Oy · Present"
                    ),
                    provider="searxng",
                ),
            ],
        )

        self.assertEqual(
            [item["full_name"] for item in result["unverified_candidates"]],
            ["Lasse Laakso"],
        )

    def test_broad_normalization_deduplicates_linkedin_stable_id_across_tiers(self):
        from key_person_discovery.pipeline import _normalize_broad_discovery_candidates

        company = CompanyProfile(name="Sialon", website="https://sialon.com")
        result = {
            "candidates": [
                {
                    "full_name": "Nico van Dongen",
                    "company_match": "verified",
                    "linkedin": [
                        {
                            "value": "https://www.linkedin.com/in/nico-van-dongen-3a6466165"
                        }
                    ],
                    "evidence": [],
                }
            ],
            "unverified_candidates": [
                {
                    "full_name": "Nico V",
                    "company_match": "probable",
                    "linkedin": [
                        {"value": "https://www.linkedin.com/in/nico-v-3a6466165"}
                    ],
                    "evidence": [],
                }
            ],
        }

        _normalize_broad_discovery_candidates(result, company, [])

        self.assertEqual(
            [item["full_name"] for item in result["candidates"]],
            ["Nico van Dongen"],
        )
        self.assertEqual(result["unverified_candidates"], [])

    def test_broad_search_ignores_company_mentions_from_similar_profiles(self):
        from key_person_discovery.pipeline import _add_linkedin_search_candidates

        company = CompanyProfile(name="Rubin Trading AD", website="https://rubin.bg")
        result = {"candidates": [], "unverified_candidates": []}
        _add_linkedin_search_candidates(
            result,
            company,
            [
                SearchResult(
                    query="q",
                    title="Ivan Ivanov - Mechanical Maintenance Engineer | LinkedIn",
                    url="https://www.linkedin.com/in/ivan-ivanov-976848103",
                    snippet=(
                        "Mechanical Maintenance Engineer at Lufthansa Technik. "
                        "Other similar profiles. Krastyo Karamanski. "
                        "Technical Director at RUBIN TRADING AD."
                    ),
                    provider="searxng",
                ),
                SearchResult(
                    query="q",
                    title="Vladimir Valev - Technical Director at Rubin Trading AD | LinkedIn",
                    url="https://www.linkedin.com/in/vladimir-valev-1234",
                    snippet="Technical Director at Rubin Trading AD",
                    provider="searxng",
                ),
            ],
        )

        self.assertEqual(
            [item["full_name"] for item in result["unverified_candidates"]],
            ["Vladimir Valev"],
        )

    def test_crm_dedupe_still_removes_broad_experiment_candidates(self):
        from key_person_discovery.pipeline import (
            _add_linkedin_search_candidates,
            _deduplicate_crm_contacts,
        )

        company = CompanyProfile(name="Example Minerals", website="https://example.com")
        profile = "https://de.linkedin.com/in/jane-doe?trk=search"
        result = {"candidates": [], "unverified_candidates": []}
        _add_linkedin_search_candidates(
            result,
            company,
            [
                SearchResult(
                    query="q",
                    title="Jane Doe - Purchasing Manager at Example Minerals",
                    url=profile,
                    snippet="Current Purchasing Manager at Example Minerals",
                    provider="searxng",
                )
            ],
        )

        _deduplicate_crm_contacts(
            result,
            [
                {
                    "name": "Jane Doe",
                    "email": "",
                    "phone": "",
                    "linkedin": "https://www.linkedin.com/in/jane-doe/",
                }
            ],
        )

        self.assertEqual(result["unverified_candidates"], [])
        self.assertEqual(result["crm_duplicates_removed"], 1)
        self.assertEqual(result["crm_duplicates"][0]["kind"], "unverified_candidate")

    def test_broad_normalization_demotes_stale_pdf_only_role_evidence(self):
        from key_person_discovery.pipeline import _normalize_broad_discovery_candidates

        company = CompanyProfile(name="Example Minerals", website="https://example.com")
        result = {
            "candidates": [
                {
                    "full_name": "Old Officer",
                    "current_title": "Legal Counsel",
                    "company_match": "verified",
                    "evidence": [
                        {
                            "source_url": "https://example.com/reports/manual-2020.pdf",
                            "quote": "Old Officer, Legal Counsel",
                            "supports": "Legal Counsel at Example Minerals",
                        }
                    ],
                }
            ],
            "unverified_candidates": [],
        }

        _normalize_broad_discovery_candidates(result, company, [])

        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["unverified_candidates"]), 1)
        candidate = result["unverified_candidates"][0]
        self.assertEqual(candidate["company_match"], "probable")
        self.assertEqual(candidate["discovery_tier"], "unverified")
        self.assertIn("stale PDF", " ".join(candidate["validation_reasons"]))


if __name__ == "__main__":
    unittest.main()
