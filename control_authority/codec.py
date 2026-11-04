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


def lease_record(st: State, slot: str, issuer_id: str, released: bool = False, now: Timestamp | None = None) -> CommandAuthority:
    """The `command_authority` record for one slot, as the manager publishes it. A conn record carries
    the fence the gate minted for the map in force -- never one of the manager's own."""
    role_name = slot.split("/")[0]
    rec = CommandAuthority(vessel_id=st.vessel_id, role=ROLE[role_name], issued_by=issuer_id, released=released,
                           active_control_station=st.active_station, responsible_roc=st.responsible_roc)
    if slot.startswith("payload/"):
        rec.payload_id = slot.split("/", 1)[1]
    lease = st.leases.get(slot)
    if lease is not None:
        rec.controller_id, rec.controller_site, rec.token = lease.holder, lease.site, lease.token
        rec.lease_ttl_seconds = max(1, lease.ttl_ms // 1000)
        rec.kind = RecordKind.RECORD_KIND_SESSION if slot.endswith("/session") else RecordKind.RECORD_KIND_LEASE
    if role_name == model.CONN and st.in_force.fence is not None:
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
