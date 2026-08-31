from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class CrmError(RuntimeError):
    pass


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        try:
            values = shlex.split(raw_value, comments=True)
        except ValueError as exc:
            raise CrmError(f"Invalid environment value for {key}") from exc
        os.environ.setdefault(key, values[0] if values else "")


def search_companies(query: str, limit: int = 20) -> list[dict[str, Any]]:
    query = query.strip()
    if len(query) < 2:
        raise ValueError("Enter at least 2 characters")
    if len(query) > 120 or not 1 <= limit <= 50:
        raise ValueError("Invalid company search")
    sql = f'''
        SELECT json_build_object(
          'id', id::text,
          'name', name,
          'website', "domainNamePrimaryLinkUrl",
          'linkedin_url', "linkedinLinkPrimaryLinkUrl",
          'country', "addressAddressCountry",
          'updated_at', "updatedAt"
        )
        FROM "{_workspace_schema()}".company
        WHERE "deletedAt" IS NULL
          AND NULLIF(btrim(name), '') IS NOT NULL
          AND name ILIKE '%' || :'search' || '%' ESCAPE '\\'
        ORDER BY
          CASE
            WHEN lower(name) = lower(:'search') THEN 0
            WHEN name ILIKE :'search' || '%' ESCAPE '\\' THEN 1
            ELSE 2
          END,
          CASE WHEN NULLIF(btrim("domainNamePrimaryLinkUrl"), '') IS NULL THEN 1 ELSE 0 END,
          name,
          id::text
        LIMIT {limit}
    '''
    return _read_companies(sql, {"search": _escape_like(query)})


def get_company(company_id: str) -> dict[str, Any]:
    try:
        company_id = str(uuid.UUID(company_id))
    except ValueError:
        raise ValueError("Invalid CRM company id")
    sql = f'''
        SELECT json_build_object(
          'id', company.id::text,
          'name', company.name,
          'website', company."domainNamePrimaryLinkUrl",
          'linkedin_url', company."linkedinLinkPrimaryLinkUrl",
          'country', company."addressAddressCountry",
          'updated_at', company."updatedAt",
          'contact_count', (
            SELECT count(*) FROM "{_workspace_schema()}".person person
            WHERE person."companyId" = company.id AND person."deletedAt" IS NULL
          ),
          'contacts', {_contacts_sql('company')}
        )
        FROM "{_workspace_schema()}".company company
        WHERE company.id = :'company_id'::uuid AND company."deletedAt" IS NULL
        LIMIT 1
    '''
    rows = _read_companies(sql, {"company_id": company_id})
    if not rows:
        raise LookupError("CRM company not found")
    return rows[0]


def list_companies(after_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
    if not 1 <= limit <= 500:
        raise ValueError("CRM page size must be between 1 and 500")
    if after_id:
        try:
            after_id = str(uuid.UUID(after_id))
        except ValueError:
            raise ValueError("Invalid CRM company cursor")
    else:
        after_id = "00000000-0000-0000-0000-000000000000"
    sql = f'''
        SELECT json_build_object(
          'id', company.id::text,
          'name', company.name,
          'website', company."domainNamePrimaryLinkUrl",
          'linkedin_url', company."linkedinLinkPrimaryLinkUrl",
          'country', company."addressAddressCountry",
          'updated_at', company."updatedAt",
          'contact_count', (
            SELECT count(*) FROM "{_workspace_schema()}".person person
            WHERE person."companyId" = company.id AND person."deletedAt" IS NULL
          ),
          'contacts', {_contacts_sql('company')}
        )
        FROM "{_workspace_schema()}".company company
        WHERE company."deletedAt" IS NULL
          AND company.id > :'after_id'::uuid
          AND NULLIF(btrim(company.name), '') IS NOT NULL
          AND NULLIF(btrim(company."domainNamePrimaryLinkUrl"), '') IS NOT NULL
        ORDER BY company.id
        LIMIT {limit}
    '''
    return _read_companies(sql, {"after_id": after_id})


def _read_companies(sql: str, variables: dict[str, str]) -> list[dict[str, Any]]:
    try:
        rows = _run_psql(sql, variables)
    except CrmError:
        raise
    except Exception as exc:
        raise CrmError(f"CRM query failed ({type(exc).__name__})") from exc
    for row in rows:
        row["website"] = _normalize_website(row.get("website"))
    return rows


def _contacts_sql(company_alias: str) -> str:
    return f'''(
        SELECT COALESCE(json_agg(json_build_object(
          'name', concat_ws(' ', person."nameFirstName", person."nameLastName"),
          'email', person."emailsPrimaryEmail",
          'additional_emails', person."emailsAdditionalEmails",
          'phone', person."phonesPrimaryPhoneNumber",
          'additional_phones', person."phonesAdditionalPhones",
          'linkedin', person."linkedinLinkPrimaryLinkUrl",
          'additional_linkedin', person."linkedinLinkSecondaryLinks"
        )), '[]'::json)
        FROM "{_workspace_schema()}".person person
        WHERE person."companyId" = {company_alias}.id
          AND person."deletedAt" IS NULL
    )'''


def _run_psql(sql: str, variables: dict[str, str]) -> list[dict[str, Any]]:
    required = ("TWENTY_DB_HOST", "TWENTY_DB_NAME", "TWENTY_DB_USER")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise CrmError("Missing CRM configuration: " + ", ".join(missing))
    psql = os.getenv("PSQL_BIN") or shutil.which("psql")
    if not psql:
        homebrew_psql = Path("/opt/homebrew/opt/libpq/bin/psql")
        psql = str(homebrew_psql) if homebrew_psql.is_file() else ""
    if not psql:
        raise CrmError("psql client is unavailable")
    command = [psql, "-X", "-qAt", "-v", "ON_ERROR_STOP=1"]
    for name, value in variables.items():
        command.extend(["-v", f"{name}={value}"])
    command.extend(
        [
            "-h",
            os.environ["TWENTY_DB_HOST"],
            "-p",
            os.getenv("TWENTY_DB_PORT", "5432"),
            "-U",
            os.environ["TWENTY_DB_USER"],
            "-d",
            os.environ["TWENTY_DB_NAME"],
        ]
    )
    environment = os.environ.copy()
    environment["PGPASSWORD"] = os.getenv("TWENTY_DB_PASSWORD", "")
    environment["PGSSLMODE"] = os.getenv("TWENTY_DB_SSLMODE", "prefer")
    environment["PGCONNECT_TIMEOUT"] = os.getenv("TWENTY_DB_CONNECT_TIMEOUT", "5")
    result = subprocess.run(
        command,
        input=f"BEGIN READ ONLY; SET LOCAL statement_timeout = '10s'; {sql}; COMMIT;",
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise CrmError("CRM query failed (psql)")
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def _workspace_schema() -> str:
    schema = os.getenv("TWENTY_WORKSPACE_SCHEMA", "").strip()
    if not re.fullmatch(r"workspace_[a-z0-9]+", schema):
        raise CrmError("TWENTY_WORKSPACE_SCHEMA is missing or invalid")
    return schema


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_website(value: Any) -> str:
    website = str(value or "").strip()
    if not website:
        return ""
    if "://" not in website:
        website = "https://" + website
    parsed = urlparse(website)
    return website if parsed.scheme in {"http", "https"} and parsed.hostname else ""
