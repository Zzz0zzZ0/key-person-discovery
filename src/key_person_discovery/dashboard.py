from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .crm import CrmError, get_company, load_env_file, search_companies
from .jobs import JobStore, PROJECT_DIR, enqueue_company, project_storage_path, spawn_runner


def load_results(
    outputs_dir: Path,
    excluded_paths: set[Path] | None = None,
) -> list[dict[str, Any]]:
    results: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    excluded_paths = excluded_paths or set()
    for path in outputs_dir.glob("*/*.json") if outputs_dir.is_dir() else []:
        if path.name == "summary.json" or path.resolve() in excluded_paths:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            modified_ns = path.stat().st_mtime_ns
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or not data.get("company_name"):
            continue
        item = {
            "run": path.parent.name,
            "file": path.name,
            "company_name": data["company_name"],
            "data": data,
        }
        key = (item["run"], item["company_name"])
        if key not in results or modified_ns > results[key][0]:
            results[key] = (modified_ns, item)
    return sorted(
        (item for _, item in results.values()),
        key=lambda item: (item["run"], item["company_name"]),
        reverse=True,
    )


class DiscoveryJobs:
    def __init__(
        self,
        outputs_dir: Path,
        inputs_dir: Path,
        profile_path: Path,
        db_path: Path | None = None,
    ) -> None:
        self.outputs_dir = outputs_dir.resolve()
        self.inputs_dir = inputs_dir.resolve()
        self.profile_path = profile_path.resolve()
        self.store = JobStore(db_path or PROJECT_DIR / "state" / "jobs.sqlite3")
        self.store.fail_stale()

    def start(self, company: dict[str, Any]) -> dict[str, Any]:
        job, created = enqueue_company(
            self.store,
            company,
            outputs_dir=self.outputs_dir,
            inputs_dir=self.inputs_dir,
            profile_path=self.profile_path,
            source="dashboard",
        )
        if created:
            spawn_runner(self.store, str(job["id"]))
        return job

    def get(self, job_id: str) -> dict[str, Any]:
        return self.store.get(job_id)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list_jobs(limit)

    def batches(self) -> list[dict[str, Any]]:
        return self.store.list_batches()


def make_handler(outputs_dir: Path, index_path: Path, jobs: DiscoveryJobs):
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/results":
                self._json(200, load_results(outputs_dir, jobs.store.noncompleted_output_paths()))
                return
            if parsed.path == "/api/crm/companies":
                try:
                    params = parse_qs(parsed.query)
                    query = params.get("q", [""])[0]
                    limit = int(params.get("limit", ["20"])[0])
                    self._json(200, search_companies(query, limit))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                except CrmError as exc:
                    self._json(503, {"error": str(exc)})
                return
            if parsed.path == "/api/jobs":
                try:
                    limit = int(parse_qs(parsed.query).get("limit", ["100"])[0])
                    self._json(200, jobs.list(limit))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                return
            if parsed.path == "/api/batches":
                self._json(200, jobs.batches())
                return
            if parsed.path.startswith("/api/jobs/"):
                try:
                    self._json(200, jobs.get(parsed.path.rsplit("/", 1)[-1]))
                except LookupError as exc:
                    self._json(404, {"error": str(exc)})
                return
            if parsed.path in {"/", "/index.html"}:
                try:
                    content = index_path.read_bytes()
                except OSError:
                    self.send_error(500, "Dashboard HTML is unavailable")
                    return
                self._send(200, content, "text/html; charset=utf-8")
                return
            self.send_error(404)

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/discover":
                self.send_error(404)
                return
            if not self._same_origin():
                self._json(403, {"error": "Cross-origin requests are not allowed"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16_384:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON object required")
                if payload.get("crm_company_id"):
                    company = get_company(str(payload["crm_company_id"]))
                else:
                    company = {"name": payload.get("name"), "website": payload.get("website")}
                self._json(202, jobs.start(company))
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            except LookupError as exc:
                self._json(404, {"error": str(exc)})
            except CrmError as exc:
                self._json(503, {"error": str(exc)})
            except OSError:
                self._json(500, {"error": "Unable to create discovery job"})

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            return not origin or urlparse(origin).netloc == self.headers.get("Host")

        def _json(self, status: int, payload: Any) -> None:
            self._send(
                status,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )

        def _send(self, status: int, content: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'",
            )
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: object) -> None:
            return

    return DashboardHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Display and start Key Person discovery jobs")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18181)
    parser.add_argument("--outputs", type=Path, default=PROJECT_DIR / "outputs")
    parser.add_argument("--inputs", type=Path, default=PROJECT_DIR / "inputs")
    parser.add_argument("--profile", type=Path, default=PROJECT_DIR / "examples" / "aceler.json")
    parser.add_argument("--db", type=Path, default=PROJECT_DIR / "state" / "jobs.sqlite3")
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        outputs_dir = project_storage_path(args.outputs, "--outputs")
        inputs_dir = project_storage_path(args.inputs, "--inputs")
        db_path = project_storage_path(args.db, "--db")
    except ValueError as exc:
        parser.error(str(exc))
    for env_file in args.env_file:
        load_env_file(env_file)
    jobs = DiscoveryJobs(outputs_dir, inputs_dir, args.profile, db_path)
    index_path = PROJECT_DIR / "web" / "index.html"
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(outputs_dir, index_path, jobs),
    )
    print(f"Key Person dashboard: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
