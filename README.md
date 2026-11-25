# jobwatch

Get a fresh list of entry-level jobs from **LinkedIn, Indeed and Naukri (India)** several times a day, already filtered to
your roles, cities and experience level, with a desktop notification when new ones appear. Apply early, while the
applicant count is still low.

Built on [JobSpy](https://github.com/speedyapply/JobSpy), plus a few fixes on top:
- real posting times (LinkedIn's "3 minutes ago" and Indeed's exact timestamp), not just the date;
- Naukri jobs without a company logo are no longer silently dropped;
- Naukri's own experience filter, so every Naukri result is in your range;
- LinkedIn/Indeed descriptions are read and a job is dropped only when it *clearly* asks for more years than you set
  ("5+ years preferred" or "1+ year with LLMs" keep the job);
- jobs outside your cities are kept only if the description says they're remote.

## Setup (5 minutes)

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) (it installs the right Python for you).

```bash
git clone <this repo> jobwatch && cd jobwatch
uv sync
cp config.example.toml config.toml      # then edit it: roles, cities, years, skills
uv run python pull.py --check            # checks your config loads
uv run python pull.py                    # one search, last 12h -> jobs.md
```

Open `jobs.md`: one line per job with the link, company, title, location, posting date and site.

## Run it on a schedule

`uv run python pull.py --watch` is safe to run as often as you like. It runs a full search at the hours in
`[schedule] slots` (catching up if your laptop was asleep), and in between a quick pass every 30 min for "easy apply"
jobs, which fill up fastest. It keeps a rolling 24h list in **`jobs_24h.md`** (new ones marked `NEW`) and pops a
desktop notification.

**macOS** (runs at login, then every 15 min):
```bash
mkdir -p logs
sed -e "s|REPO|$PWD|g" -e "s|UV|$(which uv)|g" scheduling/jobwatch.plist > ~/Library/LaunchAgents/local.jobwatch.plist
launchctl load ~/Library/LaunchAgents/local.jobwatch.plist
# stop: launchctl unload ~/Library/LaunchAgents/local.jobwatch.plist
```

**Linux** (`crontab -e`; notifications need `notify-send`):
```
*/15 * * * * cd /path/to/jobwatch && $HOME/.local/bin/uv run python pull.py --watch >> logs/watch.log 2>&1
```

**Windows**: Task Scheduler, every 15 min, program `uv`, arguments `run python pull.py --watch`, start in the repo
folder. (No notification; check `jobs_24h.md`.)

## Configure (`config.toml`)

| Section | What it controls |
|---|---|
| `[search]` | search terms per site, LinkedIn locations, max years of experience |
| `[filters]` | title words to want / skip, companies to skip, your cities (empty = anywhere in India) |
| `[schedule]` | full-search hours, quick-pass interval |
| `[fit]` | your skills, for `fit.py` |

Tips: keep LinkedIn terms few (it rate-limits; Indeed and Naukri take more). If you get too much noise, add words to
`skip_title`; if you miss jobs, loosen `want_title`.

## Optional: market survey and skill fit

```bash
uv run python survey.py backend scrape    # last 30 days of 0-2 yr postings (slow; resumable)
uv run python survey.py backend report    # -> backend_skills.md: which skills postings ask for, pay, cities
uv run python survey.py backend export    # -> backend_jds/: one file per posting + index.csv
uv run python fit.py                      # -> fit.md: postings ranked by how many asked skills you have
```
Tracks: `java`, `ai`, `backend`, `fullstack`. Useful for deciding what to learn next, and as a second list to apply from.

## Optional: MongoDB

Copy `.env.example` to `.env` and set `MONGODB_URI` to also store every job in MongoDB (e.g. to build your own tracker).
If you set a `status` field to `applied` or `skipped` on a job there, `fit.py` leaves it out.

## Notes

- Scraping is gentle (sequential, with pauses) but LinkedIn may still rate-limit you for a while; the run just skips it.
- Use it for your own job search. Respect each site's terms.
