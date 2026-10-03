"""Background jobs ("Update court cases", "Find landlord phones") and the
daily check's schedule, for Lead Desk (web.py)."""

import os
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Callable, Optional

from . import daily, db
from .daily import run_daily
from .lookup import find_contacts, providers_from
from .sources.pima_jp_case import add_cases, update_cases
from .util import PAUSED_MESSAGE, Conn, StopCheck, az_now, is_paused, now_iso

# Online, stop a long job this many seconds into a request (Vercel allows 60).
SERVERLESS_SECONDS = 40


class Job:
    """One background job: progress, result, and a cancel flag."""

    def __init__(self, name: str, label: str) -> None:
        self.name, self.label = name, label
        self.done: int = 0
        self.total: Optional[int] = None
        self.cancel = threading.Event()
        self.result: Any = None
        self.error: Optional[str] = None
        self.started_at: str = now_iso()
        self.finished_at: Optional[str] = None
        self._thread_running = threading.Event()
        self._thread_running.set()  # running from the moment it's created

    def running(self) -> bool:
        return self._thread_running.is_set()

    def progress(self, done: int, total: Optional[int]) -> None:
        self.done, self.total = done, total

    def describe(self) -> str:
        return f"{self.done} of {self.total} done" if self.total else "starting"

    def run(self, work: Callable[["Job", Callable[[], bool]], dict], should_stop: StopCheck = None) -> None:
        self._thread_running.set()
        try:
            self.result = work(self, lambda: self.cancel.is_set() or bool(should_stop and should_stop()))
            if self.cancel.is_set():
                self.result["cancelled"] = True
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            self.error = f"{self.label} stopped with an error ({type(e).__name__}). Try again later."
        finally:
            self.finished_at = now_iso()
            self._thread_running.clear()

    def public(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "running": self.running(),
            "done": self.done,
            "total": self.total,
            "result": self.result,
            "error": self.error,
            "cancelling": self.cancel.is_set(),
            "finished_at": self.finished_at,
        }


def next_daily_run(settings: dict, serverless: bool = False, now: Optional[datetime] = None) -> str:
    """When the next daily check runs, in words: "today 6:00 AM" or
    "tomorrow 6:00 AM" (Tucson time)."""
    if is_paused(settings):
        return "paused"
    local = az_now(now)
    ran_today = settings.get("last_daily_run") == local.date().isoformat()
    if local.hour < 6:
        return "today 6:00 AM"
    if not ran_today and not serverless:
        return "within the next few minutes"
    return "tomorrow 6:00 AM"


def start_github_check(session: Any = None) -> dict:
    """Online, "Check for new evictions" starts the daily GitHub Actions run
    (.github/workflows/daily.yml). Needs LEADDESK_GITHUB_TOKEN: a GitHub token
    allowed to run this repository's workflows."""
    import requests

    token = os.environ.get("LEADDESK_GITHUB_TOKEN")
    owner, repo = os.environ.get("VERCEL_GIT_REPO_OWNER"), os.environ.get("VERCEL_GIT_REPO_SLUG")
    if not (token and owner and repo):
        return {
            "started": False,
            "running": False,
            "message": "The check runs by itself every morning at 6. To start it from here too, "
            "add LEADDESK_GITHUB_TOKEN in Vercel (see docs/VERCEL.md).",
        }
    resp = (session or requests).post(
        f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/daily.yml/dispatches",
        json={"ref": os.environ.get("LEADDESK_GITHUB_REF", "main")},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    if resp.status_code >= 300:
        raise ValueError(f"GitHub didn't start the check ({resp.status_code}): {resp.text[:200]}")
    return {
        "started": True,
        "running": False,
        "message": "Started. The check takes about 15 minutes; reload then to see new leads.",
    }


class JobRunner:
    """The part of Lead Desk's ``App`` that runs the daily check and the long
    jobs, and honours the pause. Locally they run in the background like the
    daily check: the request returns at once, /api/status reports progress,
    and they can be cancelled. Online (Vercel) a request can't outlive its
    answer, so they do one batch inside the request, stopping early before
    Vercel's time limit. Pausing Lead Desk stops a running job before its
    next request to the court or a lookup service."""

    # Set up by App (web.py).
    serverless: bool
    case_client: Any
    calendar: Any
    code_cases: Any
    providers: Optional[list]
    parcel_client: Any
    geocoder: Any
    daily_lock: threading.Lock
    daily_message: Optional[str]
    stale_days: int
    lock: threading.Lock
    jobs: dict[str, Job]
    job_lock: threading.Lock

    def conn(self) -> Conn:
        raise NotImplementedError

    def settings(self, conn: Conn) -> dict:
        raise NotImplementedError

    def _ensure_base(self) -> None:
        raise NotImplementedError

    def paused(self, conn: Conn = None) -> bool:
        if conn is None:
            with self.conn() as c:
                return is_paused(self.settings(c))
        return is_paused(self.settings(conn))

    def refresh(self, body: dict) -> dict:
        """Run the daily check now and wait for it (the CLI and CI use this;
        the page uses ``start_daily``)."""
        with self.daily_lock, self.conn() as conn:
            summary = run_daily(
                conn,
                stale_days=self.stale_days,
                case_client=self.case_client,
                parcel_client=self.parcel_client,
                calendar=self.calendar,
                code_cases=self.code_cases,
                geocoder=self.geocoder,
                providers=self.providers,
                case_limit=body.get("case_limit"),
                contact_limit=int(body.get("contact_limit") or 60),
                log=self._progress,
            )
        self._ensure_base()
        return summary

    def _progress(self, message: str) -> None:
        self.daily_message = message

    def _cap(self, n: int) -> Optional[int]:
        """Online, a request must finish within Vercel's time limit, so the
        buttons do a batch at a time; the daily run does the rest."""
        return n if self.serverless else None

    def _busy(self) -> Optional[str]:
        """What is running now that a new job would collide with, or None."""
        if self.daily_lock.locked():
            return "The daily check is running and does this too. Wait for it to finish."
        for job in self.jobs.values():
            if job.running():
                return f"{job.label} is running ({job.describe()}). Wait for it or cancel it."
        return None

    def start_daily(self, body: Optional[dict] = None) -> dict:
        """Start the daily check in the background; the page polls /api/state."""
        if self.paused():
            return {"started": False, "running": False, "paused": True, "message": PAUSED_MESSAGE}
        if self.serverless:
            return start_github_check()
        if self.daily_lock.locked():
            return {"started": False, "running": True, "message": "The daily check is already running."}
        busy = self._busy()
        if busy:
            return {"started": False, "running": False, "message": busy}

        def work() -> None:
            try:
                self.refresh(body or {})
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                self.daily_message = f"the daily check stopped with an error ({type(e).__name__})"
            else:
                self.daily_message = None

        threading.Thread(target=work, daemon=True, name="daily").start()
        return {"started": True, "running": True}

    def scheduler(self, hour: int = 6, every_seconds: int = 600) -> None:
        """While Lead Desk is open, run the daily check once a day after ``hour``."""

        def loop() -> None:
            while True:
                try:
                    with self.conn() as conn:
                        if daily.due(conn, hour=hour) and not self.paused(conn):
                            self.start_daily()
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                time.sleep(every_seconds)

        threading.Thread(target=loop, daemon=True, name="scheduler").start()

    # ---- long jobs ("Update court cases", "Find landlord phones") -----------
    # Locally they run in the background like the daily check: the request
    # returns at once, /api/state reports progress, and they can be cancelled.
    # Online (Vercel) a request can't outlive its answer, so they do one
    # batch inside the request, stopping early before Vercel's time limit.

    def _run_job(self, name: str, label: str, work: Callable[[Job, Callable[[], bool]], dict]) -> dict:
        if self.paused():
            return {"started": False, "paused": True, "message": PAUSED_MESSAGE}
        with self.job_lock:  # two presses at once start one job
            job = self.jobs.get(name)
            if job and job.running():
                return {"started": False, "running": True, "message": f"{label} is already running ({job.describe()})."}
            busy = self._busy()
            if busy:
                return {"started": False, "running": False, "message": busy}
            job = self.jobs[name] = Job(name, label)
        if self.serverless:
            deadline = time.monotonic() + SERVERLESS_SECONDS
            job.run(work, should_stop=lambda: time.monotonic() > deadline)
            if job.error:
                raise RuntimeError(job.error)
            return {"started": False, "running": False, "result": job.result}
        threading.Thread(target=job.run, args=(work,), daemon=True, name=name).start()
        return {"started": True, "running": True}

    def cancel_job(self, body: dict) -> dict:
        job = self.jobs.get(str(body.get("name")))
        if not job or not job.running():
            return {"ok": True, "message": "Nothing to cancel: it already finished."}
        job.cancel.set()
        return {"ok": True, "message": f"Stopping {job.label.lower()} after the current one."}

    def find_contacts(self, body: dict) -> dict:
        def work(job: Job, should_stop: Callable[[], bool]) -> dict:
            with self.conn() as conn:
                settings = self.settings(conn)
                provs = self.providers if self.providers is not None else providers_from(settings, conn=conn)
                log: list[str] = []
                pause = db.PauseWatch(conn)
                counts = find_contacts(
                    conn,
                    provs,
                    limit=int(body.get("limit") or 0) or self._cap(25),
                    refresh=bool(body.get("refresh")),
                    log=log.append,
                    progress=job.progress,
                    should_stop=lambda: should_stop() or pause(),
                )
                counts["paused"] = pause.hit
                counts["google_used"] = any(p.name == "google" for p in provs)
                counts["messages"] = log[:10]
                return counts

        return self._run_job("contacts", "Finding landlord phones", work)

    def add_cases(self, body: dict) -> dict:
        if self.paused():
            return {"paused": True, "message": PAUSED_MESSAGE}
        log: list[str] = []
        with self.lock, self.conn() as conn:
            pause = db.PauseWatch(conn)
            counts = add_cases(conn, body.get("text") or "", self.case_client, log=log.append, should_stop=pause)
            counts["paused"] = pause.hit
            if pause.hit:
                counts["message"] = "Stopped part way because Lead Desk was paused."
        counts["messages"] = log[:10]
        return counts

    def update_cases(self, body: dict) -> dict:
        def work(job: Job, should_stop: Callable[[], bool]) -> dict:
            log: list[str] = []
            with self.conn() as conn:
                pause = db.PauseWatch(conn)
                counts = update_cases(
                    conn,
                    self.case_client,
                    limit=int(body.get("limit") or 0) or self._cap(60),
                    max_age_hours=0 if body.get("force") else 12,
                    log=log.append,
                    progress=job.progress,
                    should_stop=lambda: should_stop() or pause(),
                )
                counts["paused"] = pause.hit
            counts["messages"] = log[:10]
            return counts

        return self._run_job("cases", "Checking court cases", work)
