import base64
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse

SEARCH_PAGE = (
    "https://quest.pecs.uwaterloo.ca/psc/PB/ACADEMIC/SA/c/"
    "UW_PUBLIC_ACCESS.UW_CLASS_SRCH.GBL"
    "?NavColl=true&ICAGTarget=start"
)
POST_URL = (
    "https://quest.pecs.uwaterloo.ca/psc/PB/ACADEMIC/SA/c/"
    "UW_PUBLIC_ACCESS.UW_CLASS_SRCH.GBL"
)
QUEST_HOME = "https://quest.pecs.uwaterloo.ca/"

CHECK_SECONDS = max(30, int(os.getenv("CHECK_SECONDS", "60")))
COURSES_PER_SESSION = max(1, int(os.getenv("COURSES_PER_SESSION", "10")))
REQUEST_TIMEOUT = max(5, int(os.getenv("REQUEST_TIMEOUT", "20")))
DEFAULT_TERM_CODE = os.getenv("DEFAULT_TERM_CODE", "1269").strip() or "1269"
DB_PATH = Path(os.getenv("DB_PATH", "/data/watcher.db"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin").strip() or "admin"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153 Safari/537.36"
)

app = FastAPI(title="UW Course Watcher")
stop_event = threading.Event()
scheduler_thread: threading.Thread | None = None
quest_sessions: list[requests.Session] = []
quest_sessions_lock = threading.Lock()


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str) -> None:
    print(f"[{now_utc()}] {msg}", flush=True)


def _authorized(request: Request) -> bool:
    if not DASHBOARD_PASSWORD:
        return True
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
        return secrets.compare_digest(username, DASHBOARD_USERNAME) and secrets.compare_digest(
            password, DASHBOARD_PASSWORD
        )
    except Exception:
        return False


@app.middleware("http")
async def dashboard_auth(request: Request, call_next):
    if request.url.path == "/health" or _authorized(request):
        return await call_next(request)
    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="UW Course Watcher"'},
    )


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watch_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                term_code TEXT NOT NULL,
                subject TEXT NOT NULL,
                course TEXT NOT NULL,
                class_number TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_status TEXT,
                last_section TEXT,
                last_checked_at TEXT,
                alerted_open INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(term_code, subject, course, class_number)
            )
            """
        )
        conn.commit()


@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def list_targets(enabled_only: bool = False):
    with db_conn() as conn:
        if enabled_only:
            return conn.execute(
                "SELECT * FROM watch_targets WHERE enabled=1 ORDER BY subject, course, class_number"
            ).fetchall()
        return conn.execute(
            "SELECT * FROM watch_targets ORDER BY subject, course, class_number"
        ).fetchall()


def set_target_result(target_id: int, status: str, section: str | None, alerted_open: int | None = None):
    with db_conn() as conn:
        if alerted_open is None:
            conn.execute(
                """
                UPDATE watch_targets
                SET last_status=?, last_section=?, last_checked_at=?
                WHERE id=?
                """,
                (status, section, now_utc(), target_id),
            )
        else:
            conn.execute(
                """
                UPDATE watch_targets
                SET last_status=?, last_section=?, last_checked_at=?, alerted_open=?
                WHERE id=?
                """,
                (status, section, now_utc(), alerted_open, target_id),
            )
        conn.commit()


def new_quest_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def ensure_quest_sessions(count: int) -> list[requests.Session]:
    with quest_sessions_lock:
        while len(quest_sessions) < count:
            quest_sessions.append(new_quest_session())
            log(f"Created Quest guest session #{len(quest_sessions)}")
        return quest_sessions[:count]


def get_form_state(session: requests.Session) -> dict[str, str]:
    response = session.get(SEARCH_PAGE, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    data: dict[str, str] = {}

    for element in soup.select("input[name], select[name]"):
        name = element.get("name")
        if not name:
            continue
        if element.name == "input":
            input_type = element.get("type", "").lower()
            if input_type in {"button", "submit"}:
                continue
            if input_type == "checkbox":
                if element.has_attr("checked"):
                    data[name] = element.get("value", "Y")
                continue
            data[name] = element.get("value", "")
        elif element.name == "select":
            selected = element.select_one("option[selected]")
            if selected:
                data[name] = selected.get("value", "")
            else:
                first = element.select_one("option")
                if first:
                    data[name] = first.get("value", "")
    return data


def extract_inner_html(raw: str) -> str:
    parts = re.findall(
        r"<FIELD\b[^>]*><!\[CDATA\[(.*?)\]\]></FIELD>",
        raw,
        flags=re.DOTALL,
    )
    return "\n".join(parts)


def parse_results(raw: str) -> list[dict[str, str]]:
    html = extract_inner_html(raw)
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []

    for class_link in soup.select("a[id^='MTG_CLASS_NBR$']"):
        class_number = class_link.get_text(strip=True)
        suffix = class_link.get("id", "").split("$")[-1]
        section_link = soup.find(id=f"MTG_CLASSNAME${suffix}")
        status_container = soup.find(id=f"win0divDERIVED_CLSRCH_SSR_STATUS_LONG${suffix}")
        section = " ".join(section_link.stripped_strings) if section_link else "Unknown"

        # The user only wants real Regular sections, not Course Selection placeholders.
        if "Regular" not in section:
            continue

        status = "Unknown"
        if status_container:
            status_img = status_container.find("img")
            if status_img:
                status = status_img.get("alt", "Unknown")

        results.append(
            {
                "class_number": class_number,
                "section": section,
                "status": status.title(),
            }
        )
    return results


def search_course(
    session: requests.Session, term_code: str, subject: str, course: str
) -> tuple[list[dict[str, str]], int, float]:
    data = get_form_state(session)
    data["ICAJAX"] = "1"
    data["CLASS_SRCH_WRK2_STRM$35$"] = term_code
    data["SSR_CLSRCH_WRK_SUBJECT$0"] = subject.upper()
    data["SSR_CLSRCH_WRK_SSR_EXACT_MATCH1$1"] = "E"
    data["SSR_CLSRCH_WRK_CATALOG_NBR$1"] = str(course)
    data["SSR_CLSRCH_WRK_SSR_OPEN_ONLY$chk$3"] = "N"
    data["ICAction"] = "CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH"

    started = time.monotonic()
    response = session.post(
        POST_URL,
        data=data,
        headers={
            "Referer": SEARCH_PAGE,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=REQUEST_TIMEOUT,
    )
    elapsed = time.monotonic() - started
    response.raise_for_status()
    return parse_results(response.text), response.status_code, elapsed


def send_telegram(subject: str, course: str, result: dict[str, str]) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram not configured; cannot send alert")
        return False

    message = (
        f"🚨 {subject} {course} IS OPEN\n\n"
        f"Class #: {result['class_number']}\n"
        f"Section: {result['section']}\n"
        f"Status: {result['status']}\n\n"
        f"Open Quest: {QUEST_HOME}\n"
        f"Use class number {result['class_number']} to enrol."
    )
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        log(f"Telegram alert sent: {subject} {course}, class {result['class_number']}")
        return True
    except Exception as exc:
        log(f"Telegram alert failed: {type(exc).__name__}: {exc}")
        return False


def process_course_search(
    session: requests.Session,
    course_key: tuple[str, str, str],
    targets: list[sqlite3.Row],
) -> None:
    term_code, subject, course = course_key
    try:
        results, status_code, elapsed = search_course(session, term_code, subject, course)
        log(f"HTTP {status_code} | {subject} {course} | {elapsed:.2f}s | {len(results)} Regular section(s)")
        by_class = {r["class_number"]: r for r in results}

        for target in targets:
            result = by_class.get(str(target["class_number"]))
            if result is None:
                # Don't re-arm an alert just because a transient parse/search did not find it.
                set_target_result(target["id"], "Not found", None, None)
                log(f"  target class {target['class_number']} not found among Regular sections")
                continue

            status = result["status"]
            log(f"  class {result['class_number']} | {result['section']} | {status}")

            if status == "Open":
                if not target["alerted_open"]:
                    if send_telegram(subject, course, result):
                        set_target_result(target["id"], status, result["section"], 1)
                    else:
                        set_target_result(target["id"], status, result["section"], 0)
                else:
                    set_target_result(target["id"], status, result["section"], 1)
            elif status == "Closed":
                # Re-arm so another future Open state produces a new ping.
                set_target_result(target["id"], status, result["section"], 0)
            else:
                set_target_result(target["id"], status, result["section"], None)

    except requests.exceptions.Timeout:
        log(f"TIMEOUT | {subject} {course}")
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        log(f"HTTP ERROR {status} | {subject} {course} | {exc}")
    except Exception as exc:
        log(f"ERROR | {subject} {course} | {type(exc).__name__}: {exc}")


def worker_run(
    worker_index: int,
    session: requests.Session,
    assignments: list[tuple[tuple[str, str, str], list[sqlite3.Row]]],
    cycle_start: float,
) -> None:
    if not assignments:
        return
    interval = CHECK_SECONDS / len(assignments)
    for i, (course_key, targets) in enumerate(assignments):
        target_time = cycle_start + i * interval
        delay = target_time - time.monotonic()
        if delay > 0 and stop_event.wait(delay):
            return
        process_course_search(session, course_key, targets)


def build_assignments(rows: list[sqlite3.Row]):
    grouped: "OrderedDict[tuple[str, str, str], list[sqlite3.Row]]" = OrderedDict()
    for row in rows:
        key = (row["term_code"], row["subject"], row["course"])
        grouped.setdefault(key, []).append(row)

    items = list(grouped.items())
    if not items:
        return []

    session_count = math.ceil(len(items) / COURSES_PER_SESSION)
    buckets: list[list] = [[] for _ in range(session_count)]
    # Round-robin keeps the sessions balanced, e.g. 11 courses -> 6 + 5.
    for i, item in enumerate(items):
        buckets[i % session_count].append(item)
    return buckets


def scheduler_loop() -> None:
    log("Background scheduler started")
    while not stop_event.is_set():
        cycle_start = time.monotonic()
        rows = list_targets(enabled_only=True)
        buckets = build_assignments(rows)

        if not buckets:
            if stop_event.wait(5):
                return
            continue

        sessions = ensure_quest_sessions(len(buckets))
        distinct_courses = sum(len(bucket) for bucket in buckets)
        log(
            f"Cycle: {distinct_courses} distinct course search(es), "
            f"{len(buckets)} Quest session(s), {len(rows)} target class(es)"
        )

        threads: list[threading.Thread] = []
        for idx, bucket in enumerate(buckets):
            t = threading.Thread(
                target=worker_run,
                args=(idx, sessions[idx], bucket, cycle_start),
                daemon=True,
                name=f"quest-worker-{idx+1}",
            )
            t.start()
            threads.append(t)

        for t in threads:
            while t.is_alive() and not stop_event.is_set():
                t.join(timeout=0.5)

        remaining = cycle_start + CHECK_SECONDS - time.monotonic()
        if remaining > 0 and stop_event.wait(remaining):
            return


def status_badge(status: str | None) -> str:
    if status == "Open":
        return '<span class="badge open">OPEN</span>'
    if status == "Closed":
        return '<span class="badge closed">Closed</span>'
    if status == "Not found":
        return '<span class="badge warn">Not found</span>'
    return '<span class="badge unknown">Waiting</span>'


def page_html(message: str = "") -> str:
    rows = list_targets(False)
    telegram_state = "configured" if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else "NOT configured"
    row_html = ""
    for row in rows:
        enabled = bool(row["enabled"])
        row_html += f"""
        <tr>
          <td><strong>{escape(row['subject'])} {escape(row['course'])}</strong></td>
          <td><code>{escape(row['class_number'])}</code></td>
          <td>{escape(row['last_section'] or '—')}</td>
          <td>{status_badge(row['last_status'])}</td>
          <td>{escape(row['last_checked_at'] or 'Not checked yet')}</td>
          <td>{'Watching' if enabled else 'Paused'}</td>
          <td class="actions">
            <form method="post" action="/targets/{row['id']}/toggle"><button class="secondary">{'Pause' if enabled else 'Resume'}</button></form>
            <form method="post" action="/targets/{row['id']}/delete" onsubmit="return confirm('Delete this target?')"><button class="danger">Delete</button></form>
          </td>
        </tr>
        """

    if not row_html:
        row_html = '<tr><td colspan="7" class="empty">No class numbers added yet.</td></tr>'

    msg_html = f'<div class="message">{escape(message)}</div>' if message else ""
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>UW Course Watcher</title>
<style>
body {{ font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; margin:0; background:#f6f7f9; color:#171717; }}
.wrap {{ max-width:1100px; margin:36px auto; padding:0 18px; }}
.card {{ background:white; border:1px solid #e3e5e8; border-radius:14px; padding:22px; margin-bottom:18px; box-shadow:0 2px 8px rgba(0,0,0,.04); }}
h1 {{ margin:0 0 6px; font-size:28px; }} .sub {{ color:#666; margin-bottom:18px; }}
.grid {{ display:grid; grid-template-columns:1fr 1fr 1fr 1fr auto; gap:10px; align-items:end; }}
label {{ font-size:13px; color:#555; display:block; margin-bottom:5px; }}
input {{ width:100%; box-sizing:border-box; padding:10px 11px; border:1px solid #ccd0d5; border-radius:8px; font-size:15px; }}
button {{ border:0; background:#111; color:white; padding:10px 14px; border-radius:8px; cursor:pointer; font-weight:600; }}
button.secondary {{ background:#eceff2; color:#222; }} button.danger {{ background:#b42318; }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; padding:11px 9px; border-bottom:1px solid #eee; vertical-align:middle; }} th {{ font-size:12px; color:#666; text-transform:uppercase; letter-spacing:.03em; }}
.badge {{ display:inline-block; padding:4px 8px; border-radius:999px; font-size:12px; font-weight:700; }}
.open {{ background:#d9fbe5; color:#087a35; }} .closed {{ background:#f3f4f6; color:#555; }} .warn {{ background:#fff0c2; color:#7a5600; }} .unknown {{ background:#e9eef8; color:#3b527b; }}
.actions {{ display:flex; gap:7px; }} .actions form {{ margin:0; }} .message {{ padding:10px 12px; background:#eaf4ff; border-radius:8px; margin-bottom:12px; }} .empty {{ color:#777; text-align:center; padding:30px; }}
.meta {{ display:flex; flex-wrap:wrap; gap:10px 18px; font-size:13px; color:#666; }} code {{ background:#f2f3f5; padding:2px 5px; border-radius:5px; }}
@media(max-width:780px) {{ .grid {{ grid-template-columns:1fr 1fr; }} table {{ font-size:13px; }} .wrap {{ margin-top:18px; }} }}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>UW Course Watcher</h1>
    <div class="sub">Add the exact Quest class number you want. The watcher pings Telegram whenever that Regular section is Open.</div>
    {msg_html}
    <form method="post" action="/targets" class="grid">
      <div><label>Term code</label><input name="term_code" value="{escape(DEFAULT_TERM_CODE)}" required></div>
      <div><label>Subject</label><input name="subject" placeholder="BET" required></div>
      <div><label>Course</label><input name="course" placeholder="405" required></div>
      <div><label>Exact Class #</label><input name="class_number" placeholder="12345" required></div>
      <div><button type="submit">Add class</button></div>
    </form>
    <div class="meta" style="margin-top:16px">
      <span>Cycle: <strong>{CHECK_SECONDS}s</strong></span>
      <span>Up to <strong>{COURSES_PER_SESSION}</strong> distinct courses per Quest session</span>
      <span>Telegram: <strong>{telegram_state}</strong></span>
      <span>Database: <code>{escape(str(DB_PATH))}</code></span>
    </div>
    <form method="post" action="/test-telegram" style="margin-top:14px"><button class="secondary" type="submit">Send test Telegram</button></form>
  </div>
  <div class="card" style="overflow-x:auto">
    <table>
      <thead><tr><th>Course</th><th>Class #</th><th>Section</th><th>Status</th><th>Last checked</th><th>Watcher</th><th></th></tr></thead>
      <tbody>{row_html}</tbody>
    </table>
  </div>
</div>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, message: str = ""):
    return HTMLResponse(page_html(message))


@app.post("/targets")
def add_target(
    term_code: str = Form(...),
    subject: str = Form(...),
    course: str = Form(...),
    class_number: str = Form(...),
):
    term_code = term_code.strip()
    subject = subject.strip().upper()
    course = course.strip().upper()
    class_number = class_number.strip()
    if not term_code or not subject or not course or not class_number:
        return RedirectResponse("/?message=All+fields+are+required", status_code=303)
    try:
        with db_conn() as conn:
            conn.execute(
                """
                INSERT INTO watch_targets
                (term_code, subject, course, class_number, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (term_code, subject, course, class_number, now_utc()),
            )
            conn.commit()
        return RedirectResponse("/?message=Class+added", status_code=303)
    except sqlite3.IntegrityError:
        return RedirectResponse("/?message=That+class+is+already+being+watched", status_code=303)


@app.post("/targets/{target_id}/toggle")
def toggle_target(target_id: int):
    with db_conn() as conn:
        conn.execute(
            "UPDATE watch_targets SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?",
            (target_id,),
        )
        conn.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/targets/{target_id}/delete")
def delete_target(target_id: int):
    with db_conn() as conn:
        conn.execute("DELETE FROM watch_targets WHERE id=?", (target_id,))
        conn.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/test-telegram")
def test_telegram():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return RedirectResponse("/?message=Telegram+is+not+configured", status_code=303)
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": "✅ UW Course Watcher Telegram test succeeded."},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return RedirectResponse("/?message=Test+Telegram+sent", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/?message=Telegram+test+failed%3A+{type(exc).__name__}", status_code=303)


@app.get("/health")
def health():
    return {
        "ok": True,
        "check_seconds": CHECK_SECONDS,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
    }


@app.on_event("startup")
def startup_event():
    global scheduler_thread
    init_db()
    stop_event.clear()
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True, name="quest-scheduler")
    scheduler_thread.start()
    log("UW Course Watcher started")


@app.on_event("shutdown")
def shutdown_event():
    stop_event.set()
    log("UW Course Watcher stopping")
