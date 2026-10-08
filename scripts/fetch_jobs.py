#!/usr/bin/env python3
"""
Fetch Seattle-area nutrition, public health, research and food-regulatory
roles from several sources, score them against config.json, and write
data/jobs.json for the web page to read.

Each source runs on its own: if one fails (a site changed, an API key is
missing), the others still run and the page shows which source had trouble.

Run locally:   pip install -r requirements.txt && python scripts/fetch_jobs.py
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
OUT_PATH = ROOT / "data" / "jobs.json"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 job-radar/1.0 (personal job search, runs twice a day)"
)
TIMEOUT = 30
PAUSE = 0.6  # seconds between requests to the same site

# Direct employer sources win over aggregators when the same job shows up twice.
SOURCE_PRIORITY = {
    "workday": 5, "icims": 5, "amazon": 5, "neogov_rss": 5, "usajobs": 5,
    "uw_sph_board": 3, "adzuna": 1,
}


class SkipSource(Exception):
    """Raised when a source is not set up (for example, a missing API key)."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat() if dt else None


def clean(text) -> str:
    """Strip tags, unescape entities, collapse whitespace."""
    if text is None:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(text))
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def parse_date(value) -> datetime | None:
    """Understands ISO dates, RSS dates, 'September 25, 2026', and Workday's 'Posted 3 Days Ago'."""
    if not value:
        return None
    s = clean(value)
    now = utcnow()
    low = s.lower()

    if "today" in low or "just posted" in low:
        return now
    if "yesterday" in low:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*days?\s*ago", low)
    if m:
        return now - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\+?\s*hours?\s*ago", low)
    if m:
        return now - timedelta(hours=int(m.group(1)))

    try:  # ISO 8601
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:  # RFC 822 (RSS)
        dt = parsedate_to_datetime(s)
        if dt:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    m = re.search(rf"({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})", s)
    if m:
        try:
            return datetime.strptime(" ".join(m.groups()), "%B %d %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def fmt_money(lo, hi, period: str = "year") -> str:
    def one(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return None
        return f"${v:,.2f}" if v < 500 else f"${v:,.0f}"

    a, b = one(lo), one(hi)
    if not a and not b:
        return ""
    rng = f"{a}–{b}" if a and b and a != b else (a or b)
    try:
        hourly = float(lo or hi or 0) < 500
    except (TypeError, ValueError):
        hourly = False
    return f"{rng} an hour" if hourly else f"{rng} a {period}"


def normalize_url(url: str) -> str:
    try:
        p = urlparse(url)
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", "", ""))
    except ValueError:
        return url


def norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def keyword_regex(kw: str) -> re.Pattern:
    """'word' matches the whole word; 'word*' matches words starting with it."""
    kw = kw.strip().lower()
    prefix = kw.endswith("*")
    core = re.escape(kw.rstrip("*")).replace(r"\ ", r"\s+")
    tail = r"" if prefix else r"(?![a-z0-9])"
    return re.compile(rf"(?<![a-z0-9]){core}{tail}")


_REGEX_CACHE: dict[str, re.Pattern] = {}


def has(text: str, kw: str) -> bool:
    if kw not in _REGEX_CACHE:
        _REGEX_CACHE[kw] = keyword_regex(kw)
    return bool(_REGEX_CACHE[kw].search(text))


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    return s


def job(title, org, location, url, posted=None, salary="", text="") -> dict:
    return {
        "title": clean(title),
        "org": clean(org),
        "location": clean(location),
        "url": url,
        "posted": iso(posted) if isinstance(posted, datetime) else iso(parse_date(posted)),
        "salary": clean(salary),
        "text": clean(text)[:1500],  # used for scoring only, not saved
    }


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

def src_adzuna(cfg, src, session):
    app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not app_id or not app_key:
        raise SkipSource("Off. Add ADZUNA_APP_ID and ADZUNA_APP_KEY as repository secrets to turn it on.")
    loc = cfg["location"]
    out = []
    for term in src.get("terms") or cfg["search_terms"]:
        params = {
            "app_id": app_id, "app_key": app_key, "results_per_page": 50,
            "what": term, "where": loc["adzuna_where"], "distance": loc.get("radius_km", 30),
            "max_days_old": cfg.get("max_age_days", 45), "sort_by": "date",
            "content-type": "application/json",
        }
        r = session.get("https://api.adzuna.com/v1/api/jobs/us/search/1", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        for it in r.json().get("results", []):
            predicted = str(it.get("salary_is_predicted", "0")) == "1"
            salary = "" if predicted else fmt_money(it.get("salary_min"), it.get("salary_max"))
            out.append(job(
                it.get("title"), (it.get("company") or {}).get("display_name", ""),
                (it.get("location") or {}).get("display_name", ""), it.get("redirect_url"),
                it.get("created"), salary, it.get("description", ""),
            ))
        time.sleep(PAUSE)
    return out


def src_usajobs(cfg, src, session):
    key = os.environ.get("USAJOBS_API_KEY", "").strip()
    email = os.environ.get("USAJOBS_EMAIL", "").strip()
    if not key or not email:
        raise SkipSource("Off. Add USAJOBS_API_KEY and USAJOBS_EMAIL as repository secrets to turn it on.")
    loc = cfg["location"]
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}
    out = []
    for term in src.get("terms") or ["dietitian", "nutrition", "public health", "epidemiologist", "food safety"]:
        params = {
            "Keyword": term, "LocationName": loc["usajobs_location"],
            "Radius": loc.get("usajobs_radius_miles", 20), "ResultsPerPage": 100,
            "DatePosted": min(60, cfg.get("max_age_days", 45)),
        }
        r = session.get("https://data.usajobs.gov/api/search", params=params, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        items = (r.json().get("SearchResult") or {}).get("SearchResultItems", [])
        for it in items:
            d = it.get("MatchedObjectDescriptor") or {}
            pay = (d.get("PositionRemuneration") or [{}])[0]
            period = "year" if pay.get("RateIntervalCode", "PA") in ("PA", "Per Year") else "hour"
            summary = (d.get("UserArea") or {}).get("Details", {}).get("JobSummary", "")
            out.append(job(
                d.get("PositionTitle"), d.get("OrganizationName") or d.get("DepartmentName"),
                d.get("PositionLocationDisplay"), d.get("PositionURI"),
                d.get("PublicationStartDate"),
                fmt_money(pay.get("MinimumRange"), pay.get("MaximumRange"), period),
                summary,
            ))
        time.sleep(PAUSE)
    return out


def src_workday(cfg, src, session):
    """Any careers site at *.myworkdayjobs.com. Paste its URL into config.json."""
    u = urlparse(src["url"])
    host = u.netloc
    tenant = host.split(".")[0]
    parts = [p for p in u.path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    if not parts:
        raise ValueError("Workday URL needs the site name, like https://company.wd1.myworkdayjobs.com/SiteName")
    site = parts[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    out = []
    for term in src.get("terms") or cfg["search_terms"]:
        offset = 0
        while offset < 100:
            body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term}
            r = session.post(api, json=body, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            posts = r.json().get("jobPostings", []) or []
            for p in posts:
                path = p.get("externalPath") or ""
                out.append(job(
                    p.get("title"), src.get("name", tenant), p.get("locationsText", ""),
                    f"https://{host}/en-US/{site}{path}", p.get("postedOn"),
                    "", " ".join(p.get("bulletFields") or []),
                ))
            if len(posts) < 20:
                break
            offset += 20
            time.sleep(PAUSE)
        time.sleep(PAUSE)
    return out


def src_amazon(cfg, src, session):
    out = []
    for term in src.get("terms") or ["nutrition"]:
        params = {
            "base_query": term, "loc_query": cfg["location"].get("amazon_location", "Seattle, WA, United States"),
            "radius": "24km", "result_limit": 100, "sort": "recent", "offset": 0,
        }
        r = session.get("https://www.amazon.jobs/en/search.json", params=params, timeout=TIMEOUT,
                        headers={"Accept": "application/json"})
        r.raise_for_status()
        for j in r.json().get("jobs", []) or []:
            out.append(job(
                j.get("title"), j.get("company_name") or "Amazon",
                j.get("normalized_location") or j.get("location", ""),
                urljoin("https://www.amazon.jobs", j.get("job_path", "")),
                j.get("posted_date"), "",
                " ".join(filter(None, [j.get("description_short"), j.get("basic_qualifications")])),
            ))
        time.sleep(PAUSE)
    return out


def src_icims(cfg, src, session):
    """iCIMS career sites (Fred Hutch uses careers-fhcrc.icims.com)."""
    base = src["url"].rstrip("/")
    out, seen = [], set()
    for term in src.get("terms") or cfg["search_terms"]:
        r = session.get(f"{base}/jobs/search", params={"ss": 1, "searchKeyword": term, "in_iframe": 1},
                        timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(base + "/", a["href"])
            if not re.search(r"/jobs/\d+/[^/?#]+/job", href):
                continue
            href = href.split("?")[0]
            if href in seen:
                continue
            title = a.get("title") or a.get_text(" ", strip=True)
            title = re.sub(r"^\s*\d+\s*-\s*", "", clean(title))
            if not title or title.lower() in ("apply", "view job", "more"):
                continue
            seen.add(href)
            row = a
            for _ in range(5):  # climb to the listing row to find location / date
                if row.parent is None:
                    break
                row = row.parent
                if len(row.get_text(" ", strip=True)) > len(title) + 20:
                    break
            row_text = row.get_text(" ", strip=True)
            m = re.search(r"\bUS-([A-Z]{2})-([A-Za-z.'-]+)", row_text)
            location = f"{m.group(2)}, {m.group(1)}" if m else ""
            dm = re.search(r"Posted\s*Date\s*:?\s*([^|]{4,30})", row_text, re.I)
            out.append(job(title, src.get("name", ""), location, href, dm.group(1) if dm else None, "", row_text))
        time.sleep(PAUSE)
    return out


# Employer names that themselves contain " - ", so the title/employer split doesn't cut them in half.
HYPHENATED_ORGS = ["Public Health - Seattle & King County"]


def split_title_org(text: str) -> tuple[str, str]:
    """The UW board lists roles as 'Title - Employer'."""
    for org in HYPHENATED_ORGS:
        if text.endswith(" - " + org):
            return text[: -len(org) - 3], org
    if " - " in text:
        title, org = text.rsplit(" - ", 1)
        return title, org
    return text, ""


def src_uw_sph_board(cfg, src, session):
    """The UW School of Public Health's moderated public health job board."""
    url = src["url"]
    out, seen = [], set()
    for page in range(int(src.get("pages", 2))):
        r = session.get(url, params={"page": page} if page else None, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        found = 0
        for a in soup.find_all("a", href=True):
            if not re.search(r"/careers/job/\d+", a["href"]):
                continue
            href = urljoin(url, a["href"]).split("?")[0]
            text = a.get_text(" ", strip=True)
            if not text or href in seen:
                continue
            seen.add(href)
            found += 1
            title, org = split_title_org(text)
            head = a.find_parent(["h2", "h3", "h4", "h5"]) or a
            bits = []
            for sib in head.next_siblings:
                name = getattr(sib, "name", None)
                if name in ("h2", "h3", "h4", "h5"):
                    break
                t = sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib).strip()
                if t:
                    bits.append(t)
                if len(bits) >= 6:
                    break
            blob = " | ".join(bits)
            pm = re.search(rf"Posted:?\s*(({MONTHS})\s+\d{{1,2}},\s*\d{{4}})", blob)
            location = ""
            for b in bits:
                if b.startswith("Posted") or re.fullmatch(r"#\d+", b):
                    continue
                location = b
                break
            out.append(job(title, org, location.strip(" ,"), href, pm.group(1) if pm else None, "", ""))
        if not found:
            break
        time.sleep(PAUSE)
    return out


def src_neogov_rss(cfg, src, session):
    """GovernmentJobs.com (NEOGOV) agency feeds: King County, Washington State, City of Seattle."""
    agency = src["agency"]
    candidates = [src["feed_url"]] if src.get("feed_url") else [
        f"https://agency.governmentjobs.com/jobfeed.cfm?agency={agency}",
        f"https://www.governmentjobs.com/careers/{agency}/rss",
    ]
    last_err = None
    for feed in candidates:
        try:
            r = session.get(feed, timeout=TIMEOUT, headers={"Accept": "application/rss+xml, application/xml, text/xml"})
            r.raise_for_status()
            if "<item" not in r.text:
                raise ValueError("no job items in feed")
            root = ET.fromstring(r.content)
        except Exception as e:  # try the next candidate URL
            last_err = e
            continue
        out = []
        for item in root.iter():
            if item.tag.split("}")[-1] != "item":
                continue
            fields = {}
            for child in item:
                key = child.tag.split("}")[-1].lower()
                fields.setdefault(key, (child.text or "").strip())
            desc = fields.get("description", "")
            location = fields.get("location") or ""
            if not location:
                m = re.search(r"Location:?\s*</?\w*>?\s*([^<\n|]{3,80})", desc, re.I)
                location = m.group(1) if m else src.get("default_location", "")
            salary = fields.get("salary") or ""
            if not salary:
                m = re.search(r"\$[\d,]+(?:\.\d+)?\s*-\s*\$[\d,]+(?:\.\d+)?\s*(?:Hourly|Annually|Monthly|an hour|a year)?", clean(desc))
                salary = m.group(0) if m else ""
            out.append(job(fields.get("title"), src.get("name", agency), location,
                           fields.get("link"), fields.get("pubdate"), salary, desc))
        return out
    raise RuntimeError(f"Feed not reachable ({last_err}). Use the quick link instead.")


SOURCES = {
    "adzuna": src_adzuna,
    "usajobs": src_usajobs,
    "workday": src_workday,
    "amazon": src_amazon,
    "icims": src_icims,
    "uw_sph_board": src_uw_sph_board,
    "neogov_rss": src_neogov_rss,
}


# --------------------------------------------------------------------------- #
# Scoring, filtering, merging
# --------------------------------------------------------------------------- #

def location_ok(loc: str, cfg) -> bool:
    if not loc:
        return True  # unknown location: keep it and let the person judge
    low = loc.lower()
    allowed = cfg["location"].get("allowed_locations", [])
    if re.search(r"washington,?\s*d\.?c\.?|district of columbia|\bdc\b", low) and "seattle" not in low:
        return False
    if any(a in low for a in allowed):
        return True
    return bool(cfg["location"].get("include_remote", True) and "remote" in low)


def score_job(j: dict, cfg) -> int:
    sc = cfg["scoring"]
    title = f" {j['title'].lower()} "
    text = f" {j.get('text', '').lower()} {j['org'].lower()} "
    s = sum(w for kw, w in sc["title"].items() if has(title, kw))
    s += min(sc.get("text_cap", 6), sum(w for kw, w in sc["text"].items() if has(text, kw)))
    s += sum(w for kw, w in sc["penalties"].items() if has(title, kw))
    return s


def categorize(j: dict, cfg) -> str:
    hay = f" {j['title'].lower()} {j['org'].lower()} "
    for cat in cfg["categories"]:
        if any(has(hay, w) for w in cat["words"]):
            return cat["name"]
    return "Other"


ORG_NOISE = {"the", "inc", "llc", "corp", "corporation", "co", "com", "services", "of"}


def dedupe_key(j: dict) -> str:
    """Same role from two sources: compare the title before any comma or dash, plus the employer's first word."""
    core = re.split(r",| - | – |\(", j["title"])[0]
    org_words = [w for w in norm_text(j["org"]).split() if w not in ORG_NOISE]
    return f"{norm_text(core)}|{org_words[0] if org_words else ''}"


def job_id(j: dict) -> str:
    basis = normalize_url(j["url"]) if j.get("url") else f"{norm_text(j['title'])}|{norm_text(j['org'])}"
    return hashlib.sha1(basis.encode()).hexdigest()[:12]


def load_previous() -> dict:
    try:
        return json.loads(OUT_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"jobs": [], "meta": {}}


def run(cfg: dict) -> dict:
    session = make_session()
    now = utcnow()
    statuses, collected = [], []

    for src in cfg["sources"]:
        name = src.get("name", src["type"])
        if not src.get("enabled", True):
            statuses.append({"name": name, "type": src["type"], "state": "off", "count": 0, "message": "Turned off in config.json."})
            continue
        fn = SOURCES.get(src["type"])
        if not fn:
            statuses.append({"name": name, "type": src["type"], "state": "error", "count": 0, "message": f"Unknown source type '{src['type']}'."})
            continue
        try:
            raw = fn(cfg, src, session)
            for j in raw:
                j["source"] = name
                j["source_type"] = src["type"]
            collected.extend(raw)
            statuses.append({"name": name, "type": src["type"], "state": "ok", "count": len(raw), "message": ""})
            print(f"[ok]    {name}: {len(raw)} listings")
        except SkipSource as e:
            statuses.append({"name": name, "type": src["type"], "state": "off", "count": 0, "message": str(e)})
            print(f"[off]   {name}: {e}")
        except Exception as e:  # keep going; one broken site shouldn't stop the rest
            msg = f"{type(e).__name__}: {e}"[:300]
            statuses.append({"name": name, "type": src["type"], "state": "error", "count": 0, "message": msg})
            print(f"[error] {name}: {msg}", file=sys.stderr)

    # Score, filter, dedupe
    min_score = cfg["scoring"].get("min_score", 4)
    full = cfg["scoring"].get("full_match_score", 14)
    max_age = timedelta(days=cfg.get("max_age_days", 45))
    best: dict[str, dict] = {}
    for j in collected:
        if not j["title"] or not j["url"]:
            continue
        if not location_ok(j["location"], cfg):
            continue
        if j["posted"] and now - datetime.fromisoformat(j["posted"]) > max_age:
            continue
        s = score_job(j, cfg)
        if s < min_score:
            continue
        j["score"] = s
        j["match"] = max(1, min(99, round(100 * s / full)))
        key = dedupe_key(j)
        cur = best.get(key)
        rank = (SOURCE_PRIORITY.get(j["source_type"], 0), bool(j["salary"]), bool(j["posted"]))
        if cur is None or rank > cur["_rank"]:
            j["_rank"] = rank
            best[key] = j

    prev = load_previous()
    prev_by_id = {p["id"]: p for p in prev.get("jobs", [])}
    ok_sources = {s["name"] for s in statuses if s["state"] == "ok"}
    senior_words = cfg.get("senior_words", [])

    jobs = []
    for j in best.values():
        jid = job_id(j)
        old = prev_by_id.get(jid, {})
        title_low = f" {j['title'].lower()} "
        jobs.append({
            "id": jid,
            "title": j["title"],
            "org": j["org"],
            "location": j["location"],
            "url": j["url"],
            "posted": j["posted"],
            "first_seen": old.get("first_seen") or iso(now),
            "last_seen": iso(now),
            "salary": j["salary"],
            "category": categorize(j, cfg),
            "score": j["score"],
            "match": j["match"],
            "senior": any(has(title_low, w) for w in senior_words),
            "remote": "remote" in j["location"].lower() or has(title_low, "remote"),
            "seattle": "seattle" in j["location"].lower(),
            "source": j["source"],
        })

    # Keep earlier listings from sources that failed this run, for up to 10 days,
    # so one flaky site doesn't make everything vanish.
    kept_ids = {j["id"] for j in jobs}
    for p in prev.get("jobs", []):
        if p["id"] in kept_ids or p.get("source") in ok_sources:
            continue
        try:
            last = datetime.fromisoformat(p["last_seen"])
        except (KeyError, ValueError):
            continue
        if now - last <= timedelta(days=10):
            jobs.append(p)

    jobs.sort(key=lambda x: (x["score"], x["posted"] or x["first_seen"]), reverse=True)

    return {
        "meta": {
            "last_updated": iso(now),
            "location_label": cfg["location"].get("label", ""),
            "total": len(jobs),
            "sources": statuses,
            "version": 1,
        },
        "jobs": jobs,
    }


def main() -> int:
    cfg = json.loads(CONFIG_PATH.read_text())
    result = run(cfg)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    print(f"Wrote {len(result['jobs'])} roles to {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
