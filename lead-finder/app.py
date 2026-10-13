"""Lead Finder — local web app for bulk lead searches on top of Agent Reach tools.

Run:  ./run.sh   then open http://127.0.0.1:5050
"""

from __future__ import annotations

import csv
import inspect
import io
import json
import queue
import re
import threading
import time

from flask import Flask, Response, jsonify, request, send_from_directory

import db
import sources

app = Flask(__name__, static_folder="static")
jobs_q: "queue.Queue[int]" = queue.Queue()
cancelled: set[int] = set()

EXPORT_FIELDS = ["id", "status", "kind", "name", "title", "company", "location", "email", "phone",
                 "website", "linkedin", "twitter", "url", "profile", "size", "source", "query",
                 "campaign", "budget", "funding", "signal", "people", "posted_at", "notes", "snippet"]


FUNDING_KW = re.compile(r"\b(rais(?:e|es|ed|ing)|funding|funded|seed|series [a-f]|pre-seed)\b", re.I)


def keywords_for(source: str, keywords: list[str]) -> list[str]:
    """With a mixed list (the 'All opportunities' preset), funding phrases go only to the
    funding-news sources and skill / intent phrases only to everything else."""
    funding = [k for k in keywords if FUNDING_KW.search(k)]
    other = [k for k in keywords if not FUNDING_KW.search(k)]
    if source.startswith("funding"):
        return funding or other
    return other or funding


def build_queries(source: str, keywords: list[str], locations: list[str]) -> list[str]:
    locs = locations or [""]
    out = []
    for kw in keywords_for(source, keywords):
        for loc in locs:
            if source == "github":
                q = kw + (f' location:"{loc}"' if loc else "")
            elif source.startswith(("funding", "hn_", "reddit_hiring", "freelancer", "upwork", "linkedin")):
                q = f"{kw} {loc}"
            elif source == "twitter" and '"' not in kw:
                # Skill keyword ("shopify store") -> only tweets that ask for someone to do it.
                q = f'{kw} {loc} ("looking for" OR "need a" OR "need someone" OR hiring) (developer OR freelancer OR agency OR dev)'
            else:
                q = f"{kw} in {loc}" if loc else kw
            out.append(q.strip())
    return out


def filters_from(data) -> dict:
    keys = ("q", "campaign", "status", "source", "kind", "has", "sort", "within", "city")
    f = {k: data.get(k) for k in keys if data.get(k)}
    ids = data.get("ids")
    if ids:
        f["ids"] = ids if isinstance(ids, list) else str(ids).split(",")
    return f


# ---------------------------------------------------------------- worker

def run_search_job(job_id: int, p: dict) -> None:
    tasks = [(src, q) for src in p["sources"]
             for q in build_queries(src, p["keywords"], p["locations"])]
    found = added = 0
    for i, (src, q) in enumerate(tasks, 1):
        if job_id in cancelled:
            db.update_job(job_id, status="cancelled", log="Cancelled")
            return
        label = sources.SOURCES[src]["label"]
        n = min(int(p["per_query"]), sources.SOURCES[src]["max"])
        try:
            fn = sources.SOURCES[src]["fn"]
            days = int(p.get("days") or 7)
            leads = fn(q, n, days=days) if "days" in inspect.signature(fn).parameters else fn(q, n)
            leads, stale = fresh_only(src, leads, days)
            if stale:
                db.update_job(job_id, log=f"{label}: skipped {stale} posts older than {days} days")
            new = db.add_leads(leads, source=src, query=q, campaign=p["campaign"])
            found, added = found + len(leads), added + new
            db.update_job(job_id, log=f"{label}: “{q}” → {len(leads)} found, {new} new")
        except Exception as e:  # keep going; one failed query shouldn't kill the batch
            db.update_job(job_id, log=f"{label}: “{q}” → ERROR {e}")
        db.update_job(job_id, done=i, found=found, added=added)
    if p.get("auto_enrich"):
        run_resolve(job_id, {"campaign": p["campaign"], "kind": "funded"})
        run_enrich(job_id, {"campaign": p["campaign"]})
    db.update_job(job_id, status="done", finished_at=time.time(),
                  log=f"Finished: {found} found, {added} new leads")


# Sources whose leads are time-sensitive posts: drop anything older than the window or undated.
FRESH_SOURCES = {"twitter", "reddit_hiring", "reddit", "linkedin_posts", "linkedin_jobs",
                 "freelancer", "upwork", "hn_contract", "funding_news", "funding_feeds",
                 "india_jobs", "india_naukri", "india_linkedin",
                 "india_x", "india_reddit", "india_li_posts", "india_funding"}
# YC job postings stay open for months, so india_yc_jobs is not cut by the posts window.


def fresh_only(source: str, leads: list[dict], days: int) -> tuple[list[dict], int]:
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
    keep = []
    for lead in leads:
        lead["posted_at"] = sources.to_iso(lead.get("posted_at"))
        if source not in FRESH_SOURCES or (lead["posted_at"] and lead["posted_at"] >= cutoff):
            keep.append(lead)
    return keep, len(leads) - len(keep)


def run_resolve(job_id: int, filters: dict) -> None:
    """Funded startups arrive as just a name from a headline: look up site, contacts, founders."""
    targets = [l for l in db.all_leads(filters) if l["kind"] == "funded" and not l["resolved"]]
    if not targets:
        return
    db.update_job(job_id, log=f"Looking up website, contacts and founders for {len(targets)} funded startups…")
    hits = 0
    for i, l in enumerate(targets, 1):
        if job_id in cancelled:
            return
        info = sources.resolve_company(l["company"], l["signal"])
        changes = {k: v for k, v in info.items() if v and not l.get(k)}
        if changes.get("website") or changes.get("people"):
            hits += 1
        db.set_fields(l["id"], {**changes, "resolved": 1})
        if i % 10 == 0 or i == len(targets):
            db.update_job(job_id, log=f"Looked up {i}/{len(targets)} — found details for {hits}")


def run_enrich(job_id: int, filters: dict) -> None:
    targets = [l for l in db.all_leads(filters)
               if l["website"] and not l["enriched"] and not l["email"]]
    db.update_job(job_id, log=f"Enriching {len(targets)} leads from their websites…")
    batch_size, hits = 8, 0
    for start in range(0, len(targets), batch_size):
        if job_id in cancelled:
            return
        batch = targets[start:start + batch_size]
        urls = []
        for l in batch:
            base = l["website"].rstrip("/")
            urls += [base, base + "/contact", base + "/contact-us"]
        try:
            pages = sources.exa_enrich(urls)
        except Exception as e:
            db.update_job(job_id, log=f"Enrich batch failed: {e}")
            continue
        for l in batch:
            emails, phones = [], []
            domain = db._domain(l["website"])
            for page_url, info in pages.items():
                if db._domain(page_url) == domain:
                    emails += [e for e in info["emails"] if e not in emails]
                    phones += [p for p in info["phones"] if p not in phones]
            changes = {}
            if emails and not l["email"]:
                changes["email"] = ", ".join(emails[:3])
            if phones and not l["phone"]:
                changes["phone"] = ", ".join(phones[:2])
            hits += bool(changes)
            db.set_fields(l["id"], {**changes, "enriched": 1})
        db.update_job(job_id, log=f"Enriched {min(start + batch_size, len(targets))}/{len(targets)} "
                                  f"— contacts found for {hits}")
    db.update_job(job_id, log=f"Enrichment found new contact details for {hits} leads")


def worker() -> None:
    while True:
        job_id = jobs_q.get()
        job = db.get_job(job_id)
        if not job or job_id in cancelled:
            continue
        db.update_job(job_id, status="running")
        params = json.loads(job["params"])
        try:
            if job["kind"] == "search":
                run_search_job(job_id, params)
            else:
                run_resolve(job_id, params)
                run_enrich(job_id, params)
                db.update_job(job_id, status="done", done=1, finished_at=time.time())
        except Exception as e:
            db.update_job(job_id, status="failed", finished_at=time.time(), log=f"Crashed: {e}")


# ---------------------------------------------------------------- routes

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/india")
def india():
    return send_from_directory("static", "india.html")


@app.get("/api/sources")
def api_sources():
    return jsonify(sources.source_status())


@app.get("/api/stats")
def api_stats():
    return jsonify(db.stats())


@app.get("/api/leads")
def api_leads():
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(500, max(10, int(request.args.get("per_page", 100))))
    return jsonify(db.list_leads(filters_from(request.args), page, per_page))


@app.patch("/api/leads/<int:lead_id>")
def api_update_lead(lead_id: int):
    db.update_lead(lead_id, request.get_json(force=True) or {})
    return jsonify(ok=True)


@app.post("/api/leads/bulk")
def api_bulk():
    data = request.get_json(force=True) or {}
    f = filters_from(data.get("filters") or {})
    if not f:
        return jsonify(error="Refusing to change every lead without a filter or selection"), 400
    if data.get("action") == "delete":
        return jsonify(changed=db.delete_leads(f))
    return jsonify(changed=db.bulk_update(f, data.get("changes") or {}))


@app.get("/api/export.csv")
def api_export():
    rows = db.all_leads(filters_from(request.args))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    name = (request.args.get("campaign") or "leads").replace(" ", "_")
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


@app.post("/api/search")
def api_search():
    d = request.get_json(force=True) or {}
    # One item per line — commas are part of locations like "Austin, Texas".
    lines = lambda s: [x.strip() for x in str(s or "").splitlines() if x.strip()]
    keywords, locations = lines(d.get("keywords")), lines(d.get("locations"))
    srcs = [s for s in d.get("sources", []) if s in sources.SOURCES]
    if not keywords or not srcs:
        return jsonify(error="Add at least one keyword and pick at least one source"), 400
    params = {
        "campaign": (d.get("campaign") or "").strip() or time.strftime("Search %Y-%m-%d %H:%M"),
        "keywords": keywords, "locations": locations, "sources": srcs,
        "per_query": max(1, min(1000, int(d.get("per_query") or 50))),
        "auto_enrich": bool(d.get("auto_enrich")),
        "days": max(1, min(90, int(d.get("days") or 7))),
    }
    total = sum(len(build_queries(s, keywords, locations)) for s in srcs)
    job_id = db.create_job("search", params["campaign"], params, total)
    jobs_q.put(job_id)
    return jsonify(job_id=job_id, queries=total)


@app.post("/api/enrich")
def api_enrich():
    f = filters_from(request.get_json(force=True) or {})
    job_id = db.create_job("enrich", f.get("campaign", ""), f, 1)
    jobs_q.put(job_id)
    return jsonify(job_id=job_id)


@app.get("/api/jobs")
def api_jobs():
    return jsonify(db.list_jobs())


@app.post("/api/jobs/<int:job_id>/cancel")
def api_cancel(job_id: int):
    cancelled.add(job_id)
    job = db.get_job(job_id)
    if job and job["status"] == "queued":
        db.update_job(job_id, status="cancelled")
    return jsonify(ok=True)


db.init()
threading.Thread(target=worker, daemon=True).start()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
