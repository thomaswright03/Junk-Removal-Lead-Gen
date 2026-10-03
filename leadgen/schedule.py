"""Install the daily run as a job on this computer: ``leadgen schedule install``.

macOS: a LaunchAgent (~/Library/LaunchAgents/com.leadgen.daily.plist) that
runs ``leadgen daily`` every morning. If the Mac is asleep at that time, it
runs when the Mac wakes up.

Linux: prints the crontab line to add. Windows: prints the schtasks command.
"""

import os
import platform
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

LABEL = "com.leadgen.daily"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def command(db_path):
    return [sys.executable, "-m", "leadgen", "--db", str(Path(db_path).resolve()), "daily"]


def plist(db_path, hour, minute, workdir, log_path):
    args = "\n".join(f"        <string>{escape(a)}</string>" for a in command(db_path))
    workdir, log_path = escape(str(workdir)), escape(str(log_path))
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
    <dict>
        <key>Hour</key>
        <integer>{hour}</integer>
        <key>Minute</key>
        <integer>{minute}</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def install(db_path, hour=6, minute=0):
    db_path = Path(db_path).resolve()
    workdir = Path.cwd().resolve()
    log_path = db_path.parent / "daily.log"
    system = platform.system()
    if system == "Darwin":
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_text(plist(db_path, hour, minute, workdir, log_path), encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
        subprocess.run(["launchctl", "load", str(PLIST)], check=True)
        return (f"Daily run scheduled for {hour}:{minute:02d} every day. "
                f"Log: {log_path}. Remove with: leadgen schedule remove")
    cmd = " ".join(f'"{a}"' if " " in a else a for a in command(db_path))
    if system == "Windows":
        return ("Run this once in a Command Prompt to schedule it:\n"
                f'schtasks /Create /SC DAILY /ST {hour:02d}:{minute:02d} /TN "Lead Desk daily" '
                f'/TR "cmd /c cd /d {workdir} && {cmd} >> {log_path} 2>&1"')
    return ("Add this line with `crontab -e`:\n"
            f"{minute} {hour} * * * cd {workdir} && {cmd} >> {log_path} 2>&1")


def remove():
    if platform.system() != "Darwin":
        return "Remove the line you added with `crontab -e` (or the scheduled task on Windows)."
    if PLIST.exists():
        subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
        os.remove(PLIST)
        return "Daily run removed."
    return "No daily run was scheduled."


def status():
    if platform.system() == "Darwin":
        return f"Scheduled ({PLIST})" if PLIST.exists() else "Not scheduled"
    return "Check `crontab -l` (or Task Scheduler on Windows)."
