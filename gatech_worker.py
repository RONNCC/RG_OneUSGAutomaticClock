"""
gatech-worker: saturate weekly OneUSG hours within a daily window.

Keeps calling clock_manager.py (aka gatech-clock) in chunks with
30-40min breaks in between, staying inside --start/--end each day
until week-to-date hours reach --target.

Defaults: 09:00-18:00, target 19.0h (just under the 20h OneUSG cap),
breaks 30-40min, work chunks 60-150min.

Examples:
  gatech-worker                                  # 9-6, target 19h, 30-40m breaks
  gatech-worker --start 10:00 --end 17:00        # custom window
  gatech-worker --target 15 --break-min 20 --break-max 30
  gatech-worker --shift-min 90 --shift-max 120   # longer chunks
  gatech-worker --once                           # single shift then exit
  gatech-worker --dry-run                        # print plan, don't clock
  gatech-worker --hours                          # just show week total (passthrough)
"""

import argparse
import datetime as dt
import random
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CLOCK_MANAGER = PROJECT_DIR / "clock_manager.py"

TOTAL_PAT = re.compile(r"Week total so far:\s*(\d+(?:\.\d+)?)\s*h", re.IGNORECASE)


def log(msg):
    now = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[worker {now}] {msg}", flush=True)


def parse_hhmm(s):
    try:
        h, m = s.split(":")
        h, m = int(h), int(m)
        assert 0 <= h < 24 and 0 <= m < 60
        return h * 60 + m
    except Exception:
        raise argparse.ArgumentTypeError(f"expected HH:MM (24h), got {s!r}")


def now_minutes():
    n = dt.datetime.now()
    return n.hour * 60 + n.minute + n.second / 60.0


def next_start_datetime(start_min):
    now = dt.datetime.now()
    target = now.replace(hour=start_min // 60, minute=start_min % 60, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return target


def sleep_interruptible(seconds):
    end = time.time() + seconds
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(5, remaining))


def get_weekly_total(ui=False, debug=False, duo_timeout=None):
    cmd = [sys.executable, str(CLOCK_MANAGER), "--hours"]
    if ui:
        cmd.append("--ui")
    if debug:
        cmd.append("--debug")
    if duo_timeout is not None:
        cmd += ["--duo-timeout", str(duo_timeout)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_DIR), timeout=600)
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        m = TOTAL_PAT.search(out)
        if m:
            return float(m.group(1)), out.strip().splitlines()[-3:]
        log("Could not parse week total (page may be unreadable). Continuing anyway.")
        if debug:
            print(out[-2000:])
        return None, []
    except subprocess.TimeoutExpired:
        log("Timed out checking hours; continuing anyway.")
        return None, []
    except Exception as e:
        log(f"Hours check failed ({e}); continuing anyway.")
        return None, []


def run_clock(minutes, target, ui=False, debug=False, duo_timeout=None, dry_run=False):
    cmd = [sys.executable, str(CLOCK_MANAGER), "-m", f"{minutes:.0f}",
           "--max-hours", f"{target:.2f}"]
    if ui:
        cmd.append("--ui")
    if debug:
        cmd.append("--debug")
    if duo_timeout is not None:
        cmd += ["--duo-timeout", str(duo_timeout)]
    log(f"{'[dry-run] would run:' if dry_run else 'Running:'} {' '.join(cmd[2:])} (from {PROJECT_DIR})")
    if dry_run:
        return 0
    r = subprocess.run(cmd, cwd=str(PROJECT_DIR))
    return r.returncode


def main():
    ap = argparse.ArgumentParser(description="gatech-worker: auto-saturate OneUSG weekly hours 9-6 with breaks")
    ap.add_argument("--start", type=parse_hhmm, default="09:00", help="Daily window start HH:MM (default 09:00)")
    ap.add_argument("--end", type=parse_hhmm, default="18:00", help="Daily window end HH:MM (default 18:00)")
    ap.add_argument("--target", type=float, default=19.0, help="Weekly target hours to saturate (default 19.0, cap is 20)")
    ap.add_argument("--break-min", type=float, default=30, help="Min break minutes between shifts (default 30)")
    ap.add_argument("--break-max", type=float, default=40, help="Max break minutes between shifts (default 40)")
    ap.add_argument("--shift-min", type=float, default=60, help="Min work chunk minutes (default 60)")
    ap.add_argument("--shift-max", type=float, default=150, help="Max work chunk minutes (default 150)")
    ap.add_argument("--once", action="store_true", help="Do a single shift then exit (still respects window/target)")
    ap.add_argument("--dry-run", action="store_true", help="Print plan without clocking in/out")
    ap.add_argument("--ui", action="store_true", help="Show browser window (passthrough to gatech-clock)")
    ap.add_argument("--debug", action="store_true", help="Verbose (passthrough to gatech-clock)")
    ap.add_argument("--duo-timeout", type=int, default=None, help="Seconds to wait for Duo (passthrough)")
    ap.add_argument("--hours", action="store_true", help="Just show week-to-date hours and exit")
    ap.add_argument("--detail", action="store_true", help="With --hours, list per-day punches")
    ap.add_argument("--seed", type=int, default=None, help="Random seed for reproducible breaks/shifts")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    if args.start >= args.end:
        ap.error("--start must be before --end")
    if not (0 < args.target <= 20):
        ap.error("--target must be within (0, 20] (OneUSG hard cap is 20h)")
    if args.break_min > args.break_max or args.shift_min > args.shift_max:
        ap.error("min must be <= max for breaks/shifts")

    if args.hours:
        cmd = [sys.executable, str(CLOCK_MANAGER), "--hours"]
        if args.detail:
            cmd.append("--detail")
        if args.ui:
            cmd.append("--ui")
        if args.debug:
            cmd.append("--debug")
        sys.exit(subprocess.run(cmd, cwd=str(PROJECT_DIR)).returncode)

    start_h = f"{args.start // 60:02d}:{args.start % 60:02d}"
    end_h = f"{args.end // 60:02d}:{args.end % 60:02d}"
    log(f"Window {start_h}-{end_h}, target {args.target:.2f}h, "
        f"shifts {args.shift_min:.0f}-{args.shift_max:.0f}m, "
        f"breaks {args.break_min:.0f}-{args.break_max:.0f}m"
        + (" [dry-run]" if args.dry_run else ""))

    while True:
        if args.dry_run:
            total = None
            remaining_week_min = args.shift_max
            log("[dry-run] skipping live hours check (would run: gatech-clock --hours)")
        else:
            total, _ = get_weekly_total(ui=args.ui, debug=args.debug, duo_timeout=args.duo_timeout)
        if total is not None:
            log(f"Week total so far: {total:.2f}h ({max(0.0, args.target - total):.2f}h to target {args.target:.2f}h)")
            if total >= args.target - 1e-9:
                log(f"Target reached ({total:.2f}h >= {args.target:.2f}h). Done.")
                return 0
            remaining_week_min = (args.target - total) * 60
            if remaining_week_min < 5:
                log(f"Only {remaining_week_min:.0f}m left to target; stopping.")
                return 0
        else:
            log("Week total unknown; proceeding (clock_manager will enforce 20h cap).")
            remaining_week_min = args.shift_max

        now_min = now_minutes()
        if now_min < args.start:
            wait_min = args.start - now_min
            log(f"Before window (now {now_min/60:.2f}h). Sleeping {wait_min:.0f}m until {start_h}.")
            if args.dry_run:
                return 0
            sleep_interruptible(wait_min * 60)
            continue
        if now_min >= args.end:
            nxt = next_start_datetime(args.start)
            wait_s = (nxt - dt.datetime.now()).total_seconds()
            log(f"Past window end {end_h}. Sleeping {wait_s/3600:.1f}h until {nxt.strftime('%a %H:%M')}.")
            if args.dry_run:
                return 0
            sleep_interruptible(wait_s)
            continue

        day_left = args.end - now_min
        shift = random.uniform(args.shift_min, args.shift_max)
        shift = min(shift, day_left, remaining_week_min)
        if shift < 10:
            log(f"Only {shift:.0f}m left in window/target; taking a break instead.")
            if args.once:
                return 0
            b = random.uniform(args.break_min, args.break_max)
            b = min(b, day_left)
            log(f"Break {b:.0f}m.")
            if not args.dry_run:
                sleep_interruptible(b * 60)
            else:
                return 0
            continue

        shift = round(shift)
        log(f"Next shift: {shift}m (day has {day_left:.0f}m left).")
        rc = run_clock(shift, args.target, ui=args.ui, debug=args.debug,
                       duo_timeout=args.duo_timeout, dry_run=args.dry_run)
        if args.dry_run or args.once:
            log("Dry-run / --once finished.")
            return rc if isinstance(rc, int) else 0
        if rc != 0:
            log(f"gatech-clock exited {rc}; re-checking hours in 2m before retrying.")
            sleep_interruptible(120)
            continue

        total2, _ = get_weekly_total(ui=args.ui, debug=args.debug, duo_timeout=args.duo_timeout)
        if total2 is not None and total2 >= args.target - 1e-9:
            log(f"Target reached after shift ({total2:.2f}h). Done.")
            return 0

        now_min2 = now_minutes()
        if now_min2 >= args.end:
            continue
        brk = random.uniform(args.break_min, args.break_max)
        brk = min(brk, max(0, args.end - now_min2))
        if brk < 1:
            continue
        log(f"Break {brk:.0f}m before next shift (30-40m pattern).")
        sleep_interruptible(brk * 60)


if __name__ == "__main__":
    raise SystemExit(main())
