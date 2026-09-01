"""Self-perpetuating hourly trigger.

GitHub's `schedule:` cron is unreliable on low-activity personal repos — it
silently skipped every tick on this repo for hours. So each run re-arms the
next one itself: sleep until the next hour boundary, then fire
workflow_dispatch via the API. A run therefore always exists to launch its
successor, independent of GitHub's scheduler.

Runs as the last step of the workflow, in the background of the job? No —
GitHub kills the job's processes at the end. Instead this step *is* the wait:
it sleeps to the next boundary (capped well under the job timeout) and then
dispatches. Cheap: an idle sleep costs Actions minutes but public repos have
unlimited minutes.

Guard: only re-arms if the chain looks alive (avoids runaway parallel chains).
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GH_PAT", "")
if not REPO or not TOKEN:
    print("GITHUB_REPOSITORY/GH_PAT missing — nothing to re-arm")
    sys.exit(0)
API = f"https://api.github.com/repos/{REPO}"
H = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "rearm",
     "Accept": "application/vnd.github+json"}
TARGET_MINUTE = 7


def dispatch():
    r = requests.post(f"{API}/actions/workflows/scan.yml/dispatches",
                      headers=H, json={"ref": "main"}, timeout=30)
    print(f"dispatch -> {r.status_code} {r.text[:200]}")
    return r.status_code == 204


def chain_is_duplicated():
    """True if another run is already queued/in-progress besides this one."""
    try:
        r = requests.get(f"{API}/actions/runs?status=in_progress&per_page=10",
                         headers=H, timeout=30)
        mine = os.environ.get("GITHUB_RUN_ID")
        others = [x for x in r.json().get("workflow_runs", [])
                  if str(x["id"]) != str(mine)]
        r2 = requests.get(f"{API}/actions/runs?status=queued&per_page=10",
                          headers=H, timeout=30)
        others += [x for x in r2.json().get("workflow_runs", [])
                   if str(x["id"]) != str(mine)]
        if others:
            print(f"another run already active ({[x['id'] for x in others]}) — not re-arming")
            return True
    except Exception as e:
        print(f"chain check failed ({e}) — proceeding")
    return False


def main():
    if chain_is_duplicated():
        return
    now = datetime.now(timezone.utc)
    nxt = (now.replace(minute=TARGET_MINUTE, second=0, microsecond=0)
           + timedelta(hours=1 if now.minute >= TARGET_MINUTE else 0))
    wait = (nxt - now).total_seconds()
    # keep the job comfortably inside its timeout
    wait = max(60.0, min(wait, 62 * 60.0))
    print(f"sleeping {wait/60:.1f} min, then dispatching next run (target {nxt:%H:%M} UTC)")
    time.sleep(wait)
    if not dispatch():
        sys.exit(1)


if __name__ == "__main__":
    main()
