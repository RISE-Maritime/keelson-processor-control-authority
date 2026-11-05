"""maintenance_state (keelson protocols/maintenance.yaml): the OVERALL_COMMAND assignee's procedure,
the guards on what the manager has heard, payload authority vacated, transfers refused, and the only
way out through the checks. Literal times on the manager's clock; no network."""

import json

import pytest

from control_authority import model
from control_authority.model import Check, DofReading, TransferRequest
from control_authority.policy import parse
from control_authority.service import Manager
from control_authority.store import SCHEMA_VERSION, Store, _canonical, _digest
from fakegate import FakeGate
from test_manager import DOFS, Bus, Clock, master, take_conn

V = "usv-alpha"
BASE = {
    "command_profile": "test",
    "dofs": DOFS,
    "groups": {"throttles": ["throttle_port", "throttle_stbd"]},
    "automation_sources": ["navigation/0"],
    "processes": ["navigation/0@usv-alpha"],
    "succession": {"overall": ["chief@ROC-2"], "navigational": "*"},
}
MAINT = {"rest": {"rudder": "hold"}, "zero_tolerance_pct": 1.0, "feedback_max_age_ms": 2000, "input_max_age_ms": 3000,
         "required_checks": ["self_test", "configuration_valid"]}
POLICY = parse({**BASE, "maintenance": MAINT})
WITH_GIMBAL = parse({**BASE, "maintenance": {**MAINT, "payloads": ["gimbal"]}})
PASSED = (Check("self_test", True, evidence="run-9"), Check("configuration_valid", True))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gate(clock):
    return FakeGate(clock)


def make(tmp_path, clock, gate, policy=POLICY, bus=None):
    m = Manager(store=Store(tmp_path / "authority.sqlite"), policy=policy, vessel_id=V, gate=gate,
                publish=bus or Bus(), now_ms=clock, audit_ms=lambda: 1_800_000_000_000)
    m.boot()
    return m


def reading(pct=0.0, quality="VALID", age=100, state="TRANSITION"):
    return DofReading(pct, quality, age, state)


def at_rest(**override):
    dofs = {"rudder": reading(12.0), "throttle_port": reading(0.0), "throttle_stbd": reading(0.4)}
    dofs.update(override)
    return dofs


def request(m, a, who="ted@ROC-1", token="sess-1", **kw):
    args = {"maintainer": "anna@workshop", "reason": "autopilot firmware", "scopes": ("firmware",), **kw}
    return m.request_maintenance(who, a.revision, token, args["maintainer"], args["reason"], args["scopes"])


def to_maintenance(m, a, clock):
    assert request(m, a).outcome == model.ACCEPTED
    m.observe_armed(False)
    m.observe_resolved(at_rest())
    m.tick()
    assert m.state.maintenance.state == model.M_PREPARING
    m.tick()
    assert m.state.maintenance.state == model.M_MAINTENANCE


def guard(m, name):
    return next(g for g in m.guards() if g.name == name)


# ---------------------------------------------------------------------------- who may


def test_only_the_overall_assignee_with_a_live_session_and_current_revision_requests_it(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    a = master(m)
    nav = master(m, "anna@ROC-2", "ROC-2", model.NAVIGATIONAL, "sess-2")
    assert request(m, nav, "anna@ROC-2", "sess-2").outcome == model.REFUSED_UNAUTHORISED
    assert m.request_maintenance("ted@ROC-1", a.revision - 1, "sess-1", "anna", "why", ("software",)).outcome \
        == model.REFUSED_STALE_REVISION
    assert request(m, a, token="not-the-session").outcome == model.REFUSED_UNAUTHORISED
    clock.t += POLICY.session_ttl_ms + 1
    assert request(m, a).outcome == model.REFUSED_UNAUTHORISED  # the session lapsed
    assert m.state.maintenance.state == model.M_INACTIVE


@pytest.mark.parametrize("kw,why", [
    ({"maintainer": ""}, "names the maintainer"),
    ({"reason": ""}, "names the maintainer, the reason"),
    ({"scopes": ()}, "a scope"),
    ({"scopes": ("software", "scope_9")}, "unknown scope: scope_9"),
])
def test_a_request_names_who_why_and_what_it_may_touch(tmp_path, clock, gate, kw, why):
    m = make(tmp_path, clock, gate)
    d = request(m, master(m), **kw)
    assert d.outcome == model.REFUSED_MALFORMED and why in d.detail


# ---------------------------------------------------------------------------- the whole procedure


def test_requested_preparing_maintenance_ready_and_back_recorded_and_published(tmp_path, clock, gate):
    bus = Bus()
    m = make(tmp_path, clock, gate, bus=bus)
    a = master(m)
    d = request(m, a, scopes=("firmware", "configuration"))
    assert d.outcome == model.ACCEPTED
    rec = m.state.maintenance
    assert (rec.state, rec.revision, rec.requested_by, rec.maintainer, rec.scopes) == (
        model.M_REQUESTED, 1, "ted@ROC-1", "anna@workshop", ("firmware", "configuration"))
    assert (rec.overall_assignment_id, rec.overall_assignment_revision) == (a.assignment_id, a.revision)
    assert rec.maintenance_id and rec.issued_wall_ms == rec.since_wall_ms == 1_800_000_000_000
    assert guard(m, "disarmed").detail == "vehicle_armed not heard"

    to_ready = [model.M_PREPARING, model.M_MAINTENANCE]
    m.observe_armed(False)
    m.observe_resolved(at_rest())
    for want in to_ready:
        m.tick()
        assert m.state.maintenance.state == want
    assert m.guards() == ()  # nothing waits in MAINTENANCE
    assert m.finish_maintenance("ted@ROC-1", a.revision, "sess-1", "flashed 4.2").outcome == model.ACCEPTED
    assert m.state.maintenance.state == model.M_READY
    d = m.complete_maintenance_check("ted@ROC-1", a.revision, "sess-1", PASSED)
    assert d.outcome == model.ACCEPTED and m.state.maintenance.state == model.M_INACTIVE
    assert m.state.maintenance.revision == 5 and m.state.maintenance.checks == PASSED
    events = [r["event"] for r in m.history()]
    assert ["maintenance_requested", "maintenance_preparing", "maintenance_entered", "maintenance_finished",
            "maintenance_completed"] == [e for e in events if e.startswith("maintenance_")]
    published = [e for e in bus.effects if isinstance(e, model.PublishMaintenance)]
    assert published and any(not g.satisfied for g in published[0].guards)
    assert any("MAINTENANCE: anna@workshop may change firmware, configuration" in t for t in bus.alarms())


def test_a_guard_change_is_republished_and_an_unchanged_one_is_not(tmp_path, clock, gate):
    bus = Bus()
    m = make(tmp_path, clock, gate, bus=bus)
    request(m, master(m))
    m.tick()
    n = len([e for e in bus.effects if isinstance(e, model.PublishMaintenance)])
    m.tick()
    assert len([e for e in bus.effects if isinstance(e, model.PublishMaintenance)]) == n
    m.observe_armed(True)
    m.tick()
    pubs = [e for e in bus.effects if isinstance(e, model.PublishMaintenance)]
    assert len(pubs) == n + 1 and pubs[-1].guards[0].detail.startswith("vehicle_armed is true")


# ---------------------------------------------------------------------------- the guards


def test_armed_stale_or_unheard_never_steps(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    request(m, master(m))
    m.observe_resolved(at_rest())
    m.tick()
    assert m.state.maintenance.state == model.M_REQUESTED  # unheard
    m.observe_armed(True)
    m.tick()
    assert m.state.maintenance.state == model.M_REQUESTED
    assert "whoever holds the conn disarms her" in guard(m, "disarmed").detail
    m.observe_armed(False)
    clock.t += 3001
    m.observe_resolved(at_rest())  # the gate is fresh; the armed reading is not
    m.tick()
    assert m.state.maintenance.state == model.M_REQUESTED
    assert guard(m, "disarmed").detail == "vehicle_armed not heard for 3001 ms"


@pytest.mark.parametrize("dof,r,why", [
    ("throttle_port", reading(quality="ESTIMATED"), "feedback is ESTIMATED"),
    ("throttle_port", reading(quality="NONE"), "feedback is NONE"),
    ("throttle_port", reading(quality="STALE"), "feedback is STALE"),
    ("throttle_port", reading(pct=None), "no fresh actual value"),
    ("throttle_port", reading(age=None), "no fresh actual value"),
    ("throttle_port", reading(age=2001), "no fresh actual value"),
    ("throttle_stbd", reading(pct=-1.5), "at -1.5 %, not zero"),
    ("rudder", reading(pct=12.0, state="ACTIVE"), "driven by its source"),
])
def test_a_dof_is_at_rest_only_on_a_valid_fresh_reading(tmp_path, clock, gate, dof, r, why):
    m = make(tmp_path, clock, gate)
    request(m, master(m))
    m.observe_armed(False)
    m.observe_resolved(at_rest(**{dof: r}))
    m.tick()
    assert m.state.maintenance.state == model.M_REQUESTED
    g = guard(m, f"dof_at_rest/{dof}")
    assert not g.satisfied and why in g.detail


def test_a_held_dof_rests_anywhere_and_a_zero_dof_within_tolerance(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    request(m, master(m))
    m.observe_armed(False)
    m.observe_resolved(at_rest(rudder=reading(-20.0, state="INHIBITED"), throttle_stbd=reading(1.0)))
    assert all(g.satisfied for g in m.guards())
    assert guard(m, "dof_at_rest/rudder").detail == "holding at -20.0 %"


def test_a_dof_missing_from_resolved_control_or_a_stale_gate_is_not_at_rest(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    request(m, master(m))
    m.observe_armed(False)
    dofs = at_rest()
    del dofs["throttle_stbd"]
    m.observe_resolved(dofs)
    assert guard(m, "dof_at_rest/throttle_stbd").detail == "not in resolved_control"
    m.observe_resolved(at_rest())
    clock.t += 3001
    m.observe_armed(False)
    m.tick()
    assert m.state.maintenance.state == model.M_REQUESTED
    assert guard(m, "dof_at_rest/rudder").detail == "resolved_control not heard for 3001 ms"


def test_an_unreported_payload_fails_closed_until_it_reports_its_safe_state(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate, WITH_GIMBAL)
    request(m, master(m))
    m.observe_armed(False)
    m.observe_resolved(at_rest())
    for _ in range(3):
        m.tick()
    assert m.state.maintenance.state == model.M_PREPARING
    assert guard(m, "payload_safe/gimbal").detail == "no payload state is reported (keelson#372)"
    m.heard = model.Heard(**{**m.heard.__dict__, "payload_safe": {"gimbal": True}})  # what #372 will carry
    m.tick()
    assert m.state.maintenance.state == model.M_MAINTENANCE


# ---------------------------------------------------------------------------- what it refuses meanwhile


def test_preparing_vacates_payload_authority_and_refuses_it_until_inactive(tmp_path, clock, gate):
    bus = Bus()
    m = make(tmp_path, clock, gate, bus=bus)
    a = master(m)
    assert m.take(model.PAYLOAD, "ted@ROC-1", "ROC-1", "gimbal").outcome == model.ACCEPTED
    request(m, a)
    m.observe_armed(False)
    m.observe_resolved(at_rest())
    m.tick()
    assert "payload/gimbal" not in m.state.leases
    released = [e for e in bus.effects if isinstance(e, model.PublishLease) and e.released]
    assert [e.slot for e in released] == ["payload/gimbal"] and released[0].lease.holder == "ted@ROC-1"
    assert m.history()[-1]["payload_leases_vacated"] == ["payload/gimbal"]
    assert m.take(model.PAYLOAD, "ted@ROC-1", "ROC-1", "gimbal").outcome == model.REFUSED_MAINTENANCE
    assert m.take(model.ENGINEER, "eng@vessel", "vessel").outcome == model.ACCEPTED  # not a payload
    m.cancel_maintenance("ted@ROC-1", a.revision, "sess-1", "wrong day")
    assert m.take(model.PAYLOAD, "ted@ROC-1", "ROC-1", "gimbal").outcome == model.ACCEPTED


def test_in_maintenance_no_transfer_but_an_abort(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    a = master(m)
    to_maintenance(m, a, clock)
    d = take_conn(m)
    assert d.outcome == model.REFUSED_MAINTENANCE and "abort_automation" in d.detail
    assert gate.prepares == []
    d = m.transfer(TransferRequest(model.ABORT_AUTOMATION, "ted@ROC-1", "ROC-1", assignment_revision=a.revision,
                                   session_token="sess-1"))
    assert d.outcome == model.ACCEPTED, d.detail


def test_while_only_requested_the_conn_still_moves(tmp_path, clock, gate):
    """REQUESTED waits for her to be stopped and disarmed, which someone with the conn may need to do."""
    m = make(tmp_path, clock, gate)
    request(m, master(m))
    assert take_conn(m).outcome == model.ACCEPTED


# ---------------------------------------------------------------------------- the way out


def test_cancel_only_before_anything_changed(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    a = master(m)
    request(m, a)
    assert m.cancel_maintenance("ted@ROC-1", a.revision, "sess-1", "wrong day").outcome == model.ACCEPTED
    assert m.state.maintenance.state == model.M_INACTIVE
    to_maintenance(m, a, clock)
    d = m.cancel_maintenance("ted@ROC-1", a.revision, "sess-1", "")
    assert d.outcome == model.REFUSED_GUARD and "through the checks" in d.detail
    assert m.state.maintenance.state == model.M_MAINTENANCE


def test_a_missing_required_check_refuses_and_a_failed_one_goes_back_to_maintenance(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    a = master(m)
    to_maintenance(m, a, clock)
    assert m.complete_maintenance_check("ted@ROC-1", a.revision, "sess-1", PASSED).outcome == model.REFUSED_GUARD
    m.finish_maintenance("ted@ROC-1", a.revision, "sess-1", "")
    d = m.complete_maintenance_check("ted@ROC-1", a.revision, "sess-1", PASSED[:1])
    assert d.outcome == model.REFUSED_GUARD and "configuration_valid" in d.detail
    assert m.state.maintenance.state == model.M_READY
    dup = (*PASSED, Check("self_test", True))
    assert m.complete_maintenance_check("ted@ROC-1", a.revision, "sess-1", dup).outcome == model.REFUSED_MALFORMED
    failed = (PASSED[0], Check("configuration_valid", False, "rudder limits differ"))
    d = m.complete_maintenance_check("ted@ROC-1", a.revision, "sess-1", failed)
    assert d.outcome == model.ACCEPTED and m.state.maintenance.state == model.M_MAINTENANCE
    assert m.state.maintenance.checks == failed
    # An extra check that failed is a failure too, even if it is not required.
    m.finish_maintenance("ted@ROC-1", a.revision, "sess-1", "")
    extra = (*PASSED, Check("sea_trial", False))
    m.complete_maintenance_check("ted@ROC-1", a.revision, "sess-1", extra)
    assert m.state.maintenance.state == model.M_MAINTENANCE


def test_finish_only_from_maintenance(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    a = master(m)
    assert m.finish_maintenance("ted@ROC-1", a.revision, "sess-1", "").outcome == model.REFUSED_GUARD
    request(m, a)
    assert m.finish_maintenance("ted@ROC-1", a.revision, "sess-1", "").outcome == model.REFUSED_GUARD
    assert request(m, a).outcome == model.REFUSED_GUARD  # already REQUESTED


# ---------------------------------------------------------------------------- durability


def test_a_restart_keeps_the_procedure_and_forgets_what_it_heard(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    a = master(m)
    request(m, a)
    m.observe_armed(False)
    m.observe_resolved(at_rest())
    m.store.close()
    m2 = make(tmp_path, clock, gate)
    assert m2.state.maintenance.state == model.M_REQUESTED and m2.state.maintenance.maintainer == "anna@workshop"
    m2.tick()
    assert m2.state.maintenance.state == model.M_REQUESTED  # nothing heard since the restart
    m2.store.close()
    to = make(tmp_path / "b", clock, gate)
    to_maintenance(to, master(to), clock)
    to.store.close()
    again = make(tmp_path / "b", clock, gate)
    assert again.state.maintenance.state == model.M_MAINTENANCE  # a restart never leaves maintenance


def test_a_store_from_before_maintenance_loads_as_inactive(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    master(m)
    m.store.close()
    store = Store(tmp_path / "authority.sqlite")
    d = json.loads(dict(store.db.execute("SELECT k, v FROM meta").fetchall())["state"])
    del d["maintenance"]
    old = _canonical(d)
    store.db.executemany("UPDATE meta SET v=? WHERE k=?", [(old, "state"), (_digest(old), "state_hash")])
    assert str(SCHEMA_VERSION) == dict(store.db.execute("SELECT k, v FROM meta").fetchall())["schema"]
    st = store.load()
    assert st.maintenance == model.Maintenance() and st.overall.assignee == "ted@ROC-1"


def test_an_unknown_stored_maintenance_state_is_untrusted(tmp_path, clock, gate):
    from control_authority.store import UntrustedStore

    m = make(tmp_path, clock, gate)
    m.store.close()
    store = Store(tmp_path / "authority.sqlite")
    d = json.loads(dict(store.db.execute("SELECT k, v FROM meta").fetchall())["state"])
    d["maintenance"]["state"] = "armed"
    bad = _canonical(d)
    store.db.executemany("UPDATE meta SET v=? WHERE k=?", [(bad, "state"), (_digest(bad), "state_hash")])
    with pytest.raises(UntrustedStore):
        store.load()


# ---------------------------------------------------------------------------- policy


@pytest.mark.parametrize("maint,why", [
    ({"required_checks": []}, "at least one check"),
    ({"rest": {"keel": "hold"}}, "does not have"),
    ({"rest": {"rudder": "neutral"}}, "zero or hold"),
])
def test_the_policy_refuses_a_maintenance_section_that_would_fail_open(maint, why):
    with pytest.raises(ValueError, match=why):
        parse({**BASE, "maintenance": maint})


def test_the_default_policy_requires_a_self_test_and_a_valid_configuration():
    assert parse(BASE).maintenance.required_checks == ("self_test", "configuration_valid")
