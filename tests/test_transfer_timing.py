"""The two gate round trips of a transfer, timed and recorded (crowsnest-dev#319). COMMAND-ARCHITECTURE §7
sets a target under 100 ms for the two-phase transfer, so it is not felt at the lever. Measurement only:
no decision reads these values. The clock is injected, and the fake gate's hooks advance it mid-call."""

import pytest

from control_authority import model
from control_authority.service import GateUnreachable
from fakegate import FakeGate
from test_manager import make, take_conn


@pytest.fixture
def clock():
    class C:
        t = 1000

        def __call__(self):
            return C.t

    return C()


class Elapsed:
    """What the service reads around each gate call. Moved only by the fake gate's hooks."""

    t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def elapsed():
    return Elapsed()


@pytest.fixture
def gate(clock):
    return FakeGate(clock)


@pytest.fixture
def m(tmp_path, clock, gate, elapsed):
    mgr = make(tmp_path, clock, gate)
    mgr.elapsed_ms = elapsed
    return mgr


def events(m, name):
    return [a for a in m.store.audit() if a.get("event") == name]


def delay(gate, elapsed, prepare=0.0, commit=0.0):
    def on_prepare(_t):
        elapsed.t += prepare

    def on_commit(_t):
        elapsed.t += commit

    gate.on_prepare, gate.on_commit = on_prepare, on_commit


def the_transfer(m):
    return next(iter(m.state.transfers.values()))


def test_a_transfer_records_both_round_trips_on_itself_and_in_its_audit(m, gate, elapsed):
    delay(gate, elapsed, prepare=12.5, commit=7.5)
    assert take_conn(m).outcome == model.ACCEPTED
    t = the_transfer(m)
    assert (t.prepare_ms, t.commit_ms) == (12.5, 7.5)
    [committed] = events(m, "committed")
    assert committed["prepare_ms"] == 12.5 and "commit_ms" not in committed  # the commit has not happened yet
    [active] = events(m, "active")
    assert active["prepare_ms"] == 12.5 and active["commit_ms"] == t.commit_ms


def test_each_duration_is_its_own_call_and_not_the_time_between_them(m, gate, elapsed):
    delay(gate, elapsed, prepare=40.0, commit=3.0)
    elapsed.t = 500.0  # a clock that does not start at zero must not leak in
    take_conn(m)
    t = the_transfer(m)
    assert (t.prepare_ms, t.commit_ms) == (40.0, 3.0)


def test_the_durations_survive_a_restart(tmp_path, clock, gate, elapsed):
    m = make(tmp_path, clock, gate)
    m.elapsed_ms = elapsed
    delay(gate, elapsed, prepare=9.0, commit=4.0)
    take_conn(m)
    tid = the_transfer(m).transfer_id
    m.store.close()
    again = make(tmp_path, clock, gate)
    t = again.state.transfers[tid]
    assert (t.prepare_ms, t.commit_ms) == (9.0, 4.0)


def test_a_refused_prepare_records_how_long_the_refusal_took(m, gate, elapsed):
    delay(gate, elapsed, prepare=5.0)
    gate.unready = {"rudder", "throttle"}
    assert take_conn(m).outcome == model.REFUSED_BY_GATE
    [rejected] = events(m, "rejected")
    assert rejected["prepare_ms"] == 5.0
    assert the_transfer(m).prepare_ms == 5.0


def test_a_refused_commit_records_both(m, gate, elapsed):
    delay(gate, elapsed, prepare=6.0, commit=2.0)
    real = gate.commit
    gate.commit = lambda tid, fence: (real(tid, fence), (False, "forgotten"))[1]
    assert take_conn(m).outcome == model.REFUSED_BY_GATE
    [abandoned] = events(m, "abandoned")
    assert (abandoned["prepare_ms"], abandoned["commit_ms"]) == (6.0, 2.0)


def test_a_prepare_nobody_answered_is_labelled_unanswered_not_a_round_trip(m, gate, elapsed):
    def silent(_t, _ttl):
        elapsed.t += 2000.0
        raise GateUnreachable("no answer")

    gate.prepare = silent
    assert take_conn(m).outcome == model.REFUSED_BY_GATE
    [abandoned] = events(m, "abandoned")
    assert abandoned["unanswered_prepare_ms"] == 2000.0 and "prepare_ms" not in abandoned


def test_a_commit_nobody_answered_is_audited_and_left_for_reconcile(m, gate, elapsed):
    delay(gate, elapsed, prepare=1.0)

    def silent(_tid, _fence):
        elapsed.t += 1500.0
        raise GateUnreachable("no answer")

    gate.commit = silent
    assert take_conn(m).outcome == model.REFUSED_BY_GATE
    assert m.state.reconciled is False
    [row] = events(m, "commit_unanswered")
    assert row["unanswered_commit_ms"] == 1500.0 and "commit_ms" not in row


def test_no_decision_reads_the_measurement(tmp_path, clock, elapsed):
    """The same transfer, fast and very slow, ends in the same state apart from the numbers."""

    def run(sub, prepare, commit):
        gate = FakeGate(clock)
        m = make(tmp_path / sub, clock, gate)
        m.elapsed_ms = elapsed
        delay(gate, elapsed, prepare=prepare, commit=commit)
        d = take_conn(m)
        t = the_transfer(m)
        return d.outcome, t.phase, t.station, sorted(x.dof for x in t.mappings)

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert run("a", 0.1, 0.1) == run("b", 60_000.0, 60_000.0)
