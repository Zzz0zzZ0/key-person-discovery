from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROJECT_DIR = Path(__file__).resolve().parents[2]
ACTIVE_STATUSES = ("queued", "running")

_PHONE_REGIONS = {
    "japan": "JP",
    "日本": "JP",
    "vietnam": "VN",
    "越南": "VN",
    "korea": "KR",
    "south korea": "KR",
    "韩国": "KR",
    "韓國": "KR",
    "russia": "RU",
    "russian federation": "RU",
    "俄罗斯": "RU",
    "spain": "ES",
    "西班牙": "ES",
    "thailand": "TH",
    "泰国": "TH",
}


def project_storage_path(path: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_DIR)
    except ValueError as exc:
        raise ValueError(f"{label} must be inside the Key Search project: {PROJECT_DIR}") from exc
    return resolved


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS discovery_job (
                  id TEXT PRIMARY KEY,
                  company_key TEXT NOT NULL,
                  crm_company_id TEXT,
                  company_name TEXT NOT NULL,
                  website TEXT NOT NULL DEFAULT '',
                  source TEXT NOT NULL,
                  batch_id TEXT,
                  status TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed')),
                  stage TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  started_at TEXT,
                  heartbeat_at TEXT,
                  finished_at TEXT,
                  run TEXT NOT NULL,
                  input_path TEXT NOT NULL,
                  output_path TEXT NOT NULL,
                  log_path TEXT NOT NULL,
                  phone_region TEXT,
                  runner_pid INTEGER,
                  error TEXT
                );
                CREATE INDEX IF NOT EXISTS discovery_job_created_idx
                  ON discovery_job(created_at DESC);
                CREATE INDEX IF NOT EXISTS discovery_job_company_idx
                  ON discovery_job(company_key);
                CREATE UNIQUE INDEX IF NOT EXISTS discovery_job_one_active_company
                  ON discovery_job(company_key)
                  WHERE status IN ('queued','running');

                CREATE TABLE IF NOT EXISTS batch_run (
                  id TEXT PRIMARY KEY,
                  status TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
                  stage TEXT NOT NULL DEFAULT 'running',
                  started_at TEXT NOT NULL,
                  deadline_at TEXT NOT NULL,
                  heartbeat_at TEXT NOT NULL,
                  finished_at TEXT,
                  workers INTEGER NOT NULL,
                  duration_seconds REAL NOT NULL DEFAULT 172800,
                  active_seconds REAL NOT NULL DEFAULT 0,
                  cursor TEXT NOT NULL DEFAULT '',
                  scanned INTEGER NOT NULL DEFAULT 0,
                  enqueued INTEGER NOT NULL DEFAULT 0,
                  skipped INTEGER NOT NULL DEFAULT 0,
                  error TEXT
                );
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(batch_run)").fetchall()
            }
            if "stage" not in columns:
                connection.execute(
                    "ALTER TABLE batch_run ADD COLUMN stage TEXT NOT NULL DEFAULT 'running'"
                )
            if "duration_seconds" not in columns:
                connection.execute(
                    "ALTER TABLE batch_run ADD COLUMN duration_seconds REAL NOT NULL DEFAULT 172800"
                )
            if "active_seconds" not in columns:
                connection.execute(
                    "ALTER TABLE batch_run ADD COLUMN active_seconds REAL NOT NULL DEFAULT 0"
                )
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def insert(self, job: dict[str, Any]) -> None:
        columns = tuple(job)
        placeholders = ",".join("?" for _ in columns)
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO discovery_job ({','.join(columns)}) VALUES ({placeholders})",
                tuple(job[column] for column in columns),
            )

    def find_active(self, company_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM discovery_job WHERE company_key = ? AND status IN ('queued','running') "
                "ORDER BY created_at DESC LIMIT 1",
                (company_key,),
            ).fetchone()
        return self._public(row) if row else None

    def has_company(self, company_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM discovery_job WHERE company_key = ? AND status != 'failed' LIMIT 1",
                (company_key,),
            ).fetchone()
        return row is not None

    def get(self, job_id: str) -> dict[str, Any]:
        row = self.get_raw(job_id)
        if not row:
            raise LookupError("Discovery job not found")
        return self._public(row)

    def get_raw(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM discovery_job WHERE id = ?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("Job limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM discovery_job ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._public(row) for row in rows]

    def active_count(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT count(*) FROM discovery_job WHERE status IN ('queued','running')"
                ).fetchone()[0]
            )

    def noncompleted_output_paths(self) -> set[Path]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT output_path FROM discovery_job "
                "WHERE status != 'completed'"
            ).fetchall()
        return {Path(str(row[0])).resolve() for row in rows}

    def claim(self, job_id: str, runner_pid: int) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE discovery_job SET status='running', stage='starting', started_at=?, "
                "heartbeat_at=?, runner_pid=? WHERE id=? AND status='queued'",
                (now, now, runner_pid, job_id),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM discovery_job WHERE id=?",
                (job_id,),
            ).fetchone()
        return dict(row)

    def heartbeat(self, job_id: str, stage: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE discovery_job SET heartbeat_at=?, stage=? WHERE id=? AND status='running'",
                (utc_now(), stage, job_id),
            )

    def finish(
        self,
        job_id: str,
        *,
        completed: bool,
        error: str | None = None,
        stage: str | None = None,
    ) -> None:
        status = "completed" if completed else "failed"
        final_stage = stage or ("completed" if completed else "failed")
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE discovery_job SET status=?, stage=?, heartbeat_at=?, finished_at=?, error=? "
                "WHERE id=?",
                (status, final_stage, now, now, error, job_id),
            )

    def fail_stale(self, minutes: int = 5) -> int:
        threshold = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE discovery_job SET status='failed', stage='failed', finished_at=?, "
                "error='Runner heartbeat expired' WHERE status='running' AND heartbeat_at < ?",
                (now, threshold),
            )
        return cursor.rowcount

    def create_batch(self, *, hours: float, workers: int) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        batch = {
            "id": uuid.uuid4().hex[:12],
            "status": "running",
            "stage": "running",
            "started_at": started.isoformat(),
            "deadline_at": (started + timedelta(hours=hours)).isoformat(),
            "heartbeat_at": started.isoformat(),
            "workers": workers,
            "duration_seconds": hours * 3600,
        }
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO batch_run "
                "(id,status,stage,started_at,deadline_at,heartbeat_at,workers,duration_seconds) "
                "VALUES (:id,:status,:stage,:started_at,:deadline_at,:heartbeat_at,:workers,:duration_seconds)",
                batch,
            )
        return batch

    def update_batch(self, batch_id: str, **values: Any) -> None:
        allowed = {
            "status",
            "stage",
            "cursor",
            "scanned",
            "enqueued",
            "skipped",
            "error",
            "finished_at",
        }
        values = {key: value for key, value in values.items() if key in allowed}
        values["heartbeat_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in values)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE batch_run SET {assignments} WHERE id=?",
                (*values.values(), batch_id),
            )

    def add_batch_active_seconds(self, batch_id: str, seconds: float) -> None:
        seconds = max(0.0, seconds)
        with self._connect() as connection:
            connection.execute(
                "UPDATE batch_run SET active_seconds=MIN(duration_seconds, active_seconds + ?), "
                "heartbeat_at=? WHERE id=? AND status='running'",
                (seconds, utc_now(), batch_id),
            )

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM batch_run WHERE id=?",
                (batch_id,),
            ).fetchone()
        if not row:
            raise LookupError("Batch not found")
        return self._batch_public(row)

    def list_batches(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM batch_run ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._batch_public(row) for row in rows]

    @staticmethod
    def _public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        start = value.get("started_at") or value["created_at"]
        end = value.get("finished_at") or utc_now()
        try:
            elapsed = max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))
        except ValueError:
            elapsed = 0
        return {
            key: value.get(key)
            for key in (
                "id",
                "crm_company_id",
                "company_name",
                "website",
                "source",
                "batch_id",
                "status",
                "stage",
                "created_at",
                "started_at",
                "heartbeat_at",
                "finished_at",
                "run",
                "output_file",
                "error",
            )
        } | {"output_file": Path(value["output_path"]).name, "elapsed_seconds": elapsed}

    @staticmethod
    def _batch_public(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        now = datetime.now(timezone.utc)
        active = max(0.0, float(value.get("active_seconds") or 0))
        duration = max(0.0, float(value.get("duration_seconds") or 0))
        value["active_seconds"] = int(active)
        value["duration_seconds"] = int(duration)
        value["remaining_seconds"] = max(0, int(duration - active))
        heartbeat = datetime.fromisoformat(value["heartbeat_at"])
        value["heartbeat_age_seconds"] = max(0, int((now - heartbeat).total_seconds()))
        if value["status"] == "running" and value["heartbeat_age_seconds"] > 10:
            value["stage"] = "paused"
        return value


def enqueue_company(
    store: JobStore,
    company: dict[str, Any],
    *,
    outputs_dir: Path,
    inputs_dir: Path,
    profile_path: Path,
    source: str,
    batch_id: str | None = None,
    skip_existing: bool = False,
) -> tuple[dict[str, Any], bool]:
    name = str(company.get("name") or "").strip()
    website = normalize_website(company.get("website"))
    if not name or len(name) > 200:
        raise ValueError("Company name is required and must be at most 200 characters")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(profile, dict) or not isinstance(profile.get("industries", []), list) or not isinstance(profile.get("products", []), list):
        raise ValueError("Discovery profile is invalid")
    company_key = str(company.get("id") or f"manual:{name.casefold()}:{website}")
    active = store.find_active(company_key)
    if active:
        return active, False
    if skip_existing and store.has_company(company_key):
        raise LookupError("Company already has a discovery job")

    job_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    run = f"{'batch' if batch_id else 'web'}-{now:%Y%m%d}"
    filename = f"{slug(name)}-{job_id}.json"
    run_inputs = inputs_dir.resolve() / run
    run_outputs = outputs_dir.resolve() / run
    run_inputs.mkdir(parents=True, exist_ok=True)
    run_outputs.mkdir(parents=True, exist_ok=True)
    input_path = run_inputs / filename
    output_path = run_outputs / filename
    log_path = input_path.with_suffix(".log")
    phone_region = _phone_region(company.get("country"))
    raw_contact_count = company.get("contact_count")
    contact_count = int(raw_contact_count) if raw_contact_count is not None else None
    target_contact_count = 4 if contact_count is not None and contact_count < 4 else 1
    snapshot = {
        "name": name,
        "website": website,
        "industries": profile.get("industries", []),
        "products": profile.get("products", []),
        "crm_company_id": company.get("id"),
        "crm_linkedin_url": company.get("linkedin_url"),
        "crm_country": company.get("country"),
        "crm_updated_at": company.get("updated_at"),
        "crm_contact_count": contact_count,
        "crm_contacts": company.get("contacts") if isinstance(company.get("contacts"), list) else [],
        "target_contact_count": target_contact_count,
        "customs_search_enabled": company.get("customs_search_enabled") is True,
        "fetched_at": now.isoformat(),
        "source": "twenty_postgresql_read_only" if company.get("id") else "manual",
    }
    input_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    input_path.chmod(0o600)
    job = {
        "id": job_id,
        "company_key": company_key,
        "crm_company_id": company.get("id"),
        "company_name": name,
        "website": website,
        "source": source,
        "batch_id": batch_id,
        "status": "queued",
        "stage": "queued",
        "created_at": now.isoformat(),
        "run": run,
        "input_path": str(input_path),
        "output_path": str(output_path),
        "log_path": str(log_path),
        "phone_region": phone_region,
    }
    try:
        store.insert(job)
    except sqlite3.IntegrityError:
        active = store.find_active(company_key)
        if active:
            input_path.unlink(missing_ok=True)
            return active, False
        raise
    return store.get(job_id), True


def _phone_region(value: Any) -> str | None:
    country = " ".join(str(value or "").strip().split())
    if re.fullmatch(r"[A-Za-z]{2}", country):
        return country.upper()
    # ponytail: observed CRM labels only; replace with canonical CRM country codes
    # when the source is normalized.
    return _PHONE_REGIONS.get(country.casefold())


def spawn_runner(store: JobStore, job_id: str) -> None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("TWENTY_")}
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "key_person_discovery.job_runner",
                "--db",
                str(store.path),
                "--job-id",
                job_id,
            ],
            cwd=PROJECT_DIR,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        store.finish(job_id, completed=False, error="Unable to start discovery runner")
        raise


def normalize_website(value: Any) -> str:
    website = str(value or "").strip()
    if not website:
        return ""
    if "://" not in website:
        website = "https://" + website
    parsed = urlparse(website)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Website must be a valid HTTP(S) URL")
    return website


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:60]
    return result or "company-" + hashlib.sha256(value.encode()).hexdigest()[:10]
