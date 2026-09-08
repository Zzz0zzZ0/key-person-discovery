from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .jobs import JobStore, PROJECT_DIR
from .preflight import network_available


NETWORK_RETRY_SECONDS = 30
RECOVERY_GRACE_SECONDS = 10


def run_job(
    store: JobStore,
    job_id: str,
    network_check: Callable[[str], bool] | None = None,
) -> int:
    job = store.claim(job_id, os.getpid())
    if not job:
        return 0
    input_path = Path(job["input_path"])
    output_path = Path(job["output_path"])
    log_path = Path(job["log_path"])
    command = [
        sys.executable,
        "-m",
        "key_person_discovery.cli",
        "--company",
        str(input_path),
        "--output",
        str(output_path),
    ]
    if job.get("phone_region"):
        command.extend(["--phone-region", job["phone_region"]])
    try:
        profile = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        profile = {}
    if int(profile.get("target_contact_count") or 0) >= 4:
        command.extend(["--max-urls", "30"])
    if sys.platform == "darwin":
        command = ["/usr/bin/caffeinate", "-i", "-s", *command]
    network_check = network_check or network_available
    try:
        while True:
            store.heartbeat(job_id, "starting")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as log:
                process = subprocess.Popen(
                    command,
                    cwd=PROJECT_DIR,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            while process.poll() is None:
                store.heartbeat(job_id, _stage(job, log_path))
                time.sleep(2)
            if process.returncode != 0:
                if _wait_for_network(store, job_id, network_check):
                    continue
                store.finish(
                    job_id,
                    completed=False,
                    error=f"Discovery exited with code {process.returncode}",
                )
                return 1

            output_stage = _output_stage(output_path)
            if output_stage is None:
                store.finish(
                    job_id,
                    completed=False,
                    error="Discovery output missing or invalid",
                )
                return 1
            if output_stage == "source_limited":
                if _wait_for_network(store, job_id, network_check):
                    continue
                store.finish(
                    job_id,
                    completed=False,
                    stage=output_stage,
                    error="Public sources unavailable; retry required",
                )
                return 1
            store.finish(job_id, completed=True, stage=output_stage)
            return 0
    except Exception as exc:
        store.finish(job_id, completed=False, error=f"Runner failed ({type(exc).__name__})")
        return 1


def _wait_for_network(
    store: JobStore,
    job_id: str,
    network_check: Callable[[str], bool],
) -> bool:
    proxy_url = (
        os.getenv("KEY_PERSON_PROXY_URL", "").strip()
        or os.getenv("HTTPS_PROXY", "").strip()
        or os.getenv("HTTP_PROXY", "").strip()
    )
    if network_check(proxy_url):
        return False
    while True:
        store.heartbeat(job_id, "waiting_network")
        time.sleep(NETWORK_RETRY_SECONDS)
        if network_check(proxy_url):
            store.heartbeat(job_id, "recovering")
            time.sleep(RECOVERY_GRACE_SECONDS)
            return True


def _output_is_usable(path: Path) -> bool:
    value = _read_output(path)
    return bool(value and _value_is_usable(value))


def _output_stage(path: Path) -> str | None:
    value = _read_output(path)
    if value is None:
        return None
    if _value_is_usable(value):
        return "completed"
    diagnostic = value.get('diagnostics')
    if isinstance(diagnostic, dict):
        reason = diagnostic.get('primary')
        if reason in {'website_unverified', 'crawl_failed', 'no_evidence'}:
            return 'source_limited'
        if reason in {'target_met', 'missing_channels', 'employment_uncertain', 'crm_duplicate', 'no_target_people', 'insufficient_people'}:
            return 'no_new_contact'
    summary = value.get("run_summary")
    summary = summary if isinstance(summary, dict) else {}
    search_results = _summary_count(summary, "search_results")
    urls_crawled = _summary_count(summary, "urls_crawled")
    crawl_failures = _summary_count(summary, "crawl_failures")
    if (urls_crawled > 0 and crawl_failures >= urls_crawled) or (
        search_results == 0 and urls_crawled == 0
    ):
        return "source_limited"
    return "no_new_contact"


def _read_output(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def _value_is_usable(value: dict[str, object]) -> bool:
    if value.get("unverified_candidates"):
        return True
    if value.get("unassigned_contacts"):
        return True
    return any(
        isinstance(candidate, dict)
        and any(candidate.get(field) for field in ("linkedin", "emails", "phones"))
        for candidate in value.get("candidates", [])
    )


def _summary_count(summary: dict[str, object], key: str) -> int:
    try:
        return max(0, int(summary.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _stage(job: dict[str, object], log_path: Path) -> str:
    artifacts = Path(str(job["output_path"]) + ".artifacts")
    if (artifacts / "contact-signals.json").is_file():
        return "analyzing"
    try:
        with log_path.open("rb") as log:
            log.seek(max(0, log_path.stat().st_size - 16_384))
            tail = log.read().decode("utf-8", errors="ignore")
    except OSError:
        return "starting"
    if "[FETCH]" in tail or "[SCRAPE]" in tail:
        return "crawling"
    if "checkip" in tail or "Crawl4AI" in tail:
        return "preflight"
    return "searching"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one persistent Key Person discovery job")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    raise SystemExit(run_job(JobStore(args.db), args.job_id))


if __name__ == "__main__":
    main()
