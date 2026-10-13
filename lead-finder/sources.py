"""Lead sources. Each one calls an upstream CLI that Agent Reach installed and
returns a list of plain lead dicts (see db.LEAD_FIELDS)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
JUNK_EMAIL = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|js)$|example\.|sentry|wixpress|@2x|domain\.com", re.I)

# GUI-launched processes may not inherit the shell PATH, so add the usual tool dirs.
_EXTRA_PATH = [
    str(Path.home() / ".local/bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
]
_nvm = Path.home() / ".nvm/versions/node"
if _nvm.exists():
    _EXTRA_PATH += [str(p / "bin") for p in sorted(_nvm.iterdir(), reverse=True)]


def tool_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(_EXTRA_PATH + [env.get("PATH", "")])
    if extra:
        env.update(extra)
    return env


def which(name: str) -> str | None:
    return shutil.which(name, path=tool_env()["PATH"])


class SourceError(RuntimeError):
    pass


def to_iso(value) -> str:
    """Any date we get (epoch, RFC 2822, ISO, Twitter's format, '3 days ago') -> UTC ISO string."""
    from datetime import datetime, timedelta, timezone
    from email.utils import parsedate_to_datetime
    if value in (None, "", 0):
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    v = str(value).strip()
    m = re.match(r"(\d+)\s*(minute|hour|day|week|month)s?\s+ago", v, re.I)
    if m:
        unit = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30}[m.group(2).lower()]
        dt = datetime.now(timezone.utc) - timedelta(days=int(m.group(1)) * unit)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    for parse in (lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")),
                  parsedate_to_datetime,
                  lambda x: datetime.strptime(x, "%a %b %d %H:%M:%S %z %Y")):
        try:
            dt = parse(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError, IndexError):
            continue
    return ""


def since_date(days: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))


def run_tool(args: list[str], *, env_extra: dict | None = None, timeout: int = 300,
             _retry: int = 2) -> str:
    """Run a CLI and return stdout. Output goes to a temp file, not a pipe:
    Node CLIs (mcporter, opencli) can truncate large output written to pipes."""
    exe = which(args[0])
    if not exe:
        raise SourceError(f"`{args[0]}` is not installed")
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as out, \
         tempfile.TemporaryFile(mode="w+", encoding="utf-8") as err:
        try:
            proc = subprocess.run([exe, *args[1:]], stdout=out, stderr=err,
                                  env=tool_env(env_extra), timeout=timeout)
        except subprocess.TimeoutExpired:
            raise SourceError(f"`{args[0]}` timed out after {timeout}s")
        out.seek(0)
        err.seek(0)
        stdout, stderr = out.read(), err.read()
    if proc.returncode != 0 and args[0] == "opencli" and "Navigation rejected" in (stdout + stderr) \
            and _retry > 0:
        time.sleep(5)  # Chrome tab still busy from the previous command
        return run_tool(args, env_extra=env_extra, timeout=timeout, _retry=_retry - 1)
    if proc.returncode != 0:
        msg = (stderr or stdout).strip().splitlines()
        raise SourceError(f"`{args[0]}` failed: {' '.join(msg[-3:])[:400]}")
    return stdout


def clean_emails(text: str) -> list[str]:
    seen: list[str] = []
    for e in EMAIL_RE.findall(text or ""):
        e = e.strip(".").lower()
        if not JUNK_EMAIL.search(e) and e not in seen:
            seen.append(e)
    return seen


def clean_phones(text: str) -> list[str]:
    seen: list[str] = []
    for p in PHONE_RE.findall(text or ""):
        p = p.strip()
        digits = re.sub(r"\D", "", p)
        if 9 <= len(digits) <= 15 and p not in seen and not re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", p):
            seen.append(p)
    return seen


# ---------------------------------------------------------------- Exa (web)

def _exa_call(tool: str, args: dict, timeout: int = 240) -> str:
    raw = run_tool(["mcporter", "call", f"exa.{tool}", "--args", json.dumps(args),
                    "--output", "json"], timeout=timeout)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise SourceError("Exa returned unreadable output")
    if isinstance(data, dict) and data.get("isError"):
        raise SourceError(f"Exa error: {str(data.get('content'))[:300]}")
    parts = data.get("content", []) if isinstance(data, dict) else []
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _field(block: str, name: str) -> str:
    m = re.search(rf"^- {name}:\s*(.+)$", block, re.M)
    return m.group(1).strip() if m else ""


def _split_exa(text: str) -> list[dict]:
    results = []
    for block in re.split(r"\n-{3,}\n", text):
        m_url = re.search(r"^URL:\s*(\S+)", block, re.M)
        if not m_url:
            continue
        m_title = re.search(r"^Title:\s*(.+)$", block, re.M)
        hl = block.split("Highlights:", 1)[1] if "Highlights:" in block else block
        results.append({"title": m_title.group(1).strip() if m_title else "",
                        "url": m_url.group(1).strip(), "body": hl.strip()})
    return results


def _summary(body: str, limit: int = 400) -> str:
    lines = [ln.strip() for ln in body.splitlines()
             if ln.strip() and ln.strip() != "..." and not ln.strip().startswith(("- ", "#"))]
    return " ".join(lines)[:limit]


def exa_companies(query: str, n: int) -> list[dict]:
    text = _exa_call("web_search_exa", {
        "query": f"category:company {query}",
        "numResults": n,
        "objective": f"Businesses matching: {query}. Prefer company homepages and profiles "
                     "with contact details (email, phone, LinkedIn, headquarters).",
    })
    leads = []
    for r in _split_exa(text):
        b = r["body"]
        homepage = _field(b, "Homepage")
        website = ("https://" + homepage) if homepage and not homepage.startswith("http") else (homepage or r["url"])
        linkedin = _field(b, "LinkedIn")
        emp = re.search(r"employs ([\d,]+) people", b)
        phones = [p.strip() for p in _field(b, "Phone").split(",") if p.strip()] or clean_phones(b)[:1]
        leads.append({
            "kind": "company",
            "name": r["title"].title() if r["title"].isupper() else r["title"],
            "title": _field(b, "Industry"),
            "company": r["title"],
            "location": _field(b, "Headquarters"),
            "website": website,
            "email": ", ".join(clean_emails(_field(b, "Emails") or b)[:3]),
            "phone": ", ".join(phones[:2]),
            "linkedin": ("https://" + linkedin) if linkedin and not linkedin.startswith("http") else linkedin,
            "url": r["url"],
            "size": emp.group(1) if emp else "",
            "snippet": _summary(b),
        })
    return leads


def exa_people(query: str, n: int) -> list[dict]:
    text = _exa_call("web_search_exa", {
        "query": f"category:people {query}",
        "numResults": n,
        "objective": f"Professional profiles of people matching: {query}. "
                     "Pull their current role, company and location.",
    })
    leads = []
    for r in _split_exa(text):
        b = r["body"]
        lines = [ln.strip() for ln in b.splitlines() if ln.strip() and ln.strip() != "..."]
        headline = lines[0] if lines else ""
        location = lines[1] if len(lines) > 1 and len(lines[1]) < 80 and "," in lines[1] else ""
        cur = re.search(r"###\s*(.+?)\s+-\s+\[([^\]]+)\]\([^)]*\)\s*\(Current\)", b)
        role, company = (cur.group(1), cur.group(2)) if cur else (headline, "")
        if not company and " at " in headline:
            company = headline.split(" at ", 1)[1].split("|")[0].strip()
        elif not company and "@" in headline:
            company = headline.split("@", 1)[1].split("|")[0].strip()
        is_li = "linkedin.com/in/" in r["url"]
        leads.append({
            "kind": "person",
            "name": r["title"].split("|")[0].split(" - ")[0].strip(),
            "title": role[:200],
            "company": company,
            "location": location,
            "email": ", ".join(clean_emails(b)[:2]),
            "linkedin": r["url"] if is_li else "",
            "url": r["url"],
            "snippet": _summary(b),
        })
    return leads


def exa_enrich(urls: list[str]) -> dict[str, dict]:
    """Fetch pages and pull emails/phones. Returns {url: {email, phone}}."""
    text = _exa_call("web_fetch_exa", {"urls": urls, "maxCharacters": 15000}, timeout=300)
    found: dict[str, dict] = {}
    blocks = re.split(r"(?m)^(?=(?:Title|URL):)", text)
    current = None
    for block in blocks:
        m = re.search(r"^URL:\s*(\S+)", block, re.M)
        if m:
            current = m.group(1)
        if not current:
            continue
        entry = found.setdefault(current, {"emails": [], "phones": []})
        entry["emails"] += [e for e in clean_emails(block) if e not in entry["emails"]]
        entry["phones"] += [p for p in clean_phones(block) if p not in entry["phones"]]
    return found


# ---------------------------------------------------------------- GitHub

_GH_QUERY = """
query($q: String!, $n: Int!, $after: String) {
  search(query: $q, type: USER, first: $n, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on User { login name email bio company location websiteUrl twitterUsername url
                    followers { totalCount } }
      ... on Organization { login name email description location websiteUrl twitterUsername url }
    }
  }
}"""


def github_users(query: str, n: int) -> list[dict]:
    leads, after = [], None
    gql = _GH_QUERY
    while len(leads) < n:
        args = ["gh", "api", "graphql", "-f", f"query={gql}", "-F", f"q={query}",
                "-F", f"n={min(100, n - len(leads))}"]
        if after:
            args += ["-f", f"after={after}"]
        try:
            data = json.loads(run_tool(args, timeout=120))
        except SourceError as e:
            # Public emails need the read:user scope; carry on without them if it's missing.
            if "email" in str(e) and "scope" in str(e) and " email " in gql:
                gql = gql.replace(" email ", " ")
                continue
            raise
        if data.get("errors"):
            raise SourceError(f"GitHub: {data['errors'][0].get('message')}")
        search = data["data"]["search"]
        for u in search["nodes"]:
            if not u:
                continue
            twitter = u.get("twitterUsername")
            leads.append({
                "kind": "developer",
                "name": u.get("name") or u["login"],
                "title": (u.get("bio") or u.get("description") or "")[:200],
                "company": (u.get("company") or "").lstrip("@"),
                "location": u.get("location") or "",
                "website": u.get("websiteUrl") or "",
                "email": u.get("email") or "",
                "twitter": f"https://x.com/{twitter}" if twitter else "",
                "url": u["url"],
                "size": str((u.get("followers") or {}).get("totalCount", "")),
                "snippet": f"@{u['login']}",
            })
        if not search["pageInfo"]["hasNextPage"]:
            break
        after = search["pageInfo"]["endCursor"]
    return leads


# ---------------------------------------------------------------- Reddit (OpenCLI)

def reddit_posts(query: str, n: int) -> list[dict]:
    raw = run_tool(["opencli", "reddit", "search", query, "--limit", str(n),
                    "--sort", "new", "--time", "month", "-f", "json"], timeout=240)
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        raise SourceError("Reddit: unreadable output (is the OpenCLI Chrome extension installed and are you logged in to reddit.com?)")
    if isinstance(items, dict):
        items = items.get("data") or items.get("items") or []
    leads = []
    for it in items:
        author = it.get("author") or ""
        if not author or author in ("[deleted]", "AutoModerator"):
            continue
        text = f"{it.get('title', '')} — {it.get('selftext', '')}"
        leads.append({
            "kind": "post",
            "name": f"u/{author}",
            "title": it.get("title", "")[:200],
            "company": f"r/{it.get('subreddit', '')}",
            "email": ", ".join(clean_emails(text)[:2]),
            "url": it.get("url", ""),
            "profile": f"https://www.reddit.com/user/{author}",
            "snippet": text[:400],
        })
    return leads


# ---------------------------------------------------------------- Twitter

def _twitter_env() -> dict:
    cfg = Path.home() / ".agent-reach" / "config.yaml"
    data = {}
    if cfg.exists():
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    token = os.environ.get("TWITTER_AUTH_TOKEN") or data.get("twitter_auth_token")
    ct0 = os.environ.get("TWITTER_CT0") or data.get("twitter_ct0")
    if not (token and ct0):
        raise SourceError("Twitter cookies not set. Run: agent-reach configure twitter-cookies")
    return {"TWITTER_AUTH_TOKEN": token, "TWITTER_CT0": ct0}


# Cut the commonest non-client tweets at search time (X search supports -"phrase").
X_EXCLUDE = ('-"for hire" -"hire me" -"DMs open" -"DMs are open" -"open for" -"available for" '
             '-"shouldn\'t need" -"don\'t need" -"no longer need" -"developer account"')


def _tweets_opencli(query: str, n: int) -> list[dict]:
    """OpenCLI rides the logged-in Chrome session — no cookies needed."""
    items = json.loads(run_tool(["opencli", "twitter", "search", query, "--product", "live",
                                 "--exclude", "retweets", "--limit", str(n), "-f", "json"],
                                timeout=300))
    return [{"handle": t.get("author", ""), "name": t.get("author", ""), "text": t.get("text", ""),
             "url": t.get("url", ""), "date": t.get("created_at", "")} for t in items or []]


def _tweets_twitter_cli(query: str, n: int) -> list[dict]:
    payload = json.loads(run_tool(["twitter", "search", query, "-n", str(n), "-t", "latest",
                                   "--exclude", "retweets", "--json"],
                                  env_extra=_twitter_env(), timeout=240))
    if isinstance(payload, dict) and payload.get("ok") is False:
        raise SourceError(f"Twitter: {payload.get('error', {}).get('message', payload)}")
    out = []
    for t in payload.get("data", []) if isinstance(payload, dict) else payload:
        a = t.get("author") or {}
        handle = a.get("screenName", "")
        out.append({"handle": handle, "name": a.get("name") or handle, "text": t.get("text", ""),
                    "url": f"https://x.com/{handle}/status/{t.get('id')}",
                    "date": t.get("createdAtISO", "")})
    return out




def twitter_posts(query: str, n: int, days: int = 7) -> list[dict]:
    """Tweets from people who want to hire / outsource development."""
    query = f"{query} {X_EXCLUDE} since:{since_date(days)}"
    errors = []
    tweets = None
    for backend in (_tweets_opencli, _tweets_twitter_cli):
        try:
            tweets = backend(query, n)
            break
        except (SourceError, json.JSONDecodeError) as e:
            errors.append(str(e))
    if tweets is None:
        raise SourceError("; ".join(errors))
    leads = []
    for t in tweets:
        text, handle = t["text"] or "", t["handle"]
        if not handle or text.lstrip().startswith("@") or not client_post(text):
            continue
        leads.append({
            "kind": "hiring",
            "name": f"@{handle}",
            "title": text.split("\n", 1)[0][:200],
            "email": ", ".join(clean_emails(_deobfuscate(text))[:2]),
            "twitter": f"https://x.com/{handle}",
            "url": t["url"],
            "profile": f"https://x.com/{handle}",
            "signal": "X: wants a developer",
            "posted_at": t["date"],
            "snippet": text[:400].replace("\n", " "),
        })
    return leads


# ---------------------------------------------------------------- Funding news (free RSS)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) LeadFinder/1.0"}
AMOUNT = r"(?:US\s?)?(?:[$€£₹]|USD|EUR|GBP|INR|Rs\.?)\s?[\d.,]+\s?(?:[MBK]\b|mn\b|bn\b|million|billion|crore|lakh|cr\b)?"
ROUND = r"pre-?seed|seed|series\s+[a-f]\b|angel|bridge|growth|venture|strategic"
FUNDING_RE = re.compile(
    rf"^(?P<co>.+?)\s+(?:raises|raised|secures|secured|lands|landed|closes|closed|bags|nabs|"
    rf"snags|gets|grabs|picks up|scores|completes|announces|wins)\s+(?P<rest>.*)$", re.I)
NOT_COMPANY = re.compile(r"\b(fund|vc|venture capital|investors?|cap|cohort|report|round-?up|"
                         r"startups|daily|weekly|how|why|what|these|top \d+)\b", re.I)


def _http_get(url: str, timeout: int = 20) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _rss_items(xml: str) -> list[dict]:
    import html
    from xml.etree import ElementTree as ET
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        get = lambda tag: html.unescape((item.findtext(tag) or "").strip())
        out.append({"title": get("title"), "link": get("link"), "date": get("pubDate"),
                    "source": get("source"), "desc": re.sub(r"<[^>]+>", " ", get("description"))})
    return out


def parse_funding(headline: str) -> dict | None:
    """'Satlyt Raises $8M Seed to Expand X - Payload Space' -> company, amount, round."""
    title = re.sub(r"\s+[-–|]\s+[^-–|]{2,60}$", "", headline).strip()  # drop " - Publisher"
    m = FUNDING_RE.match(title)
    if not m:
        return None
    co, rest = m.group("co").strip(" :'\"‘’“”"), m.group("rest")
    if re.search(r"\b(fund|cap|cohort|vehicle|lp)\b", rest[:60], re.I):
        return None  # a VC raising its own fund, not a startup
    co = re.split(r"\s+(?:in|from|of|based in)\s+[A-Z]", co)[0]  # "Outmarket in California AI"
    if re.search(r"\b(startup|company|team|firm|founders?)$", co, re.I):
        return None  # "Tampa edtech startup" — no usable name
    amt = re.search(AMOUNT, rest, re.I)
    rnd = re.search(ROUND, rest, re.I)
    if not amt and not rnd:
        return None
    # "Houston startup Foo" / "Saudi AI startup Gaia" -> "Foo" / "Gaia"
    sub = re.search(r"\b(?:startup|company|platform|maker|firm|provider|app)\s+(.+)$", co, re.I)
    if sub:
        co = sub.group(1)
    co = re.sub(r"^(exclusive|breaking|scoop)\s*[:|-]\s*", "", co, flags=re.I).strip()
    if not co or len(co.split()) > 5 or NOT_COMPANY.search(co) or not co[0].isupper():
        return None
    return {"company": co,
            "funding": " ".join(x for x in [amt.group(0).strip() if amt else "",
                                            rnd.group(0).title() if rnd else ""] if x),
            "about": title}


def funding_news(query: str, n: int, days: int = 14) -> list[dict]:
    from urllib.parse import quote_plus
    url = (f"https://news.google.com/rss/search?q={quote_plus(query)}+when:{days}d"
           "&hl=en-US&gl=US&ceid=US:en")
    try:
        items = _rss_items(_http_get(url))
    except Exception as e:
        raise SourceError(f"Google News: {e}")
    leads = []
    for it in items:
        f = parse_funding(it["title"])
        if not f:
            continue
        leads.append({
            "kind": "funded", "name": f["company"], "company": f["company"],
            "funding": f["funding"], "signal": f["about"], "url": it["link"],
            "posted_at": it["date"], "snippet": f"{f['about']} ({it['source']})",
        })
        if len(leads) >= n:
            break
    return leads


TECH_FEEDS = ["https://techcrunch.com/category/venture/feed/",
              "https://feeds.feedburner.com/venturebeat/SZYF"]


def funding_feeds(query: str, n: int, days: int = 14) -> list[dict]:
    """Publisher RSS feeds; the query is used as an optional keyword filter."""
    words = [w.lower() for w in re.findall(r"\w+", query)
             if w.lower() not in {"startup", "startups", "raises", "raise", "funding", "round", "in"}]
    leads = []
    for feed in TECH_FEEDS:
        try:
            items = _rss_items(_http_get(feed))
        except Exception:
            continue
        for it in items:
            f = parse_funding(it["title"])
            text = (it["title"] + " " + it["desc"]).lower()
            if f and (not words or any(w in text for w in words)):
                leads.append({"kind": "funded", "name": f["company"], "company": f["company"],
                              "funding": f["funding"], "signal": f["about"], "url": it["link"],
                              "posted_at": it["date"], "snippet": it["desc"][:400]})
    return leads[:n]


def resolve_company(name: str, context: str) -> dict:
    """Find a funded company's website / contact details / founders via Exa (free)."""
    out: dict = {}
    norm = re.sub(r"[^a-z0-9]", "", name.lower())
    try:
        for c in exa_companies(f"{name}", 3):
            cand = re.sub(r"[^a-z0-9]", "", (c["name"] + c["website"]).lower())
            if norm and norm in cand:
                out = {k: c[k] for k in ("website", "email", "phone", "linkedin", "location", "size")
                       if c.get(k)}
                if c.get("title"):
                    out["title"] = c["title"]
                break
    except SourceError:
        pass
    try:
        people = []
        for p in exa_people(f"founder CEO CTO of {name} {context[:80]}", 5):
            if norm and norm in re.sub(r"[^a-z0-9]", "", (p["company"] + p["title"] + p["snippet"]).lower()):
                people.append(f"{p['name']} — {p['title'][:60]} — {p['url']}")
        if people:
            out["people"] = "\n".join(people[:3])
    except SourceError:
        pass
    return out


# ---------------------------------------------------------------- Outsourcing intent

def _deobfuscate(text: str) -> str:
    t = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*|\s+at\s+(?=[\w-]+\s*(?:[\[(]\s*dot\s*[\])]|\s+dot\s+|\.))", "@", text, flags=re.I)
    return re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*|\s+dot\s+(?=[a-z]{2,6}\b)", ".", t, flags=re.I)


CONTRACT_RE = re.compile(r"\b(contract|contractor|freelanc\w*|fixed[- ]fee|project[- ]based|agency|"
                         r"consult\w*|outsourc\w*|part[- ]time|1099|c2c)\b", re.I)


def hn_contract(query: str, n: int, days: int = 30) -> list[dict]:
    """Companies in the latest HN 'Who is hiring?' thread offering contract / freelance work."""
    import html
    import urllib.parse
    stories = json.loads(_http_get("https://hn.algolia.com/api/v1/search_by_date?"
                                   "tags=story,author_whoishiring&hitsPerPage=10"))["hits"]
    threads = [s for s in stories if "who is hiring" in s["title"].lower()][:2]
    generic = {"contract", "freelance", "any", "all", "remote", "looking", "need", "needs",
               "developer", "developers", "someone", "build", "agency", "dev", "hire", "hiring",
               "technical", "cofounder", "mvp", "app", "for", "the", "and", "with", "to", "in", "a"}
    words = [w for w in re.findall(r"\w+", query.lower()) if w not in generic and len(w) > 2]
    leads = []
    for s in threads:
        url = (f"https://hn.algolia.com/api/v1/search?tags=comment,story_{s['objectID']}"
               f"&hitsPerPage=1000")
        for c in json.loads(_http_get(url))["hits"]:
            if str(c.get("parent_id")) != s["objectID"]:
                continue  # replies, not job posts
            text = html.unescape(re.sub(r"<p>", "\n", c.get("comment_text") or ""))
            text = re.sub(r"<[^>]+>", " ", text)
            head = text.split("\n", 1)[0]
            if not CONTRACT_RE.search(head + " " + text[:600]):
                continue
            if words and not any(w in text.lower() for w in words):
                continue
            parts = [p.strip() for p in head.split("|")]
            links = re.findall(r"https?://[^\s<>\"')]+", text)
            site = next((l for l in links if not re.search(r"news\.ycombinator|lever|greenhouse|"
                                                           r"ashby|workable|forms\.|calendly|linkedin", l)), "")
            leads.append({
                "kind": "hiring", "name": parts[0][:80], "company": parts[0][:80],
                "title": " | ".join(parts[1:4])[:200], "location": "",
                "website": site, "email": ", ".join(clean_emails(_deobfuscate(text))[:2]),
                "url": f"https://news.ycombinator.com/item?id={c['objectID']}",
                "profile": f"https://news.ycombinator.com/user?id={c.get('author')}",
                "signal": "HN Who is hiring — " + (CONTRACT_RE.search(head + " " + text[:600]).group(0)),
                "posted_at": c.get("created_at", ""), "snippet": text[:400],
            })
            if len(leads) >= n:
                return leads
    return leads


SOFTWARE_RE = re.compile(
    r"\b(develop\w*|devs?|software|web ?app|website|web site|mobile app|apps?|ios|android|mvp|saas|"
    r"platform|engineer\w*|programm\w*|coder|coding|full[- ]?stack|front[- ]?end|back[- ]?end|"
    r"react|next\.?js|node|python|django|flutter|swift|kotlin|shopify|wordpress|webflow|api|"
    r"automation|ai agent|chatbot|cto)\b", re.I)
HIRING_SUBS = ["forhire", "slavelabour", "gameDevClassifieds", "hiring", "freelance_forhire",
               "startups", "SaaS", "Entrepreneur", "smallbusiness", "EntrepreneurRideAlong",
               "cofounder", "SideProject", "nocode", "shopify", "Wordpress"]
# Subreddits where buyers tag their posts: [Hiring], [Task], [Paid].
TAGGED_SUBS = {"forhire", "freelance_forhire", "hiring", "slavelabour", "gameDevClassifieds"}
BUYER_TAG_RE = re.compile(r"\[\s*(hiring|task|paid)\s*\]", re.I)


# ---------------------------------------------------------------- Is this post a client?
# A lead is someone who wants dev work done and will pay for it. Every post source (X, Reddit,
# LinkedIn, on both pages) runs through client_post(); the patterns come from real misses:
# freelancers advertising, product ads riffing on "need a developer", full-time job ads,
# and non-dev gigs (writers, creators, testers).

DEV_WORK = (r"\b(?:developers?|devs?|programmers?|coders?|engineers?|agency|agencies|freelancers?|websites?|"
            r"web\s*(?:app|site|design)|apps?|mvp|saas|shopify|wordpress|webflow|automation|chatbot|bot|"
            r"scripts?|platform|software|build|create|fix|develop)\b")
CLIENT_RE = re.compile(
    r"\[\s*(?:hiring|task|paid)\s*\]"
    # "I'm looking for / we need …" + dev work in the same sentence
    r"|\b(?:i'?m|i am|we'?re|we are|i|we|currently)\s+(?:(?:currently|actually|urgently|also|still|really|now)\s+)?"
    r"(?:looking\s+for|searching\s+for|seeking|need|want\s+to\s+hire|trying\s+to\s+find)\s+[^.!?\n]{0,40}?" + DEV_WORK +
    # a sentence that starts with the request: "Need a developer…", "From the forums: Looking for a Developer"
    r"|(?:^|[.!?:\n—–-]\s*)(?:need|needed|looking\s+for|seeking|hiring)\s+[^.!?\n]{0,40}?" + DEV_WORK +
    r"|\bhir(?:e|ing)\s+(?:a|an)?\s*(?:freelance\s+)?(?:developer|dev|programmer|coder|agency|freelancer)"
    r"|\b(?:developer|programmer|freelancer|agency|dev)s?\s+(?:wanted|needed|required)\b"
    r"|\burgent(?:ly)?\s+(?:need|needed|required|looking\s+for)\b[^.!?\n]{0,40}?" + DEV_WORK +
    r"|\b(?:can|could)\s+(?:someone|anyone|somebody)\s+(?:build|make|create|fix|develop|help\s+(?:me|us)\s+build)"
    r"|\bwho\s+can\s+(?:build|make|create|develop|fix)"
    r"|\b(?:any(?:one)?|can\s+you)\s+(?:recommend|know)\s+(?:a|an)\s+(?:good\s+)?(?:[\w.+#-]+\s+)?"
    r"(?:developer|dev|agency|freelancer)"
    r"|\b(?:budget|fixed\s+(?:price|budget)|will\s+pay|paid\s+(?:project|gig|task)|"
    r"send\s+(?:me\s+)?(?:your\s+)?portfolio|dm\s+(?:me\s+)?(?:your\s+)?(?:portfolio|rates?|quote))\b",
    re.I | re.M)
SELLER_RE = re.compile(
    r"\[\s*for\s*hire\s*\]|\bfor\s+hire\b|\bfore\s+hire\b"
    r"|open\s+(?:for|to)\s+(?:a\s+few\s+|new\s+)?(?:freelance\s+)?(?:work|projects?|clients?|gigs?|opportunit\w*|roles?|commissions)"
    r"|available\s+for\s+(?:hire|work|projects?|freelance)|\bhire\s+me\b|\bdms?\s+(?:are\s+)?open"
    r"|if\s+you(?:'re|\s+are)?\s+(?:an?\s+\w+\s+)?(?:need|needing|looking\s+for)\s+(?:a|an)\s+[\w\s/-]{0,20}"
    r"(?:developer|dev|designer|agency)"
    r"|(?:know\s+someone|anyone)\s+who\s+needs\s+(?:a|an|one)"
    r"|reintroducing\s+myself|\bi'?m\s+(?:a|an)\s+[\w\s/.-]{0,30}(?:developer|engineer|designer|freelancer)\b"
    r"|\b(?:i|we)\s+(?:build|help|offer|specialize|provide)\b|\bmy\s+(?:services|portfolio|work)\b"
    r"|let'?s\s+connect|looking\s+to\s+connect|\bdm\s+me\s+for\b"
    r"|looking\s+for\s+(?:a\s+)?(?:new\s+)?(?:\w+\s+)?(?:job|role|position|work|opportunit\w*|team|clients?|projects?|gigs?)\b"
    r"|open\s+to\s+work|#opentowork|seeking\s+(?:a\s+)?(?:new\s+)?(?:role|job|position|opportunit)"
    r"|tiny\s+(?:dev|software)\s+(?:shop|studio)|\bour\s+(?:agency|studio|team)\s+(?:builds|helps|can)",
    re.I)
# Ads and opinions that merely contain "need a developer" ("Small businesses shouldn't need a developer…").
AD_RE = re.compile(
    r"(?:shouldn'?t|doesn'?t|don'?t|won'?t|no\s+longer|never|without(?:\s+needing)?)\s+(?:\w+\s+){0,3}"
    r"(?:need|needing|hire|hiring)\b"
    r"|\bdo\s+(?:i|you|we)\s+(?:really\s+|even\s+)?need\s+(?:a|an)\b|you\s+(?:only\s+)?need\s+developers?\s+for"
    r"|the\s+line\s+where|hot\s+take|\bhacks?\b|\btips\b|🧵|\bthread\b|job\s+ads?\s+(?:before|now)"
    r"|where\s+would\s+you\s+look|how\s+(?:do|would|can)\s+(?:i|you)\s+find"
    r"|developer\s+(?:account|console|mode|tools|options|program|key|license|docs)"
    r"|job\s+alert|follow\s+back|\bf4f\b|drop\s+your|#connect\b|now\s+hiring:",
    re.I)
# Full-time employment and staffing — a job, not a client — unless the post is clearly freelance.
EMPLOY_RE = re.compile(
    r"full[-\s]?time|\bLPA\b|\bCTC\b|per\s+annum|\bsalary\b|\bbenefits\b|\byoe\b|job\s+(?:title|type|description)"
    r"|type\s+of\s+employment|work\s+mode|apprentice|freshers?\b|\binterns?(?:hip)?\b|hiring\s+drive|walk-?in"
    r"|eligibility|\bpositions\b|usa\s+only|located\s+in\s+the\s+us|talent\s+(?:outsourcing|acquisition)"
    r"|\brecruit\w*|staffing|placement|our\s+client\b|opportunity\s+with|\breq:|\bjd\b|immediate\s+joiner"
    r"|notice\s+period|\d+\s*[-–]\s*\d+\s*(?:years?|yrs?)|\d+\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:exp|experience)"
    r"|join\s+(?:my|our)\s+team|\bWFO\b",
    re.I)
FREELANCE_RE = re.compile(
    r"freelanc\w*|\bcontract(?:or)?\b|one[-\s]?off|fixed\s+(?:price|budget)|per\s+hour|/\s*h(?:ou)?r\b"
    r"|\bbudget\b|\bgig\b|project[-\s]basis|\[\s*(?:task|paid)\s*\]",
    re.I)
# What they're hiring for must be dev work, not writing / video / testing / art.
NON_DEV_RE = re.compile(
    r"\bwriters?\b|copywrit\w*|content\s+creators?|\bcreators\b|\bugc\b|social\s+media\s+(?:manager|marketer|"
    r"management|specialist)|video\s+editor|\bvoice\b|\bactors?\b|marketer|\bmarketing\b|\bsales\b|\bclosers?\b"
    r"|appointment\s+setter|virtual\s+assistant|\bva\b|(?:beta|app|user|usability|iphone|android)\s+testers?|try\s+(?:my|our)\s+app|\bsurvey\b"
    r"|participants|\b3d\b|\bartist\b|illustrator|animator|graphic\s+designer|\blogo\b|\btutor\b"
    r"|photographer|gpu|cluster|video\s+(?:developer|editor|editing|creator)|\banimation\b|\bediting\b"
    r"|property|real\s+estate|realty|\bbhk\b|apartment|\bflats?\s+(?:for\s+(?:sale|rent)|in\s+sector)|\bplots?\b|\bvilla|builder\s+floor"
    r"|senior[-\s]living|housing\s+project",
    re.I)
# Not a paid gig: co-founder / equity / free feedback asks.
UNPAID_RE = re.compile(
    r"\bequity\b|co-?founder|\bpartners?(?:hip)?\b|own\s+a\s+piece|rev(?:enue)?[\s-]?share|\bunpaid\b"
    r"|\bvolunteer|for\s+free\b|\bfeedback\b|technical\s+review|break\s+it\b|free\s+builds?",
    re.I)
QUESTION_OK_RE = re.compile(r"(?:can|could)\s+(?:someone|anyone)|who\s+can|(?:recommend|know)\s+(?:a|an)", re.I)


def client_post(text: str, subject: str = "") -> bool:
    """True when the post comes from someone who wants (paid) dev work done.
    subject = the post's title (Reddit); tweets / LinkedIn posts use their first lines."""
    t = (text or "").replace("’", "'").replace("‘", "'")
    t = re.sub(r"“[^”]{0,80}”|\"[^\"]{0,80}\"", " ", t)
    subj = (subject or t[:250]).replace("’", "'").replace("‘", "'")
    head = t[:700]
    tagged = BUYER_TAG_RE.search(subj)
    if SELLER_RE.search(head) or AD_RE.search(head) or UNPAID_RE.search(head):
        return False
    if NON_DEV_RE.search(subj):
        return False
    if EMPLOY_RE.search(t[:1500]) and not FREELANCE_RE.search(t[:1500]):
        return False
    if not SOFTWARE_RE.search(head):
        return False
    if not tagged and subj.rstrip().endswith("?") and not QUESTION_OK_RE.search(subj):
        return False  # a question to the sub, not a request
    return bool(tagged or CLIENT_RE.search(head))


def reddit_hiring(query: str, n: int, days: int = 7) -> list[dict]:
    """Reddit posts from people who want to hire / outsource development."""
    leads, errors = [], []
    per_sub = max(10, min(50, n // 3))
    for sub in HIRING_SUBS:
        q = query.replace('"', "") if sub in TAGGED_SUBS else query
        try:
            raw = run_tool(["opencli", "reddit", "search", q, "--subreddit", sub, "--sort", "new",
                            "--time", "day" if days <= 1 else "week" if days <= 7 else "month",
                            "--limit", str(per_sub), "-f", "json"], timeout=180)
            items = json.loads(raw)
        except (SourceError, json.JSONDecodeError) as e:
            errors.append(f"r/{sub}: {e}")
            continue
        for it in items if isinstance(items, list) else []:
            title, body = it.get("title", ""), it.get("selftext", "") or ""
            text = f"{title}\n{body}"
            author = it.get("author") or ""
            if not author or author in ("[deleted]", "AutoModerator"):
                continue
            hiring_tag = BUYER_TAG_RE.search(title)
            if not client_post(text, title):
                continue  # [For Hire] ads, full-time jobs, writers / testers, questions…
            leads.append({
                "kind": "hiring", "name": f"u/{author}", "title": title[:200],
                "company": "r/" + str(it.get("subreddit") or sub).removeprefix("r/"),
                "email": ", ".join(clean_emails(_deobfuscate(text))[:2]),
                "url": it.get("url", ""), "profile": f"https://www.reddit.com/user/{author}",
                "signal": f"Reddit [{hiring_tag.group(1).title()}]" if hiring_tag else "Reddit: wants a developer",
                "posted_at": to_iso(it.get("created_utc")),
                "snippet": text[:400].replace("\n", " "),
            })
    if not leads and errors:
        raise SourceError("; ".join(errors[:2]))
    return leads[:n]


# ---------------------------------------------------------------- Project marketplaces

INTENT_WORDS = re.compile(r"\b(looking|for|need|needs|an?|someone|to|hire|hiring|freelance|"
                          r"developer|developers|dev|agency|the|my|build|built|seeking|want)\b", re.I)


def skill_terms(query: str) -> str:
    """'"looking for an app developer"' -> 'app'  (marketplaces want skill keywords)."""
    return re.sub(r"\s+", " ", INTENT_WORDS.sub(" ", query.replace('"', ""))).strip()


def freelancer_projects(query: str, n: int, days: int = 7) -> list[dict]:
    """Open projects on Freelancer.com — public API, no login."""
    from urllib.parse import urlencode
    q = skill_terms(query)
    if not q:
        return []
    leads, offset = [], 0
    while len(leads) < n:
        params = urlencode({"query": q, "limit": min(100, n - len(leads)), "offset": offset,
                            "full_description": "true", "sort_field": "time_updated",
                            "project_statuses[]": "active"})
        data = json.loads(_http_get(f"https://www.freelancer.com/api/projects/0.1/projects/active/?{params}"))
        if data.get("status") != "success":
            raise SourceError(f"Freelancer.com: {data.get('message', 'error')}")
        projects = data["result"]["projects"]
        for pr in projects:
            cur, b = pr.get("currency") or {}, pr.get("budget") or {}
            lo, hi = b.get("minimum"), b.get("maximum")
            budget = f"{cur.get('sign', '')}{lo:,.0f}–{hi:,.0f} {cur.get('code', '')}" if lo and hi else ""
            if pr.get("type") == "hourly" and budget:
                budget += "/hr"
            bids = (pr.get("bid_stats") or {}).get("bid_count", 0)
            leads.append({
                "kind": "project", "name": pr.get("title", "")[:200], "title": budget,
                "company": "Freelancer.com", "location": cur.get("country", ""),
                "budget": budget,
                "url": f"https://www.freelancer.com/projects/{pr.get('seo_url', pr.get('id'))}",
                "signal": f"Freelancer.com project · {bids} bids",
                "posted_at": to_iso(pr.get("time_submitted")),
                "snippet": (pr.get("description") or pr.get("preview_description") or "")[:400],
            })
        if len(projects) < 100:
            break
        offset += len(projects)
    return leads[:n]


def upwork_jobs(query: str, n: int, days: int = 7) -> list[dict]:
    """Upwork job search through OpenCLI (needs you logged in to upwork.com in Chrome)."""
    q = skill_terms(query)
    if not q:
        return []
    try:
        raw = run_tool(["opencli", "upwork", "search", q, "--sort", "recency",
                        "--per_page", str(max(10, min(50, n))), "-f", "json"], timeout=240)
    except SourceError as e:
        raise SourceError(f"Upwork failed — log in to upwork.com in Chrome, then retry ({e})")
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        raise SourceError("Upwork: log in to upwork.com in Chrome first")
    if isinstance(items, dict):
        raise SourceError(f"Upwork: {items.get('error', {}).get('message', 'error')} — log in to upwork.com in Chrome")
    leads = []
    for j in items:
        budget = str(j.get("budget") or "")
        leads.append({
            "kind": "project", "name": str(j.get("title", ""))[:200],
            "title": " · ".join(x for x in [str(j.get("type") or ""), budget,
                                           str(j.get("experienceLevel") or "")] if x),
            "company": "Upwork", "location": str(j.get("clientCountry") or ""),
            "budget": budget, "url": str(j.get("url") or ""),
            "signal": f"Upwork job · proposals {j.get('proposalsTier') or '?'}"
                      + (f" · client ★{j['clientRating']}" if j.get("clientRating") else ""),
            "posted_at": to_iso(j.get("publishedOn")),
            "snippet": ", ".join(j.get("skills") or []) if isinstance(j.get("skills"), list) else str(j.get("skills") or ""),
        })
    return leads[:n]


# ---------------------------------------------------------------- LinkedIn

LI_AGE_RE = re.compile(r"^(\d+)\s*(m|h|d|w|mo|yr)\s*•", re.M)
LI_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400, "yr": 365 * 86400}
OPEN_TO_WORK_RE = re.compile(r"#?open ?to ?work|seeking (?:a )?new role|i'?m looking for .{0,40}roles?\b|"
                             r"looking for (?:a )?(?:new )?(?:job|role|position|opportunit)", re.I)


def _parse_li_post(block: str) -> dict | None:
    prof = re.search(r"\((https://www\.linkedin\.com/(?:in|company)/[^)/?]+)/?[^)]*\)", block)
    age = LI_AGE_RE.search(block)
    if not prof or not age:
        return None
    lines = [ln.strip() for ln in block.splitlines()]
    name = ""
    for i, ln in enumerate(lines):
        if re.match(r"^•\s*(1st|2nd|3rd\+?|Following)", ln):
            prev = [x for x in lines[:i] if x and x not in ("[", "]") and not x.startswith(("!", "]("))]
            name = prev[-1] if prev else ""
            break
    head_start = block.find("](" + prof.group(1))
    after = block[age.end():]
    after = after.split("Follow", 1)[1] if "\nFollow\n" in after[:400] else after
    text = re.split(r"\n(?:Like|\[\s*\n\s*\d+ reactions?|Only connections can comment)", after)[0]
    text = re.sub(r"\[\*\*(#?\w+)\*\*\]\([^)]*\)", r"\1", text)      # [**#tag**](url) -> #tag
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)|\]\([^)]*\)|^\[$|…\s*more", " ", text, flags=re.M)
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()
    headline = ""
    m = re.search(r"\n•\s*(?:1st|2nd|3rd\+?|Following)\s*\n+\]\([^)]*\)\s*\n+([^\n]+)", block)
    if m:
        headline = m.group(1).strip()
    secs = int(age.group(1)) * LI_UNITS[age.group(2)]
    return {"name": name or prof.group(1).rsplit("/", 1)[-1].replace("-", " ").title(),
            "profile": prof.group(1), "headline": headline, "text": text,
            "posted_at": to_iso(int(time.time() - secs)), "_head": head_start}




def linkedin_posts(query: str, n: int, days: int = 7) -> list[dict]:
    """Latest LinkedIn posts via LinkedIn's own content search, read through your Chrome
    session (OpenCLI). One results page per keyword to stay gentle on your account."""
    from urllib.parse import quote
    window = "past-24h" if days <= 1 else "past-week" if days <= 7 else "past-month"
    url = (f"https://www.linkedin.com/search/results/content/?keywords={quote(query)}"
           f'&datePosted=%22{window}%22&sortBy=%22date_posted%22')
    with tempfile.TemporaryDirectory() as out:
        run_tool(["opencli", "web", "read", "--url", url, "--output", out, "--download-images", "false",
                  "--wait", "4", "-f", "json"], timeout=180)
        md_files = list(Path(out).rglob("*.md"))
        if not md_files:
            raise SourceError("LinkedIn posts: page was empty — log in to linkedin.com in Chrome")
        page = md_files[0].read_text(encoding="utf-8")
    if "## Feed post" not in page:
        raise SourceError("LinkedIn posts: no results page — log in to linkedin.com in Chrome")
    leads = []
    for block in page.split("## Feed post")[1:]:
        post = _parse_li_post(block)
        if not post:
            continue
        text = post["text"]
        if OPEN_TO_WORK_RE.search(text) or not client_post(text):
            continue  # job seekers, "we're hiring" job ads, freelancers selling
        leads.append({
            "kind": "hiring", "name": post["name"],
            "title": text.split("\n", 1)[0][:200], "company": post["headline"][:120],
            "email": ", ".join(clean_emails(_deobfuscate(text))[:2]),
            "phone": ", ".join(clean_phones(text)[:1]),
            "url": post["profile"], "profile": post["profile"], "linkedin": post["profile"],
            "signal": "LinkedIn post: hiring", "posted_at": post["posted_at"],
            "snippet": text[:400].replace("\n", " "),
        })
    time.sleep(3)  # pace page loads
    return leads[:n]


def linkedin_jobs(query: str, n: int, days: int = 7) -> list[dict]:
    """LinkedIn contract / part-time / temporary jobs via OpenCLI (your Chrome login)."""
    q = skill_terms(query)
    if not q:
        return []
    window = "24h" if days <= 1 else "week" if days <= 7 else "month"
    try:
        raw = run_tool(["opencli", "linkedin", "search", q, "--job-type", "contract,part-time,temporary",
                        "--date-posted", window, "--limit", str(min(100, n)), "-f", "json"], timeout=300)
        items = json.loads(raw)
    except (SourceError, json.JSONDecodeError) as e:
        raise SourceError(f"LinkedIn jobs failed — log in to linkedin.com in Chrome ({e})")
    leads = []
    for j in items if isinstance(items, list) else []:
        if not SOFTWARE_RE.search(j.get("title", "")):
            continue  # LinkedIn's fuzzy matching returns e.g. "LIC agent" for "mobile app"
        leads.append({
            "kind": "project", "name": j.get("title", "")[:200],
            "title": j.get("company", ""), "company": j.get("company", ""),
            "location": j.get("location", ""), "budget": j.get("salary", ""),
            "url": j.get("url", ""), "signal": "LinkedIn contract job",
            "posted_at": to_iso(j.get("listed")), "snippet": f"{j.get('company', '')} · {j.get('location', '')}",
        })
    return leads


# ---------------------------------------------------------------- India: Chennai / Noida / Gurugram
# Agent Reach `india_jobs` channel (skill/references/career.md). Wellfound slugs were checked
# live: Chennai is "madras" and Gurugram is "gurgaon" — "chennai" falls back to a global list
# and "gurugram" returns 0 results. YC has no Chennai page.

INDIA_CITIES = {
    # name: (words that must appear in a job's location, Wellfound slug, YC slug or None)
    "Chennai": (("chennai", "madras"), "madras", None),
    "Noida": (("noida",), "noida", "noida"),
    "Gurugram": (("gurugram", "gurgaon"), "gurgaon", "gurugram"),
}


def india_cities(query: str) -> tuple[str, list[str]]:
    """'react in Noida' -> ('react', ['Noida']); no known city -> all three."""
    m = re.match(r"^(.*)\s+in\s+(.+)$", query.strip(), re.I)
    if m:
        for city, (words, _, _) in INDIA_CITIES.items():
            if m.group(2).strip().lower() in (city.lower(), *words):
                return m.group(1).strip(), [city]
    return query.strip(), list(INDIA_CITIES)


def jobspy_installed() -> bool:
    import importlib.util
    return importlib.util.find_spec("jobspy") is not None


def _val(v) -> str:
    return "" if v is None or v != v else str(v).strip()  # v != v catches pandas NaN


def india_jobs(query: str, n: int, days: int = 7, sites: tuple[str, ...] = ("naukri", "linkedin")) -> list[dict]:
    """Naukri / LinkedIn jobs per city via python-jobspy (no login)."""
    if not jobspy_installed():
        raise SourceError("python-jobspy not installed — run: .venv/bin/pip install python-jobspy")
    from jobspy import scrape_jobs

    kw, cities = india_cities(query)
    # "company:Cityfurnish" = every open role at one company (the India page's "Find jobs here").
    company_only = kw[8:].strip() if kw.lower().startswith("company:") else ""
    terms = company_only or skill_terms(kw)
    if not terms:
        return []  # pure intent phrase ("need a developer") — not a job search term
    if not company_only and not re.search(r"\b(develop|engineer|designer|manager|analyst|architect)", terms, re.I):
        terms += " developer"
    leads, seen = [], set()
    for city in cities:
        words = INDIA_CITIES[city][0]
        try:
            df = scrape_jobs(site_name=list(sites), search_term=terms, location=city,
                             results_wanted=min(n, 50), hours_old=max(24, days * 24))
        except Exception as e:
            raise SourceError(f"JobSpy failed for {city}: {e}")
        kept = 0
        for j in df.to_dict("records"):
            if kept >= n:
                break  # cap per city, so one busy city can't crowd out the others
            loc = _val(j.get("location"))
            # LinkedIn mixes in neighbouring NCR cities; keep only the city asked for.
            if not any(w in loc.lower() for w in words):
                continue
            title, company = _val(j.get("title")), _val(j.get("company"))
            key = (city, title.lower(), company.lower())  # Naukri repeats a job under several URLs
            if key in seen:
                continue
            if company_only:
                if company_only.lower() not in company.lower():
                    continue  # job boards fill up with lookalike companies
            elif not SOFTWARE_RE.search(title):
                continue  # Naukri's fuzzy match returns e.g. "IT Recruiter" for "React"
            seen.add(key)
            kept += 1
            lo, hi, cur = _val(j.get("min_amount")), _val(j.get("max_amount")), _val(j.get("currency"))
            pay = f"{cur} {lo}–{hi}".strip() if lo or hi else ""
            leads.append({
                "kind": "job", "name": title[:200], "title": title, "company": company,
                "location": loc, "budget": pay, "url": _val(j.get("job_url")),
                "website": _val(j.get("company_url_direct")),
                "signal": f"Hiring in {city} ({_val(j.get('site')).title()})",
                "posted_at": to_iso(_val(j.get("date_posted"))),
                "snippet": " · ".join(x for x in [company, loc, _val(j.get("experience_range"))] if x),
            })
    return leads


def india_naukri(query: str, n: int, days: int = 7) -> list[dict]:
    return india_jobs(query, n, days, sites=("naukri",))


def india_linkedin(query: str, n: int, days: int = 7) -> list[dict]:
    return india_jobs(query, n, days, sites=("linkedin",))


_startup_cache: dict[tuple, tuple[float, list[dict]]] = {}


def _reader(url: str) -> str:
    """Jina Reader (free, no key) — same fallback Agent Reach uses for web pages."""
    return _http_get("https://r.jina.ai/" + url, timeout=60)


def _wellfound_city(city: str, slug: str, n: int = 10) -> list[dict]:
    """Hiring startups from Wellfound's city list, reading as many pages (~10 each, max 5) as n needs."""
    leads, pages = [], 1
    page = 1
    while page <= pages and len(leads) < n:
        text = _reader(f"https://wellfound.com/startups/location/{slug}" + (f"?page={page}" if page > 1 else ""))
        m = re.search(r"#### (\d+) results total", text)
        title = text.split("\n", 1)[0].lower()  # e.g. "...Startups in Gurgaon, India" for Gurugram
        if not m or m.group(1) == "0" or not any(w in title for w in INDIA_CITIES[city][0]):
            if page == 1:
                raise SourceError(f"Wellfound returned no {city} list (slug '{slug}' may have changed)")
            break
        total = re.search(r"Page \d+ of (\d+)", text)
        pages = min(5, int(total.group(1)) if total else 1)
        for block in re.split(r"\n(?=\[!\[Image \d+: [^\]]+ company logo\])", text)[1:]:
            name = re.match(r"\[!\[Image \d+: (.+?) company logo\]", block)
            slug_m = re.search(r"wellfound\.com/company/([\w-]+)", block)
            if not name or not slug_m:
                continue
            jobs = re.search(r"View all (\d+) jobs? at", block)
            if not jobs:
                continue  # not hiring right now
            tagline = re.search(r"\n\n([^\n\[*#!][^\n]{10,}?)\s+([\d,]+-[\d,]+|[\d,]+\+) Employees", block)
            site = re.search(r"Website\[(https?://[^\]]+)\]", block)
            size = re.search(r"Employees ([\d,+-]+)", block)
            leads.append({
                "kind": "company", "name": name.group(1), "company": name.group(1),
                "location": city, "website": site.group(1) if site else "",
                "size": size.group(1) if size else "",
                "url": f"https://wellfound.com/company/{slug_m.group(1)}/jobs",
                "signal": f"Startup hiring in {city} — {jobs.group(1)} open jobs on Wellfound",
                "snippet": tagline.group(1).strip() if tagline else "",
            })
        page += 1
    return leads


_yc_cache: dict[str, tuple[float, list[dict]]] = {}


def _yc_postings(slug: str) -> list[dict]:
    """Every posting on YC's city jobs page, read from the page's embedded JSON.
    Noida and Gurugram share one Delhi-NCR list, so callers filter by location."""
    import html as _html
    hit = _yc_cache.get(slug)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    page = _http_get(f"https://www.ycombinator.com/jobs/location/{slug}", timeout=30)
    m = re.search(r'data-page="([^"]+)"', page)
    if not m:
        raise SourceError("YC jobs page changed — no job data found")
    posts = json.loads(_html.unescape(m.group(1))).get("props", {}).get("jobPostings") or []
    _yc_cache[slug] = (time.time(), posts)
    return posts


def _in_city(text: str, city: str) -> bool:
    return any(w in (text or "").lower() for w in INDIA_CITIES[city][0])


def _yc_age(s: str) -> str:
    """YC's '2 days' / 'about 1 year' -> ISO date."""
    m = re.search(r"(\d+|an?)\s+(minute|hour|day|week|month|year)", s or "")
    if not m:
        return ""
    n, unit = (1 if m.group(1) in ("a", "an") else int(m.group(1))), m.group(2)
    if unit == "year":
        n, unit = n * 12, "month"
    return to_iso(f"{n} {unit}s ago")


def _yc_city(city: str, slug: str, n: int = 10) -> list[dict]:
    """YC startups with open roles in the city (one lead per company)."""
    companies: dict[str, dict] = {}
    for p in _yc_postings(slug):
        if not _in_city(p.get("location", ""), city):
            continue
        name = p.get("companyName", "")
        c = companies.setdefault(name, {
            "kind": "company", "name": name, "company": name, "location": city,
            "url": "https://www.ycombinator.com" + p.get("companyUrl", "") + "/jobs",
            "snippet": p.get("companyOneLiner", ""), "_jobs": 0, "_batch": p.get("companyBatchName", "")})
        c["_jobs"] += 1
    leads = []
    for c in companies.values():
        c["signal"] = f"YC {c.pop('_batch')} startup hiring in {city} — {c.pop('_jobs')} open jobs"
        leads.append(c)
    return leads


def india_yc_jobs(query: str, n: int, days: int = 30) -> list[dict]:
    """Individual YC job postings in the city (title, salary, experience, skills)."""
    _, cities = india_cities(query)
    leads = []
    for city in cities:
        slug = INDIA_CITIES[city][2]
        if not slug:
            continue  # YC has no Chennai page
        kept = 0
        for p in _yc_postings(slug):
            if kept >= n or not _in_city(p.get("location", ""), city):
                continue
            kept += 1
            co, batch = p.get("companyName", ""), p.get("companyBatchName", "")
            skills = ", ".join(s.get("name", s) if isinstance(s, dict) else str(s) for s in p.get("skills") or [])
            leads.append({
                "kind": "job", "name": p.get("title", "")[:200], "title": p.get("title", ""), "company": co,
                "location": p.get("location", ""), "budget": p.get("salaryRange", ""),
                "url": "https://www.ycombinator.com" + p.get("url", ""),
                "signal": f"Hiring in {city} (YC {batch})", "posted_at": _yc_age(p.get("createdAt", "")),
                "snippet": " · ".join(x for x in [f"{co} (YC {batch})", p.get("location", ""),
                                                  p.get("minExperience") or "", p.get("type") or "", skills] if x),
            })
    return leads


# ---- Hiring posts (X / Reddit / LinkedIn) in the city

# Client posts: people in the city who want a developer / agency. Clients rarely name a tech
# stack, so these search for the request itself; the role chips only drive the job boards.
# Broad on purpose (an exact-phrase list found ~1 tweet a month); client_post() does the picking.
CLIENT_ASK = '("need a" OR "need an" OR "looking for" OR "anyone know" OR "recommend" OR "need someone")'
CLIENT_WHAT = '(developer OR "web developer" OR "app developer" OR agency OR freelancer OR website OR app)'


def india_x(query: str, n: int, days: int = 7) -> list[dict]:
    """Tweets from people in the city who need a developer or agency."""
    _, cities = india_cities(query)
    leads, errors = [], []
    for city in cities:
        places = " OR ".join(INDIA_CITIES[city][0])
        q = f"({places}) {CLIENT_ASK} {CLIENT_WHAT} {X_EXCLUDE} since:{since_date(days)}"
        tweets = None
        for backend in (_tweets_opencli, _tweets_twitter_cli):
            try:
                tweets = backend(q, n)
                break
            except (SourceError, json.JSONDecodeError) as e:
                errors.append(str(e))
        for t in tweets or []:
            text, handle = t["text"] or "", t["handle"]
            if not handle or text.lstrip().startswith("@") or not _in_city(text, city) or not client_post(text):
                continue
            leads.append({
                "kind": "hiring", "name": f"@{handle}", "title": text.split("\n", 1)[0][:200],
                "email": ", ".join(clean_emails(_deobfuscate(text))[:2]),
                "twitter": f"https://x.com/{handle}", "url": t["url"], "profile": f"https://x.com/{handle}",
                "location": city, "signal": f"X: client in {city}", "posted_at": t["date"],
                "snippet": text[:400].replace("\n", " "),
            })
    if not leads and errors:
        raise SourceError("; ".join(errors[:2]))
    return leads


INDIA_SUBS = {
    "Chennai": ["Chennai", "developersIndia", "IndianStartups", "StartUpIndia"],
    "Noida": ["noida", "delhi", "developersIndia", "IndianStartups"],
    "Gurugram": ["gurgaon", "delhi", "developersIndia", "IndianStartups"],
}


def india_reddit(query: str, n: int, days: int = 7) -> list[dict]:
    """Reddit posts from people who need a developer: the city's own sub plus India-wide subs."""
    _, cities = india_cities(query)
    window = "day" if days <= 1 else "week" if days <= 7 else "month"
    leads, errors, seen = [], [], set()
    for city in cities:
        for sub in INDIA_SUBS[city]:
            city_sub = sub.lower() in INDIA_CITIES[city][0] or sub == "Chennai"
            q = "developer" if city_sub else f"developer {city}"
            try:
                items = json.loads(run_tool(["opencli", "reddit", "search", q, "--subreddit", sub, "--sort", "new",
                                             "--time", window, "--limit", "25", "-f", "json"], timeout=180))
            except (SourceError, json.JSONDecodeError) as e:
                errors.append(f"r/{sub}: {e}")
                continue
            for it in items if isinstance(items, list) else []:
                title, body = it.get("title", ""), it.get("selftext", "") or ""
                text, author, url = f"{title}\n{body}", it.get("author") or "", it.get("url", "")
                if (not author or author in ("[deleted]", "AutoModerator") or url in seen
                        or not (city_sub or _in_city(text, city)) or not client_post(text, title)):
                    continue
                seen.add(url)
                leads.append({
                    "kind": "hiring", "name": f"u/{author}", "title": title[:200],
                    "company": f"r/{sub}", "email": ", ".join(clean_emails(_deobfuscate(text))[:2]),
                    "url": url, "profile": f"https://www.reddit.com/user/{author}", "location": city,
                    "signal": f"Reddit: client in {city}", "posted_at": to_iso(it.get("created_utc")),
                    "snippet": text[:400].replace("\n", " "),
                })
    if not leads and errors:
        raise SourceError("; ".join(errors[:2]))
    return leads[:n * len(cities)]


def india_li_posts(query: str, n: int, days: int = 7) -> list[dict]:
    """LinkedIn posts from people in the city looking for a developer / agency (your Chrome login)."""
    _, cities = india_cities(query)
    leads = []
    for city in cities:
        q = f'("looking for a developer" OR "need a developer" OR "looking for an agency") {city}'
        for lead in linkedin_posts(q, n, days):
            lead.update(location=city, signal=f"LinkedIn post: client in {city}")
            leads.append(lead)
    return leads


# ---- Startups that just raised money in the city (Google News India, free RSS)

def _exa_company(name: str) -> dict | None:
    """Exa company profile (HQ, size, website…) for an exact name match."""
    norm = re.sub(r"[^a-z0-9]", "", name.lower())
    try:
        for c in exa_companies(name, 3):
            if norm and norm in re.sub(r"[^a-z0-9]", "", (c["name"] + c["website"]).lower()):
                return c
    except SourceError:
        pass
    return None


def _headcount(size: str) -> int:
    m = re.search(r"[\d,]+", size or "")
    return int(m.group(0).replace(",", "")) if m else 0


def india_funding(query: str, n: int, days: int = 30) -> list[dict]:
    """Startups in the city that just raised money. Headlines rarely name the city, so a
    candidate is kept when the headline does, or when an Exa lookup puts its HQ there."""
    from urllib.parse import quote_plus
    _, cities = india_cities(query)
    leads, seen = [], set()
    for city in cities:
        candidates = []
        for word in INDIA_CITIES[city][0]:
            for q in (f'"{word}" startup raises', f'"{word}-based" raises funding'):
                url = (f"https://news.google.com/rss/search?q={quote_plus(q + f' when:{days}d')}"
                       "&hl=en-IN&gl=IN&ceid=IN:en")
                try:
                    candidates += _rss_items(_http_get(url))
                except Exception as e:
                    raise SourceError(f"Google News: {e}")
        found, lookups = 0, 0
        for it in candidates:
            if found >= n:
                break
            # "Gurugram-based Foo raises…" / "Noida's Foo raises…" -> "Foo raises…"
            head = re.sub(r"^\s*(?:[\w&.-]+\s+){0,2}?[\w-]+(?:-based|-headquartered|'s|’s)\s+", "",
                          it["title"], flags=re.I)
            f = parse_funding(head)
            if not f:
                continue
            # "NBFC Seeds Fincap" / "D2C brand Foo" -> "Seeds Fincap" / "Foo"
            f["company"] = re.sub(r"^(?:(?:NBFC|D2C|B2B|SaaS|AI|EV|fintech|edtech|healthtech|agritech|"
                                  r"deep-?tech|brand|platform)\s+)+", "", f["company"], flags=re.I) or f["company"]
            key = f["company"].lower()
            if any(key in x or x in key for x in seen):
                continue  # same company under a slightly different headline name
            seen.add(key)
            named = _in_city(it["title"], city)
            if lookups >= 20 and not named:
                continue
            lookups += 1
            co = _exa_company(f["company"]) or {}
            if not named and not _in_city(co.get("location", ""), city):
                continue  # e.g. a Bengaluru startup that only mentions the city in passing
            if _headcount(co.get("size", "")) > 2000:
                continue  # a large company raising money (e.g. Landmark Group), not a startup
            extra = {k: co[k] for k in ("website", "email", "phone", "linkedin", "size") if co.get(k)}
            found += 1
            leads.append({
                "kind": "funded", "name": f["company"], "company": f["company"], "location": city,
                "funding": f["funding"], "signal": it["title"], "url": it["link"],
                "posted_at": it["date"], "snippet": f"{it['title']} ({it['source']})", **extra,
            })
    return leads


def india_startups(query: str, n: int, sites: tuple[str, ...] = ("wellfound", "yc")) -> list[dict]:
    """Startups hiring in Chennai / Noida / Gurugram (Wellfound + YC city pages)."""
    _, cities = india_cities(query)
    leads, errors = [], []
    for city in cities:
        _, wf_slug, yc_slug = INDIA_CITIES[city]
        for site, fetch, slug in (("wellfound", _wellfound_city, wf_slug), ("yc", _yc_city, yc_slug)):
            if site not in sites or not slug:
                continue
            cached = _startup_cache.get((site, city, n))
            if cached and time.time() - cached[0] < 900:  # one fetch per city per campaign
                leads += cached[1][:n]
                continue
            try:
                found = fetch(city, slug, n)
            except Exception as e:
                errors.append(str(e))
                continue
            _startup_cache[(site, city, n)] = (time.time(), found)
            leads += found[:n]  # cap per city and site
    if not leads and errors:
        raise SourceError("; ".join(errors))
    return leads


def india_wellfound(query: str, n: int) -> list[dict]:
    return india_startups(query, n, sites=("wellfound",))


def india_yc(query: str, n: int) -> list[dict]:
    return india_startups(query, n, sites=("yc",))

SOURCES = {
    "linkedin_posts": {"label": "LinkedIn hiring posts", "fn": linkedin_posts, "max": 100, "needs": "opencli"},
    "linkedin_jobs": {"label": "LinkedIn contract jobs", "fn": linkedin_jobs, "max": 100, "needs": "opencli"},
    "freelancer": {"label": "Projects — Freelancer.com (no login)", "fn": freelancer_projects, "max": 500, "needs": None},
    "upwork": {"label": "Projects — Upwork (Chrome login)", "fn": upwork_jobs, "max": 50, "needs": "opencli"},
    "funding_news": {"label": "Funded startups — Google News", "fn": funding_news, "max": 100, "needs": None},
    "funding_feeds": {"label": "Funded startups — TechCrunch/VentureBeat", "fn": funding_feeds, "max": 100, "needs": None},
    "hn_contract": {"label": "Outsourcing — Hacker News contract jobs", "fn": hn_contract, "max": 500, "needs": None},
    "reddit_hiring": {"label": "Outsourcing — Reddit hiring posts", "fn": reddit_hiring, "max": 300, "needs": "opencli"},
    "exa_companies": {"label": "Companies (web)", "fn": exa_companies, "max": 100, "needs": "mcporter"},
    "exa_people": {"label": "People (LinkedIn profiles)", "fn": exa_people, "max": 100, "needs": "mcporter"},
    "github": {"label": "GitHub users", "fn": github_users, "max": 1000, "needs": "gh"},
    "reddit": {"label": "Reddit posts (intent)", "fn": reddit_posts, "max": 100, "needs": "opencli"},
    "twitter": {"label": "Outsourcing — Twitter/X posts", "fn": twitter_posts, "max": 100, "needs": "twitter"},
    "india_jobs": {"label": "India jobs — Chennai/Noida/Gurugram (Naukri + LinkedIn)", "fn": india_jobs, "max": 50, "needs": None},
    "india_startups": {"label": "India startups hiring — Chennai/Noida/Gurugram (Wellfound + YC)", "fn": india_startups, "max": 200, "needs": None},
    "india_naukri": {"label": "India jobs — Naukri", "fn": india_naukri, "max": 50, "needs": None},
    "india_linkedin": {"label": "India jobs — LinkedIn", "fn": india_linkedin, "max": 50, "needs": None},
    "india_wellfound": {"label": "India startups — Wellfound", "fn": india_wellfound, "max": 50, "needs": None},
    "india_yc": {"label": "India startups — YC", "fn": india_yc, "max": 50, "needs": None},
    "india_yc_jobs": {"label": "India jobs — Y Combinator", "fn": india_yc_jobs, "max": 50, "needs": None},
    "india_x": {"label": "India hiring posts — X", "fn": india_x, "max": 50, "needs": "twitter"},
    "india_reddit": {"label": "India hiring posts — Reddit", "fn": india_reddit, "max": 50, "needs": "opencli"},
    "india_li_posts": {"label": "India hiring posts — LinkedIn", "fn": india_li_posts, "max": 50, "needs": "opencli"},
    "india_funding": {"label": "India funded startups — Google News", "fn": india_funding, "max": 50, "needs": None},
}


def source_status() -> dict:
    status = {}
    for key, s in SOURCES.items():
        ok = s["needs"] is None or bool(which(s["needs"]))
        note = "" if ok else f"{s['needs']} not installed"
        if key in ("twitter", "india_x"):
            # OpenCLI (Chrome session) is tried first; twitter-cli cookies are the fallback.
            ok, note = bool(which("opencli")), ""
            if not ok:
                try:
                    _twitter_env()
                    ok = bool(which("twitter"))
                except SourceError as e:
                    note = str(e)
        if key in ("india_jobs", "india_naukri", "india_linkedin") and not jobspy_installed():
            ok, note = False, "python-jobspy not installed"
        status[key] = {"label": s["label"], "ok": ok, "note": note, "max": s["max"]}
    return status
