"""authority_manager/v1 request → reply, against the fake gate. No network."""

import pytest
from keelson.interfaces import AuthorityManager_pb2 as pb
from keelson.interfaces.VehicleCommon_pb2 import CommandResult
from keelson.payloads.CommandAuthority_pb2 import Role
from keelson.payloads.ControlMapping_pb2 import ControlMapping
from keelson.payloads.NavigationAuthority_pb2 import FunctionAuthority, NavigationFunction

from control_authority import model
from control_authority.rpc import Handlers
from control_authority.service import Manager
from control_authority.store import Store
from fakegate import FakeGate
from test_manager import DOFS, POLICY, Bus, Clock

ACCEPTED, DENIED = CommandResult.COMMAND_RESULT_ACCEPTED, CommandResult.COMMAND_RESULT_DENIED


@pytest.fixture
def h(tmp_path):
    clock = Clock()
    gate = FakeGate(clock)
    m = Manager(store=Store(tmp_path / "a.sqlite"), policy=POLICY, vessel_id="usv-alpha", gate=gate, publish=Bus(), now_ms=clock)
    m.boot()
    handlers = Handlers(m, "authority-manager/0", 5, 15)
    handlers.gate, handlers.clock = gate, clock
    return handlers


def rows(station):
    return [ControlMapping(dof=d, execution_source=f"{station}/helm-1/{d}") for d in DOFS]


def caller(who="ted@ROC-1", site="ROC-1", **kw):
    return pb.Caller(controller_id=who, controller_site=site, **kw)


def take_conn(h, who="ted@ROC-1", site="ROC-1"):
    return h.request(pb.RequestRoleRequest(caller=caller(who, site), role=Role.ROLE_CONN, mappings=rows(site)))


def test_taking_the_conn_answers_with_the_record_and_the_gates_fence(h):
    r = take_conn(h)
    assert r.result == ACCEPTED and r.transfer_id
    assert r.record.controller_id == "ted@ROC-1" and r.record.token
    assert (r.fence.term, r.fence.generation) == (h.gate.term, h.gate.generation)
    assert (r.record.fence.term, r.record.fence.generation) == (r.fence.term, r.fence.generation)


def test_a_fence_or_lease_id_on_a_proposal_is_ignored(h):
    forged = rows("ROC-1")
    for row in forged:
        row.fence.term, row.fence.generation, row.authority_lease_id = "ff" * 16, 999, "forged"
    r = h.request(pb.RequestRoleRequest(caller=caller(), role=Role.ROLE_CONN, mappings=forged))
    assert r.result == ACCEPTED and r.fence.generation == h.gate.generation != 999
    assert all(m.authority_lease_id != "forged" for m in h.gate.mappings)


def test_refusals_are_typed(h):
    take_conn(h)
    r = take_conn(h, "anna@ROC-1")
    assert (r.result, r.refusal) == (DENIED, pb.Refusal.REFUSAL_HELD)
    h.gate.unready = {"rudder"}
    tok = h.m.state.leases[model.CONN].token
    r = h.transfer(pb.TransferRoleRequest(caller=caller(token=tok), role=Role.ROLE_CONN, new_holder="anna@ROC-1",
                                          new_holder_site="ROC-1", mappings=rows("ROC-1")))
    assert (r.result, r.refusal) == (DENIED, pb.Refusal.REFUSAL_GATE) and len(r.rejected_mapping_ids) == 1
    r = h.request(pb.RequestRoleRequest(caller=caller(), role=Role.ROLE_OVERALL_COMMAND))
    assert r.refusal == pb.Refusal.REFUSAL_MALFORMED


def test_renew_and_release_of_the_conn(h):
    take_conn(h)
    tok = h.m.state.leases[model.CONN].token
    assert h.renew(pb.RenewRequest(caller=caller(token=tok), role=Role.ROLE_CONN)).result == ACCEPTED
    assert h.release(pb.ReleaseRequest(caller=caller(token="wrong"), role=Role.ROLE_CONN)).refusal == pb.Refusal.REFUSAL_UNAUTHORISED
    assert h.release(pb.ReleaseRequest(caller=caller(token=tok), role=Role.ROLE_CONN)).result == ACCEPTED
    assert model.CONN not in h.m.state.leases


def _master(h):
    h.m.assign(model.OVERALL, "ted@ROC-1", "procedure")
    a = h.m.state.overall
    r = h.renew(pb.RenewRequest(caller=caller(token="s1", assignment_id=a.assignment_id), role=Role.ROLE_OVERALL_COMMAND))
    assert r.result == ACCEPTED and r.record.assignment.assignee == "ted@ROC-1"
    return a


def test_the_master_assigns_navigational_command_by_transfer_and_it_takes_control(h):
    a = _master(h)
    r = h.transfer(pb.TransferRoleRequest(caller=caller(token="s1", assignment_id=a.assignment_id, assignment_revision=a.revision),
                                          role=Role.ROLE_NAVIGATIONAL_COMMAND, new_holder="anna@ROC-2"))
    assert r.result == ACCEPTED, r.detail
    nav = h.m.state.navigational
    h.renew(pb.RenewRequest(caller=caller("anna@ROC-2", "ROC-2", token="s2", assignment_id=nav.assignment_id),
                            role=Role.ROLE_NAVIGATIONAL_COMMAND))
    take_conn(h)
    r = h.take_control(pb.TakeControlRequest(caller=caller("anna@ROC-2", "ROC-2", token="s2", assignment_id=nav.assignment_id,
                                                           assignment_revision=nav.revision), mappings=rows("ROC-2")))
    assert r.result == ACCEPTED and r.record.controller_id == "anna@ROC-2" and h.gate.station == "ROC-2"
    stale = h.take_control(pb.TakeControlRequest(caller=caller("anna@ROC-2", "ROC-2", token="s2", assignment_id=nav.assignment_id,
                                                               assignment_revision=nav.revision - 1), mappings=rows("ROC-2")))
    assert stale.refusal == pb.Refusal.REFUSAL_STALE_REVISION


def test_assume_command_through_the_interface(h):
    _master(h)
    h.clock.t += POLICY.session_ttl_ms + 1
    h.tick()
    r = h.assume_command(pb.AssumeCommandRequest(caller=caller("chief@ROC-2", "ROC-2"), role=Role.ROLE_OVERALL_COMMAND,
                                                 reason="master unreachable", evidence=["no VHF answer"], authorised_by=["owner"]))
    assert r.result == ACCEPTED and r.assignment.assignee == "chief@ROC-2" and r.assignment.succession.previous_assignee == "ted@ROC-1"


def test_navigation_authority_through_the_interface(h):
    a = _master(h)
    fa = FunctionAuthority(function=NavigationFunction.NAVIGATION_FUNCTION_TRACK_KEEPING, holder="navigation/0@usv-alpha",
                           holder_lease_id="L", control_method=4)
    fa.envelope.reroute_allowed = True
    r = h.set_navigation_authority(pb.SetNavigationAuthorityRequest(
        caller=caller(token="s1", assignment_id=a.assignment_id, assignment_revision=a.revision),
        function=NavigationFunction.NAVIGATION_FUNCTION_TRACK_KEEPING, authority=fa))
    assert r.result == ACCEPTED and r.document.revision == 1 and r.document.functions[0].envelope.reroute_allowed
    assert h.m.state.navigation["track_keeping"].envelope == {"reroute_allowed": True}


def test_engineer_handover_is_a_typed_limitation_never_silence(h):
    r = h.transfer(pb.TransferRoleRequest(caller=caller(), role=Role.ROLE_ENGINEER, new_holder="eng@vessel"))
    assert r.result == CommandResult.COMMAND_RESULT_UNSUPPORTED and r.detail


def test_get_and_history(h):
    take_conn(h)
    st = h.get(pb.GetAuthorityRequest())
    assert st.reconciled and st.active_control_station == "ROC-1" and st.fence.generation == h.gate.generation
    assert {m.dof for m in st.mappings} == set(DOFS) and all(m.fence.generation == st.fence.generation for m in st.mappings)
    hist = h.history(pb.HistoryRequest())
    events = [r.event for r in hist.records]
    assert events[:2] == ["boot", "reconciled"] and {"prepared", "committed", "active"} <= set(events)
    active = next(r for r in hist.records if r.event == "active")
    assert active.new_fence.generation == h.gate.generation and active.at.seconds > 0
    later = h.history(pb.HistoryRequest(after_seq=hist.records[-1].seq))
    assert list(later.records) == []


def test_maintenance_through_the_interface(h):
    from keelson.payloads.MaintenanceState_pb2 import MaintenanceState

    a = _master(h)
    c = caller(token="s1", assignment_id=a.assignment_id, assignment_revision=a.revision)
    r = h.request_maintenance(pb.RequestMaintenanceRequest(caller=c, maintainer="anna@workshop", reason="firmware",
                                                           scopes=[MaintenanceState.SCOPE_FIRMWARE]))
    assert r.result == ACCEPTED, r.detail
    assert (r.record.state, r.record.requested_by, list(r.record.scopes)) == (
        MaintenanceState.STATE_REQUESTED, "ted@ROC-1", [MaintenanceState.SCOPE_FIRMWARE])
    assert r.record.HasField("issued_at") and r.record.manager_id == "authority-manager/0"
    assert [(g.name, g.satisfied) for g in r.record.guards][:1] == [("disarmed", False)]
    assert h.get(pb.GetAuthorityRequest()).maintenance.state == MaintenanceState.STATE_REQUESTED
    # Refusals are typed, and the record still comes back.
    again = h.request_maintenance(pb.RequestMaintenanceRequest(caller=c, maintainer="x", reason="y", scopes=[1]))
    assert (again.result, again.refusal) == (DENIED, pb.Refusal.REFUSAL_GUARD)
    assert again.record.state == MaintenanceState.STATE_REQUESTED
    assert h.finish_maintenance(pb.FinishMaintenanceRequest(caller=c)).refusal == pb.Refusal.REFUSAL_GUARD
    assert h.cancel_maintenance(pb.CancelMaintenanceRequest(caller=c, reason="later")).result == ACCEPTED
    assert h.get(pb.GetAuthorityRequest()).maintenance.state == MaintenanceState.STATE_INACTIVE


def test_an_unspecified_or_unknown_scope_is_refused_never_dropped(h):
    a = _master(h)
    c = caller(token="s1", assignment_id=a.assignment_id, assignment_revision=a.revision)
    for scopes in ([0], [1, 99]):
        r = h.request_maintenance(pb.RequestMaintenanceRequest(caller=c, maintainer="anna", reason="x", scopes=scopes))
        assert r.refusal == pb.Refusal.REFUSAL_MALFORMED, scopes


def test_resolved_control_is_read_absent_as_absent(h):
    from keelson.payloads.ResolvedControl_pb2 import ControlState, FeedbackQuality, ResolvedControl, ResolvedDof

    rc = ResolvedControl(dofs=[ResolvedDof(dof="rudder", actual_pct=0.0, actual_age_ms=40,
                                           feedback_quality=FeedbackQuality.FEEDBACK_QUALITY_VALID,
                                           control_state=ControlState.CONTROL_STATE_TRANSITION),
                               ResolvedDof(dof="throttle_port")])
    h.observe_resolved(rc)
    got = h.m.heard.resolved
    assert got["rudder"] == model.DofReading(0.0, "VALID", 40, "TRANSITION")
    assert got["throttle_port"] == model.DofReading(None, "UNSPECIFIED", None, "UNSPECIFIED")
