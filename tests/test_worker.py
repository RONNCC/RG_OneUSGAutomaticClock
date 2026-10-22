"""Tests for gatech_worker recovery/clock-out behaviour.

No browser, no network: every child process is faked except the two tests that
deliberately exercise the real subprocess wrappers with `python -c`.
"""

import sys
import types

import pytest

import gatech_worker as gw


@pytest.fixture(autouse=True)
def notifications(monkeypatch):
    """Never fire real macOS notifications from tests."""
    gw.reset_abort()
    calls = []
    mod = types.ModuleType("notifications")
    mod.notify_user_with_ack = lambda title, message, require_ack=False: calls.append((title, message))
    monkeypatch.setitem(sys.modules, "notifications", mod)
    return calls


@pytest.fixture
def sleeps(monkeypatch):
    """Record sleeps instead of waiting."""
    recorded = []
    monkeypatch.setattr(gw, "sleep_interruptible", lambda seconds: recorded.append(seconds))
    return recorded


class FakeRun:
    """Stand-in for gatech_worker.run_proc: replays scripted rc / interrupts."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, cmd, timeout_s=None, label="child"):
        self.calls.append(list(cmd))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, BaseException):
            raise step
        return step

    @property
    def flags(self):
        return [c[1:] for c in self.calls]


def modes(run):
    """Subcommand-ish view of each faked child invocation."""
    return ["--clock-out" if "--clock-out" in c else ("--hours" if "--hours" in c else "shift") for c in run.calls]


def args(**over):
    base = dict(
        start=0, end=23 * 60 + 59, target=19.0,
        break_min=20.0, break_max=30.0, shift_min=60.0, shift_max=150.0,
        once=False, dry_run=False, ui=False, debug=False, duo_timeout=None, seed=None,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


class StopLoop(Exception):
    """Sentinel used to leave worker_loop after the behaviour under test."""


# --- get_weekly_total -------------------------------------------------------

def test_get_weekly_total_parses_week_total(monkeypatch):
    monkeypatch.setattr(gw, "run_captured", lambda cmd, timeout_s=None, label="child": (
        0, "Checking...\nWeek total so far: 12.34 h\nok"))
    total, lines = gw.get_weekly_total()
    assert total == 12.34
    assert lines[-1] == "ok"


def test_get_weekly_total_unparsable_output_returns_none(monkeypatch):
    monkeypatch.setattr(gw, "run_captured", lambda cmd, timeout_s=None, label="child": (0, "login failed"))
    assert gw.get_weekly_total() == (None, [])


def test_get_weekly_total_hung_child_returns_none(monkeypatch):
    monkeypatch.setattr(gw, "run_captured", lambda cmd, timeout_s=None, label="child": (None, ""))
    assert gw.get_weekly_total() == (None, [])


def test_get_weekly_total_interrupt_propagates(monkeypatch):
    def boom(cmd, timeout_s=None, label="child"):
        raise KeyboardInterrupt

    monkeypatch.setattr(gw, "run_captured", boom)
    with pytest.raises(KeyboardInterrupt):
        gw.get_weekly_total()


# --- ensure_clocked_out -----------------------------------------------------

def test_ensure_clocked_out_retries_then_succeeds(monkeypatch, sleeps):
    run = FakeRun([1, 5, 0])
    monkeypatch.setattr(gw, "run_proc", run)
    assert gw.ensure_clocked_out() is True
    assert modes(run) == ["--clock-out"] * 3
    assert sleeps == [gw.RECOVERY_BACKOFF_S, gw.RECOVERY_BACKOFF_S]


def test_ensure_clocked_out_gives_up_and_notifies(monkeypatch, sleeps, notifications):
    run = FakeRun([1])
    monkeypatch.setattr(gw, "run_proc", run)
    assert gw.ensure_clocked_out() is False
    assert len(run.calls) == gw.RECOVERY_ATTEMPTS
    assert "Could NOT confirm clocked-out" in notifications[-1][1]


def test_ensure_clocked_out_retries_ctrl_c_without_consuming_attempts(monkeypatch, sleeps, notifications):
    run = FakeRun([KeyboardInterrupt(), 0])
    monkeypatch.setattr(gw, "run_proc", run)
    assert gw.ensure_clocked_out() is True
    assert len(run.calls) == 2
    assert sleeps == []  # no backoff after an interrupt: clock out immediately
    assert gw.abort_requested() is True


def test_ensure_clocked_out_aborts_after_interrupt_limit(monkeypatch, sleeps, notifications):
    run = FakeRun([KeyboardInterrupt()])
    monkeypatch.setattr(gw, "run_proc", run)
    assert gw.ensure_clocked_out() is False
    assert len(run.calls) == gw.RECOVERY_INTERRUPT_LIMIT
    assert "Verify your OneUSG timecard" in notifications[-1][1]


# --- run_clock --------------------------------------------------------------

def test_run_clock_propagates_ctrl_c_after_notifying(monkeypatch, notifications):
    def boom(cmd, timeout_s=None, label="child"):
        raise KeyboardInterrupt

    monkeypatch.setattr(gw, "run_proc", boom)
    with pytest.raises(KeyboardInterrupt):
        gw.run_clock(60, 19.0)
    assert notifications and "interrupted" in notifications[-1][1].lower()


# --- run_cli startup gate ---------------------------------------------------

def test_startup_clock_out_runs_before_any_shift(monkeypatch, sleeps):
    run = FakeRun([0])
    monkeypatch.setattr(gw, "run_proc", run)
    seen = []
    monkeypatch.setattr(gw, "worker_loop", lambda a, pending_recovery=False: seen.append(pending_recovery))

    assert gw.run_cli(["--start", "09:00", "--end", "18:00"]) == 0
    assert modes(run) == ["--clock-out"]
    assert seen == [False]


def test_startup_clock_out_failure_blocks_shifts(monkeypatch, sleeps):
    run = FakeRun([1])
    monkeypatch.setattr(gw, "run_proc", run)
    seen = []
    monkeypatch.setattr(gw, "worker_loop", lambda a, pending_recovery=False: seen.append(pending_recovery))

    assert gw.run_cli([]) == 0
    assert modes(run) == ["--clock-out"] * gw.RECOVERY_ATTEMPTS
    assert seen == [True]


def test_dry_run_skips_startup_clock_out(monkeypatch, sleeps):
    run = FakeRun([0])
    monkeypatch.setattr(gw, "run_proc", run)
    seen = []
    monkeypatch.setattr(gw, "worker_loop", lambda a, pending_recovery=False: seen.append(pending_recovery))

    gw.run_cli(["--dry-run"])
    assert run.calls == []
    assert seen == [False]


# --- run_cli / main interrupt handling --------------------------------------

def test_ctrl_c_during_shift_clocks_out_and_exits_130(monkeypatch, sleeps):
    run = FakeRun([0])
    monkeypatch.setattr(gw, "run_proc", run)

    def interrupted(a, pending_recovery=False):
        raise KeyboardInterrupt

    monkeypatch.setattr(gw, "worker_loop", interrupted)
    assert gw.main([]) == 130
    # startup clock-out, then the final clock-out after Ctrl+C
    assert modes(run) == ["--clock-out", "--clock-out"]


def test_ctrl_c_exits_nonzero_when_clock_out_unconfirmed(monkeypatch, sleeps, notifications):
    run = FakeRun([1])
    monkeypatch.setattr(gw, "run_proc", run)

    def interrupted(a, pending_recovery=False):
        raise KeyboardInterrupt

    monkeypatch.setattr(gw, "worker_loop", interrupted)
    assert gw.main([]) == 1
    assert notifications


def test_ctrl_c_during_startup_clock_out_confirms_then_exits(monkeypatch, sleeps, notifications):
    run = FakeRun([KeyboardInterrupt(), 0])
    monkeypatch.setattr(gw, "run_proc", run)
    monkeypatch.setattr(gw, "worker_loop", lambda a, pending_recovery=False: pytest.fail("no shifts after Ctrl+C"))

    assert gw.main([]) == 130
    assert len(run.calls) == 2  # interrupted attempt retried, clock-out confirmed


def test_ctrl_c_during_final_clock_out_confirms_then_exits(monkeypatch, sleeps, notifications):
    run = FakeRun([0, KeyboardInterrupt(), 0])  # startup, interrupted final, retried final
    monkeypatch.setattr(gw, "run_proc", run)
    monkeypatch.setattr(gw, "worker_loop", lambda a, pending_recovery=False: (_ for _ in ()).throw(KeyboardInterrupt))

    assert gw.main([]) == 130
    assert modes(run) == ["--clock-out"] * 3


def test_main_never_raises_keyboard_interrupt(monkeypatch, sleeps):
    """Escape hatch: an interrupt outside the guarded paths becomes a clean rc."""
    monkeypatch.setattr(gw, "run_cli", lambda argv=None: (_ for _ in ()).throw(KeyboardInterrupt))
    assert gw.main([]) == 130


# --- worker_loop ------------------------------------------------------------

def test_abnormal_shift_recovers_before_next_shift(monkeypatch, sleeps):
    events = []
    monkeypatch.setattr(gw, "run_clock", lambda *a, **k: events.append("shift") or 130)
    monkeypatch.setattr(gw, "ensure_clocked_out", lambda **k: events.append("clock-out") or True)

    hours = iter([(0.0, []), StopLoop()])

    def fake_total(**k):
        nxt = next(hours)
        if isinstance(nxt, StopLoop):
            raise nxt
        return nxt

    monkeypatch.setattr(gw, "get_weekly_total", fake_total)

    with pytest.raises(StopLoop):
        gw.worker_loop(args())
    assert events == ["shift", "clock-out"]


def test_pending_recovery_blocks_shifts_until_confirmed(monkeypatch, sleeps):
    shifts = []
    monkeypatch.setattr(gw, "run_clock", lambda *a, **k: shifts.append(1) or 0)

    outcomes = iter([False, StopLoop()])

    def fake_clock_out(**k):
        nxt = next(outcomes)
        if isinstance(nxt, StopLoop):
            raise nxt
        return nxt

    monkeypatch.setattr(gw, "ensure_clocked_out", fake_clock_out)
    monkeypatch.setattr(gw, "get_weekly_total", lambda **k: pytest.fail("no hours check while clocked in"))

    with pytest.raises(StopLoop):
        gw.worker_loop(args(), pending_recovery=True)
    assert shifts == []


def test_once_with_abnormal_shift_returns_failure_when_recovery_fails(monkeypatch, sleeps):
    monkeypatch.setattr(gw, "run_clock", lambda *a, **k: 1)
    monkeypatch.setattr(gw, "ensure_clocked_out", lambda **k: False)
    monkeypatch.setattr(gw, "get_weekly_total", lambda **k: (0.0, []))
    assert gw.worker_loop(args(once=True)) == 1


def test_worker_loop_exits_on_abort_instead_of_starting_a_shift(monkeypatch, sleeps):
    monkeypatch.setattr(gw, "run_clock", lambda *a, **k: pytest.fail("no shift after Ctrl+C"))
    monkeypatch.setattr(gw, "get_weekly_total", lambda **k: pytest.fail("no hours check after Ctrl+C"))
    gw.request_abort()
    assert gw.worker_loop(args()) == 130


# --- real subprocess wrappers ----------------------------------------------

def test_run_proc_returns_child_rc():
    assert gw.run_proc([sys.executable, "-c", "raise SystemExit(3)"], label="probe") == 3


def test_run_captured_kills_hung_child(monkeypatch):
    timeout, out = gw.run_captured(
        [sys.executable, "-c", "import time; time.sleep(60)"], timeout_s=1, label="probe")
    assert timeout is None
    assert out == ""


def test_run_captured_returns_output():
    rc, out = gw.run_captured([sys.executable, "-c", "print('Week total so far: 1.00 h')"], label="probe")
    assert rc == 0
    assert "Week total so far: 1.00 h" in out
