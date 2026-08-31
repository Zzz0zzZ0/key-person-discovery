from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

from .crm import list_companies, load_env_file
from .jobs import (
    JobStore,
    PROJECT_DIR,
    enqueue_company,
    project_storage_path,
    spawn_runner,
    utc_now,
)


MAX_ACTIVE_GAP_SECONDS = 30.0
RECOVERY_GRACE_SECONDS = 10.0


def _active_delta(previous: datetime, current: datetime) -> float:
    elapsed = (current - previous).total_seconds()
    return elapsed if 0 <= elapsed <= MAX_ACTIVE_GAP_SECONDS else 0.0


def run_batch(
    *,
    store: JobStore,
    outputs_dir: Path,
    inputs_dir: Path,
    profile_path: Path,
    duration_hours: float,
    workers: int,
    page_size: int,
    max_companies: int,
) -> dict[str, object]:
    batch = store.create_batch(hours=duration_hours, workers=workers)
    batch_id = str(batch["id"])
    previous_tick = datetime.now(timezone.utc)
    cursor = ""
    buffer: list[dict[str, object]] = []
    scanned = enqueued = skipped = 0
    exhausted = False
    try:
        while not max_companies or enqueued < max_companies:
            current_tick = datetime.now(timezone.utc)
            elapsed = (current_tick - previous_tick).total_seconds()
            active_delta = _active_delta(previous_tick, current_tick)
            previous_tick = current_tick
            store.add_batch_active_seconds(batch_id, active_delta)
            if store.get_batch(batch_id)["remaining_seconds"] <= 0:
                break
            if elapsed > MAX_ACTIVE_GAP_SECONDS:
                store.update_batch(batch_id, stage="recovering")
                time.sleep(RECOVERY_GRACE_SECONDS)
                continue
            store.update_batch(batch_id, stage="running")
            store.fail_stale()
            while store.active_count() < workers and (not max_companies or enqueued < max_companies):
                if not buffer and not exhausted:
                    buffer = list_companies(cursor, page_size)
                    exhausted = not buffer
                if not buffer:
                    break
                company = buffer.pop(0)
                cursor = str(company["id"])
                scanned += 1
                if store.has_company(cursor):
                    skipped += 1
                    continue
                job, created = enqueue_company(
                    store,
                    company,
                    outputs_dir=outputs_dir,
                    inputs_dir=inputs_dir,
                    profile_path=profile_path,
                    source="batch",
                    batch_id=batch_id,
                    skip_existing=True,
                )
                if created:
                    spawn_runner(store, str(job["id"]))
                    enqueued += 1
                    print(f"[{enqueued}] {job['company_name']} ({job['id']})", flush=True)
            store.update_batch(
                batch_id,
                cursor=cursor,
                scanned=scanned,
                enqueued=enqueued,
                skipped=skipped,
            )
            if exhausted and store.active_count() == 0:
                break
            remaining = float(store.get_batch(batch_id)["remaining_seconds"])
            time.sleep(min(2.0, remaining))
        store.update_batch(batch_id, status="completed", stage="completed", finished_at=utc_now())
    except KeyboardInterrupt:
        store.update_batch(
            batch_id,
            status="failed",
            stage="failed",
            finished_at=utc_now(),
            error="Interrupted",
        )
        raise
    except Exception as exc:
        store.update_batch(
            batch_id,
            status="failed",
            stage="failed",
            finished_at=utc_now(),
            error=f"Batch failed ({type(exc).__name__})",
        )
        raise
    return store.list_batches(1)[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run CRM Key Person discovery for up to 48 hours")
    parser.add_argument("--duration-hours", type=float, default=48)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-companies", type=int, default=0, help="0 means no count limit")
    parser.add_argument("--dry-run", action="store_true", help="List the first CRM companies without creating jobs")
    parser.add_argument("--db", type=Path, default=PROJECT_DIR / "state" / "jobs.sqlite3")
    parser.add_argument("--outputs", type=Path, default=PROJECT_DIR / "outputs")
    parser.add_argument("--inputs", type=Path, default=PROJECT_DIR / "inputs")
    parser.add_argument("--profile", type=Path, default=PROJECT_DIR / "examples" / "aceler.json")
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    args = parser.parse_args()
    if not 0 < args.duration_hours <= 168:
        parser.error("--duration-hours must be greater than 0 and at most 168")
    if not 1 <= args.workers <= 4:
        parser.error("--workers must be between 1 and 4")
    if not 1 <= args.page_size <= 500:
        parser.error("--page-size must be between 1 and 500")
    if args.max_companies < 0:
        parser.error("--max-companies cannot be negative")
    try:
        db_path = project_storage_path(args.db, "--db")
        outputs_dir = project_storage_path(args.outputs, "--outputs")
        inputs_dir = project_storage_path(args.inputs, "--inputs")
    except ValueError as exc:
        parser.error(str(exc))
    for env_file in args.env_file:
        load_env_file(env_file)
    if args.dry_run:
        limit = min(args.page_size, args.max_companies or 10)
        for company in list_companies(limit=limit):
            print(f"{company['id']}\t{company['name']}\t{company['website']}")
        return
    result = run_batch(
        store=JobStore(db_path),
        outputs_dir=outputs_dir,
        inputs_dir=inputs_dir,
        profile_path=args.profile,
        duration_hours=args.duration_hours,
        workers=args.workers,
        page_size=args.page_size,
        max_companies=args.max_companies,
    )
    print(f"Batch {result['id']} {result['status']}: enqueued={result['enqueued']} skipped={result['skipped']}")


if __name__ == "__main__":
    main()
