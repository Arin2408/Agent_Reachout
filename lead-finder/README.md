# Lead Finder

Local web app for finding leads in bulk, using the tools Agent Reach installed.

## Start

```bash
bash lead-finder/run.sh   # from the repo root
```

Opens http://127.0.0.1:5050. Stop with Ctrl+C. Leads are saved in `leads.db` (SQLite) in this folder.

## Daily campaign (Reddit, X, LinkedIn)

1. Be logged in to reddit.com, x.com and linkedin.com in Chrome (OpenCLI extension installed).
2. Pick the work you do, keep Reddit / X / LinkedIn ticked, choose **Only posts from the last**
   (24 hours is best for daily runs), click **Find people who want to hire**.
3. Leads land in the **New** tab, newest post first. Open post → Message → mark *contacted*.

Every source drops posts older than the window you pick; the list's "Posted within" filter
hides older leads already in the database. LinkedIn posts are read from LinkedIn's own
"Latest" search — one page per keyword, with a pause between pages, to protect your account.

## Presets (one click)

- **Recently funded startups** — funding headlines from Google News + TechCrunch/VentureBeat
  (free RSS). Each startup is then looked up for website, email, phone, company LinkedIn and
  founders' LinkedIn profiles, and its website is checked for emails. A startup that just raised
  money usually needs to build fast — good time to pitch.
- **Companies wanting to outsource** — Hacker News "Who is hiring" posts offering contract /
  freelance / part-time work (often with an email), plus Reddit `[Hiring]` and "need a developer"
  posts from r/forhire, r/startups, r/SaaS, r/Entrepreneur and similar. Reddit leads are contacted
  by DM via their profile link.

Everything is free: Google News RSS, Hacker News API, Exa's free MCP endpoint, OpenCLI (your own
Chrome login) and the GitHub CLI.

## How to get hundreds of leads

Each line in **What are you looking for?** is combined with each line in **Locations**, for every
source you tick. 3 keywords × 5 cities × 1 source × 100 results = 15 queries, up to 1,500 leads.
Duplicates (same website / LinkedIn / profile) are skipped automatically.

| Source | Gives you | Needs |
|---|---|---|
| Companies (web) | Business name, website, email, phone, LinkedIn, HQ, staff count | mcporter + Exa (free) |
| People (LinkedIn profiles) | Name, role, company, location, LinkedIn URL | mcporter + Exa (free) |
| GitHub users | Developers: name, bio, company, location, website, X | `gh auth login` |
| Reddit posts | People posting about your keyword (buying intent) | OpenCLI + logged in to reddit.com in Chrome |
| Twitter/X posts | People tweeting about your keyword | `agent-reach configure twitter-cookies` |

**Find emails on websites** visits each company's homepage and /contact page to pull emails and
phone numbers for leads that don't have one yet.

Use **Export CSV** to download the leads that match your current filters (or only the selected ones).
