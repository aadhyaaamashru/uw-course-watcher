# UW Quest Multi-Course Watcher

A small 24/7 web app that watches multiple University of Waterloo courses in **public Quest Class Search** and emails you when a seat appears.

## What changed in this version

- Add/remove courses from a simple browser UI — no `.env` editing per course.
- Watch any number of courses.
- Checks courses **in parallel**.
- Uses fixed polling cycles: approximately `00:00`, `01:00`, `02:00` when `CHECK_SECONDS=60`, rather than waiting 60 seconds *after* the previous scrape finishes.
- Stores courses and previous seat counts in SQLite so cloud restarts do not erase your watch list.
- Pause/resume individual courses.
- Shows last seat count, enrolment/capacity, reserve indicator, check time, and scrape errors.
- Sends email only when availability appears or increases, not every minute.

## Parallel behavior

With `MAX_CONCURRENCY=4` and four courses:

```text
0:00  CS 349 ─┐
      CS 346 ─┼── all start together
      BET 430 ┤
      STAT 331┘

1:00  all four start together again
2:00  all four start together again
```

If you watch 10 courses with a concurrency of 4, they run in parallel batches of up to four. This avoids opening too many Quest sessions simultaneously.

## Run locally

```bash
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
uvicorn app:app --host 0.0.0.0 --port 8080
```

Open:

```text
http://localhost:8080
```

Then use **Add course** in the dashboard.

For each watch, enter:

- Term, e.g. `Fall 2026`
- Subject, e.g. `CS`
- Course, e.g. `349`
- **Class Number** — recommended because it uniquely identifies the class
- Section — optional alternative if you do not know the class number
- Career — normally `Undergraduate`

## Email setup

Put these in `.env` locally or in your cloud provider's environment variables:

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your_google_app_password
ALERT_TO=you@gmail.com
```

For Gmail, use a **Google App Password**, not your normal Gmail password.

If email settings are omitted, the dashboard/checker still runs but alerts are only visible in logs/UI.

## Polling settings

```env
CHECK_SECONDS=60
MAX_CONCURRENCY=4
```

`CHECK_SECONDS` has a safety floor of 30 seconds in the code. A 60-second interval is recommended.

`MAX_CONCURRENCY=4` means at most four Quest browser contexts run at once. You can increase it, but keeping it modest is friendlier to Quest and uses less RAM.

## Docker

```bash
docker compose up -d --build
```

Then open:

```text
http://localhost:8080
```

The Docker volume stores `/data/watcher.db`, so your course list/state survives container restarts.

## Railway deployment

1. Put this folder in a GitHub repository.
2. Create a Railway project from the repository.
3. Railway detects the included `Dockerfile`.
4. Add the environment variables from `.env.example` in Railway Variables.
5. Add a Railway persistent volume mounted at `/data`.
6. Deploy.
7. In Railway networking/settings, generate a public domain for the service.
8. Open that domain from your phone or computer to add/remove courses.

The app listens on Railway's `PORT` automatically and exposes `/health` for the included Railway health check.

## Data stored

The SQLite database at `/data/watcher.db` stores:

- your watched course identifiers
- enabled/paused state
- last seat count
- capacity/enrolment
- last check/error
- last alert seat count

It does **not** store your Waterloo username or password. The watcher uses public Quest Class Search.

## Reserve capacity

Quest can show available seats that are reserved for particular student groups. The watcher displays whether reserve capacity was detected and includes a warning in the alert. It cannot know whether *you personally* satisfy a reserve requirement, so confirm that in Quest before enrolling.

## Files

- `app.py` — dashboard + SQLite + parallel Quest worker + email alerts
- `Dockerfile` — cloud container
- `docker-compose.yml` — local/VPS continuous deployment
- `railway.toml` — Railway health/restart config
- `.env.example` — runtime/email settings
- `requirements.txt` — Python dependencies
