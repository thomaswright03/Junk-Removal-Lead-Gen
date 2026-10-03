"""Install the daily run as a job on this computer: ``leadgen schedule install``.

macOS: a LaunchAgent (~/Library/LaunchAgents/com.leadgen.daily.plist) that
runs ``leadgen daily --if-due`` every morning. If the Mac is asleep at that
time, it runs when the Mac wakes up.

Linux: prints the crontab line to add. Windows: prints the schtasks command.

The job also starts again 1, 2, 4 and 6 hours later; with ``--if-due`` those
runs do nothing unless the morning's check failed and its same-day retry is
due (see daily.py).
"""

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

LABEL = "com.leadgen.daily"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


# Hours after the first run when the job starts again, for same-day retries.
RETRY_HOURS = (1, 2, 4, 6)


def command(db_path: Any) -> list[str]:
    return [sys.executable, "-m", "leadgen", "--db", str(Path(db_path).resolve()), "daily", "--if-due"]


def run_hours(hour: int) -> list[int]:
    """The hours the job starts: ``hour``, then the retry hours that day."""
    return [hour] + [hour + h for h in RETRY_HOURS if hour + h < 24]


def plist(db_path: Any, hour: int, minute: int, workdir: Any, log_path: Any) -> str:
    args = "\n".join(f"        <string>{escape(a)}</string>" for a in command(db_path))
    workdir, log_path = escape(str(workdir)), escape(str(log_path))
    times = "\n".join(
        "        <dict>\n"
        f"            <key>Hour</key>\n            <integer>{h}</integer>\n"
        f"            <key>Minute</key>\n            <integer>{minute}</integer>\n"
        "        </dict>"
        for h in run_hours(hour)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>WorkingDirectory</key>
    <string>{workdir}</string>
    <key>StartCalendarInterval</key>
    <array>
{times}
    </array>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def install(db_path: Any, hour: int = 6, minute: int = 0) -> str:
    db_path = Path(db_path).resolve()
    workdir = Path.cwd().resolve()
    log_path = db_path.parent / "daily.log"
    system = platform.system()
    if system == "Darwin":
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_text(plist(db_path, hour, minute, workdir, log_path), encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
        subprocess.run(["launchctl", "load", str(PLIST)], check=True)
        return (
            f"Daily run scheduled for {hour}:{minute:02d} every day (and again later that day if a site "
            "couldn't be reached). "
            f"Log: {log_path}. Remove with: leadgen schedule remove"
        )
    cmd = " ".join(f'"{a}"' if " " in a else a for a in command(db_path))
    if system == "Windows":
        return (
            "Run this once in a Command Prompt to schedule it:\n"
            f"schtasks /Create /SC DAILY /ST {hour:02d}:{minute:02d} /RI 60 /DU {RETRY_HOURS[-1]:02d}:30 "
            '/TN "Lead Desk daily" '
            f'/TR "cmd /c cd /d {workdir} && {cmd} >> {log_path} 2>&1"'
        )
    hours = ",".join(str(h) for h in run_hours(hour))
    return f"Add this line with `crontab -e`:\n{minute} {hours} * * * cd {workdir} && {cmd} >> {log_path} 2>&1"


def remove() -> str:
    if platform.system() != "Darwin":
        return "Remove the line you added with `crontab -e` (or the scheduled task on Windows)."
    if PLIST.exists():
        subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
        os.remove(PLIST)
        return "Daily run removed."
    return "No daily run was scheduled."


def status() -> str:
    if platform.system() == "Darwin":
        return f"Scheduled ({PLIST})" if PLIST.exists() else "Not scheduled"
    return "Check `crontab -l` (or Task Scheduler on Windows)."
