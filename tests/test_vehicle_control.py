"""MIGRATION (crowsnest-dev#269): the committed map compiled into the vessel's vehicle_control/v1. The
target is derived from committed state; a map that does not compile whole is cleared, never partly
installed; with no port nothing is called. No network."""

from dataclasses import replace

import pytest

from control_authority import model, vehicle_control
from control_authority.model import Mapping, TransferRequest
from control_authority.policy import parse
from control_authority.service import Manager, VehicleControlUnreachable
from control_authority.store import Store
from fakegate import FakeGate

V = "usv-alpha"
DOFS = ["rudder", "throttle"]
NAV = "navigation/0@usv-alpha"


def table(station):
    return [{"execution_source": f"{station}/helm-1/rudder", "dof": "rudder", "entity_id": station,
             "subject": "wheel_position_pct", "source_id": "helm-1"},
            {"execution_source": f"{station}/helm-1/throttle", "dof": "throttle", "entity_id": station,
             "subject": "lever_position_pct", "source_id": "helm-1", "invert": True}]


VC = {
    "responder": "mavlink",
    "axes": {"rudder": "steering", "throttle": "throttle"},
    "sources": [*table("ROC-1"), *table("ROC-2"),
                {"execution_source": "navigation/0", "dof": "rudder", "subject": "rudder_angle_pct", "source_id": "navigation/0"},
                {"execution_source": "navigation/0", "dof": "throttle", "subject": "throttle_pct", "source_id": "navigation/0"}],
    "max_axis_age_s": 0.5,
}
BASE = {"command_profile": "test", "dofs": DOFS, "automation_sources": ["navigation/0"], "processes": [NAV],
        "succession": {"overall": ["chief@ROC-2"], "navigational": "*"}}
POLICY = parse({**BASE, "vehicle_control": VC})


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


class FakeVehicleControl:
    """The vessel's vehicle_control/v1, as far as the manager can see it: what it was last told."""

    def __init__(self):
        self.calls: list = []
        self.down = False

    def set(self, target):
        if self.down:
            raise VehicleControlUnreachable("no answer")
        self.calls.append(target)

    @property
    def axes(self):
        return {axis: (s.entity_id, s.subject, s.source_id) for axis, s in self.calls[-1].axes}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gate(clock):
    return FakeGate(clock)


@pytest.fixture
def vessel():
    return FakeVehicleControl()


def make(tmp_path, clock, gate, vessel, policy=POLICY, bus=None):
    m = Manager(store=Store(tmp_path / "authority.sqlite"), policy=policy, vessel_id=V, gate=gate,
                publish=bus if bus is not None else Bus(), now_ms=clock, vehicle=vessel)
    m.boot()
    return m


def human_map(station, envelope=()):
    return tuple(Mapping(d, "", "", f"{station}/helm-1/{d}", envelope) for d in DOFS)


def take_conn(m, who="ted@ROC-1", site="ROC-1", **kw):
    return m.transfer(TransferRequest(model.GRANT, who, site, mappings=human_map(site, **kw)))


def master(m, who="ted@ROC-1", site="ROC-1", role=model.OVERALL, token="sess-1"):
    if role == model.OVERALL:
        m.assign(model.OVERALL, who, "procedure")
    else:
        m.assign(model.NAVIGATIONAL, who, m.state.overall.assignee, m.state.overall.revision, "sess-1")
    a = m.state.assignment(role)
    assert m.session(role, who, site, token, a.assignment_id).outcome == model.ACCEPTED
    return a


ROC1 = {"steering": ("ROC-1", "wheel_position_pct", "helm-1"), "throttle": ("ROC-1", "lever_position_pct", "helm-1")}
ROC2 = {"steering": ("ROC-2", "wheel_position_pct", "helm-1"), "throttle": ("ROC-2", "lever_position_pct", "helm-1")}


def audits(m, event):
    return [a for a in m.history() if a.get("event") == event]


# ---------------------------------------------------------------------------- the policy table


@pytest.mark.parametrize("vc, why", [
    ({"axes": {}}, "responder"),
    ({**VC, "axes": {"keel": "steering"}}, "does not have"),
    ({**VC, "axes": {"rudder": "steering", "throttle": "steering"}}, "two DOFs"),
    ({**VC, "sources": [{"execution_source": "ROC-1/x", "dof": "keel", "subject": "s"}]}, "DOF of this vessel"),
    ({**VC, "sources": [{"execution_source": "ROC-1/x", "dof": "rudder"}]}, "no subject"),
    ({**VC, "sources": [VC["sources"][0], VC["sources"][0]]}, "twice"),
])
def test_a_bad_table_is_refused_at_load(vc, why):
    with pytest.raises(ValueError, match=why):
        parse({**BASE, "vehicle_control": vc})


def test_a_port_without_the_table_refuses_to_construct(tmp_path, clock, gate, vessel):
    with pytest.raises(ValueError, match="vehicle_control table"):
        Manager(store=Store(tmp_path / "a.sqlite"), policy=parse(BASE), vessel_id=V, gate=gate, publish=Bus(),
                now_ms=clock, vehicle=vessel)


# ---------------------------------------------------------------------------- the target, pure


def _state_with(rows, lease_id="L1", renewed_ms=1_000, ttl_ms=2_000):
    conn = model.Lease(model.CONN, "ted@ROC-1", "ROC-1", lease_id, lease_id, renewed_ms, ttl_ms)
    rows = tuple(replace(r, authority_holder="ted@ROC-1", authority_lease_id="L1") for r in rows)
    return model.State(V, leases={model.CONN: conn}, in_force=model.InForce(rows, "ROC-1", model.Fence("T", 2)),
                       reconciled=True)


def test_the_target_is_exactly_the_rows_a_live_conn_holds():
    t = vehicle_control.target(_state_with(human_map("ROC-1")), POLICY.vehicle_control, 1_500)
    assert t.problems == () and {a for a, _ in t.axes} == {"steering", "throttle"}
    assert dict(t.axes)["throttle"].invert is True
    pb = vehicle_control.to_proto(t, POLICY.vehicle_control)
    assert set(pb.axes) == {"steering", "throttle"} and pb.axes["steering"].entity_id == "ROC-1"
    assert pb.max_axis_age_s == pytest.approx(0.5)


@pytest.mark.parametrize("state", [
    _state_with(human_map("ROC-1"), renewed_ms=-5_000),  # the conn lapsed
    _state_with(human_map("ROC-1"), lease_id="L-older"),  # the rows name another lease
    replace(_state_with(human_map("ROC-1")), leases={}),  # nobody holds the conn
    replace(_state_with(()), in_force=model.InForce()),  # no map in force
])
def test_no_live_conn_holding_the_rows_is_an_empty_target(state):
    assert vehicle_control.target(state, POLICY.vehicle_control, 1_500) == vehicle_control.EMPTY


@pytest.mark.parametrize("rows, why", [
    (human_map("ROC-3"), "no vehicle_control source for ROC-3/helm-1/rudder"),
    (human_map("ROC-1", envelope=(-30.0, 30.0, 0.0)), "narrower"),
    (human_map("ROC-1", envelope=(-100.0, 100.0, 20.0)), "narrower"),
])
def test_a_map_that_does_not_compile_whole_is_emptied_and_named(rows, why):
    t = vehicle_control.target(_state_with(rows), POLICY.vehicle_control, 1_500)
    assert t.axes == () and any(why in p for p in t.problems)


def test_one_row_without_an_axis_empties_the_whole_target():
    vcp = replace(POLICY.vehicle_control, axes={"rudder": "steering"})
    t = vehicle_control.target(_state_with(human_map("ROC-1")), vcp, 1_500)
    assert t.axes == () and t.problems == ("throttle: the vessel has no vehicle_control axis for it",)


def test_full_travel_and_no_rate_limit_compile():
    t = vehicle_control.target(_state_with(human_map("ROC-1", envelope=(-100.0, 100.0, 0.0))), POLICY.vehicle_control, 1_500)
    assert t.problems == () and len(t.axes) == 2


# ---------------------------------------------------------------------------- the manager drives it


def test_a_take_installs_exactly_the_committed_rows_and_a_release_clears_them(tmp_path, clock, gate, vessel):
    m = make(tmp_path, clock, gate, vessel)
    assert vessel.calls == [vehicle_control.EMPTY]  # boot, reconciled, nobody holds the conn: cleared
    assert take_conn(m).outcome == model.ACCEPTED
    assert vessel.axes == ROC1
    conn = m.state.leases[model.CONN]
    assert m.renew(model.CONN, conn.token).outcome == model.ACCEPTED
    assert len(vessel.calls) == 2  # a renewal changes nothing on the vessel
    assert m.release(model.CONN, conn.token).outcome == model.ACCEPTED
    assert vessel.calls[-1] == vehicle_control.EMPTY
    assert [a["event"] for a in m.history() if a["event"].startswith("vehicle_control")] == [
        "vehicle_control_cleared", "vehicle_control_installed", "vehicle_control_cleared"]


def test_a_lapsed_conn_clears_the_vessel(tmp_path, clock, gate, vessel):
    m = make(tmp_path, clock, gate, vessel)
    take_conn(m)
    clock.t += POLICY.ttl(model.CONN) + 1
    m.tick()
    assert vessel.calls[-1] == vehicle_control.EMPTY


def test_a_grant_between_humans_replaces_the_rows_in_one_call(tmp_path, clock, gate, vessel):
    m = make(tmp_path, clock, gate, vessel)
    take_conn(m)
    conn = m.state.leases[model.CONN]
    # Within the active station: the grant hands the conn over, the rows move to the new holder's lease.
    d = m.transfer(TransferRequest(model.GRANT, conn.holder, "ROC-1", conn_token=conn.token, new_holder="bo@ROC-1",
                                   new_holder_site="ROC-1", mappings=human_map("ROC-1")))
    assert d.outcome == model.ACCEPTED, d.detail
    assert len(vessel.calls) == 2  # same sources, same target: nothing to change on the vessel
    assert vessel.axes == ROC1


def test_a_handover_and_take_control_install_the_new_station_and_abort_clears(tmp_path, clock, gate, vessel):
    m = make(tmp_path, clock, gate, vessel)
    a = master(m)
    nav = master(m, "anna@ROC-2", "ROC-2", model.NAVIGATIONAL, token="sess-2")
    take_conn(m)
    d = m.transfer(TransferRequest(model.STATION_HANDOVER, "ted@ROC-1", "ROC-1", assignment_revision=a.revision,
                                   session_token="sess-1", station="ROC-2", new_holder="anna@ROC-2",
                                   new_holder_site="ROC-2", mappings=human_map("ROC-2")))
    assert d.outcome == model.ACCEPTED, d.detail
    assert vessel.axes == ROC2
    # The navigator takes the conn: its own rows.
    conn = m.state.leases[model.CONN]
    auto = tuple(Mapping(dof, "", "", "navigation/0") for dof in DOFS)
    d = m.transfer(TransferRequest(model.GRANT, conn.holder, conn.site, conn_token=conn.token, new_holder=NAV,
                                   new_holder_site="usv-alpha", mappings=auto))
    assert d.outcome == model.ACCEPTED, d.detail
    assert vessel.axes == {"steering": ("", "rudder_angle_pct", "navigation/0"), "throttle": ("", "throttle_pct", "navigation/0")}
    # Take control back at ROC-2.
    d = m.transfer(TransferRequest(model.TAKE_CONTROL, "anna@ROC-2", "ROC-2", assignment_revision=nav.revision,
                                   session_token="sess-2", mappings=human_map("ROC-2")))
    assert d.outcome == model.ACCEPTED, d.detail
    assert vessel.axes == ROC2
    # The navigator again, then an abort: nobody drives.
    conn = m.state.leases[model.CONN]
    m.transfer(TransferRequest(model.GRANT, conn.holder, conn.site, conn_token=conn.token, new_holder=NAV,
                               new_holder_site="usv-alpha", mappings=auto))
    d = m.transfer(TransferRequest(model.ABORT_AUTOMATION, "anna@ROC-2", "ROC-2", assignment_revision=nav.revision,
                                   session_token="sess-2"))
    assert d.outcome == model.ACCEPTED, d.detail
    assert vessel.calls[-1] == vehicle_control.EMPTY


def test_a_refused_transfer_changes_nothing_on_the_vessel(tmp_path, clock, gate, vessel):
    m = make(tmp_path, clock, gate, vessel)
    take_conn(m)
    gate.unready = {"rudder"}
    conn = m.state.leases[model.CONN]
    d = m.transfer(TransferRequest(model.GRANT, conn.holder, "ROC-1", conn_token=conn.token, new_holder="bo@ROC-1",
                                   new_holder_site="ROC-1", mappings=human_map("ROC-1")))
    assert d.outcome == model.REFUSED_BY_GATE
    assert len(vessel.calls) == 2 and vessel.axes == ROC1


def test_an_uncompilable_map_is_cleared_and_alarms_once(tmp_path, clock, gate, vessel):
    bus = Bus()
    m = make(tmp_path, clock, gate, vessel, bus=bus)
    assert take_conn(m, envelope=(-30.0, 30.0, 0.0)).outcome == model.ACCEPTED  # the gate takes it; the vessel cannot
    assert vessel.calls == [vehicle_control.EMPTY]  # already cleared at boot: nothing installed
    assert sum("does not compile" in a for a in bus.alarms()) == 1
    m.tick()
    m.tick()
    assert sum("does not compile" in a for a in bus.alarms()) == 1
    assert len(audits(m, "vehicle_control_uncompilable")) == 1


def test_a_vessel_that_does_not_answer_alarms_once_and_tick_retries(tmp_path, clock, gate, vessel):
    bus = Bus()
    m = make(tmp_path, clock, gate, vessel, bus=bus)
    vessel.down = True
    take_conn(m)
    conn = m.state.leases[model.CONN]
    m.renew(model.CONN, conn.token)  # not a tick: no retry, and no second alarm
    m.tick()
    m.tick()
    assert sum("did not take the mapping" in a for a in bus.alarms()) == 1
    assert len(audits(m, "vehicle_control_failed")) == 1
    vessel.down = False
    m.tick()
    assert vessel.axes == ROC1
    assert audits(m, "vehicle_control_installed")


def test_the_install_follows_the_commit_never_precedes_it(tmp_path, clock, gate, vessel):
    m = make(tmp_path, clock, gate, vessel)
    seen = []
    vessel.set = lambda t: seen.append((t, Store(tmp_path / "authority.sqlite").load().in_force.mappings))
    take_conn(m)
    t, stored = seen[-1]
    assert t.axes and {r.dof for r in stored} == set(DOFS)


def test_without_the_port_nothing_is_called(tmp_path, clock, gate):
    m = Manager(store=Store(tmp_path / "authority.sqlite"), policy=POLICY, vessel_id=V, gate=gate, publish=Bus(),
                now_ms=clock)
    m.boot()
    take_conn(m)
    m.tick()
    assert m.vehicle is None and not audits(m, "vehicle_control_installed") and not audits(m, "vehicle_control_cleared")


def test_a_restart_reinstalls_the_map_a_live_conn_holds(tmp_path, clock, gate, vessel):
    m = make(tmp_path, clock, gate, vessel)
    take_conn(m)
    m.store.close()
    again = FakeVehicleControl()
    make(tmp_path, clock, gate, again)
    assert again.axes == ROC1


def test_a_restart_with_the_gate_silent_installs_nothing_but_still_clears_on_lapse(tmp_path, clock, gate, vessel):
    m = make(tmp_path, clock, gate, vessel)
    take_conn(m)
    m.store.close()
    gate.down = True
    again = FakeVehicleControl()
    m2 = make(tmp_path, clock, gate, again)
    m2.tick()
    assert again.calls == []  # unreconciled: never an install
    clock.t += 3 * POLICY.ttl(model.CONN)
    m2.tick()
    assert again.calls == [vehicle_control.EMPTY]


def test_the_example_table_loads_and_names_every_dof():
    from pathlib import Path

    from control_authority.policy import load

    p = load(Path(__file__).resolve().parent.parent / "config/policy.vehicle-control.example.json")
    assert set(p.vehicle_control.axes) == set(p.dofs)
