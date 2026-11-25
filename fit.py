"""Score survey postings against your skills (config.toml [fit]).

  uv run python survey.py backend scrape && uv run python survey.py backend export   # first, per track
  uv run python fit.py                                                               # -> fit.md
"""
import csv
import datetime as dt
import re
from pathlib import Path

from dotenv import dotenv_values

from pull import CFG, HERE

FIT = CFG["fit"]
# skill -> regex. Put the names you have in config.toml [fit] have; everything else counts as missing.
SKILLS = {
 "Python": r"\bpython\b", "FastAPI": r"fastapi", "Django": r"django", "Flask": r"\bflask\b",
 "Node.js": r"node\.?js|\bnode\b", "Express": r"express(\.js)?\b", "NestJS": r"nest\.?js",
 "TypeScript": r"typescript", "JavaScript": r"javascript|\bjs\b", "Go": r"\bgolang\b|\bgo\b(?= |,|/)",
 "Java": r"\bjava\b(?!\s*script)", "Spring": r"spring", "Kotlin": r"kotlin", "C++": r"c\+\+", "C#/.NET": r"c#|\.net\b", "PHP": r"\bphp\b",
 "React": r"react(?!\s*native)", "Next.js": r"next\.?js", "Angular": r"angular", "Vue": r"\bvue",
 "HTML/CSS": r"\bhtml|\bcss\b", "Tailwind": r"tailwind", "Redux": r"redux", "Flutter/RN": r"flutter|react native",
 "PostgreSQL": r"postgres", "MySQL": r"mysql", "MongoDB": r"mongo", "Redis": r"redis",
 "SQL": r"\bsql\b", "GraphQL": r"graphql", "REST": r"\brest(ful)?\b", "WebSockets": r"websocket", "Kafka": r"kafka",
 "Docker": r"docker", "Kubernetes": r"kubernetes|\bk8s\b", "AWS": r"\baws\b|amazon web",
 "GCP": r"\bgcp\b|google cloud", "Azure": r"\bazure\b", "CI/CD": r"ci/cd|github actions|jenkins",
 "Linux": r"linux", "Git": r"\bgit\b|github",
 "LLM": r"\bllms?\b|large language", "GenAI": r"gen ?ai|generative", "RAG": r"\brag\b|retrieval.augmented",
 "Agents": r"agentic|ai agents?|multi.agent", "LangChain": r"langchain", "LangGraph": r"langgraph",
 "MCP": r"\bmcp\b|model context", "Prompt eng.": r"prompt engineering",
 "Vector DB": r"vector (db|database|store)|pinecone|qdrant|faiss|pgvector|weaviate|chroma|milvus",
 "OpenAI/Claude/Gemini": r"openai|gpt-?4|anthropic|claude|gemini", "Hugging Face": r"hugging ?face|transformers",
 "PyTorch": r"pytorch", "TensorFlow": r"tensorflow|keras", "scikit-learn": r"scikit|sklearn",
 "Pandas/NumPy": r"pandas|numpy", "Fine-tuning": r"fine.?tun|lora|peft", "NLP": r"\bnlp\b|natural language processing",
 "Computer vision": r"computer vision|opencv|yolo", "MLOps": r"mlops|mlflow|kubeflow",
 "Spark/Big data": r"spark|hadoop|databricks", "Power BI/Tableau": r"power ?bi|tableau", "Statistics": r"statistic",
}
HAVE = set(FIT["have"])
assert HAVE <= set(SKILLS), f"unknown skills in [fit] have: {HAVE - set(SKILLS)}"
SKIP_TITLE = r"\bintern|data analyst|data scientist|\bsales\b|support|teacher|trainer|faculty|mentor|tester|qa\b"
NEAR = re.compile("|".join(FIT.get("near", [])) or r"(?!)", re.I)
norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())

# Optional: skip jobs already marked applied/skipped in MongoDB (status field), if you track them there.
seen, seen_ct, applied_co = set(), set(), set()
cfg = dotenv_values(HERE / ".env")
if cfg.get("MONGODB_URI"):
    from pymongo import MongoClient
    col = MongoClient(cfg["MONGODB_URI"], serverSelectionTimeoutMS=15000)[cfg.get("MONGODB_DB") or "jobs"]["jobs"]
    for d in col.find({"status": {"$in": ["applied", "skipped"]}}, {"url": 1, "company": 1, "title": 1, "status": 1}):
        seen.add(d.get("url")); seen_ct.add((norm(d.get("company")), norm(d.get("title"))))
        if d.get("status") == "applied":
            applied_co.add(norm(d.get("company")))
cutoff = str(dt.date.today() - dt.timedelta(days=14))

out = []
for track in FIT["tracks"]:
    index = HERE / f"{track}_jds" / "index.csv"
    if not index.exists():
        print(f"no {index.relative_to(HERE)}; run: uv run python survey.py {track} scrape && uv run python survey.py {track} export")
        continue
    for r in csv.DictReader(open(index)):
        if r["flags"] or re.search(SKIP_TITLE, r["title"], re.I) or r["posted"] < cutoff: continue
        if r["url"] in seen or (norm(r["company"]), norm(r["title"])) in seen_ct: continue
        if r["pay_max_month_inr"] and float(r["pay_max_month_inr"]) < FIT.get("min_monthly_pay_inr", 0): continue
        text = (index.parent / r["file"]).read_text().lower()
        asked = [k for k, rx in SKILLS.items() if re.search(rx, text)]
        if len(asked) < 4: continue  # too vague to score
        have = [k for k in asked if k in HAVE]
        # ponytail: keyword fit, not semantic; misses "or" alternatives (Java or Python)
        out.append((round(100 * len(have) / len(asked)), len(have), track, r, [k for k in asked if k not in HAVE], norm(r["company"]) in applied_co))

# rank: >=80% fit first, then number of matched skills (+2 if the location is "near"), then fit
out.sort(key=lambda x: (x[0] >= 80, x[1] + (2 if NEAR.search(x[3]["location"] + " " + x[3]["title"]) else 0), x[0]), reverse=True)
uniq, keys = [], set()
for x in out:
    k = (norm(x[3]["company"]), norm(x[3]["title"]))
    if k not in keys: keys.add(k); uniq.append(x)
with open(HERE / "fit.md", "w") as f:
    f.write(f"# Skill fit, 0-2 yr postings since {cutoff}\n\n{len(uniq)} postings scored.\n"
            "Fit = share of the skills the JD names that you have. * = already applied to another role at this company.\n\n")
    f.write("| Fit | Matched | Track | Company | Title | Location | Pay/mo | Posted | Missing | Link |\n|---|---|---|---|---|---|---|---|---|---|\n")
    for fit, n, track, r, miss, ap in uniq:
        pay = f"{int(float(r['pay_min_month_inr'] or 0))//1000}-{int(float(r['pay_max_month_inr']))//1000}k" if r["pay_max_month_inr"] else ""
        f.write(f"| {fit}% | {n} | {track} | {r['company']}{' *' if ap else ''} | {r['title']} | {r['location']} | {pay} | {r['posted']} | {', '.join(miss)} | {r['url']} |\n")
print(f"{len(uniq)} postings scored -> fit.md")
