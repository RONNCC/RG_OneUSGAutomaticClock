"""
gatech-worker: saturate weekly OneUSG hours within a daily window.

Keeps calling clock_manager.py (aka gatech-clock) in chunks with
30-40min breaks in between, staying inside --start/--end each day
until week-to-date hours reach --target.

The window may cross midnight (e.g. --start 09:00 --end 07:00), so
long runs like 22h work without extra scripts.

Durability:
  - Every child run has a hard timeout (shift + 45m grace) and is
    killed on hangs so the worker never wedges silently.
  - If a child exits abnormally (nonzero rc, timeout, kill), the worker
    runs recovery mode (`gatech-clock --clock-out`, idempotent) before
    planning anything else, so an unclocked-out punch is never followed
    straight into a new clock-in.
  - Consecutive failures back off exponentially (2m -> 20m); successes
    reset the backoff.
  - Ctrl+C is forwarded to the child (which clocks out cleanly) and a
    final recovery clock-out guards the exit.

Defaults: 09:00-18:00, target 19.0h (just under the 20h OneUSG cap),
breaks 30-40min, work chunks 60-150min.

Examples:
  gatech-worker                                  # 9-6, target 19h, 30-40m breaks
  gatech-worker --start 10:00 --end 17:00        # custom window
  gatech-worker --start 09:00 --end 07:00        # overnight window (22h)
  gatech-worker --target 15 --break-min 20 --break-max 30
  gatech-worker --shift-min 90 --shift-max 120   # longer chunks
  gatech-worker --once                           # single shift then exit
  gatech-worker --dry-run                        # print plan, don't clock
  gatech-worker --hours                          # just show week total (passthrough)
"""

import argparse
import datetime as dt
import os
import random
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CLOCK_MANAGER = PROJECT_DIR / "clock_manager.py"

TOTAL_PAT = re.compile(r"Week total so far:\s*(\d+(?:\.\d+)?)\s*h", re.IGNORECASE)

HOURS_CHECK_TIMEOUT = 900      # seconds for --hours logins
RECOVERY_TIMEOUT = 900         # seconds for --clock-out logins
RECOVERY_ATTEMPTS = 3          # --clock-out attempts per ensure_clocked_out call
RECOVERY_BACKOFF_S = 180       # spacing between failed recovery attempts
SHIFT_TIMEOUT_GRACE = 45 * 60  # extra seconds beyond shift length before kill
KILL_GRACE_S = 15              # seconds between SIGTERM and SIGKILL
RECOVERY_RETRY_PERIOD = 15 * 60  # pacing when recovery keeps failing
BACKOFF_INITIAL = 120
BACKOFF_CAP = 20 * 60


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


def in_window(now_min, start_min, end_min):
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min  # window crosses midnight


def minutes_until(minute_target, now_min):
    return (minute_target - now_min) % 1440


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


def passthrough_flags(ui=False, debug=False, duo_timeout=None):
    flags = []
    if ui:
        flags.append("--ui")
    if debug:
        flags.append("--debug")
    if duo_timeout is not None:
        flags += ["--duo-timeout", str(duo_timeout)]
    return flags


def kill_tree(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=KILL_GRACE_S)
        except Exception:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
    try:
        proc.wait(timeout=30)
    except Exception:
        pass


def run_proc(cmd, timeout_s=None, label="child"):
    """Run a subprocess in its own session. Returns rc (int) or None on hang/timeout.

    The child gets its own process group so a timeout kill also reaps
    caffeinate/chromedriver. Ctrl+C is NOT auto-forwarded: run_clock catches
    KeyboardInterrupt inside the worker and forwards SIGINT so the child's
    own clock-out flow can finish.
    """
    proc = subprocess.Popen(cmd, cwd=str(PROJECT_DIR), start_new_session=True)
    log(f"{label} started (pid {proc.pid})" + (f", hard timeout {timeout_s // 60:.0f}m" if timeout_s else ""))
    try:
        rc = proc.wait(timeout=timeout_s)
        log(f"{label} finished (rc={rc})")
        return rc
    except subprocess.TimeoutExpired:
        log(f"{label} exceeded {timeout_s // 60:.0f}m; killing process tree!")
        kill_tree(proc)
        log(f"{label} killed.")
        return None
    except KeyboardInterrupt:
        log(f"Ctrl+C: forwarding SIGINT to {label} so it can clock out cleanly...")
        try:
            os.killpg(proc.pid, signal.SIGINT)
        except Exception:
            pass
        try:
            rc = proc.wait(timeout=SHIFT_TIMEOUT_GRACE)
            log(f"{label} exited rc={rc} after interrupt.")
        except Exception:
            log(f"{label} did not exit gracefully after Ctrl+C; killing.")
            kill_tree(proc)
        raise


def get_weekly_total(ui=False, debug=False, duo_timeout=None):
    cmd = [sys.executable, str(CLOCK_MANAGER), "--hours"] + passthrough_flags(ui, debug, duo_timeout)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_DIR), timeout=HOURS_CHECK_TIMEOUT)
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        m = TOTAL_PAT.search(out)
        if m:
            return float(m.group(1)), out.strip().splitlines()[-3:]
        log("Could not parse week total (page may be unreadable). Continuing anyway.")
        if debug:
            print(out[-2000:])
        return None, []
    except subprocess.TimeoutExpired:
        log(f"Hours check exceeded {HOURS_CHECK_TIMEOUT // 60}m; continuing anyway.")
        return None, []
    except Exception as e:
        log(f"Hours check failed ({e}); continuing anyway.")
        return None, []


def run_clock(minutes, target, ui=False, debug=False, duo_timeout=None, dry_run=False):
    """Run one gatech-clock shift. Returns child rc (int; None = timed out and was killed)."""
    cmd = [
        sys.executable, str(CLOCK_MANAGER),
        "-m", f"{minutes:.0f}",
        "--max-hours", f"{target:.2f}",
    ] + passthrough_flags(ui, debug, duo_timeout)
    log(f"{'[dry-run] would run:' if dry_run else 'Running:'} {' '.join(cmd[2:])} (from {PROJECT_DIR})")
    if dry_run:
        return 0
    try:
        return run_proc(cmd, timeout_s=minutes * 60 + SHIFT_TIMEOUT_GRACE,
                        label=f"{minutes:.0f}m shift")
    except KeyboardInterrupt:
        run_interrupted_cleanup("The running shift was interrupted; a final clock-out check follows.")
        return 130


def ensure_clocked_out(ui=False, debug=False, duo_timeout=None):
    """Force the timecard back to 'out' via the idempotent --clock-out mode.

    Returns True when a clean rc==0 confirms we are not clocked in.
    On persistent failure notifies (silent notification + loud log) and
    returns False; callers must not start new shifts until True.
    """
    import notifications

    cmd = [sys.executable, str(CLOCK_MANAGER), "--clock-out"] + passthrough_flags(ui, debug, duo_timeout)
    for attempt in range(1, RECOVERY_ATTEMPTS + 1):
        log(f"Recovery clock-out attempt {attempt}/{RECOVERY_ATTEMPTS}...")
        rc = run_proc(cmd, timeout_s=RECOVERY_TIMEOUT, label="recovery clock-out")
        if rc == 0:
            log("Recovery clock-out succeeded (not clocked in).")
            return True
        log(f"Recovery attempt {attempt} failed (rc={rc}).")
        if attempt < RECOVERY_ATTEMPTS:
            log(f"Waiting {RECOVERY_BACKOFF_S // 60}m before next attempt...")
            sleep_interruptible(RECOVERY_BACKOFF_S)
    notifications.notify_user_with_ack(
        "gatech-worker",
        "Could NOT confirm clocked-out state after multiple recovery attempts. Verify your OneUSG timecard!",
    )
    return False


def run_interrupted_cleanup(message):
    import notifications

    notifications.notify_user_with_ack("gatech-worker", message)
    log(message)


def main():
    ap = argparse.ArgumentParser(description="gatech-worker: auto-saturate OneUSG weekly hours with breaks")
    ap.add_argument("--start", type=parse_hhmm, default="09:00", help="Daily window start HH:MM (default 09:00)")
    ap.add_argument("--end", type=parse_hhmm, default="18:00", help="Daily window end HH:MM (default 18:00; may be before --start to cross midnight)")
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

    if args.start == args.end:
        ap.error("--start and --end must differ")
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
    overnight = args.end <= args.start
    log(f"Window {start_h}-{end_h}" + (" (crosses midnight)" if overnight else "")
        + f", target {args.target:.2f}h, shifts {args.shift_min:.0f}-{args.shift_max:.0f}m, "
        f"breaks {args.break_min:.0f}-{args.break_max:.0f}m"
        + (" [dry-run]" if args.dry_run else ""))

    try:
        worker_loop(args)
        return 0
    except KeyboardInterrupt:
        log("Worker interrupted; running final recovery clock-out check before exit...")
        try:
            if not ensure_clocked_out(ui=args.ui, debug=args.debug, duo_timeout=args.duo_timeout):
                return 1
        except Exception as e:
            log(f"Final recovery clock-out failed: {e}")
            return 1
        return 130


def worker_loop(args):
    consecutive_failures = 0
    pending_recovery = False
    recovery_alerted = False

    while True:
        now_min = now_minutes()

        # Never plan or start shifts while the timecard state is unknown.
        if pending_recovery:
            if ensure_clocked_out(ui=args.ui, debug=args.debug, duo_timeout=args.duo_timeout):
                pending_recovery = False
                recovery_alerted = False
            else:
                log(f"Recovery still failing; retrying in {RECOVERY_RETRY_PERIOD // 60}m (no shifts until confirmed clocked out).")
                sleep_interruptible(RECOVERY_RETRY_PERIOD)
                new_min = now_minutes()
                if not in_window(new_min, args.start, args.end):
                    log("Out of window; sleeping until window start (recovery still unconfirmed).")
                    sleep_interruptible(minutes_until(args.start, new_min) * 60)
                continue

        if not in_window(now_min, args.start, args.end):
            wait_s = minutes_until(args.start, now_min) * 60
            next_start = next_start_datetime(args.start)
            log(f"Out of window {start_h_str(args)}. Sleeping {wait_s / 3600:.1f}h until {next_start.strftime('%a %H:%M')}.")
            if args.dry_run:
                return
            sleep_interruptible(wait_s)
            continue

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
                return
            remaining_week_min = (args.target - total) * 60
            if remaining_week_min < 5:
                log(f"Only {remaining_week_min:.0f}m left to target; stopping (window keeps within target rules).")
                return
        else:
            log("Week total unknown; proceeding (clock_manager will enforce 20h cap).")
            remaining_week_min = args.shift_max

        day_left = minutes_until(args.end, now_min)
        shift = random.uniform(args.shift_min, args.shift_max)
        shift = min(shift, day_left, remaining_week_min)
        if shift < 10:
            log(f"Only {shift:.0f}m left in window/target; taking a break instead.")
            if args.once:
                return
            b = random.uniform(args.break_min, args.break_max)
            b = min(b, day_left)
            log(f"Break {b:.0f}m.")
            if not args.dry_run:
                sleep_interruptible(b * 60)
            else:
                return
            continue

        shift = round(shift)
        log(f"Next shift: {shift}m (day has {day_left:.0f}m left).")
        rc = run_clock(shift, args.target, ui=args.ui, debug=args.debug,
                       duo_timeout=args.duo_timeout, dry_run=args.dry_run)

        if args.dry_run or args.once:
            log("Dry-run / --once finished.")
            if not args.dry_run and rc not in (0, 130):
                pending_recovery = True
                ensure_clocked_out(ui=args.ui, debug=args.debug, duo_timeout=args.duo_timeout)
            return
        if rc is None or rc not in (0, 130):
            consecutive_failures += 1
            wait = min(BACKOFF_INITIAL * (2 ** (consecutive_failures - 1)), BACKOFF_CAP)
            log(f"Shift run ended abnormally (rc={rc}); recovery clock-out next, "
                f"then {wait // 60}m backoff before any new shift.")
            pending_recovery = True
            sleep_interruptible(wait)
            continue

        consecutive_failures = 0

        total2, _ = get_weekly_total(ui=args.ui, debug=args.debug, duo_timeout=args.duo_timeout)
        if total2 is not None and total2 >= args.target - 1e-9:
            log(f"Target reached after shift ({total2:.2f}h). Done.")
            return

        now_min2 = now_minutes()
        if not in_window(now_min2, args.start, args.end):
            continue
        brk = random.uniform(args.break_min, args.break_max)
        brk = min(brk, minutes_until(args.end, now_min2))
        if brk < 1:
            continue
        log(f"Break {brk:.0f}m before next shift.")
        sleep_interruptible(brk * 60)


def start_h_str(args):
    return f"{args.start // 60:02d}:{args.start % 60:02d}-{args.end // 60:02d}:{args.end % 60:02d}"


if __name__ == "__main__":
    raise SystemExit(main())
