# UW Course Watcher — final UI version

A small Railway-hosted app that watches exact University of Waterloo Quest class numbers and sends a Telegram push when a **Regular** section is **Open**.

## How it works

- Add `Subject + Course + exact Class #` from the web dashboard.
- Targets are stored in SQLite at `/data/watcher.db`.
- Distinct courses are spread evenly across a 60-second cycle.
- One persistent Quest guest cookie session handles up to 10 distinct courses.
- 11–20 distinct courses automatically use 2 Quest guest sessions, 21–30 use 3, etc.
- Multiple class numbers under the same course use one Quest search.
- If a target is Open on the very first check, Telegram alerts immediately.
- It does not ping every minute while the same class stays Open. A Closed result re-arms the alert so a later Open state can notify again.

## Railway variables

```env
CHECK_SECONDS=60
COURSES_PER_SESSION=10
REQUEST_TIMEOUT=20
DEFAULT_TERM_CODE=1269
DB_PATH=/data/watcher.db

TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=choose-a-password
```

`DASHBOARD_PASSWORD` is recommended because the Railway domain is public. If omitted, the UI has no authentication.

## Railway volume

Attach a persistent volume mounted at:

```text
/data
```

This keeps your UI-added class numbers and alert state across restarts/redeploys.

## Railway domain

Because this final version has a web UI, enable Railway **Public Networking** and generate a domain. Open that URL to add/pause/delete exact class numbers.

## Update the existing repo

From the existing local repo, copy these files over the repo root and push:

```bash
git add .
git commit -m "Final direct Quest watcher UI"
git push
```

Railway should redeploy the same service automatically.
