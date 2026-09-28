#!/usr/bin/env python3
"""Daily refresh of the sde-job-seeker share board's job snapshot.

For each company in data/companies.json with a public ATS board
(Greenhouse / Lever / Ashby), pulls open postings, keeps SWE titles in
Remote / Seattle-metro locations, extracts JD skill keywords with the
shared vocab (same matching rule as the page's resume parser), and
writes data/jobs.json + data/meta.json.

Companies without a mappable public board (ats=null) keep their
previous snapshot entries, flagged with "carried": true.

Usage: python3 scripts/refresh_jobs.py [--companies out.json]
Safe to run daily; only rewrites data/jobs.json and data/meta.json.
"""
import html as htmlmod
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

INCLUDE = ["software engineer", "software development engineer", "sde",
           "backend", "back-end", "systems engineer", "infrastructure",
           "platform engineer", "data engineer", "distributed",
           "full stack", "fullstack", "full-stack"]
EXCLUDE = ["frontend", "front-end", "front end", "ios", "android", "mobile",
           "data scientist", "research scientist", "applied scientist",
           "product manager", "engineering manager", "manager,",
           "designer", "sales", "recruit", "support", "staff ",
           "staff,", "principal", "distinguished", "fellow",
           "director", "vp ", "vice president", "chief", "intern",
           "tax", "legal", "finance", "accounting", "counsel", "staff",
           # obvious non-SWE / junior roles that slip through loose INCLUDE terms
           "facilities", "electrical engineer", "data center",
           "program manager", "new grad", "newgrad",
           "entry level", "entry-level",
           "it systems engineer", "it infrastructure engineer",
           "manufacturing", "mechanical engineer", "quality engineer",
           "sourcing operations"]
DATA_AI = ["data", "infrastructure", "platform", "distributed", "streaming",
           "kafka", "database", "ml", "machine learning", "ai ", "llm",
           "vector", "etl", "warehouse", "lakehouse"]
SEATTLE_METRO = ("seattle", "bellevue", "kirkland", "redmond")


def get_json(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def fetch_board(ats, slug):
    """Return list of (ext_id, title, location, url, description_html)."""
    if ats == "greenhouse":
        d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
        return [(str(j.get("id")), j.get("title", ""),
                 (j.get("location") or {}).get("name", ""),
                 j.get("absolute_url", ""),
                 j.get("content", "") or "") for j in d.get("jobs", [])]
    if ats == "lever":
        d = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        return [(str(j.get("id")), j.get("text", ""),
                 (j.get("categories") or {}).get("location", ""),
                 j.get("hostedUrl", ""),
                 j.get("description", "") or "") for j in (d or [])]
    if ats == "ashby":
        d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        out = []
        for j in d.get("jobs", []):
            locs = [j.get("location") or ""]
            locs += [(s or {}).get("location") or "" for s in (j.get("secondaryLocations") or [])]
            if j.get("isRemote"):
                locs.append("Remote")
            loc = "; ".join(x for x in locs if x)
            out.append((str(j.get("id")), j.get("title", ""), loc,
                        f"https://jobs.ashbyhq.com/{slug}/{j.get('id')}",
                        j.get("descriptionHtml", "") or ""))
        return out
    raise ValueError(f"unknown ats {ats}")


def title_ok(t):
    tl = t.lower()
    return any(k in tl for k in INCLUDE) and not any(k in tl for k in EXCLUDE)


def loc_ok(loc):
    ll = loc.lower()
    return ("remote" in ll) or any(k in ll for k in SEATTLE_METRO)


def loc_label(loc):
    ll = loc.lower()
    remote = "remote" in ll
    sea = any(k in ll for k in SEATTLE_METRO)
    if remote and sea:
        return "Remote / Seattle"
    if remote:
        return "Remote"
    if sea:
        return "Seattle"
    return loc.strip()[:60]


def level_of(title):
    tl = title.lower()
    if any(k in tl for k in ["senior", "sr.", "sr ", "lead ", "principal", "staff"]):
        return "Senior"
    return "SDE2/mid"


def tag_of(title):
    tl = title.lower()
    return "数据/AI" if any(k in tl for k in DATA_AI) else "通用后端"


def strip_html(html_text):
    text = re.sub(r"<script.*?</script>", " ", html_text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmlmod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def keyword_patterns(vocab):
    pats = []
    for kw in vocab:
        body = r"\s+".join(re.escape(p) for p in kw.split())
        pats.append((kw, re.compile(r"(^|[^a-z0-9+.#])" + body + r"(?=$|[^a-z0-9+.#])", re.I)))
    return pats


def extract_keywords(text, patterns):
    return sorted({kw for kw, pat in patterns if pat.search(text)})


def main():
    companies = json.load(open(os.path.join(DATA, "companies.json"), encoding="utf-8"))
    vocab = json.load(open(os.path.join(DATA, "vocab.json"), encoding="utf-8"))
    patterns = keyword_patterns(vocab)

    prev_jobs = []
    jobs_path = os.path.join(DATA, "jobs.json")
    if os.path.exists(jobs_path):
        prev_jobs = json.load(open(jobs_path, encoding="utf-8"))

    jobs, errors, seen = [], [], set()
    per_company = {}
    for comp in companies:
        name, ats, slug = comp["name"], comp.get("ats"), comp.get("slug")
        if not ats or not slug:
            continue
        try:
            postings = fetch_board(ats, slug)
        except Exception as e:  # board moved / down: keep board alive, contribute nothing
            errors.append(f"{name}: {type(e).__name__} {str(e)[:80]}")
            per_company[name] = 0
            continue
        n = 0
        for ext_id, title, loc, url, desc_html in postings:
            if not title or not title_ok(title) or not loc_ok(loc):
                continue
            label = loc_label(loc)
            key = (name, title.strip().lower(), label)
            if key in seen:
                continue
            seen.add(key)
            text = strip_html(desc_html) if desc_html else ""
            kws = extract_keywords(text, patterns) if text else []
            jobs.append({
                "company": name,
                "title": title.strip(),
                "location": label,
                "tag": tag_of(title),
                "level": level_of(title),
                "url": url,
                "jd_fetched": bool(text),
                "jd_keywords": kws,
                "jd_note": f"{len(text)} chars",
            })
            n += 1
        per_company[name] = n

    # carry over companies without a public board
    carried = 0
    no_api = {c["name"] for c in companies if not c.get("ats")}
    for j in prev_jobs:
        if j.get("company") in no_api:
            j = dict(j)
            j["carried"] = True
            jobs.append(j)
            carried += 1

    jobs.sort(key=lambda j: (j["company"].lower(), j["title"].lower()))
    json.dump(jobs, open(jobs_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    meta = {"updated_at": today, "jobs": len(jobs),
            "companies": len(companies),
            "companies_with_jobs": len({j["company"] for j in jobs}),
            "carried_over": carried,
            "errors": errors,
            "note": "refreshed daily by .github/workflows/refresh.yml"}
    json.dump(meta, open(os.path.join(DATA, "meta.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    print(f"REFRESH date={today} jobs={len(jobs)} "
          f"companies_with_jobs={meta['companies_with_jobs']} carried={carried}")
    if errors:
        print("ERRORS:")
        for e in errors:
            print("  " + e)


if __name__ == "__main__":
    main()
