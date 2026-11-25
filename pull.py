"""Watch LinkedIn, Indeed and Naukri (India) for fresh jobs that match config.toml.

  uv run python pull.py             # one search, jobs posted in the last 12h -> jobs.md
  uv run python pull.py 24          # last 24h
  uv run python pull.py --watch     # scheduled mode: rolling 24h list in jobs_24h.md + desktop notification
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values
from jobspy import scrape_jobs
from jobspy.indeed import Indeed
from jobspy.linkedin import LinkedIn
from jobspy.naukri import Naukri
from jobspy.naukri.model import Job as _NaukriJob

HERE = Path(__file__).parent
_cfg = Path(os.environ.get("JOBWATCH_CONFIG") or next(p for p in (HERE / "config.toml", HERE / "config.example.toml") if p.exists()))
CFG = tomllib.loads(_cfg.read_text())
S, F = CFG["search"], CFG["filters"]


def words(ws: list[str]) -> re.Pattern | None:
    return re.compile(r"\b(" + "|".join(ws) + r")\b", re.I) if ws else None


TERMS = S["terms"]
INDEED_TERMS = TERMS + S.get("indeed_extra_terms", [])
NAUKRI_TERMS = TERMS + S.get("naukri_extra_terms", [])
LOCATIONS = S.get("linkedin_locations", ["India"])
MAX_REQUIRED_YEARS = S["max_required_years"]
MAX_MIN_YEARS = S["naukri_max_min_years"]
SLOTS = tuple(CFG["schedule"]["slots"])
QUICK_MINUTES = CFG["schedule"].get("quick_pass_minutes", 30)
WANT, SKIP, BIG = words(F.get("want_title", [])), words(F.get("skip_title", [])), words(F.get("skip_companies", []))
_codes = "|".join(F.get("state_codes", []))
LOC_OK = (re.compile(r"\b(" + "|".join(F["locations"] + ["remote"]) + r")\b|^((" + (_codes or "-") + r"), )?India$|^$", re.I)
          if F.get("locations") else re.compile(""))  # empty pattern = anywhere

# JobSpy keeps only the posting *date*; record the real posting time per job_url.
# Indeed gives an exact epoch; LinkedIn cards say "3 minutes ago", so we subtract that from now.
POSTED: dict[str, tuple[str, bool]] = {}  # job_url -> (ISO UTC time, approximate?)
UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000}


def _ago(text: str) -> datetime | None:
    m = re.search(r"(\d+)\s+(second|minute|hour|day|week|month)s?\s+ago", text or "")
    return datetime.now(timezone.utc) - timedelta(seconds=int(m[1]) * UNITS[m[2]]) if m else None


# JobSpy marks logoPathV3 required, so any Naukri job without a company logo was silently dropped.
_NaukriJob.model_fields["logoPathV3"].default = None
_NaukriJob.model_rebuild(force=True)
_nk_params = Naukri._search_params
NAUKRI_EXP: int | None = None  # Naukri's own filter: only jobs whose experience range includes this many years
Naukri._search_params = lambda self: _nk_params(self) | ({"experience": NAUKRI_EXP} if NAUKRI_EXP is not None else {})


def min_years(text: str) -> int | None:
    """'1-3 Yrs' -> 1, '2+ years' -> 2; None if there's no number."""
    m = re.search(r"(\d+)", text or "")
    return int(m[1]) if m else None


_NUM = r"(\d{1,2})(?:\.\d)?"
_YRS = r"\s*\+?\s*(?:years?|yrs?)\b"
_REQ = [re.compile(p, re.I) for p in (
    _NUM + r"\s*\+" + _YRS,                                            # 5+ years
    r"(?:minimum|min\.?|at\s*least|atleast)\s*(?:of\s*)?" + _NUM + _YRS,  # minimum 4 years
    _NUM + r"\s*(?:-|–|—|to)\s*\d{1,2}" + _YRS,                         # 4-6 years
    r"experience\s*(?:required|range)?\s*[:\-]\s*" + _NUM + r"\b",      # Experience: 5
    _NUM + _YRS + r"\s*(?:of\s*)?(?:\w+\s*){0,4}experience",             # 4 years of Python experience
)]
_SOFT = re.compile(r"prefer|nice|plus|bonus|ideal|good to have|desirable|advantage|track record|in business|"
                   r"founded|company|clients|serving|since|history|legacy|old\b", re.I)


def required_years(desc: str) -> int | None:
    """Smallest experience requirement stated in a job description, or None if it doesn't state one.

    Built to avoid dropping good jobs: soft lines (preferred, company age) are ignored, and with several
    requirements the smallest wins, so "4+ years overall, 1+ year with LLMs" gives 1 and the job is kept.
    """
    found = []
    for line in re.split(r"[\n.;•]", desc or ""):
        if _SOFT.search(line):
            continue
        for rx in _REQ:
            found += [int(m.group(1)) for m in rx.finditer(line) if int(m.group(1)) <= 15]
    return min(found) if found else None


def linkedin_description(url: str) -> str:
    """Public job-posting text from LinkedIn's guest endpoint; '' on any failure (the job is then kept)."""
    m = re.search(r"/jobs/view/(\d+)", url or "")
    if not m:
        return ""
    import html, urllib.request
    try:
        req = urllib.request.Request(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m[1]}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        page = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    except Exception:
        return ""
    body = re.search(r"show-more-less-html__markup[^>]*>(.*?)</div>", page, re.S)
    return html.unescape(re.sub(r"<[^>]+>", "\n", body[1])) if body else ""


REMOTE = re.compile(r"\b(fully remote|100% remote|remote[- ](?:role|position|job|work|first|opportunity)|work(?:ing)? from home|wfh|"
                    r"location\s*:?\s*remote|remote\s*\(|this is a remote)\b", re.I)


def too_senior(job: dict) -> bool:
    desc = job.pop("_desc", "") or (linkedin_description(job["url"]) if job["site"] == "linkedin" else "")
    if job["site"] == "linkedin":
        time.sleep(1)  # be gentle with LinkedIn
    if job.pop("_remote_only", False) and not REMOTE.search(desc):
        return True  # outside your locations and not remote
    years = required_years(desc)
    return years is not None and years > MAX_REQUIRED_YEARS


_li, _in = LinkedIn._process_job, Indeed._process_job


def _li_job(self, card, job_id):
    post = _li(self, card, job_id)
    t = card.find("time")
    if when := _ago(t.get_text(" ", strip=True) if t else ""):
        POSTED[post.job_url] = (when.isoformat(timespec="seconds"), True)
    return post


def _in_job(self, raw):
    post = _in(self, raw)
    if ms := raw.get("dateOnIndeed"):
        POSTED[post.job_url] = (datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds"), False)
    return post


LinkedIn._process_job, Indeed._process_job = _li_job, _in_job

PIPELINE = HERE / "jobs.md"
ROLLING = HERE / "jobs_24h.md"
STATE = HERE / "jobs_24h.json"


def wanted(title: str, company: str = "") -> bool:
    return (not WANT or bool(WANT.search(title))) and not (SKIP and SKIP.search(title)) and not (BIG and BIG.search(company))


def search(hours_old: int, per_term: int = 25, easy: bool = False) -> list[dict]:
    """easy=True is the quick pass between slots: Indeed "Easily apply" and apply-on-Naukri jobs only
    (LinkedIn rate-limits, so it stays on the slots)."""
    seen, seen_keys, jobs = set(), set(), []
    queries = [] if easy else [("linkedin", t, loc, per_term) for t in TERMS for loc in LOCATIONS]
    queries += [("indeed", t, "India", 100) for t in INDEED_TERMS]
    # Naukri's filter keeps ranges that include the value; m-1 and m together cover every range starting <= m.
    queries += [("naukri", t, ("India", y), 100) for t in NAUKRI_TERMS for y in sorted({max(0, MAX_MIN_YEARS - 1), MAX_MIN_YEARS})]
    global NAUKRI_EXP
    for site, term, loc, n in queries:
        if site == "naukri":
            loc, NAUKRI_EXP = loc
        try:
            df = scrape_jobs(site_name=[site], search_term=term, location=loc, country_indeed="India",
                             results_wanted=n, hours_old=hours_old, easy_apply=easy or None)
        except Exception as e:  # one blocked site shouldn't lose the rest
            print(f"skip {term!r} {loc!r}: {e}", file=sys.stderr)
            continue
        for j in df.to_dict("records"):
            title, company = str(j.get("title") or ""), str(j.get("company") or "")
            url = j.get("job_url_direct") if isinstance(j.get("job_url_direct"), str) else j.get("job_url")
            key = f"{company.strip()} | {title.strip()}".lower()
            exp = str(j.get("experience_range") or "") if isinstance(j.get("experience_range"), str) else ""
            if site == "naukri" and (min_years(exp) or 0) > MAX_MIN_YEARS:
                continue
            loc_ok = j.get("is_remote") == True or bool(LOC_OK.search(str(j.get("location") or "").strip()))
            # LinkedIn cards carry no remote flag: keep those for a description check in too_senior().
            if not url or url in seen or key in seen_keys or not wanted(title, company) or (not loc_ok and site != "linkedin"):
                continue
            seen.add(url); seen_keys.add(key)
            posted = j.get("date_posted")
            posted_at, approx = POSTED.get(j["job_url"], (None, True))
            jobs.append({"url": url, "company": company, "title": title, "location": str(j.get("location") or ""),
                         "posted": str(posted) if posted else "n/a", "site": j["site"],
                         "posted_at": posted_at, "posted_approx": approx, **({"exp": exp} if exp else {}), **({"easy": True} if easy else {}),
                         "_desc": str(j.get("description") or "") if site == "indeed" else "",
                         **({} if loc_ok else {"_remote_only": True})})
    return jobs


def save(jobs: list[dict]) -> None:
    """Optional: upsert into MongoDB if MONGODB_URI is set in .env; never fail the run over it."""
    cfg = dotenv_values(HERE / ".env")
    if not jobs or not cfg.get("MONGODB_URI"):
        return
    from pymongo import MongoClient, UpdateOne
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ops = []
    for j in jobs:
        # LinkedIn times are "N units ago" estimates; the first sighting is the most precise, so keep it.
        keep = {"posted_at", "posted_approx"} if j.get("posted_approx", True) else set()
        seen = datetime.fromisoformat(j["first_seen"]).astimezone(timezone.utc).isoformat(timespec="seconds") if j.get("first_seen") else now
        ops.append(UpdateOne({"url": j["url"]}, {"$set": {k: v for k, v in j.items() if k not in keep | {"first_seen"}},
                                                 "$setOnInsert": {"retrieved_at": seen, **{k: j.get(k) for k in keep}}},
                             upsert=True))
    for attempt in range(3):
        try:
            MongoClient(cfg["MONGODB_URI"], serverSelectionTimeoutMS=15000)[cfg.get("MONGODB_DB") or "jobs"]["jobs"].bulk_write(ops)
            return
        except Exception as e:
            print(f"mongo save failed (attempt {attempt + 1}/3): {str(e)[:200]}", file=sys.stderr)
            time.sleep(20)


def notify(msg: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Job watch" sound name "Glass"'])
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", "Job watch", msg])


def due(last: datetime | None, now: datetime) -> bool:
    """True if a slot hour has started since the last run (each slot runs once, even if we slept through it)."""
    if last is None:
        return True
    t = last.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while t <= now:
        if t.hour in SLOTS:
            return True
        t += timedelta(hours=1)
    return False


def row(j: dict) -> str:
    return f"- [ ] {j['url']} | {j['company']} | {j['title']} | {j['location']} | posted: {j['posted']} | via {j['site']}" + (f" | {j['exp']}" if j.get("exp") else "") + (" | EASY" if j.get("easy") else "")


def site_ok(job: dict) -> bool:
    """Naukri is already filtered server-side; LinkedIn/Indeed jobs go through the description check."""
    return job["site"] == "naukri" or not too_senior(job)


def main(hours_old: int = 12, per_term: int = 25) -> None:
    jobs = [j for j in search(hours_old, per_term) if site_ok(j)]
    save(jobs)
    PIPELINE.write_text(f"# Jobs posted in the last {hours_old}h\n\n" + "\n".join(map(row, jobs)) + "\n")
    print(f"added {len(jobs)} -> {PIPELINE.name}")


def watch() -> None:
    """Scheduled run: search since the last run (catches up after sleep), keep a rolling 24h file."""
    now = datetime.now()
    state = json.loads(STATE.read_text()) if STATE.exists() else {"last_run": None, "jobs": []}
    easy = False
    if not due(state["last_run"] and datetime.fromisoformat(state["last_run"]), now) and "--force" not in sys.argv:
        # Between slots: a quick easy-apply pass (those fill up within the hour).
        last = max(filter(None, [state["last_run"], state.get("last_quick")]))
        if (now - datetime.fromisoformat(last)).total_seconds() < (QUICK_MINUTES - 1) * 60:
            return
        easy = True
    since = (now - datetime.fromisoformat(state["last_run"])).total_seconds() / 3600 if state["last_run"] else 24
    hours = 2 if easy else max(1, min(24, math.ceil(since) + 1))  # +1h overlap so nothing falls between runs
    cutoff = now - timedelta(hours=24)
    kept = [j for j in state["jobs"] if datetime.fromisoformat(j["first_seen"]) >= cutoff]
    dropped = {u: t for u, t in state.get("dropped", {}).items() if datetime.fromisoformat(t) >= cutoff}
    known = {j["url"] for j in kept} | {f"{j['company']} | {j['title']}".lower() for j in kept} | set(dropped)
    new = []
    for j in search(hours, easy=easy):
        if j["url"] in known or f"{j['company']} | {j['title']}".lower() in known:
            continue
        if site_ok(j):
            new.append(j | {"first_seen": now.isoformat(timespec="minutes")})
        else:
            dropped[j["url"]] = now.isoformat(timespec="minutes")
    for j in kept:
        j.pop("_desc", None)
    stamp = now.isoformat(timespec="minutes")
    state = {"last_run": state["last_run"] if easy else stamp, "last_quick": stamp, "jobs": new + kept, "dropped": dropped}
    save(state["jobs"])
    STATE.write_text(json.dumps(state, indent=1))
    lines = [f"# Jobs first seen in the last 24h (updated {state['last_run']}, searched last {hours}h)", ""]
    lines += [f"{row(j)} | seen: {j['first_seen']}" + (" | NEW" if j in new else "") for j in state["jobs"]]
    ROLLING.write_text("\n".join(lines) + "\n")
    print(f"{now:%F %R} {'easy-apply ' if easy else ''}searched {hours}h: {len(new)} new, {len(state['jobs'])} in last 24h, "
          f"{len(dropped)} dropped (too senior / out of area)")
    if new:
        notify((f"{len(new)} new quick-apply jobs." if easy else f"{len(new)} new jobs.") + f" {len(state['jobs'])} in the last 24h.")


def selfcheck() -> None:
    assert REMOTE.search("This is a full-time remote role") and REMOTE.search("Remote (India)")
    assert REMOTE.search("Location: Remote") and REMOTE.search("work from home") and not REMOTE.search("collaborate with remote teams")
    assert min_years("1-3 Yrs") == 1 and min_years("2+ years") == 2 and min_years("") is None and min_years("5-8 Yrs") == 5
    assert required_years("Experience: 5+ years") == 5 and required_years("Minimum 4 years of experience in Python") == 4
    assert required_years("4-6 years of experience") == 4 and required_years("3+ years") == 3
    assert required_years("4+ years overall.\n1+ year with LLMs") == 1   # smallest requirement wins
    assert required_years("5+ years experience preferred") is None       # soft wording never drops a job
    assert required_years("We have 10+ years track record serving clients") is None
    assert required_years("Exp Range : 1-4 years") == 1 and required_years("No experience needed") is None
    assert _ago("2 hours ago") and not _ago("Reposted") and (datetime.now(timezone.utc) - _ago("3 minutes ago")).seconds in range(179, 182)
    d = datetime(2026, 10, 24, 6, 10)
    if 9 in SLOTS and 6 in SLOTS:
        assert due(None, d) and due(d, d.replace(hour=10)) and not due(d, d.replace(hour=8, minute=59))
    assert LOC_OK.search("Remote, India") and LOC_OK.search("India") and LOC_OK.search("")


if __name__ == "__main__":
    selfcheck()
    if sys.argv[1:2] == ["--check"]:
        print(f"ok (config: {_cfg.name})")
    elif sys.argv[1:2] == ["--watch"]:
        watch()
    else:
        main(*map(int, sys.argv[1:]))
