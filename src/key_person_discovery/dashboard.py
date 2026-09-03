from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

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


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def create_app(outputs_dir: Path, index_path: Path, jobs: DiscoveryJobs) -> FastAPI:
    app = FastAPI(title="Key Person Discovery", docs_url=None, redoc_url=None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(400, exc.errors()[0]["msg"])

    @app.middleware("http")
    async def protect_dashboard(request: Request, call_next):
        response = None
        if request.method == "POST" and request.url.path == "/api/discover":
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.headers.get("Host"):
                response = _error(403, "Cross-origin requests are not allowed")
            else:
                try:
                    length = int(request.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if not 0 < length <= 16_384:
                    response = _error(400, "Invalid request size")
        if response is None:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'"
        )
        return response

    @app.get("/api/results")
    def results() -> list[dict[str, Any]]:
        return load_results(outputs_dir, jobs.store.noncompleted_output_paths())

    @app.get("/api/crm/companies")
    def crm_companies(q: str = "", limit: int = 20):
        try:
            return search_companies(q, limit)
        except ValueError as exc:
            return _error(400, str(exc))
        except CrmError as exc:
            return _error(503, str(exc))

    @app.get("/api/jobs")
    def list_jobs(limit: int = 100):
        try:
            return jobs.list(limit)
        except ValueError as exc:
            return _error(400, str(exc))

    @app.get("/api/batches")
    def batches():
        return jobs.batches()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        try:
            return jobs.get(job_id)
        except LookupError as exc:
            return _error(404, str(exc))

    @app.post("/api/discover", status_code=202)
    def discover_company(payload: dict[str, Any]):
        try:
            if payload.get("crm_company_id"):
                company = get_company(str(payload["crm_company_id"]))
            else:
                company = {"name": payload.get("name"), "website": payload.get("website")}
            return jobs.start(company)
        except ValueError as exc:
            return _error(400, str(exc))
        except LookupError as exc:
            return _error(404, str(exc))
        except CrmError as exc:
            return _error(503, str(exc))
        except OSError:
            return _error(500, "Unable to create discovery job")

    def index():
        if not index_path.is_file():
            return _error(500, "Dashboard HTML is unavailable")
        return FileResponse(index_path, media_type="text/html")

    app.get("/", include_in_schema=False)(index)
    app.get("/index.html", include_in_schema=False)(index)
    return app


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
    print(f"Key Person dashboard: http://{args.host}:{args.port}")
    uvicorn.run(create_app(outputs_dir, index_path, jobs), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
