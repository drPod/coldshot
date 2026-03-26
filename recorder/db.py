from __future__ import annotations

import csv
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_CSV_PATH = _PROJECT_ROOT / "data" / "outreach.csv"

_CSV_COLUMNS = [
    "date",
    "person_name",
    "person_title",
    "org_name",
    "org_domain",
    "email",
    "subject",
    "body",
    "status",
]

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    pipeline_stage TEXT,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS api_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL REFERENCES sessions(id),
    ts             TEXT NOT NULL,
    endpoint       TEXT NOT NULL,
    request_body   TEXT NOT NULL,
    response_body  TEXT NOT NULL,
    status_code    INTEGER NOT NULL,
    credits_used   INTEGER,
    latency_ms     INTEGER NOT NULL,
    error          TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL REFERENCES sessions(id),
    ts             TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt         TEXT NOT NULL,
    response       TEXT NOT NULL,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    latency_ms     INTEGER NOT NULL,
    org_domain     TEXT,
    person_id      INTEGER,
    verdict        TEXT
);

CREATE TABLE IF NOT EXISTS outreach (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT REFERENCES sessions(id),
    ts              TEXT NOT NULL,
    org_name        TEXT NOT NULL,
    org_domain      TEXT NOT NULL,
    person_name     TEXT NOT NULL,
    person_title    TEXT,
    person_id       INTEGER,
    email           TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    gmail_msg_id    TEXT,
    status          TEXT NOT NULL DEFAULT 'sent',
    follow_up_at    TEXT,
    follow_up_count INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS targets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT REFERENCES sessions(id),
    discovered_at       TEXT NOT NULL,
    org_name            TEXT NOT NULL,
    org_domain          TEXT NOT NULL,
    org_employee_count  INTEGER,
    person_name         TEXT NOT NULL,
    person_title        TEXT,
    person_level        TEXT,
    person_function     TEXT,
    person_id           INTEGER,
    person_linkedin     TEXT,
    person_location     TEXT,
    person_country      TEXT,
    sumble_url          TEXT,
    qualification       TEXT,
    targeting_reason    TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    outreach_id         INTEGER REFERENCES outreach(id),
    pain_points         TEXT,
    suggested_subject   TEXT
);

CREATE TABLE IF NOT EXISTS discovered_orgs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT REFERENCES sessions(id),
    discovered_at       TEXT NOT NULL,
    org_name            TEXT NOT NULL,
    org_domain          TEXT,
    employee_count      INTEGER,
    industry            TEXT,
    hq_country          TEXT,
    hq_state            TEXT,
    linkedin_url        TEXT,
    verdict             TEXT NOT NULL,
    reason              TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_to_csv(row: dict[str, str]) -> None:
    """Append one outreach row to the CSV log. Creates headers if file is new."""
    write_header = not _CSV_PATH.exists() or _CSV_PATH.stat().st_size == 0
    try:
        with _CSV_PATH.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
    except OSError:
        pass  # never kill the pipeline over a CSV write failure


class Recorder:
    """Records every pipeline interaction to a local SQLite database."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = _PROJECT_ROOT / "data" / "cold_sales.db"
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._lock = threading.Lock()
        self._session_id: str | None = None

    def _migrate(self) -> None:
        """Add columns that may be missing from older schemas."""
        for col, typ in [("pain_points", "TEXT"), ("suggested_subject", "TEXT")]:
            try:
                self._conn.execute(f"ALTER TABLE targets ADD COLUMN {col} {typ}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

    # ── session lifecycle ────────────────────────────────────────────

    def start_session(
        self, pipeline_stage: str = "", notes: str = ""
    ) -> str:
        with self._lock:
            sid = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO sessions (id, started_at, pipeline_stage, notes) "
                "VALUES (?, ?, ?, ?)",
                (sid, _now(), pipeline_stage, notes),
            )
            self._conn.commit()
            self._session_id = sid
            return sid

    def end_session(self) -> None:
        with self._lock:
            if self._session_id is None:
                return
            self._conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (_now(), self._session_id),
            )
            self._conn.commit()
            self._session_id = None

    # ── recording methods ────────────────────────────────────────────

    def record_api_call(
        self,
        *,
        endpoint: str,
        request_body: dict[str, Any],
        response_body: dict[str, Any] | None,
        status_code: int,
        latency_ms: int,
        credits_used: int | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO api_calls "
                    "(session_id, ts, endpoint, request_body, response_body, "
                    "status_code, credits_used, latency_ms, error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._session_id or "",
                        _now(),
                        endpoint,
                        json.dumps(request_body),
                        json.dumps(response_body or {}),
                        status_code,
                        credits_used,
                        latency_ms,
                        error,
                    ),
                )
                self._conn.commit()
            except sqlite3.Error:
                pass  # never kill the pipeline over a recording failure

    def record_llm_call(
        self,
        *,
        purpose: str,
        model: str,
        prompt: str,
        response: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int,
        org_domain: str | None = None,
        person_id: int | None = None,
        verdict: str | None = None,
    ) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO llm_calls "
                    "(session_id, ts, purpose, model, prompt, response, "
                    "input_tokens, output_tokens, latency_ms, org_domain, "
                    "person_id, verdict) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._session_id or "",
                        _now(),
                        purpose,
                        model,
                        prompt,
                        response,
                        input_tokens,
                        output_tokens,
                        latency_ms,
                        org_domain,
                        person_id,
                        verdict,
                    ),
                )
                self._conn.commit()
            except sqlite3.Error:
                pass

    def record_outreach(
        self,
        *,
        org_name: str,
        org_domain: str,
        person_name: str,
        person_title: str | None = None,
        person_id: int | None = None,
        email: str,
        subject: str,
        body: str,
        gmail_msg_id: str | None = None,
    ) -> int:
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO outreach "
                    "(session_id, ts, org_name, org_domain, person_name, "
                    "person_title, person_id, email, subject, body, gmail_msg_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._session_id or "",
                        _now(),
                        org_name,
                        org_domain,
                        person_name,
                        person_title,
                        person_id,
                        email,
                        subject,
                        body,
                        gmail_msg_id,
                    ),
                )
                self._conn.commit()
                _append_to_csv({
                    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "person_name": person_name,
                    "person_title": person_title or "",
                    "org_name": org_name,
                    "org_domain": org_domain,
                    "email": email,
                    "subject": subject,
                    "body": body,
                    "status": "sent",
                })
                return cur.lastrowid or 0
            except sqlite3.Error:
                return 0

    def update_outreach(
        self,
        outreach_id: int,
        *,
        status: str | None = None,
        follow_up_at: str | None = None,
        notes: str | None = None,
    ) -> None:
        sets: list[str] = []
        vals: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            vals.append(status)
        if follow_up_at is not None:
            sets.append("follow_up_at = ?")
            vals.append(follow_up_at)
        if notes is not None:
            sets.append("notes = ?")
            vals.append(notes)
        if not sets:
            return
        vals.append(outreach_id)
        with self._lock:
            try:
                self._conn.execute(
                    f"UPDATE outreach SET {', '.join(sets)} WHERE id = ?", vals
                )
                self._conn.commit()
            except sqlite3.Error:
                pass

    # ── discovered orgs ──────────────────────────────────────────────

    def record_discovered_org(
        self,
        *,
        org_name: str,
        org_domain: str | None = None,
        employee_count: int | None = None,
        industry: str | None = None,
        hq_country: str | None = None,
        hq_state: str | None = None,
        linkedin_url: str | None = None,
        verdict: str,
        reason: str = "",
    ) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO discovered_orgs "
                    "(session_id, discovered_at, org_name, org_domain, employee_count, "
                    "industry, hq_country, hq_state, linkedin_url, verdict, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._session_id or "",
                        _now(),
                        org_name,
                        org_domain,
                        employee_count,
                        industry,
                        hq_country,
                        hq_state,
                        linkedin_url,
                        verdict,
                        reason,
                    ),
                )
                self._conn.commit()
            except sqlite3.Error:
                pass

    # ── targets ───────────────────────────────────────────────────────

    def record_target(
        self,
        *,
        org_name: str,
        org_domain: str,
        org_employee_count: int | None = None,
        person_name: str,
        person_title: str | None = None,
        person_level: str | None = None,
        person_function: str | None = None,
        person_id: int | None = None,
        person_linkedin: str | None = None,
        person_location: str | None = None,
        person_country: str | None = None,
        sumble_url: str | None = None,
        qualification: str | None = None,
        targeting_reason: str | None = None,
    ) -> int:
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO targets "
                    "(session_id, discovered_at, org_name, org_domain, "
                    "org_employee_count, person_name, person_title, person_level, "
                    "person_function, person_id, person_linkedin, person_location, "
                    "person_country, sumble_url, qualification, targeting_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._session_id or "",
                        _now(),
                        org_name,
                        org_domain,
                        org_employee_count,
                        person_name,
                        person_title,
                        person_level,
                        person_function,
                        person_id,
                        person_linkedin,
                        person_location,
                        person_country,
                        sumble_url,
                        qualification,
                        targeting_reason,
                    ),
                )
                self._conn.commit()
                return cur.lastrowid or 0
            except sqlite3.Error:
                return 0

    def get_next_target(self) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.row_factory = sqlite3.Row
            cur.execute(
                "SELECT * FROM targets WHERE status = 'pending' ORDER BY id LIMIT 1"
            )
            row = cur.fetchone()
            if row is None:
                return None
            return dict(row)

    def update_target_research(
        self,
        target_id: int,
        *,
        pain_points: str | None = None,
        suggested_subject: str | None = None,
    ) -> None:
        """Update one or both research fields on a target."""
        sets: list[str] = []
        vals: list[Any] = []
        if pain_points is not None:
            sets.append("pain_points = ?")
            vals.append(pain_points)
        if suggested_subject is not None:
            sets.append("suggested_subject = ?")
            vals.append(suggested_subject)
        if not sets:
            return
        vals.append(target_id)
        with self._lock:
            try:
                self._conn.execute(
                    f"UPDATE targets SET {', '.join(sets)} WHERE id = ?", vals
                )
                self._conn.commit()
            except sqlite3.Error:
                pass

    def get_person_eval_cache(self, org_domain: str) -> dict[int, tuple[bool, str]]:
        """Cached person evaluations for an org: {person_id: (is_target, reasoning)}."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT person_id, verdict, response FROM llm_calls "
                "WHERE purpose = 'evaluate_person' "
                "AND org_domain = ? AND person_id IS NOT NULL",
                (org_domain,),
            )
            return {
                row[0]: (row[1] == "TARGET", row[2] or "")
                for row in cur.fetchall()
            }

    def get_ready_targets(self) -> list[dict[str, Any]]:
        """Pending targets that are fully researched but never shown to user."""
        with self._lock:
            cur = self._conn.cursor()
            cur.row_factory = sqlite3.Row
            cur.execute(
                "SELECT * FROM targets "
                "WHERE status = 'pending' "
                "AND pain_points IS NOT NULL "
                "AND suggested_subject IS NOT NULL"
            )
            return [dict(row) for row in cur.fetchall()]

    def get_targets_needing_research(self) -> list[dict[str, Any]]:
        """Targets missing pain points or subject (interrupted or failed)."""
        with self._lock:
            cur = self._conn.cursor()
            cur.row_factory = sqlite3.Row
            cur.execute(
                "SELECT * FROM targets "
                "WHERE status = 'pending' "
                "AND (pain_points IS NULL OR suggested_subject IS NULL)"
            )
            return [dict(row) for row in cur.fetchall()]

    def mark_target_emailed(self, target_id: int, outreach_id: int) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE targets SET status = 'emailed', outreach_id = ? "
                    "WHERE id = ?",
                    (outreach_id, target_id),
                )
                self._conn.commit()
            except sqlite3.Error:
                pass

    def mark_target_skipped(self, target_id: int) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE targets SET status = 'skipped' WHERE id = ?",
                    (target_id,),
                )
                self._conn.commit()
            except sqlite3.Error:
                pass

    def mark_target_drafted(self, target_id: int) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE targets SET status = 'drafted' WHERE id = ?",
                    (target_id,),
                )
                self._conn.commit()
            except sqlite3.Error:
                pass

    def get_stats(self) -> dict[str, int]:
        """Return pipeline statistics."""
        with self._lock:
            cur = self._conn.cursor()

            cur.execute("SELECT COUNT(*) FROM discovered_orgs")
            orgs_discovered = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM discovered_orgs WHERE verdict = 'YES'")
            orgs_qualified = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM targets")
            targets_found = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM targets WHERE status = 'emailed'")
            emails_sent = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM targets WHERE status = 'skipped'")
            targets_skipped = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM targets WHERE status = 'drafted'")
            targets_drafted = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM targets WHERE status = 'pending'")
            targets_pending = cur.fetchone()[0]

            return {
                "orgs_discovered": orgs_discovered,
                "orgs_qualified": orgs_qualified,
                "targets_found": targets_found,
                "emails_sent": emails_sent,
                "targets_skipped": targets_skipped,
                "targets_drafted": targets_drafted,
                "targets_pending": targets_pending,
            }

    def is_org_known(self, org_domain: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM targets WHERE org_domain = ? LIMIT 1",
                (org_domain,),
            )
            return cur.fetchone() is not None

    def get_known_domains(self) -> set[str]:
        """Domains from both discovered_orgs and targets — avoids re-qualifying."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT DISTINCT org_domain FROM discovered_orgs "
                "WHERE org_domain IS NOT NULL "
                "UNION "
                "SELECT DISTINCT org_domain FROM targets "
                "WHERE org_domain IS NOT NULL"
            )
            return {row[0] for row in cur.fetchall()}

    def get_qualified_without_targets(self) -> list[dict[str, Any]]:
        """YES-verdict orgs that never made it to the targets table."""
        with self._lock:
            cur = self._conn.cursor()
            cur.row_factory = sqlite3.Row
            cur.execute(
                "SELECT d.org_name, d.org_domain, d.employee_count, d.reason "
                "FROM discovered_orgs d "
                "LEFT JOIN targets t ON d.org_domain = t.org_domain "
                "WHERE d.verdict = 'YES' "
                "AND d.org_domain IS NOT NULL "
                "AND t.id IS NULL "
                "GROUP BY d.org_domain"
            )
            return [dict(row) for row in cur.fetchall()]

    def get_outreach_id_by_gmail_msg(self, gmail_msg_id: str) -> int | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id FROM outreach WHERE gmail_msg_id = ?",
                (gmail_msg_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    # ── lifecycle ────────────────────────────────────────────────────

    def close(self) -> None:
        self.end_session()
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Recorder:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
