"""SQLite storage for leads and search jobs."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

DB_PATH = Path(__file__).parent / "leads.db"

LEAD_FIELDS = ["kind", "name", "title", "company", "location", "website", "email", "phone",
               "linkedin", "twitter", "url", "profile", "size", "snippet",
               "funding", "signal", "people", "posted_at", "budget"]
EDITABLE = {"status", "notes", "email", "phone", "name", "company", "title"}
STATUSES = ["new", "contacted", "replied", "qualified", "won", "rejected"]

_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    with connect() as c:
        c.executescript(f"""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT UNIQUE,
            source TEXT, query TEXT, campaign TEXT,
            {", ".join(f"{f} TEXT DEFAULT ''" for f in LEAD_FIELDS)},
            status TEXT DEFAULT 'new', notes TEXT DEFAULT '',
            enriched INTEGER DEFAULT 0,
            created_at REAL, updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads(campaign);
        CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, campaign TEXT, params TEXT,
            status TEXT DEFAULT 'queued',
            done INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
            found INTEGER DEFAULT 0, added INTEGER DEFAULT 0,
            log TEXT DEFAULT '', created_at REAL, finished_at REAL
        );
        """)
        have = {r[1] for r in c.execute("PRAGMA table_info(leads)")}
        for f in LEAD_FIELDS:
            if f not in have:
                c.execute(f"ALTER TABLE leads ADD COLUMN {f} TEXT DEFAULT ''")
        if "resolved" not in have:
            c.execute("ALTER TABLE leads ADD COLUMN resolved INTEGER DEFAULT 0")
        # Jobs that were running when the server stopped will never finish.
        c.execute("UPDATE jobs SET status='interrupted' WHERE status IN ('queued','running')")


def _domain(url: str) -> str:
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def dedupe_key(lead: dict) -> str:
    kind = lead.get("kind")
    if kind == "funded":
        return "fn:" + re.sub(r"[^a-z0-9]", "", lead.get("company", "").lower())
    if kind == "company" and lead.get("website"):
        return "co:" + _domain(lead["website"])
    if kind == "person" and lead.get("linkedin"):
        return "li:" + lead["linkedin"].rstrip("/").lower().split("?")[0]
    if kind == "post" and lead.get("profile"):
        return "pr:" + lead["profile"].lower()
    return "url:" + (lead.get("url") or lead.get("name", "")).rstrip("/").lower()


def add_leads(leads: list[dict], *, source: str, query: str, campaign: str) -> int:
    added, now = 0, time.time()
    with _lock, connect() as c:
        for lead in leads:
            row = {f: str(lead.get(f) or "") for f in LEAD_FIELDS}
            cur = c.execute(
                f"INSERT OR IGNORE INTO leads (dedupe_key, source, query, campaign, "
                f"{', '.join(LEAD_FIELDS)}, created_at, updated_at) VALUES "
                f"(?, ?, ?, ?, {', '.join('?' * len(LEAD_FIELDS))}, ?, ?)",
                [dedupe_key(lead), source, query, campaign, *row.values(), now, now])
            added += cur.rowcount
    return added


def _where(filters: dict) -> tuple[str, list]:
    clauses, args = [], []
    if filters.get("q"):
        like = f"%{filters['q']}%"
        clauses.append("(name LIKE ? OR title LIKE ? OR company LIKE ? OR location LIKE ? "
                       "OR email LIKE ? OR snippet LIKE ? OR notes LIKE ? OR signal LIKE ? OR people LIKE ?)")
        args += [like] * 9
    for col in ("campaign", "status", "source", "kind"):
        if filters.get(col):
            # source / kind accept "a,b" (either); campaign names may contain commas.
            vals = [v for v in str(filters[col]).split(",") if v] if col in ("source", "kind") \
                else [filters[col]]
            if vals:  # "," alone would otherwise build an invalid "IN ()"
                clauses.append(f"{col} IN ({', '.join('?' * len(vals))})")
                args += vals
    if filters.get("city"):
        # India leads record the searched city in location, or in signal ("Hiring in Noida (Naukri)").
        # A funded lead's signal is a news headline, which can name other cities, so only location counts.
        clauses.append("(location = ? OR (kind != 'funded' AND signal LIKE ?))")
        args += [filters["city"], f"%in {filters['city']}%"]
    if filters.get("has") == "email":
        clauses.append("email != ''")
    elif filters.get("has") == "phone":
        clauses.append("phone != ''")
    elif filters.get("has") == "contact":
        clauses.append("(email != '' OR phone != '')")
    elif filters.get("has") == "none":
        clauses.append("email = '' AND phone = ''")
    if filters.get("within"):
        days = float(filters["within"])
        clauses.append("posted_at >= ?")
        args.append(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400)))
    if filters.get("ids"):
        ids = [int(i) for i in filters["ids"]]
        clauses.append(f"id IN ({', '.join('?' * len(ids))})")
        args += ids
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", args


SORTS = {"recent": "posted_at DESC, id DESC", "newest": "id DESC", "oldest": "id ASC", "name": "name COLLATE NOCASE ASC",
         "company": "company COLLATE NOCASE ASC"}


def list_leads(filters: dict, page: int = 1, per_page: int = 100) -> dict:
    where, args = _where(filters)
    order = SORTS.get(filters.get("sort") or "recent", "posted_at DESC, id DESC")
    with connect() as c:
        total = c.execute(f"SELECT COUNT(*) FROM leads {where}", args).fetchone()[0]
        rows = c.execute(f"SELECT * FROM leads {where} ORDER BY {order} LIMIT ? OFFSET ?",
                         [*args, per_page, (page - 1) * per_page]).fetchall()
    return {"total": total, "page": page, "per_page": per_page, "leads": [dict(r) for r in rows]}


def all_leads(filters: dict) -> list[dict]:
    where, args = _where(filters)
    with connect() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM leads {where} ORDER BY id", args)]


def update_lead(lead_id: int, changes: dict) -> None:
    changes = {k: v for k, v in changes.items() if k in EDITABLE}
    if not changes:
        return
    with _lock, connect() as c:
        c.execute(f"UPDATE leads SET {', '.join(f'{k} = ?' for k in changes)}, updated_at = ? "
                  "WHERE id = ?", [*changes.values(), time.time(), lead_id])


def set_fields(lead_id: int, changes: dict) -> None:
    """Internal update (enrichment/resolution) — any lead column."""
    allowed = set(LEAD_FIELDS) | {"enriched", "resolved"}
    changes = {k: v for k, v in changes.items() if k in allowed}
    if not changes:
        return
    with _lock, connect() as c:
        c.execute(f"UPDATE leads SET {', '.join(f'{k} = ?' for k in changes)}, updated_at = ? "
                  "WHERE id = ?", [*changes.values(), time.time(), lead_id])


def bulk_update(filters: dict, changes: dict) -> int:
    changes = {k: v for k, v in changes.items() if k in EDITABLE}
    where, args = _where(filters)
    if not changes:
        return 0
    with _lock, connect() as c:
        return c.execute(f"UPDATE leads SET {', '.join(f'{k} = ?' for k in changes)}, "
                         f"updated_at = ? {where}", [*changes.values(), time.time(), *args]).rowcount


def delete_leads(filters: dict) -> int:
    where, args = _where(filters)
    with _lock, connect() as c:
        return c.execute(f"DELETE FROM leads {where}", args).rowcount


def stats() -> dict:
    with connect() as c:
        def col(sql):
            return [r[0] for r in c.execute(sql)]
        return {
            "total": c.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
            "with_email": c.execute("SELECT COUNT(*) FROM leads WHERE email != ''").fetchone()[0],
            "with_phone": c.execute("SELECT COUNT(*) FROM leads WHERE phone != ''").fetchone()[0],
            "by_status": {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM leads GROUP BY status")},
            "campaigns": col("SELECT DISTINCT campaign FROM leads WHERE campaign != '' ORDER BY campaign"),
            "sources": col("SELECT DISTINCT source FROM leads ORDER BY source"),
        }


# ---------------------------------------------------------------- jobs

def create_job(kind: str, campaign: str, params: dict, total: int) -> int:
    with _lock, connect() as c:
        return c.execute("INSERT INTO jobs (kind, campaign, params, total, created_at) "
                         "VALUES (?, ?, ?, ?, ?)",
                         [kind, campaign, json.dumps(params), total, time.time()]).lastrowid


def update_job(job_id: int, *, log: str | None = None, **fields) -> None:
    with _lock, connect() as c:
        if fields:
            c.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                      [*fields.values(), job_id])
        if log:
            stamp = time.strftime("%H:%M:%S")
            c.execute("UPDATE jobs SET log = log || ? WHERE id = ?", [f"[{stamp}] {log}\n", job_id])


def get_job(job_id: int) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM jobs WHERE id = ?", [job_id]).fetchone()
    return dict(r) if r else None


def list_jobs(limit: int = 20) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", [limit])]
