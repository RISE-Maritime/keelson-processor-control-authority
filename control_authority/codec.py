"""The model's records as keelson's protobufs (PR #331): what goes to the gate, what comes back, and
what the manager publishes. The only module that imports keelson's types, so model.py stays pure."""

from __future__ import annotations

from google.protobuf.timestamp_pb2 import Timestamp
from keelson.payloads.CommandAuthority_pb2 import (
    Assignment as PbAssignment,
)
from keelson.payloads.CommandAuthority_pb2 import (
    CommandAssignment,
    CommandAuthority,
    RecordKind,
    Role,
)
from keelson.payloads.CommandAuthority_pb2 import (
    Succession as PbSuccession,
)
from keelson.payloads.ControlMapping_pb2 import ControlMapping, TransferAck, TransferCommit, TransferKind, TransferPrepare
from keelson.payloads.Fence_pb2 import Fence as PbFence
from keelson.payloads.NavigationAuthority_pb2 import FunctionAuthority, FunctionEnvelope, NavigationAuthority, NavigationFunction

from . import model
from .model import Assignment, Fence, Mapping, State, Transfer
from .service import GateAck

KIND = {
    model.GRANT: TransferKind.TRANSFER_KIND_GRANT,
    model.TAKE_CONTROL: TransferKind.TRANSFER_KIND_TAKE_CONTROL,
    model.STATION_HANDOVER: TransferKind.TRANSFER_KIND_STATION_HANDOVER,
    model.ABORT_AUTOMATION: TransferKind.TRANSFER_KIND_ABORT_AUTOMATION,
}
ROLE = {
    model.CONN: Role.ROLE_CONN,
    model.OVERALL: Role.ROLE_OVERALL_COMMAND,
    model.NAVIGATIONAL: Role.ROLE_NAVIGATIONAL_COMMAND,
    model.ENGINEER: Role.ROLE_ENGINEER,
    model.PAYLOAD: Role.ROLE_PAYLOAD_AUTHORITY,
}


def fence_to_proto(f: Fence | None) -> PbFence | None:
    return None if f is None else PbFence(term=f.term, generation=f.generation)


def fence_from_proto(p) -> Fence | None:
    return None if p is None or not p.term else Fence(p.term, int(p.generation))


def mapping_to_proto(m: Mapping, fence: Fence | None = None) -> ControlMapping:
    pb = ControlMapping(mapping_id=m.mapping_id, dof=m.dof, authority_holder=m.authority_holder,
                        authority_lease_id=m.authority_lease_id, execution_source=m.execution_source, group_id=m.group_id)
    if m.envelope:
        lo, hi, rate = m.envelope
        pb.execution_envelope.min_pct, pb.execution_envelope.max_pct, pb.execution_envelope.max_rate_pct_per_s = lo, hi, rate
    if fence is not None:
        pb.fence.CopyFrom(fence_to_proto(fence))
    return pb


def mapping_from_proto(pb: ControlMapping) -> Mapping:
    env = ()
    if pb.HasField("execution_envelope"):
        e = pb.execution_envelope
        env = (e.min_pct, e.max_pct, e.max_rate_pct_per_s)
    return Mapping(pb.dof, pb.authority_holder, pb.authority_lease_id, pb.execution_source, env, pb.group_id, pb.mapping_id)


def transfer_prepare(t: Transfer, prepare_ttl_ms: int, vessel_id: str) -> TransferPrepare:
    """No fence anywhere in it: the gate mints one (I2)."""
    return TransferPrepare(transfer_id=t.transfer_id, vessel_id=vessel_id, kind=KIND[t.kind],
                           mappings=[mapping_to_proto(m) for m in t.mappings], active_control_station=t.station,
                           prepare_ttl_ms=prepare_ttl_ms, requested_by=t.requested_by, reason=t.reason)


def ack_from_proto(pb: TransferAck) -> GateAck:
    fence = fence_from_proto(pb.proposed_fence) if pb.HasField("proposed_fence") else None
    return GateAck(pb.accepted, fence, pb.reason, tuple(pb.rejected_mapping_ids))


def transfer_commit(transfer_id: str, fence: Fence) -> TransferCommit:
    return TransferCommit(transfer_id=transfer_id, fence=fence_to_proto(fence))


def _assignment_to_proto(a: Assignment | None) -> PbAssignment | None:
    if a is None:
        return None
    pb = PbAssignment(assignee=a.assignee, assignment_id=a.assignment_id, revision=a.revision, synthesised=a.synthesised)
    if a.succession is not None:
        s = a.succession
        pb.succession.CopyFrom(PbSuccession(previous_assignee=s.previous_assignee, reason=s.reason,
                                            evidence=list(s.evidence), authorised_by=list(s.authorised_by)))
    return pb


def slot_path(vessel_id: str, slot: str) -> str:
    """The `command_authority` key's template: `{vessel}` for the conn, `{vessel}/{slot}` otherwise
    (`{vessel}/engineer`, `{vessel}/payload/{id}`, `{vessel}/overall/session`, `{vessel}/navigation`)."""
    return vessel_id if slot == model.CONN else f"{vessel_id}/{slot}"


def lease_record(st: State, slot: str, issuer_id: str, released: bool = False, now: Timestamp | None = None,
                 lease=None) -> CommandAuthority:
    """The `command_authority` record for one slot, as the manager publishes it. A conn record carries
    the fence the gate minted for the map in force -- never one of the manager's own. A released
    lease is no longer in the state, so its identity is passed in."""
    role_name = slot.split("/")[0]
    rec = CommandAuthority(vessel_id=st.vessel_id, role=ROLE[role_name], issued_by=issuer_id, released=released,
                           active_control_station=st.active_station, responsible_roc=st.responsible_roc)
    if slot.startswith("payload/"):
        rec.payload_id = slot.split("/", 1)[1]
    lease = lease if lease is not None else st.leases.get(slot)
    if lease is not None:
        rec.controller_id, rec.controller_site, rec.token = lease.holder, lease.site, lease.token
        rec.lease_ttl_seconds = max(1, lease.ttl_ms // 1000)
        rec.kind = RecordKind.RECORD_KIND_SESSION if slot.endswith("/session") else RecordKind.RECORD_KIND_LEASE
    if slot.endswith("/session"):
        a = st.assignment(role_name)
        if a is not None:
            rec.assignment.CopyFrom(_assignment_to_proto(a))
    if role_name == model.CONN and st.in_force.fence is not None and not released:
        rec.fence.CopyFrom(fence_to_proto(st.in_force.fence))
    if now is not None:
        rec.last_heartbeat.CopyFrom(now)
    return rec


def assignment_feed(st: State, issuer_id: str, heartbeat_interval_seconds: int, feed_ttl_seconds: int,
                    now: Timestamp | None = None) -> CommandAssignment:
    """`command_assignment/{vessel}`: the manager is its one writer."""
    feed = CommandAssignment(vessel_id=st.vessel_id, issuer_id=issuer_id,
                             heartbeat_interval_seconds=heartbeat_interval_seconds, feed_ttl_seconds=feed_ttl_seconds)
    if st.overall is not None:
        feed.overall.CopyFrom(_assignment_to_proto(st.overall))
    if st.navigational is not None:
        feed.navigational.CopyFrom(_assignment_to_proto(st.navigational))
    if now is not None:
        feed.issued_at.CopyFrom(now)
    return feed


def navigation_document(st: State) -> NavigationAuthority:
    doc = NavigationAuthority(assignment_id=st.navigational.assignment_id if st.navigational else "",
                              revision=st.navigation_revision)
    for name, fn in sorted(st.navigation.items()):
        fa = FunctionAuthority(function=NavigationFunction.Value(f"NAVIGATION_FUNCTION_{name.upper()}"),
                               holder=fn.holder, holder_lease_id=fn.holder_lease_id, control_method=fn.method)
        if fn.envelope:
            fa.envelope.CopyFrom(FunctionEnvelope(**fn.envelope))
        doc.functions.append(fa)
    return doc


def assignment_record(st: State, role: str, issuer_id: str) -> CommandAuthority:
    """The stored assignment record at `{vessel}/overall` or `{vessel}/navigational`: no TTL."""
    rec = CommandAuthority(vessel_id=st.vessel_id, role=ROLE[role], kind=RecordKind.RECORD_KIND_ASSIGNMENT,
                           issued_by=issuer_id, lease_ttl_seconds=0, active_control_station=st.active_station,
                           responsible_roc=st.responsible_roc)
    a = st.assignment(role)
    if a is None:
        rec.released = True
    else:
        rec.controller_id = a.assignee
        rec.assignment.CopyFrom(_assignment_to_proto(a))
    return rec


def navigation_record(st: State, issuer_id: str) -> CommandAuthority:
    """The navigation authority document at `{vessel}/navigation`: an assignment, not a lease."""
    rec = CommandAuthority(vessel_id=st.vessel_id, role=Role.ROLE_NAVIGATION_AUTHORITY,
                           kind=RecordKind.RECORD_KIND_ASSIGNMENT, issued_by=issuer_id, lease_ttl_seconds=0)
    rec.navigation_authority.CopyFrom(navigation_document(st))
    return rec


def function_name(value: int) -> str:
    """keelson.NavigationFunction → the manager's key, e.g. NAVIGATION_FUNCTION_TRACK_KEEPING → track_keeping."""
    return NavigationFunction.Name(value).removeprefix("NAVIGATION_FUNCTION_").lower()


def nav_function_from_proto(fa: FunctionAuthority):
    from google.protobuf.json_format import MessageToDict

    env = MessageToDict(fa.envelope, preserving_proto_field_name=True) if fa.HasField("envelope") else {}
    return model.NavFunction(fa.holder, fa.holder_lease_id, int(fa.control_method), env)


# ---------------------------------------------------------------------------- maintenance
# MaintenanceState is keelson's maintenance PR (stacked on #361): imported where used, like the
# interface types, so the rest of the manager still imports on a build without it.


def _maintenance_pb():
    from keelson.payloads.MaintenanceState_pb2 import MaintenanceState

    return MaintenanceState


def maintenance_record(st: State, guards, manager_id: str, now: Timestamp | None = None):
    MaintenanceState = _maintenance_pb()
    m = st.maintenance
    out = MaintenanceState(
        vessel_id=st.vessel_id, state=MaintenanceState.State.Value(f"STATE_{m.state.upper()}"),
        maintenance_id=m.maintenance_id, revision=m.revision, requested_by=m.requested_by,
        overall_assignment_id=m.overall_assignment_id, overall_assignment_revision=m.overall_assignment_revision,
        maintainer=m.maintainer, reason=m.reason,
        scopes=[MaintenanceState.Scope.Value(f"SCOPE_{s.upper()}") for s in m.scopes],
        guards=[MaintenanceState.Guard(name=g.name, satisfied=g.satisfied, detail=g.detail) for g in guards],
        checks=[MaintenanceState.CheckResult(name=c.name, passed=c.passed, detail=c.detail, evidence=c.evidence)
                for c in m.checks],
        manager_id=manager_id,
    )
    if m.issued_wall_ms:
        out.issued_at.FromMilliseconds(m.issued_wall_ms)
    if m.since_wall_ms:
        out.since.FromMilliseconds(m.since_wall_ms)
    if now is not None:
        out.timestamp.CopyFrom(now)
    return out


def scopes_from_proto(values) -> tuple[str, ...]:
    """Scope enums as the model's names. UNSPECIFIED, or a number this build does not know, keeps a
    name the model refuses as unknown -- never a silent drop that would narrow the request."""
    MaintenanceState = _maintenance_pb()
    names = []
    for v in values:
        try:
            names.append(MaintenanceState.Scope.Name(v).removeprefix("SCOPE_").lower())
        except ValueError:
            names.append(f"scope_{v}")
    return tuple(names)


def checks_from_proto(pbs) -> tuple[model.Check, ...]:
    return tuple(model.Check(c.name, bool(c.passed), c.detail, c.evidence) for c in pbs)


def resolved_from_proto(rc) -> dict:
    """`resolved_control`'s DOFs as the guard reads them. Absent stays absent: no actual is None,
    never 0."""
    from keelson.payloads.ResolvedControl_pb2 import ControlState, FeedbackQuality

    return {
        d.dof: model.DofReading(
            actual_pct=d.actual_pct if d.HasField("actual_pct") else None,
            feedback_quality=FeedbackQuality.Name(d.feedback_quality).removeprefix("FEEDBACK_QUALITY_"),
            actual_age_ms=d.actual_age_ms if d.HasField("actual_age_ms") else None,
            control_state=ControlState.Name(d.control_state).removeprefix("CONTROL_STATE_"),
        )
        for d in rc.dofs
    }


def authority_state(st: State, issuer_id: str, heartbeat_interval_seconds: int, feed_ttl_seconds: int, guards=()):
    from keelson.interfaces.AuthorityManager_pb2 import AuthorityState

    out = AuthorityState(vessel_id=st.vessel_id, active_control_station=st.active_station, reconciled=st.reconciled,
                         records=[lease_record(st, slot, issuer_id) for slot in sorted(st.leases)],
                         mappings=[mapping_to_proto(m, st.in_force.fence) for m in st.in_force.mappings])
    out.assignments.CopyFrom(assignment_feed(st, issuer_id, heartbeat_interval_seconds, feed_ttl_seconds))
    out.navigation.CopyFrom(navigation_document(st))
    if st.in_force.fence is not None:
        out.fence.CopyFrom(fence_to_proto(st.in_force.fence))
    out.maintenance.CopyFrom(maintenance_record(st, guards, issuer_id))
    return out


def audit_record(seq: int, at_wall_ms: int, rec: dict):
    """One stored audit dict as `AuditRecord`: the common fields typed, the rest as JSON."""
    import json

    from keelson.interfaces.AuthorityManager_pb2 import AuditRecord

    def rows(key):
        return [ControlMapping(dof=r["dof"], authority_holder=r["authority_holder"], execution_source=r["execution_source"])
                for r in rec.get(key) or []]

    typed = {"event", "transfer_id", "by", "station", "old_map", "new_map", "old_fence", "fence", "reason"}
    out = AuditRecord(seq=seq, event=rec.get("event", ""), transfer_id=rec.get("transfer_id", ""),
                      requested_by=rec.get("by", ""), station=rec.get("station", ""), reason=rec.get("reason", ""),
                      old_mappings=rows("old_map"), new_mappings=rows("new_map"),
                      detail_json=json.dumps({k: v for k, v in rec.items() if k not in typed}, sort_keys=True))
    out.at.FromMilliseconds(at_wall_ms)
    for key, field_name in (("old_fence", "old_fence"), ("fence", "new_fence")):
        f = rec.get(key)
        if f:
            getattr(out, field_name).CopyFrom(PbFence(term=f["term"], generation=f["generation"]))
    return out
