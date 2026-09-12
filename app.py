import os
import re
import asyncio
import sqlite3
import smtplib
import html
import json
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from time import monotonic

from dotenv import load_dotenv
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from playwright.async_api import async_playwright

load_dotenv()

HELP_URL = "https://uwaterloo.ca/the-centre/quest/quest-help/how-do-i-search-class"
DB_PATH = Path(os.getenv("DB_PATH", "/data/watcher.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
CHECK_SECONDS = max(30, int(os.getenv("CHECK_SECONDS", "60")))
MAX_CONCURRENCY = max(1, int(os.getenv("MAX_CONCURRENCY", "4")))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
SCREENSHOT_ON_ERROR = os.getenv("SCREENSHOT_ON_ERROR", "false").lower() == "true"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

runtime = {
    "playwright": None,
    "browser": None,
    "worker": None,
    "last_cycle_started": None,
    "last_cycle_finished": None,
}


def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS courses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                term TEXT NOT NULL,
                subject TEXT NOT NULL,
                course TEXT NOT NULL,
                class_number TEXT DEFAULT '',
                section TEXT DEFAULT '',
                career TEXT DEFAULT 'Undergraduate',
                enabled INTEGER NOT NULL DEFAULT 1,
                available_seats INTEGER,
                capacity INTEGER,
                enrolled INTEGER,
                quest_status TEXT,
                reserve_present INTEGER,
                last_checked TEXT,
                last_error TEXT,
                last_alerted_seats INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def now_str():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def normalize(value):
    return re.sub(r"\s+", " ", value or " ").strip()


async def first_visible(locator):
    try:
        count = await locator.count()
    except Exception:
        return None
    for i in range(count):
        item = locator.nth(i)
        try:
            if await item.is_visible():
                return item
        except Exception:
            pass
    return None


async def find_input(root, label_patterns):
    for pat in label_patterns:
        try:
            loc = root.get_by_label(re.compile(pat, re.I))
            item = await first_visible(loc)
            if item:
                return item
        except Exception:
            pass

    try:
        labels = root.locator("label")
        count = await labels.count()
    except Exception:
        return None
    for i in range(count):
        lab = labels.nth(i)
        try:
            txt = normalize(await lab.inner_text())
        except Exception:
            continue
        if any(re.search(p, txt, re.I) for p in label_patterns):
            target = await lab.get_attribute("for")
            if target:
                cand = root.locator(f"#{target}")
                try:
                    if await cand.count() and await cand.first.is_visible():
                        return cand.first
                except Exception:
                    pass
    return None


async def set_field(root, patterns, value):
    field = await find_input(root, patterns)
    if not field:
        return False
    tag = await field.evaluate("el => el.tagName.toLowerCase()")
    if tag == "select":
        options = field.locator("option")
        wanted = value.strip().lower()
        for i in range(await options.count()):
            opt = options.nth(i)
            label = normalize(await opt.inner_text())
            opt_value = (await opt.get_attribute("value") or "").strip()
            if label.lower() == wanted or opt_value.lower() == wanted or label.lower().startswith(wanted):
                await field.select_option(index=i)
                return True
        return False
    else:
        await field.fill(value)
    return True


async def choose_term(root, term):
    field = await find_input(root, [r"^Term$", r"Academic Term", r"Term"])
    if not field:
        return False
    tag = await field.evaluate("el => el.tagName.toLowerCase()")
    if tag != "select":
        return False
    options = field.locator("option")
    for i in range(await options.count()):
        opt = options.nth(i)
        txt = normalize(await opt.inner_text())
        if term.lower() in txt.lower():
            await field.select_option(index=i)
            return True
    return False


async def enter_public_quest(page):
    await page.goto(HELP_URL, wait_until="domcontentloaded", timeout=60_000)
    public_link = page.get_by_role("link", name=re.compile(r"Quest Class Search page from this link", re.I))
    if not await public_link.count():
        public_link = page.locator('a[href*="quest.pecs.uwaterloo.ca"]').last
    href = await public_link.get_attribute("href")
    if not href:
        raise RuntimeError("Could not find the public Quest Class Search URL.")
    await page.goto(href, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(1800)
    text = normalize(await page.locator("body").inner_text())
    if "User ID" in text and "Password" in text and "Class Search" not in text:
        raise RuntimeError("Quest redirected the public browser session to sign-in.")


async def dump_quest_diagnostics(page, course):
    print(f"\n=== QUEST DIAGNOSTICS: {course['subject']} {course['course']} ===", flush=True)
    try:
        print(f"PAGE URL: {page.url}", flush=True)
        print(f"PAGE TITLE: {await page.title()}", flush=True)
    except Exception as exc:
        print(f"PAGE META ERROR: {exc}", flush=True)

    frames = page.frames
    print(f"FRAME COUNT: {len(frames)}", flush=True)
    for idx, frame in enumerate(frames):
        try:
            print(f"FRAME[{idx}] name={frame.name!r} url={frame.url}", flush=True)
            inputs = frame.locator("input, select, textarea, button")
            count = min(await inputs.count(), 120)
            print(f"FRAME[{idx}] controls={count}", flush=True)
            for i in range(count):
                el = inputs.nth(i)
                try:
                    info = await el.evaluate("""el => ({
                        tag: el.tagName,
                        type: el.getAttribute('type'),
                        id: el.id,
                        name: el.getAttribute('name'),
                        value: el.value,
                        placeholder: el.getAttribute('placeholder'),
                        aria: el.getAttribute('aria-label'),
                        title: el.getAttribute('title'),
                        text: (el.innerText || '').trim().slice(0,120),
                        visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                    })""")
                    print(f"  CONTROL[{i}] {info}", flush=True)
                except Exception as exc:
                    print(f"  CONTROL[{i}] <inspect error: {exc}>", flush=True)

            labels = frame.locator("label")
            lcount = min(await labels.count(), 80)
            for i in range(lcount):
                lab = labels.nth(i)
                try:
                    txt = normalize(await lab.inner_text())
                    target = await lab.get_attribute("for")
                    if txt:
                        print(f"  LABEL[{i}] text={txt!r} for={target!r}", flush=True)
                except Exception:
                    pass

            body_text = normalize(await frame.locator("body").inner_text(timeout=3000))
            print(f"FRAME[{idx}] BODY PREVIEW: {body_text[:2500]}", flush=True)
        except Exception as exc:
            print(f"FRAME[{idx}] INSPECTION ERROR: {exc}", flush=True)

    print("=== END QUEST DIAGNOSTICS ===\n", flush=True)


async def get_quest_search_frame(page):
    """Return the PeopleSoft frame that contains the real public class-search form."""
    # Waterloo Quest renders the public search form inside this PeopleSoft frame.
    for frame in page.frames:
        if "UW_CLASS_SRCH.GBL" in frame.url:
            try:
                if await frame.locator('#SSR_CLSRCH_WRK_SUBJECT\$0').count():
                    return frame
            except Exception:
                pass

    # Fallback: identify the frame by the actual stable PeopleSoft controls.
    for frame in page.frames:
        try:
            if (
                await frame.locator('#SSR_CLSRCH_WRK_SUBJECT\$0').count()
                and await frame.locator('#SSR_CLSRCH_WRK_CATALOG_NBR\$1').count()
            ):
                return frame
        except Exception:
            pass
    return None


async def select_option_by_text(locator, wanted):
    wanted = wanted.strip().lower()
    options = locator.locator("option")
    for i in range(await options.count()):
        opt = options.nth(i)
        txt = normalize(await opt.inner_text())
        # PeopleSoft sometimes includes bidi control characters in option labels.
        txt_clean = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", txt).strip()
        if wanted == txt_clean.lower() or wanted in txt_clean.lower():
            await locator.select_option(index=i)
            return True
    return False


async def perform_search(page, course):
    root = await get_quest_search_frame(page)
    if root is None:
        await dump_quest_diagnostics(page, course)
        raise RuntimeError("Could not find the Quest class-search frame.")

    # Use the exact PeopleSoft controls observed on Waterloo's live public Quest page.
    term = root.locator('#CLASS_SRCH_WRK2_STRM\$35\$')
    subject = root.locator('#SSR_CLSRCH_WRK_SUBJECT\$0')
    match_mode = root.locator('#SSR_CLSRCH_WRK_SSR_EXACT_MATCH1\$1')
    catalog = root.locator('#SSR_CLSRCH_WRK_CATALOG_NBR\$1')
    career = root.locator('#SSR_CLSRCH_WRK_ACAD_CAREER\$2')
    open_only = root.locator('#SSR_CLSRCH_WRK_SSR_OPEN_ONLY\$3')
    search_btn = root.locator('#CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH')

    if not await term.count() or not await subject.count() or not await catalog.count():
        await dump_quest_diagnostics(page, course)
        raise RuntimeError("Quest search controls were not found in the expected frame.")

    if not await select_option_by_text(term, course["term"]):
        raise RuntimeError(f"Could not select term {course['term']!r} in Quest.")

    await subject.fill(course["subject"])

    # Force an exact course-number match, then fill the *actual* catalog-number input.
    # The visible 'Course Number' label points at the comparator select, not this input,
    # which is why the previous generic label-based code failed.
    if await match_mode.count():
        try:
            await match_mode.select_option(label="is exactly")
        except Exception:
            await select_option_by_text(match_mode, "is exactly")
    await catalog.fill(course["course"])

    if await career.count():
        if not await select_option_by_text(career, course["career"]):
            # Career is useful but not required to submit the search.
            print(f"Warning: could not select career {course['career']!r}; continuing.", flush=True)

    if await open_only.count():
        try:
            if await open_only.is_checked():
                await open_only.uncheck()
        except Exception:
            pass

    if not await search_btn.count():
        raise RuntimeError("Could not find Quest Search button.")

    await search_btn.click()
    # PeopleSoft performs a postback inside the frame. Give it time to replace the content.
    await page.wait_for_timeout(3500)

    # Re-resolve the frame after the postback in case PeopleSoft replaced it.
    result_root = await get_quest_search_frame(page)
    return result_root or root


async def open_target_class(root, course):
    body_text = normalize(await root.locator("body").inner_text())
    if course["subject"] not in body_text.upper() or course["course"] not in body_text.upper():
        raise RuntimeError(f"Results do not appear to contain {course['subject']} {course['course']}.")

    if course["class_number"]:
        links = root.get_by_role("link", name=re.compile(rf"\b{re.escape(course['class_number'])}\b"))
        item = await first_visible(links)
        if item:
            await item.click()
            await root.wait_for_timeout(1400)
            return

    if course["section"]:
        item = await first_visible(root.get_by_text(re.compile(re.escape(course["section"]), re.I)))
        if item:
            try:
                await item.click()
                await root.wait_for_timeout(1400)
                return
            except Exception:
                pass

    # Only allow first-match fallback when no section/class was specified.
    if not course["class_number"] and not course["section"]:
        links = root.locator("a")
        for i in range(await links.count()):
            a = links.nth(i)
            try:
                txt = normalize(await a.inner_text())
                if re.fullmatch(r"\d{4,5}(?:\s+.*)?", txt):
                    await a.click()
                    await root.wait_for_timeout(1400)
                    return
            except Exception:
                pass
    else:
        raise RuntimeError("Target class number/section was not found in search results.")


def extract_number(text, label):
    patterns = [rf"{label}\s*[:\-]?\s*(\d+)", rf"{label}\s+[^\d]{{0,20}}(\d+)"]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return int(m.group(1))
    return None


async def parse_availability(root):
    text = normalize(await root.locator("body").inner_text())
    capacity = extract_number(text, r"Class Capacity")
    enrolled = extract_number(text, r"Enrollment Total")
    available = extract_number(text, r"Available Seats")
    if available is None and capacity is not None and enrolled is not None:
        available = capacity - enrolled
    if available is None:
        raise RuntimeError("Could not parse Available Seats from Quest.")

    status = None
    if re.search(r"\bStatus\b.{0,40}\bOpen\b", text, re.I):
        status = "Open"
    elif re.search(r"\bStatus\b.{0,40}\bClosed\b", text, re.I):
        status = "Closed"
    reserve_present = bool(re.search(r"Reserve Capacity", text, re.I))
    return {
        "capacity": capacity,
        "enrolled": enrolled,
        "available_seats": available,
        "status": status,
        "reserve_present": reserve_present,
    }


async def check_course(course, browser, semaphore):
    async with semaphore:
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        page = await context.new_page()
        try:
            await enter_public_quest(page)
            root = await perform_search(page, course)
            await open_target_class(root, course)
            return await parse_availability(root)
        except Exception:
            if SCREENSHOT_ON_ERROR:
                try:
                    shot = f"/data/error-course-{course['id']}.png"
                    await page.screenshot(path=shot, full_page=True)
                    print(f"Saved Quest error screenshot: {shot}", flush=True)
                except Exception as shot_exc:
                    print(f"Could not save Quest error screenshot: {shot_exc}", flush=True)
            raise
        finally:
            await context.close()


def send_telegram_sync(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Telegram not configured; alert suppressed.", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    if not data.get("ok"):
        raise RuntimeError(f"Telegram send failed: {data}")


async def save_result_and_maybe_alert(course, result):
    checked = now_str()
    seats = result["available_seats"]
    previous = course["available_seats"]
    last_alerted = course["last_alerted_seats"]

    should_alert = seats > 0 and (
        previous is None or previous <= 0 or (seats > previous and (last_alerted is None or seats > last_alerted))
    )

    with db_conn() as conn:
        conn.execute(
            """UPDATE courses SET available_seats=?, capacity=?, enrolled=?, quest_status=?,
               reserve_present=?, last_checked=?, last_error=NULL WHERE id=?""",
            (seats, result["capacity"], result["enrolled"], result["status"], int(result["reserve_present"]), checked, course["id"]),
        )
        conn.commit()

    if should_alert:
        reserve_note = (
            "\nWARNING: Quest shows reserve capacity. Confirm that the available seat is available to your student group."
            if result["reserve_present"] else ""
        )
        target = course["class_number"] or course["section"] or "first matching class"
        body = (
            f"Quest Class Search shows an opening.\n\n"
            f"Course: {course['subject']} {course['course']}\n"
            f"Term: {course['term']}\nTarget: {target}\n"
            f"Available Seats: {seats}\nEnrollment Total: {result['enrolled']}\n"
            f"Class Capacity: {result['capacity']}\nQuest status: {result['status']}\n"
            f"Detected: {checked}{reserve_note}\n\nOpen Quest and enrol immediately."
        )
        try:
            await asyncio.to_thread(send_telegram_sync, f"🚨 UW seat alert: {course['subject']} {course['course']} — {seats} available\n\n{body}")
            with db_conn() as conn:
                conn.execute("UPDATE courses SET last_alerted_seats=? WHERE id=?", (seats, course["id"]))
                conn.commit()
            print(f"ALERT SENT: {course['subject']} {course['course']} ({seats} seats)", flush=True)
        except Exception as exc:
            print(f"Telegram alert failed: {exc}", flush=True)


async def mark_error(course_id, message):
    with db_conn() as conn:
        conn.execute("UPDATE courses SET last_checked=?, last_error=? WHERE id=?", (now_str(), str(message)[:800], course_id))
        conn.commit()


async def run_cycle():
    runtime["last_cycle_started"] = now_str()
    with db_conn() as conn:
        courses = [dict(r) for r in conn.execute("SELECT * FROM courses WHERE enabled=1 ORDER BY id").fetchall()]

    if not courses:
        runtime["last_cycle_finished"] = now_str()
        return

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def one(course):
        label = f"{course['subject']} {course['course']}"
        try:
            result = await check_course(course, runtime["browser"], semaphore)
            await save_result_and_maybe_alert(course, result)
            print(f"[{now_str()}] {label}: {result['available_seats']} available", flush=True)
        except Exception as exc:
            await mark_error(course["id"], exc)
            print(f"[{now_str()}] {label}: ERROR {exc}", flush=True)

    await asyncio.gather(*(one(c) for c in courses))
    runtime["last_cycle_finished"] = now_str()


async def watcher_loop():
    while True:
        started = monotonic()
        try:
            await run_cycle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Watcher cycle failed: {exc}", flush=True)
        elapsed = monotonic() - started
        await asyncio.sleep(max(0.0, CHECK_SECONDS - elapsed))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    runtime["playwright"] = await async_playwright().start()
    runtime["browser"] = await runtime["playwright"].chromium.launch(headless=HEADLESS)
    runtime["worker"] = asyncio.create_task(watcher_loop())
    try:
        yield
    finally:
        if runtime["worker"]:
            runtime["worker"].cancel()
            try:
                await runtime["worker"]
            except asyncio.CancelledError:
                pass
        if runtime["browser"]:
            await runtime["browser"].close()
        if runtime["playwright"]:
            await runtime["playwright"].stop()


app = FastAPI(lifespan=lifespan)


CSS = """
<style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#171717;background:#f7f7f8}
*{box-sizing:border-box} body{margin:0}.wrap{max-width:1050px;margin:36px auto;padding:0 18px}.top{display:flex;justify-content:space-between;gap:20px;align-items:end;margin-bottom:20px}
h1{margin:0;font-size:30px} .muted{color:#666;font-size:14px}.card{background:white;border:1px solid #e6e6e8;border-radius:14px;padding:18px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,.03)}
.grid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}.field label{display:block;font-size:12px;color:#555;margin-bottom:5px}.field input,.field select{width:100%;padding:10px;border:1px solid #d7d7da;border-radius:8px;background:white}
button,.btn{border:0;border-radius:8px;padding:9px 12px;cursor:pointer;text-decoration:none;display:inline-block;font-weight:600}.primary{background:#111;color:white}.ghost{background:#efeff1;color:#222}.danger{background:#fff0f0;color:#a00}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px 9px;border-bottom:1px solid #eee;font-size:14px}th{color:#666;font-size:12px}.status{font-weight:700}.open{color:#087a35}.full{color:#a11}.unknown{color:#777}.err{font-size:12px;color:#a11;max-width:300px}.actions{display:flex;gap:6px;flex-wrap:wrap}
@media(max-width:800px){.grid{grid-template-columns:1fr 1fr}.top{align-items:start;flex-direction:column}table{display:block;overflow-x:auto}}
</style>
"""


def esc(v):
    return html.escape("" if v is None else str(v))


def course_status(row):
    if row["last_error"]:
        return '<span class="status unknown">⚠ Error</span>'
    if row["available_seats"] is None:
        return '<span class="status unknown">● Waiting</span>'
    if row["available_seats"] > 0:
        return f'<span class="status open">● {row["available_seats"]} open</span>'
    return '<span class="status full">● Full</span>'


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with db_conn() as conn:
        rows = conn.execute("SELECT * FROM courses ORDER BY id DESC").fetchall()

    table_rows = ""
    for r in rows:
        target = r["class_number"] or r["section"] or "Any"
        reserve = "Yes" if r["reserve_present"] else ("No" if r["reserve_present"] == 0 else "—")
        table_rows += f"""
        <tr>
          <td><b>{esc(r['subject'])} {esc(r['course'])}</b><br><span class='muted'>{esc(r['term'])}</span></td>
          <td>{esc(target)}</td><td>{course_status(r)}</td>
          <td>{esc(r['enrolled']) if r['enrolled'] is not None else '—'} / {esc(r['capacity']) if r['capacity'] is not None else '—'}</td>
          <td>{reserve}</td><td>{esc(r['last_checked'] or 'Not checked yet')}</td>
          <td><div class='actions'>
            <form method='post' action='/courses/{r['id']}/toggle'><button class='ghost'>{'Pause' if r['enabled'] else 'Resume'}</button></form>
            <form method='post' action='/courses/{r['id']}/delete' onsubmit="return confirm('Delete this watcher?')"><button class='danger'>Delete</button></form>
          </div>{f"<div class='err'>{esc(r['last_error'])}</div>" if r['last_error'] else ''}</td>
        </tr>"""

    if not table_rows:
        table_rows = "<tr><td colspan='7' class='muted'>No courses yet. Add one below.</td></tr>"

    telegram_state = "configured" if (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) else "not configured"
    body = f"""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>{CSS}<meta http-equiv='refresh' content='20'></head><body><div class='wrap'>
      <div class='top'><div><h1>UW Course Watcher</h1><div class='muted'>Parallel Quest checks every {CHECK_SECONDS}s · concurrency {MAX_CONCURRENCY} · Telegram {telegram_state}</div></div>
      <div class='muted'>Cycle: {esc(runtime['last_cycle_started'] or 'starting…')}</div></div>
      <div class='card'><table><thead><tr><th>Course</th><th>Class / section</th><th>Status</th><th>Enrolled / cap</th><th>Reserve</th><th>Last checked</th><th></th></tr></thead><tbody>{table_rows}</tbody></table></div>
      <div class='card'><h2 style='margin-top:0'>Add course</h2>
      <form method='post' action='/courses'><div class='grid'>
        <div class='field'><label>Term</label><input name='term' value='Fall 2026' required></div>
        <div class='field'><label>Subject</label><input name='subject' placeholder='CS' required></div>
        <div class='field'><label>Course</label><input name='course' placeholder='349' required></div>
        <div class='field'><label>Class # (recommended)</label><input name='class_number' placeholder='12345'></div>
        <div class='field'><label>Section (optional)</label><input name='section' placeholder='LEC 001'></div>
        <div class='field'><label>Career</label><select name='career'><option>Undergraduate</option><option>Graduate</option></select></div>
      </div><div style='margin-top:12px'><button class='primary'>+ Start watching</button></div></form></div>
      <div class='muted'>Tip: use the exact Quest Class Number whenever possible. The watcher reports reserve capacity but cannot determine your personal eligibility for a reserved seat.</div>
    </div></body></html>"""
    return HTMLResponse(body)


@app.post("/courses")
async def add_course(
    term: str = Form(...), subject: str = Form(...), course: str = Form(...),
    class_number: str = Form(""), section: str = Form(""), career: str = Form("Undergraduate")
):
    term = term.strip()
    subject = subject.strip().upper()
    course = course.strip().upper()
    class_number = class_number.strip()
    section = section.strip().upper()
    if not (term and subject and course):
        return JSONResponse({"error": "term, subject and course are required"}, status_code=400)
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO courses(term,subject,course,class_number,section,career) VALUES(?,?,?,?,?,?)",
            (term, subject, course, class_number, section, career.strip() or "Undergraduate"),
        )
        conn.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/courses/{course_id}/toggle")
async def toggle_course(course_id: int):
    with db_conn() as conn:
        conn.execute("UPDATE courses SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (course_id,))
        conn.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/courses/{course_id}/delete")
async def delete_course(course_id: int):
    with db_conn() as conn:
        conn.execute("DELETE FROM courses WHERE id=?", (course_id,))
        conn.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/health")
async def health():
    return {"ok": True, "last_cycle_started": runtime["last_cycle_started"], "last_cycle_finished": runtime["last_cycle_finished"]}
