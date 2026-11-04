"""The manager against a fake gate: the transfer, the durability of both halves, reconciliation, the
assignments and their revision, succession, the legacy profile, the navigation document. No network."""

import sqlite3

import pytest

from control_authority import model
from control_authority.model import Mapping, NavFunction, TransferRequest
from control_authority.policy import parse
from control_authority.service import Manager
from control_authority.store import Store, UntrustedStore
from fakegate import FakeGate

V = "usv-alpha"
DOFS = ["rudder", "throttle_port", "throttle_stbd"]
POLICY = parse({
    "command_profile": "test",
    "dofs": DOFS,
    "groups": {"throttles": ["throttle_port", "throttle_stbd"]},
    "automation_sources": ["navigation/0"],
    "processes": ["navigation/0@usv-alpha"],
    "succession": {"overall": ["chief@ROC-2"], "navigational": "*"},
})
NAV = "navigation/0@usv-alpha"
MASTER_ID = "ted@ROC-1"


class Clock:
    def __init__(self):
        self.t = 1_000

    def __call__(self):
        return self.t


class Bus:
    def __init__(self):
        self.effects = []

    def __call__(self, effect, state):
        self.effects.append(effect)

    def alarms(self):
        return [e.text for e in self.effects if isinstance(e, model.Alarm)]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gate(clock):
    return FakeGate(clock)


def make(tmp_path, clock, gate, policy=POLICY, bus=None):
    m = Manager(store=Store(tmp_path / "authority.sqlite"), policy=policy, vessel_id=V, gate=gate,
                publish=bus or Bus(), now_ms=clock)
    m.boot()
    return m


def human_map(station):
    return tuple(Mapping(d, "", "", f"{station}/helm-1/{d}") for d in DOFS)


def auto_map():
    return tuple(Mapping(d, "", "", "navigation/0") for d in DOFS)


def take_conn(m, who="ted@ROC-1", site="ROC-1"):
    return m.transfer(TransferRequest(model.GRANT, who, site, mappings=human_map(site)))


def master(m, who="ted@ROC-1", site="ROC-1", role=model.OVERALL, token="sess-1"):
    if role == model.OVERALL:
        m.assign(model.OVERALL, who, "procedure")
    else:
        m.assign(model.NAVIGATIONAL, who, m.state.overall.assignee, m.state.overall.revision, "sess-1")
    a = m.state.assignment(role)
    assert m.session(role, who, site, token, a.assignment_id).outcome == model.ACCEPTED
    return a


# ---------------------------------------------------------------------------- I2: only the gate mints


def test_a_free_conn_is_taken_through_the_gate_and_carries_the_gates_fence(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    d = take_conn(m)
    assert d.outcome == model.ACCEPTED
    assert m.state.in_force.fence == gate.fence and gate.generation == 2
    conn = m.state.leases[model.CONN]
    assert conn.holder == "ted@ROC-1" and m.state.active_station == "ROC-1"
    assert all(row.authority_lease_id == conn.lease_id and row.authority_holder == "ted@ROC-1" for row in gate.mappings)
    # ...and that id is the token on the published record: the only identity a gate can check.
    assert conn.lease_id == conn.token


def test_the_proposal_carries_no_fence_and_the_manager_never_makes_one(tmp_path, clock, gate):
    from control_authority.codec import transfer_prepare

    m = make(tmp_path, clock, gate)
    take_conn(m)
    pb = transfer_prepare(gate.prepares[0], 2000, V)
    assert all(not row.HasField("fence") for row in pb.mappings)
    # Every fence the manager ever recorded is one the gate exposed or ACKed.
    seen = {(gate.term, g) for g in range(1, gate.generation + 1)}
    for t in m.state.transfers.values():
        assert t.fence is None or (t.fence.term, t.fence.generation) in seen


def test_nothing_is_proposed_before_the_gate_has_been_read(tmp_path, clock, gate):
    gate.down = True
    m = make(tmp_path, clock, gate)
    assert not m.state.reconciled
    assert take_conn(m).outcome == model.REFUSED_NOT_RECONCILED
    gate.down = False
    assert take_conn(m).outcome == model.ACCEPTED  # the next transfer reconciles first


def test_prepared_is_durable_before_the_gate_hears_the_proposal_and_committed_before_the_commit(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    seen = {}

    def phase(tid):
        db = sqlite3.connect(tmp_path / "authority.sqlite")
        import json

        st = json.loads(db.execute("SELECT v FROM meta WHERE k='state'").fetchone()[0])
        db.close()
        return st["transfers"][tid]["phase"]

    gate.on_prepare = lambda t: seen.setdefault("prepare", phase(t.transfer_id))
    gate.on_commit = lambda tid: seen.setdefault("commit", phase(tid))
    take_conn(m)
    assert seen == {"prepare": model.PREPARED, "commit": model.COMMITTED}


# ---------------------------------------------------------------------------- I10: all or nothing


def test_one_unmatched_lever_naks_the_whole_transfer_and_the_old_holder_keeps_the_helm(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    take_conn(m, "ted@ROC-1", "ROC-1")
    before = (m.state.in_force, m.state.leases[model.CONN])
    gate.unready = {"throttle_stbd"}
    conn = m.state.leases[model.CONN]
    d = m.transfer(TransferRequest(model.GRANT, "ted@ROC-1", "ROC-1", conn_token=conn.token,
                                   new_holder="anna@ROC-1", new_holder_site="ROC-1", mappings=human_map("ROC-1")))
    assert d.outcome == model.REFUSED_BY_GATE
    assert (m.state.in_force, m.state.leases[model.CONN]) == before
    assert gate.mappings and all(row.authority_holder == "ted@ROC-1" for row in gate.mappings)


@pytest.mark.parametrize("case", ["missing_dof", "half_group", "other_station", "unknown_dof", "twice"])
def test_the_map_moves_the_whole_helm_at_one_station(tmp_path, clock, gate, case):
    m = make(tmp_path, clock, gate)
    take_conn(m)
    rows = list(human_map("ROC-1"))
    if case == "missing_dof":
        rows = rows[1:]  # no rudder
    elif case == "half_group":
        rows = [r for r in rows if r.dof != "throttle_stbd"]
    elif case == "other_station":
        rows[0] = Mapping("rudder", "", "", "ROC-2/helm-1/rudder")
    elif case == "unknown_dof":
        rows.append(Mapping("stern_thruster", "", "", "ROC-1/helm-1/stern_thruster"))
    else:
        rows.append(rows[0])
    conn = m.state.leases[model.CONN]
    d = m.transfer(TransferRequest(model.GRANT, "ted@ROC-1", "ROC-1", conn_token=conn.token, new_holder="anna@ROC-1",
                                   new_holder_site="ROC-1", mappings=tuple(rows)))
    assert d.outcome == model.REFUSED_MAP and len(gate.prepares) == 1  # never proposed


def test_between_humans_only_the_holder_grants_and_only_within_the_station(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    take_conn(m, "ted@ROC-1", "ROC-1")
    d = m.transfer(TransferRequest(model.GRANT, "anna@ROC-1", "ROC-1", mappings=human_map("ROC-1")))
    assert d.outcome == model.REFUSED_HELD  # no seizure between humans
    conn = m.state.leases[model.CONN]
    d = m.transfer(TransferRequest(model.GRANT, "ted@ROC-1", "ROC-1", conn_token=conn.token, new_holder="bo@ROC-2",
                                   new_holder_site="ROC-2", mappings=human_map("ROC-2")))
    assert d.outcome == model.REFUSED_UNAUTHORISED  # moving the station is the master's
    d = m.transfer(TransferRequest(model.GRANT, "ted@ROC-1", "ROC-1", conn_token=conn.token, new_holder="anna@ROC-1",
                                   new_holder_site="ROC-1", mappings=human_map("ROC-1")))
    assert d.outcome == model.ACCEPTED and m.state.leases[model.CONN].holder == "anna@ROC-1"
    assert m.state.leases[model.CONN].token != conn.token  # the granting station's token is dead


def test_operational_profile_has_no_station_until_the_master_sets_one(tmp_path, clock, gate):
    op = parse({"command_profile": "operational", "dofs": DOFS, "groups": {"throttles": ["throttle_port", "throttle_stbd"]}})
    m = make(tmp_path, clock, gate, policy=op)
    assert take_conn(m).outcome == model.REFUSED_UNAUTHORISED


# ---------------------------------------------------------------------------- I13: durable on both sides


def test_manager_crash_between_ack_and_commit_then_gate_restart_abandons_cleanly(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    take_conn(m)
    in_force = gate.fence
    conn = m.state.leases[model.CONN]
    # Decide and commit up to COMMITTED, then "crash" before the COMMIT is sent.
    d = model.begin_transfer(m.state, POLICY, TransferRequest(model.GRANT, "ted@ROC-1", "ROC-1", conn_token=conn.token,
                                                              new_holder="anna@ROC-1", new_holder_site="ROC-1",
                                                              mappings=human_map("ROC-1")), clock())
    m.store.commit(d.state, d.audits)
    ack = gate.prepare(d.effects[0].transfer, 2000)
    d2 = model.on_ack(d.state, d.transfer_id, ack.accepted, ack.fence)
    m.store.commit(d2.state, d2.audits)
    m.store.close()
    gate.restart()  # and the gate's reservation is gone too
    m2 = make(tmp_path, clock, gate)
    assert m2.state.transfers[d.transfer_id].phase == model.ABANDONED
    assert m2.state.in_force.fence == in_force == gate.fence  # nothing half-applied
    assert m2.state.leases[model.CONN].holder == "ted@ROC-1"  # the conn in force stays in force
    nxt = m2.transfer(TransferRequest(model.GRANT, "ted@ROC-1", "ROC-1", conn_token=conn.token, new_holder="anna@ROC-1",
                                      new_holder_site="ROC-1", mappings=human_map("ROC-1")))
    assert nxt.outcome == model.ACCEPTED and m2.state.in_force.fence.generation == in_force.generation + 1


def test_gate_applied_the_commit_but_the_manager_died_before_recording_it(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    take_conn(m)
    conn = m.state.leases[model.CONN]
    d = model.begin_transfer(m.state, POLICY, TransferRequest(model.GRANT, "ted@ROC-1", "ROC-1", conn_token=conn.token,
                                                              new_holder="anna@ROC-1", new_holder_site="ROC-1",
                                                              mappings=human_map("ROC-1")), clock())
    ack = gate.prepare(d.effects[0].transfer, 2000)
    d2 = model.on_ack(d.state, d.transfer_id, ack.accepted, ack.fence)
    m.store.commit(d2.state, d2.audits)
    assert gate.commit(d.transfer_id, ack.fence)[0]  # the gate activated it
    m.store.close()
    m2 = make(tmp_path, clock, gate)
    assert m2.state.transfers[d.transfer_id].phase == model.ACTIVE
    assert m2.state.leases[model.CONN].holder == "anna@ROC-1" and m2.state.in_force.fence == gate.fence


def test_manager_restart_mid_prepare_interrupts_no_conn_and_the_next_prepare_gets_n_plus_1(tmp_path, clock, gate):
    """I2: the gate keeps (T, n); the orphaned PREPARE expires on prepare_ttl_ms; reconcile; (T, n+1)."""
    m = make(tmp_path, clock, gate)
    take_conn(m)
    n = gate.generation
    conn = m.state.leases[model.CONN]
    d = model.begin_transfer(m.state, POLICY, TransferRequest(model.GRANT, "ted@ROC-1", "ROC-1", conn_token=conn.token,
                                                              new_holder="anna@ROC-1", new_holder_site="ROC-1",
                                                              mappings=human_map("ROC-1")), clock())
    m.store.commit(d.state, d.audits)
    gate.prepare(d.effects[0].transfer, POLICY.prepare_ttl_ms)  # reserved, never committed
    m.store.close()
    clock.t += POLICY.prepare_ttl_ms + 1
    m2 = make(tmp_path, clock, gate)
    assert gate.generation == n and m2.state.leases[model.CONN].holder == "ted@ROC-1"
    assert m2.state.leases[model.CONN].live(clock())  # rebased onto the new process's clock
    nxt = m2.transfer(TransferRequest(model.GRANT, "ted@ROC-1", "ROC-1", conn_token=conn.token, new_holder="anna@ROC-1",
                                      new_holder_site="ROC-1", mappings=human_map("ROC-1")))
    assert nxt.outcome == model.ACCEPTED and gate.generation == n + 1


def test_manager_dead_and_the_conn_lapses_the_gate_fences_alone_and_the_manager_adopts_it(tmp_path, clock, gate):
    bus = Bus()
    m = make(tmp_path, clock, gate, bus=bus)
    take_conn(m)
    m.store.close()
    gate.fence_alone()
    bus2 = Bus()
    m2 = make(tmp_path, clock, gate, bus=bus2)
    assert m2.state.in_force.fence == gate.fence and m2.state.in_force.mappings == ()
    assert any("differed" in a for a in bus2.alarms())
    assert any(r["event"] == "reconcile_disagreement" for r in m2.history())


def test_a_fresh_manager_adopts_the_gate_without_an_alarm(tmp_path, clock, gate):
    bus = Bus()
    m = make(tmp_path, clock, gate, bus=bus)
    assert m.state.in_force.fence == gate.fence and bus.alarms() == []


def test_a_commit_the_gate_no_longer_remembers_is_abandoned_and_forces_a_reconcile(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    gate.on_commit = lambda tid: gate.restart()
    d = take_conn(m)
    assert d.outcome == model.REFUSED_BY_GATE and not m.state.reconciled
    gate.on_commit = None
    assert take_conn(m).outcome == model.ACCEPTED


def test_one_transfer_at_a_time(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    d = model.begin_transfer(m.state, POLICY, TransferRequest(model.GRANT, "ted@ROC-1", "ROC-1", mappings=human_map("ROC-1")),
                             clock())
    again = model.begin_transfer(d.state, POLICY, TransferRequest(model.GRANT, "ted@ROC-1", "ROC-1",
                                                                  mappings=human_map("ROC-1")), clock())
    assert again.outcome == model.REFUSED_BUSY


# ---------------------------------------------------------------------------- take control, abort, handover


def _automation_holds_the_conn(m):
    conn = m.state.leases[model.CONN]
    d = m.transfer(TransferRequest(model.GRANT, conn.holder, conn.site, conn_token=conn.token, new_holder=NAV,
                                   new_holder_site="usv-alpha", mappings=auto_map()))
    assert d.outcome == model.ACCEPTED, d.detail
    assert m.state.leases[model.CONN].holder == NAV


def test_take_control_from_automation_moves_station_map_and_epoch_together(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    master(m, "ted@ROC-1", "ROC-1")
    a = master(m, "anna@ROC-2", "ROC-2", model.NAVIGATIONAL, token="sess-2")
    take_conn(m, "ted@ROC-1", "ROC-1")
    _automation_holds_the_conn(m)
    n = gate.generation
    d = m.transfer(TransferRequest(model.TAKE_CONTROL, "anna@ROC-2", "ROC-2", assignment_revision=a.revision,
                                   session_token="sess-2", mappings=human_map("ROC-2")))
    assert d.outcome == model.ACCEPTED, d.detail
    assert m.state.active_station == "ROC-2" == gate.station and gate.generation == n + 1
    assert m.state.leases[model.CONN].holder == "anna@ROC-2"


def test_take_control_needs_the_assignment_and_its_live_session(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    master(m)
    a = master(m, "anna@ROC-2", "ROC-2", model.NAVIGATIONAL, token="sess-2")
    take_conn(m)
    req = dict(mappings=human_map("ROC-2"))
    assert m.transfer(TransferRequest(model.TAKE_CONTROL, "bo@ROC-2", "ROC-2", assignment_revision=a.revision,
                                      session_token="sess-2", **req)).outcome == model.REFUSED_UNAUTHORISED
    assert m.transfer(TransferRequest(model.TAKE_CONTROL, "anna@ROC-2", "ROC-2", assignment_revision=a.revision,
                                      session_token="wrong", **req)).outcome == model.REFUSED_UNAUTHORISED
    clock.t += POLICY.session_ttl_ms + 1
    m.tick()
    assert m.transfer(TransferRequest(model.TAKE_CONTROL, "anna@ROC-2", "ROC-2", assignment_revision=a.revision,
                                      session_token="sess-2", **req)).outcome == model.REFUSED_UNAUTHORISED


def test_abort_automation_fences_the_navigator_and_proposes_no_map(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    a = master(m)
    take_conn(m)
    _automation_holds_the_conn(m)
    with_map = TransferRequest(model.ABORT_AUTOMATION, "ted@ROC-1", "ROC-1", assignment_revision=a.revision,
                               session_token="sess-1", mappings=human_map("ROC-1"))
    assert m.transfer(with_map).outcome == model.REFUSED_MALFORMED
    d = m.transfer(TransferRequest(model.ABORT_AUTOMATION, "ted@ROC-1", "ROC-1", assignment_revision=a.revision,
                                   session_token="sess-1"))
    assert d.outcome == model.ACCEPTED
    assert model.CONN not in m.state.leases and m.state.in_force.mappings == () and m.state.in_force.fence == gate.fence


def test_station_handover_is_the_masters_and_moves_every_dof(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    a = master(m)
    nav = master(m, "anna@ROC-2", "ROC-2", model.NAVIGATIONAL, token="sess-2")
    take_conn(m)
    req = dict(station="ROC-3", new_holder="cy@ROC-3", new_holder_site="ROC-3", mappings=human_map("ROC-3"))
    assert m.transfer(TransferRequest(model.STATION_HANDOVER, "anna@ROC-2", "ROC-2", assignment_revision=nav.revision,
                                      session_token="sess-2", **req)).outcome == model.REFUSED_UNAUTHORISED
    d = m.transfer(TransferRequest(model.STATION_HANDOVER, "ted@ROC-1", "ROC-1", assignment_revision=a.revision,
                                   session_token="sess-1", **req))
    assert d.outcome == model.ACCEPTED and m.state.active_station == "ROC-3" == gate.station
    assert {row.dof for row in gate.mappings} == set(DOFS)


# ---------------------------------------------------------------------------- I7: revision revokes


def test_assume_command_revokes_the_old_masters_authorisation(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    old = master(m, "ted@ROC-1", "ROC-1")
    assert m.assume_command(model.OVERALL, "chief@ROC-2", "master unreachable", ["no answer on VHF"], ["owner"]).outcome \
        == model.REFUSED_NOT_UNREACHABLE  # his session is live
    clock.t += POLICY.session_ttl_ms + 1
    m.tick()
    assert m.assume_command(model.OVERALL, "mallory@ROC-9", "x", ["y"], []).outcome == model.REFUSED_UNAUTHORISED
    assert m.assume_command(model.OVERALL, "chief@ROC-2", "", [], []).outcome == model.REFUSED_MALFORMED
    d = m.assume_command(model.OVERALL, "chief@ROC-2", "master unreachable", ["no answer on VHF"], ["owner"])
    assert d.outcome == model.ACCEPTED and m.state.overall.revision == old.revision + 1
    assert m.state.overall.succession.previous_assignee == "ted@ROC-1"
    # He comes back and acts under the old revision: refused with the reason.
    m.session(model.OVERALL, "ted@ROC-1", "ROC-1", "sess-1", old.assignment_id)
    r = m.transfer(TransferRequest(model.ABORT_AUTOMATION, "ted@ROC-1", "ROC-1", assignment_revision=old.revision,
                                   session_token="sess-1"))
    assert r.outcome == model.REFUSED_UNAUTHORISED  # no longer the assignee at all
    assert m.assign(model.NAVIGATIONAL, "anna@ROC-2", "ted@ROC-1", old.revision, "sess-1").outcome == model.REFUSED_UNAUTHORISED
    m.session(model.OVERALL, "chief@ROC-2", "ROC-2", "sess-c", m.state.overall.assignment_id)
    assert m.assign(model.NAVIGATIONAL, "anna@ROC-2", "chief@ROC-2", old.revision, "sess-c").outcome == model.REFUSED_STALE_REVISION
    assert m.assign(model.NAVIGATIONAL, "anna@ROC-2", "chief@ROC-2", m.state.overall.revision, "nope").outcome \
        == model.REFUSED_UNAUTHORISED  # the master's own session, or nothing


def test_the_same_assignee_acting_under_an_older_revision_is_refused(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    master(m)
    first = master(m, "anna@ROC-2", "ROC-2", model.NAVIGATIONAL, token="sess-2")
    m.assign(model.NAVIGATIONAL, "bo@ROC-2", "ted@ROC-1", m.state.overall.revision, "sess-1")
    again = master(m, "anna@ROC-2", "ROC-2", model.NAVIGATIONAL, token="sess-3")
    assert again.revision == first.revision + 2
    r = m.transfer(TransferRequest(model.ABORT_AUTOMATION, "anna@ROC-2", "ROC-2", assignment_revision=first.revision,
                                   session_token="sess-3"))
    assert r.outcome == model.REFUSED_STALE_REVISION


def test_assume_touches_no_lease_map_or_fence(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    master(m)
    take_conn(m)
    before = (m.state.in_force, dict(m.state.leases), gate.generation)
    for _ in range(POLICY.session_ttl_ms // 1000 + 1):  # the conn holder heartbeats; the master's session lapses
        clock.t += 1000
        assert m.renew(model.CONN, m.state.leases[model.CONN].token).outcome == model.ACCEPTED
    m.tick()
    m.assume_command(model.OVERALL, "chief@ROC-2", "unreachable", ["e"], ["owner"])
    assert m.state.in_force == before[0] and gate.generation == before[2]
    assert m.state.leases[model.CONN].holder == before[1][model.CONN].holder


def test_overall_and_navigational_are_never_software(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    assert m.assign(model.OVERALL, NAV, "procedure").outcome == model.REFUSED_SOFTWARE


# ---------------------------------------------------------------------------- leases and sessions


def test_a_lapsed_session_alarms_and_the_assignment_stands(tmp_path, clock, gate):
    bus = Bus()
    m = make(tmp_path, clock, gate, bus=bus)
    a = master(m)
    clock.t += POLICY.session_ttl_ms + 1
    m.tick()
    assert m.state.overall == a and any("unreachable" in t for t in bus.alarms())


def test_engineer_and_payload_leases(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    assert m.take(model.ENGINEER, "eng@vessel", "vessel").outcome == model.ACCEPTED
    assert m.take(model.ENGINEER, "other@ROC-1", "ROC-1").outcome == model.REFUSED_HELD
    assert m.take(model.CONN, "x@ROC-1", "ROC-1").outcome == model.REFUSED_MALFORMED  # the conn is a transfer
    assert m.take(model.PAYLOAD, "op@ROC-1", "ROC-1", payload_id="sonar").outcome == model.ACCEPTED
    clock.t += POLICY.ttl(model.PAYLOAD) + 1
    m.tick()
    assert "payload/sonar" not in m.state.leases and "engineer" in m.state.leases


def test_legacy_single_operator_synthesises_the_assignments_for_the_conn_holder(tmp_path, clock, gate):
    legacy = parse({"command_profile": "legacy_single_operator", "dofs": DOFS,
                    "groups": {"throttles": ["throttle_port", "throttle_stbd"]}})
    m = make(tmp_path, clock, gate, policy=legacy)
    take_conn(m)
    m.tick()
    assert m.state.overall.assignee == "ted@ROC-1" and m.state.overall.synthesised
    rev = m.state.overall.revision
    conn = m.state.leases[model.CONN]
    m.transfer(TransferRequest(model.GRANT, "ted@ROC-1", "ROC-1", conn_token=conn.token, new_holder="anna@ROC-1",
                               new_holder_site="ROC-1", mappings=human_map("ROC-1")))
    m.tick()
    assert m.state.navigational.assignee == "anna@ROC-1" and m.state.overall.revision == rev + 1


def test_the_navigation_document_revision_moves_on_change_only(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    master(m)
    nav = master(m, "anna@ROC-2", "ROC-2", model.NAVIGATIONAL, token="sess-2")
    fn = NavFunction(NAV, "lease-1", 4, {"reroute_allowed": False})
    assert m.set_navigation_function("track_keeping", fn, "anna@ROC-2", nav.revision, "sess-2").outcome == model.ACCEPTED
    assert m.state.navigation_revision == 1
    m.set_navigation_function("track_keeping", fn, "anna@ROC-2", nav.revision, "sess-2")
    assert m.state.navigation_revision == 1
    m.set_navigation_function("track_keeping", NavFunction(NAV, "lease-1", 3), "anna@ROC-2", nav.revision, "sess-2")
    assert m.state.navigation_revision == 2
    assert m.set_navigation_function("track_keeping", None, "bo@ROC-2", nav.revision, "x").outcome == model.REFUSED_UNAUTHORISED


# ---------------------------------------------------------------------------- the store


def test_a_tampered_store_is_refused_never_guessed_past(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    take_conn(m)
    m.store.close()
    db = sqlite3.connect(tmp_path / "authority.sqlite")
    db.execute("UPDATE meta SET v=replace(v, 'ted@ROC-1', 'mallory@ROC-9') WHERE k='state'")
    db.commit()
    db.close()
    with pytest.raises(UntrustedStore):
        make(tmp_path, clock, gate)


def test_the_state_survives_a_restart(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    master(m)
    take_conn(m)
    st = m.state
    m.store.close()
    m2 = make(tmp_path, clock, gate)
    assert m2.state.overall == st.overall and m2.state.in_force == st.in_force
    assert m2.state.leases[model.CONN].token == st.leases[model.CONN].token
    assert [r["event"] for r in m2.history()].count("active") == 1


# ---------------------------------------------------------------------------- --assign-overall (joint review of f88320c)


def test_bootstrap_assigns_only_when_nobody_holds_overall(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    assert m.bootstrap_overall(MASTER_ID, "").outcome == model.REFUSED_MALFORMED  # names its maintainer
    d = m.bootstrap_overall(MASTER_ID, "engineer@vessel")
    assert d.outcome == model.ACCEPTED and m.state.overall.assignee == MASTER_ID and m.state.overall.revision == 1
    assert m.bootstrap_overall(MASTER_ID, "engineer@vessel").detail == "already assigned"
    assert m.state.overall.revision == 1
    assert any(r["event"] == "bootstrap_overall" and r["maintainer"] == "engineer@vessel" for r in m.history())


def test_bootstrap_never_undoes_a_succession(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    m.bootstrap_overall(MASTER_ID, "engineer@vessel")
    m.assume_command(model.OVERALL, "chief@ROC-2", "master unreachable", ["no answer"], ["owner"])
    d = m.bootstrap_overall(MASTER_ID, "engineer@vessel")
    assert d.outcome == model.REFUSED_HELD and m.state.overall.assignee == "chief@ROC-2"


def test_bootstrap_refuses_software(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    assert m.bootstrap_overall(NAV, "engineer@vessel").outcome == model.REFUSED_SOFTWARE

