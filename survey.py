"""Fresher job-market survey per track, India, last 30 days (LinkedIn, Indeed, Naukri).

  python survey.py <track> scrape   # slow; writes <track>_jobs.json after every query (resumable)
  python survey.py <track> export   # <track>_jds/: one .md per posting + index.csv, for other agents
  python survey.py <track> report   # <track>_skills.md: skills, market size, locations, pay
tracks: java, ai, backend, fullstack
"""
import csv
import json
import re
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pull  # reuses its JobSpy patches (Naukri logo fix, Naukri experience filter), required_years(), BIG
from jobspy import scrape_jobs
from jobspy.linkedin import LinkedIn

HERE = Path(__file__).parent
HOURS = 30 * 24
I = re.I
TRACKS = {
    "java": dict(max_years=1, label="Java", title_desc=True,  # Java postings are kept on a desc mention too
                 terms=["Java Developer", "Java Fresher", "Junior Java Developer", "Java Backend Developer",
                        "Spring Boot Developer", "Java Software Engineer", "Associate Software Engineer Java",
                        "Java Full Stack Developer", "Graduate Engineer Trainee Java"],
                 keep=re.compile(r"\bjava\b(?!\s*script)|\bspring\b|\bj2ee\b|\bjakarta\b", I), drop=None),
    "ai": dict(max_years=2, label="AI / GenAI engineer",
               terms=["AI Engineer", "Generative AI Engineer", "GenAI Developer", "LLM Engineer", "Agentic AI Developer",
                      "AI Developer", "Machine Learning Engineer", "AI ML Engineer Fresher"],
               keep=re.compile(r"\b(ai|ml|llm|genai|gen ai|generative|agentic|machine learning|nlp|deep learning|"
                               r"computer vision|rag|prompt)\b", I),
               drop=re.compile(r"\b(annotat\w*|label+ing|data entry|analyst|data scientist|content|voice process)\b", I)),
    "backend": dict(max_years=2, label="Backend (Python / Node / Go)",
                    terms=["Python Developer", "Backend Developer", "Python Backend Developer", "FastAPI Developer",
                           "Django Developer", "Node.js Developer", "NestJS Developer", "Golang Developer"],
                    keep=re.compile(r"back[- ]?end|python|fastapi|django|flask|node|nest\.?js|golang|\bgo\b|api developer", I),
                    drop=re.compile(r"\bjava\b(?!\s*script)|\.net|php|data scientist|analyst|full[- ]?stack", I)),
    "fullstack": dict(max_years=2, label="Full-stack / frontend (React / Next.js / MERN)",
                      terms=["Full Stack Developer", "MERN Stack Developer", "React Developer", "Next.js Developer",
                             "Frontend Developer", "React JS Fresher"],
                      keep=re.compile(r"full[- ]?stack|\bmern\b|\bmean\b|react(?!\s*native)|next\.?js|front[- ]?end|"
                                      r"web developer|ui developer", I),
                      drop=re.compile(r"\bjava\b(?!\s*script)|\.net|php|wordpress|shopify|designer", I)),
}

# LinkedIn's own "Entry level" filter (f_E=2); JobSpy doesn't expose it.
_li_params = LinkedIn._search_params
LinkedIn._search_params = lambda self: _li_params(self) | {"f_E": "2"}

SENIOR = re.compile(r"\b(senior|sr|staff|principal|lead|manager|director|head|architect|intern|internship|"
                    r"iii|ii|sde\s*-?\s*[23]|vp|consultant|specialist|expert|trainer|faculty|teacher|tutor|"
                    r"android|ios|kotlin|flutter|mobile|react native|sales|marketing|bpo|support)\b", I)
JUNIOR = re.compile(r"fresher|freshers|entry[- ]level|junior|graduate|trainee|0\s*(?:-|to)\s*[12]\s*(?:years?|yrs?)|"
                    r"no experience|campus|batch|20(?:24|25|26) pass", I)
PAID = re.compile(r"training fee|registration fee|security deposit|refundable deposit|course fee|placement fee|"
                  r"paid training|pay (?:for|the) training|job guarantee|placement guarantee|placement assistance", I)
STAFFING = re.compile(r"staffing|recruit|manpower|placement|consultancy|hr services|hr solutions|talent|outsourc|"
                      r"jobs?\b|careers?\b|hiring", I)


def paths(track: str) -> tuple[Path, Path, Path]:
    return HERE / f"{track}_jobs.json", HERE / f"{track}_skills.md", HERE / f"{track}_jds"


def fresher_ok(j: dict, t: dict) -> bool:
    """Within t['max_years']. Naukri: stated range starts low enough. Others: description's stated requirement;
    Indeed (no level filter) with no stated years also needs a junior signal; LinkedIn is entry-level filtered."""
    title, desc = j["title"], j["desc"]
    if SENIOR.search(title) or (t["drop"] and t["drop"].search(title)):
        return False
    if not (t["keep"].search(title) or (t.get("title_desc") and t["keep"].search(desc))):
        return False
    if j["site"] == "naukri":
        y = pull.min_years(j.get("exp", ""))
        return y is None or y <= t["max_years"]
    y = pull.required_years(desc)
    if y is not None:
        return y <= t["max_years"]
    return j["site"] == "linkedin" or bool(JUNIOR.search(title + "\n" + desc))


def run(site: str, store: dict, t: dict, data: Path) -> None:
    m = t["max_years"]
    for term in t["terms"]:
        # Naukri's filter keeps ranges that include the value; m-1 and m together cover every range starting <= m
        for exp in ((m - 1, m) if site == "naukri" else (None,)):
            if site == "naukri":  # global shared with the other site threads; only Naukri may touch it
                pull.NAUKRI_EXP = exp
            try:
                df = scrape_jobs(site_name=[site], search_term=term, location="India", country_indeed="India",
                                 results_wanted=1000 if site != "linkedin" else 400, hours_old=HOURS,
                                 fetch_description=True)
            except Exception as e:
                print(f"skip {site} {term!r}: {e}", file=sys.stderr)
                continue
            for r in df.to_dict("records"):
                s = lambda k: r.get(k) if isinstance(r.get(k), str) else ""
                n = lambda k: r.get(k) if isinstance(r.get(k), (int, float)) and r.get(k) == r.get(k) else None
                skills = [x.strip() for x in r["skills"].split(",")] if isinstance(r.get("skills"), str) else []
                if site == "naukri" or r["job_url"] not in store:  # never replace a LinkedIn desc with a blocked empty one
                    store[r["job_url"]] = {"url": r["job_url"], "site": site, "title": s("title"),
                                           "company": s("company"), "posted": str(r.get("date_posted") or ""),
                                           "exp": s("experience_range"), "level": s("job_level"),
                                           "location": s("location"), "remote": bool(r.get("is_remote") is True),
                                           "pay_min": n("min_amount"), "pay_max": n("max_amount"),
                                           "pay_interval": s("interval"), "currency": s("currency"),
                                           "desc": s("description"), "tags": skills}
            data.write_text(json.dumps(list(store.values())))
            print(f"{site} {term!r} exp={exp}: {len(df)} rows, {len(store)} total", flush=True)


def scrape(track: str) -> None:
    data = paths(track)[0]
    store = {j["url"]: j for j in json.loads(data.read_text())} if data.exists() else {}
    sites = sys.argv[3:] or ["naukri", "indeed", "linkedin"]
    with ThreadPoolExecutor(3) as ex:  # one thread per site; each site stays sequential to avoid throttling
        list(ex.map(lambda site: run(site, store, TRACKS[track], data), sites))


# name -> regex; each counted once per job.
SKILLS = {
    # Java
    "Java": r"\bjava\b(?!\s*script)", "Core Java": r"core\s*java", "OOP": r"\boops?\b|object[- ]oriented",
    "Multithreading / Concurrency": r"multi-?threading|concurrency", "Java 8+ (Streams/Lambdas)": r"java\s*(?:8|11|17|21)|streams?\s*api|lambda",
    "Spring": r"\bspring\b(?!\s*boot)", "Spring Boot": r"spring\s*-?boot", "Spring MVC": r"spring\s*mvc",
    "Spring Security / JWT / OAuth": r"spring security|\bjwt\b|oauth", "Hibernate / JPA": r"hibernate|\bjpa\b",
    "J2EE / Jakarta EE": r"j2ee|jee\b|jakarta", "Maven": r"maven", "Gradle": r"gradle",
    # Python / Node / Go backend
    "Python": r"python", "FastAPI": r"fast\s*api", "Django": r"django", "Flask": r"flask",
    "Node.js": r"node\.?js|\bnode\b", "Express": r"express(?:\.?js)?\b", "NestJS": r"nest\.?js", "Go": r"golang|\bgo\b(?= |,|/)(?=.*(?:lang|developer|backend|micro|api))",
    "Async / Celery / queues": r"asyncio|async\b|celery|background (?:jobs|tasks)", "WebSockets": r"websocket|socket\.io",
    "REST APIs": r"\brest(?:ful)?\b|web services?|\bapis?\b", "GraphQL": r"graphql", "Microservices": r"micro-?services?",
    # data
    "SQL": r"\bsql\b", "MySQL": r"mysql", "PostgreSQL": r"postgres", "Oracle DB": r"oracle(?:\s*(?:db|database|sql))?|pl/?sql",
    "MongoDB": r"mongo", "NoSQL": r"nosql", "Redis / Caching": r"redis|caching", "Kafka": r"kafka",
    "RabbitMQ / messaging": r"rabbitmq|message queue|\bjms\b|activemq|\bsqs\b", "Elasticsearch": r"elastic\s*search",
    "Firebase / Supabase": r"firebase|supabase",
    # frontend
    "HTML/CSS": r"\bhtml5?\b|\bcss3?\b", "JavaScript": r"javascript|\bjs\b|es6", "TypeScript": r"typescript",
    "React": r"\breact(?:\.?js)?\b(?!\s*native)", "Next.js": r"next\.?js", "Redux / state mgmt": r"redux|zustand|context api",
    "Tailwind": r"tailwind", "Bootstrap": r"bootstrap", "Angular": r"angular", "Vue": r"\bvue",
    "MERN / MEAN stack": r"\bmern\b|\bmean\b stack", "Responsive design": r"responsive",
    # AI
    "LLMs / GenAI": r"\bllms?\b|gen\s*ai|generative ai|large language", "LLM APIs (OpenAI, Gemini, Claude)": r"openai|gpt-?\d|gemini|claude|anthropic|groq",
    "RAG": r"\brag\b|retrieval[- ]augmented", "Vector DBs": r"vector (?:db|database|store|search)|pinecone|weaviate|qdrant|chroma|faiss|pgvector|milvus",
    "LangChain": r"langchain", "LangGraph": r"langgraph", "LlamaIndex": r"llama\s*index",
    "AI agents / agentic": r"agentic|ai agents?|multi-agent|crewai|autogen|\bagents?\b", "MCP": r"\bmcp\b|model context protocol",
    "Prompt engineering": r"prompt engineering|prompting|prompt design", "Fine-tuning": r"fine-?tun|lora|peft",
    "Hugging Face / Transformers": r"hugging\s*face|transformers", "PyTorch": r"pytorch|\btorch\b", "TensorFlow / Keras": r"tensorflow|keras",
    "scikit-learn": r"scikit|sklearn", "Pandas / NumPy": r"pandas|numpy", "NLP": r"\bnlp\b|natural language",
    "Computer vision": r"computer vision|opencv|\bcv\b models?", "Machine learning": r"machine learning|\bml\b",
    "Deep learning": r"deep learning|neural network", "MLOps / model deployment": r"mlops|model deployment|mlflow|model serving",
    "Evaluation / LLM evals": r"\bevals?\b|evaluation framework|llm evaluation",
    "Speech / voice AI": r"speech|voice ai|\btts\b|\bstt\b|\basr\b|whisper",
    # tooling / infra
    "Git": r"\bgit\b|github|gitlab|bitbucket", "JUnit / Mockito / unit testing": r"junit|mockito|unit test|pytest|jest",
    "Docker": r"docker|container", "Kubernetes": r"kubernetes|\bk8s\b", "AWS": r"\baws\b|amazon web services",
    "Azure": r"azure", "GCP": r"\bgcp\b|google cloud", "Cloud (generic)": r"\bcloud\b",
    "CI/CD": r"ci\s*/\s*cd|jenkins|github actions|gitlab ci|ci pipelines?|deployment pipelines?", "Linux / shell": r"linux|unix|shell script|bash",
    "Postman / Swagger": r"postman|swagger|openapi", "Agile / Scrum / JIRA": r"agile|scrum|jira",
    # fundamentals
    "DSA": r"data structures?|algorithms?|\bdsa\b", "Design patterns": r"design patterns?",
    "SOLID / clean code": r"\bsolid\b|clean code", "System design / LLD": r"system design|low[- ]level design|\blld\b|\bhld\b",
    "Debugging": r"debugging|troubleshoot", "Communication skills": r"communication", "Problem solving": r"problem[- ]solving|analytical",
}
SKILLS = {k: re.compile(v, I) for k, v in SKILLS.items()}
NICE = re.compile(r"good[- ]to[- ]have|nice[- ]to[- ]have|preferred|\bbonus\b|\bplus\b|desirable|advantage|"
                  r"added advantage|familiarity|exposure to|optional|would be great", I)
HARD = re.compile(r"must|required|requirements|mandatory|responsibilit|qualification|key skills|what you|"
                  r"you will|we are looking|eligibility|skills?\s*:", I)


def classify(desc: str, tags: list[str]) -> dict[str, str]:
    """skill -> 'req' or 'nice'. A skill is 'nice' only if every mention sits in a nice-to-have line/section."""
    found: dict[str, str] = {}
    nice_section = False
    for line in desc.splitlines():
        heading = len(line) < 80 and (line.lstrip().startswith(("#", "**")) or line.rstrip().endswith(":"))
        if heading:
            nice_section = bool(NICE.search(line)) or (nice_section and not HARD.search(line))
        nice = nice_section or bool(NICE.search(line))
        for name, rx in SKILLS.items():
            if rx.search(line):
                found[name] = "req" if found.get(name) == "req" or not nice else "nice"
    for tag in tags:  # Naukri recruiter tags = key skills
        for name, rx in SKILLS.items():
            if rx.search(tag):
                found[name] = "req"
    return found


def monthly_pay(j: dict) -> tuple[float | None, float | None]:
    """INR per month, or (None, None) when not stated / not INR."""
    per = {"yearly": 1 / 12, "monthly": 1, "weekly": 4.33, "daily": 22, "hourly": 176}.get(j.get("pay_interval") or "")
    if not per or (j.get("currency") or "INR") != "INR":
        return None, None
    f = lambda v: round(v * per) if v else None
    return f(j.get("pay_min")), f(j.get("pay_max"))


def flags(j: dict) -> list[str]:
    out = []
    if pull.BIG and pull.BIG.search(j["company"]):
        out.append("big_it")
    if PAID.search(j["desc"]):
        out.append("paid_training")
    if STAFFING.search(j["company"]):
        out.append("staffing")
    return out


def city(j: dict) -> str:
    if j.get("remote") or re.search(r"remote|work from home|wfh", j.get("location", ""), I):
        return "Remote"
    c = (j.get("location") or "").split(",")[0].split(" - ")[-1].strip()
    c = {"Bangalore": "Bengaluru", "Gurugram": "Gurgaon", "New Delhi": "Delhi", "Navi Mumbai": "Mumbai", "Thane": "Mumbai"}.get(c, c)
    return c if c and c.lower() not in ("india", "in") else "Not stated"


def fresher_jobs(track: str) -> list[dict]:
    data, t = paths(track)[0], TRACKS[track]
    # no text (e.g. LinkedIn 429 on the detail fetch) = nothing to count; it would only dilute the percentages
    jobs = [j for j in json.loads(data.read_text()) if (len(j["desc"]) > 200 or j["tags"]) and fresher_ok(j, t)]
    seen, uniq = set(), []
    for j in jobs:  # same posting is often on several sites/terms
        key = (j["company"].lower().strip(), j["title"].lower().strip())
        if key not in seen:
            seen.add(key); uniq.append(j)
    return uniq


def export(track: str) -> None:
    _, _, out = paths(track)
    t = TRACKS[track]
    out.mkdir(exist_ok=True)
    for f in out.glob("*.md"):
        f.unlink()
    jobs = fresher_jobs(track)
    with open(out / "index.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "site", "title", "company", "posted", "experience", "location", "pay_min_month_inr",
                    "pay_max_month_inr", "flags", "url", "skill_tags"])
        for i, j in enumerate(jobs, 1):
            slug = re.sub(r"[^a-z0-9]+", "-", f"{j['company']} {j['title']}".lower()).strip("-")[:60]
            name = f"{i:04d}-{j['site']}-{slug}.md"
            lo, hi = monthly_pay(j)
            pay = f"₹{lo or '?'}–{hi or '?'} / month" if lo or hi else "not stated"
            meta = [f"# {j['title']} — {j['company']}", "", f"- site: {j['site']}", f"- url: {j['url']}",
                    f"- posted: {j['posted']}", f"- experience: {j['exp'] or 'not stated'}",
                    f"- location: {j.get('location') or 'not stated'}{' (remote)' if j.get('remote') else ''}",
                    f"- pay: {pay}", f"- flags: {', '.join(flags(j)) or 'none'}",
                    f"- skill_tags: {', '.join(j['tags']) or 'none'}", "", "## Job description", ""]
            (out / name).write_text("\n".join(meta) + j["desc"].strip() + "\n")
            w.writerow([name, j["site"], j["title"], j["company"], j["posted"], j["exp"], j.get("location", ""),
                        lo or "", hi or "", ";".join(flags(j)), j["url"], "; ".join(j["tags"])])
    (out / "README.md").write_text(
        f"# 0-{t['max_years']} yr {t['label']} job postings, India, last 30 days\n\n{len(jobs)} postings from LinkedIn "
        f"(Entry level filter), Indeed and Naukri (experience filter), scraped by ../survey.py ({track} track).\n\n"
        f"- Search terms: {', '.join(t['terms'])}.\n"
        f"- Kept: track keywords in the title; no senior/lead/intern/sales/mobile titles; description asks "
        f"<={t['max_years']} yr (or, with no stated years, a fresher/junior signal on Indeed); Naukri range starting "
        f"at <={t['max_years']} yr.\n"
        "- Deduplicated by company + title; postings with no description text dropped.\n"
        "- Flags (heuristic, not removed): big_it = large IT services / big tech; paid_training = description mentions "
        "fees, deposits or placement guarantees; staffing = company name looks like a recruiter/staffing firm.\n"
        "- Pay is normalised to INR per month where the posting states it (most don't).\n"
        "- `index.csv` lists every file with site, title, company, date, experience, location, pay, flags, URL, "
        "Naukri skill tags.\n- Each `NNNN-site-company-title.md` has a metadata header then the full job description.\n"
        f"- Raw unfiltered data (all scraped postings, every experience level): ../{track}_jobs.json\n")
    print(f"exported {len(jobs)} postings to {out}")


def report(track: str) -> None:
    _, rep, _ = paths(track)
    t = TRACKS[track]
    uniq = fresher_jobs(track)
    n = len(uniq)
    req, nice = Counter(), Counter()
    for j in uniq:
        for name, kind in classify(j["desc"], j["tags"]).items():
            (req if kind == "req" else nice)[name] += 1
    per_site = Counter(j["site"] for j in uniq)
    flagged = Counter(f for j in uniq for f in flags(j))
    clean = sum(not flags(j) for j in uniq)
    dates = sorted(j["posted"] for j in uniq if j["posted"][:2] == "20")
    pct = lambda c, d=n: f"{100 * c / d:.0f}%" if d else "-"
    rows = sorted(SKILLS, key=lambda s: -(req[s] + nice[s]))
    core = [s for s in rows if req[s] / n >= 0.15]
    good = [s for s in rows if s not in core and (req[s] + nice[s]) / n >= 0.04]
    pays = [p for j in uniq for p in [monthly_pay(j)] if p[0] or p[1]]
    lows = [lo or hi for lo, hi in pays]
    highs = [hi or lo for lo, hi in pays]
    out = [f"# 0-{t['max_years']} yr {t['label']} roles in India",
           f"{n} unique postings ({', '.join(f'{k} {v}' for k, v in per_site.items())}), "
           f"posted {dates[0] if dates else '?'} to {dates[-1] if dates else '?'}. "
           f"% = share of postings mentioning the skill.", "",
           "## Market size", "",
           f"- {n} postings; {clean} with no flags. Flagged: " + (", ".join(f"{k} {v}" for k, v in flagged.items()) or "none"),
           "- Locations: " + ", ".join(f"{c} {v}" for c, v in Counter(city(j) for j in uniq).most_common(10)),
           f"- Pay stated in {len(pays)} postings ({pct(len(pays))})" + (
               f": median ₹{statistics.median(lows):,.0f}–{statistics.median(highs):,.0f}/month; "
               f"{sum(h >= 25000 for h in highs)} of {len(pays)} reach ₹25k/month at the top of the range." if pays else "."),
           "", "## Core (asked as a requirement in >=15% of postings)", "", "| Skill | Required | Nice-to-have |", "|---|---|---|"]
    out += [f"| {s} | {pct(req[s])} | {pct(nice[s])} |" for s in core]
    out += ["", "## Good to have (4-15%, or mostly listed as nice-to-have)", "", "| Skill | Any mention | Of which nice-to-have |", "|---|---|---|"]
    out += [f"| {s} | {pct(req[s] + nice[s])} | {pct(nice[s])} |" for s in good]
    top_tags = Counter(x.strip().lower() for j in uniq for x in j["tags"]).most_common(40)
    out += ["", "## Naukri recruiter key-skill tags (raw, top 40)", "", ", ".join(f"{x} ({c})" for x, c in top_tags)]
    rep.write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    c = classify("Requirements:\n- Java, Spring Boot\n\nGood to have:\n- Docker\n- Kafka\nResponsibilities:\n- Write SQL", [])
    assert c["Java"] == c["Spring Boot"] == c["SQL"] == "req" and c["Docker"] == c["Kafka"] == "nice", c
    assert classify("Kafka is a plus", [])["Kafka"] == "nice" and "Java" not in classify("JavaScript", [])
    assert "LLMs / GenAI" not in classify("We are an AI-first company", [])
    J = TRACKS["java"]
    j = {"site": "indeed", "title": "Java Developer", "desc": "2+ years of Java experience", "tags": []}
    assert not fresher_ok(j, J) and fresher_ok(j | {"desc": "Freshers welcome. Java"}, J)
    assert not fresher_ok(j | {"title": "Senior Java Dev", "desc": "java"}, J)
    a = {"site": "linkedin", "title": "AI Engineer", "desc": "Build RAG with 2 years of experience", "tags": []}
    assert fresher_ok(a, TRACKS["ai"]) and not fresher_ok(a | {"title": "AI Sales Executive"}, TRACKS["ai"])
    assert not fresher_ok(a | {"title": "Data Annotation Specialist AI"}, TRACKS["ai"])
    b = {"site": "naukri", "title": "Python Backend Developer", "desc": "x", "exp": "1-3 Yrs", "tags": []}
    assert fresher_ok(b, TRACKS["backend"]) and not fresher_ok(b | {"exp": "3-5 Yrs"}, TRACKS["backend"])
    assert not fresher_ok(b | {"title": "Java Backend Developer"}, TRACKS["backend"])
    f = {"site": "naukri", "title": "React Developer", "desc": "x", "exp": "0-2 Yrs", "tags": []}
    assert fresher_ok(f, TRACKS["fullstack"]) and not fresher_ok(f | {"title": "React Native Developer"}, TRACKS["fullstack"])
    assert monthly_pay({"pay_min": 300000, "pay_max": 600000, "pay_interval": "yearly", "currency": "INR"}) == (25000, 50000)
    assert city({"location": "Bangalore, Karnataka, IN"}) == "Bengaluru" and city({"location": "x", "remote": True}) == "Remote"
    track, cmd = sys.argv[1], sys.argv[2]
    assert track in TRACKS, f"track must be one of {list(TRACKS)}"
    {"scrape": scrape, "report": report, "export": export}[cmd](track)
