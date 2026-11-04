"""`authority_manager/v1`: each procedure's request protobuf → one Manager call → its reply protobuf.
No Zenoh here (bin/ serves these), so every procedure is tested without a network.

ONE LOCK DECIDES. zenoh-python runs each procedure's queryable on its own thread; every handler takes
the same lock for the whole call -- a transfer's gate round trips included -- so the manager decides
one request at a time, in arrival order, exactly as service.py requires. A transfer holds the lock
for at most two gate calls, each bounded by the gate client's timeout.
"""

from __future__ import annotations

import threading

from keelson.interfaces.AuthorityManager_pb2 import (
    AssumeCommandReply,
    AuthorityReply,
    HistoryReply,
    Refusal,
    SetNavigationAuthorityReply,
)
from keelson.interfaces.VehicleCommon_pb2 import CommandResult
from keelson.payloads.CommandAuthority_pb2 import Role

from . import codec, model
from .model import Decision, TransferRequest

REFUSAL = {
    model.REFUSED_HELD: Refusal.REFUSAL_HELD,
    model.REFUSED_UNAUTHORISED: Refusal.REFUSAL_UNAUTHORISED,
    model.REFUSED_STALE_REVISION: Refusal.REFUSAL_STALE_REVISION,
    model.REFUSED_SOFTWARE: Refusal.REFUSAL_SOFTWARE,
    model.REFUSED_NOT_RECONCILED: Refusal.REFUSAL_NOT_RECONCILED,
    model.REFUSED_BUSY: Refusal.REFUSAL_BUSY,
    model.REFUSED_MAP: Refusal.REFUSAL_MAP,
    model.REFUSED_BY_GATE: Refusal.REFUSAL_GATE,
    model.REFUSED_NOT_UNREACHABLE: Refusal.REFUSAL_NOT_UNREACHABLE,
    model.REFUSED_MALFORMED: Refusal.REFUSAL_MALFORMED,
}

LEASE_ROLE = {Role.ROLE_CONN: model.CONN, Role.ROLE_ENGINEER: model.ENGINEER, Role.ROLE_PAYLOAD_AUTHORITY: model.PAYLOAD}
ASSIGNMENT_ROLE = {Role.ROLE_OVERALL_COMMAND: model.OVERALL, Role.ROLE_NAVIGATIONAL_COMMAND: model.NAVIGATIONAL}


def result_of(outcome: str) -> tuple[int, int]:
    if outcome == model.ACCEPTED:
        return CommandResult.COMMAND_RESULT_ACCEPTED, Refusal.REFUSAL_UNSPECIFIED
    return CommandResult.COMMAND_RESULT_DENIED, REFUSAL.get(outcome, Refusal.REFUSAL_UNSPECIFIED)


def _token(caller) -> str:
    return caller.token if caller.HasField("token") else ""


def _revision(caller):
    return caller.assignment_revision if caller.HasField("assignment_revision") else None


def _rows(pbs):
    # Lease ids and fences on a proposal are ignored: the manager fills in the one, the gate mints the other.
    return tuple(model.Mapping(m.dof, "", "", m.execution_source, codec.mapping_from_proto(m).envelope, m.group_id)
                 for m in pbs)


def _denied(refusal: int, detail: str) -> AuthorityReply:
    return AuthorityReply(result=CommandResult.COMMAND_RESULT_DENIED, refusal=refusal, detail=detail)


class Handlers:
    def __init__(self, manager, issuer_id: str, heartbeat_interval_seconds: int, feed_ttl_seconds: int):
        self.m = manager
        self.issuer_id = issuer_id
        self.hb_s = heartbeat_interval_seconds
        self.feed_ttl_s = feed_ttl_seconds
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ replies

    def _reply(self, d: Decision, slot: str | None = None) -> AuthorityReply:
        result, refusal = result_of(d.outcome)
        out = AuthorityReply(result=result, refusal=refusal, detail=d.detail, transfer_id=d.transfer_id,
                             rejected_mapping_ids=list(d.rejected_mapping_ids))
        st = self.m.state
        if slot is not None and slot in st.leases:
            out.record.CopyFrom(codec.lease_record(st, slot, self.issuer_id))
        if d.transfer_id and d.outcome == model.ACCEPTED and st.in_force.fence is not None:
            out.fence.CopyFrom(codec.fence_to_proto(st.in_force.fence))
        return out

    # ------------------------------------------------------------------ procedures

    def request(self, req) -> AuthorityReply:
        c = req.caller
        with self.lock:
            if req.role == Role.ROLE_CONN:
                d = self.m.transfer(TransferRequest(model.GRANT, c.controller_id, c.controller_site, req.reason,
                                                    conn_token=_token(c), mappings=_rows(req.mappings)))
                return self._reply(d, model.CONN)
            if req.role in (Role.ROLE_ENGINEER, Role.ROLE_PAYLOAD_AUTHORITY):
                role = LEASE_ROLE[req.role]
                d = self.m.take(role, c.controller_id, c.controller_site, req.payload_id, _token(c) or None)
                return self._reply(d, model.slot_of(role, req.payload_id))
        return _denied(Refusal.REFUSAL_MALFORMED, "an assignment changes by transfer or assume_command, never by request")

    def renew(self, req) -> AuthorityReply:
        c = req.caller
        with self.lock:
            if req.role in LEASE_ROLE:
                slot = model.slot_of(LEASE_ROLE[req.role], req.payload_id)
                return self._reply(self.m.renew(slot, _token(c)), slot)
            if req.role in ASSIGNMENT_ROLE:
                role = ASSIGNMENT_ROLE[req.role]
                d = self.m.session(role, c.controller_id, c.controller_site, _token(c), c.assignment_id)
                return self._reply(d, model.session_slot(role))
        return _denied(Refusal.REFUSAL_MALFORMED, "not a lease or a session")

    def release(self, req) -> AuthorityReply:
        c = req.caller
        with self.lock:
            if req.role in LEASE_ROLE:
                return self._reply(self.m.release(model.slot_of(LEASE_ROLE[req.role], req.payload_id), _token(c)))
            if req.role in ASSIGNMENT_ROLE:
                return self._reply(self.m.release(model.session_slot(ASSIGNMENT_ROLE[req.role]), _token(c)))
        return _denied(Refusal.REFUSAL_MALFORMED, "not a lease or a session")

    def transfer(self, req) -> AuthorityReply:
        c = req.caller
        with self.lock:
            if req.role == Role.ROLE_CONN:
                if req.HasField("station"):
                    d = self.m.transfer(TransferRequest(model.STATION_HANDOVER, c.controller_id, c.controller_site, req.reason,
                                                        new_holder=req.new_holder, new_holder_site=req.new_holder_site,
                                                        assignment_revision=_revision(c), session_token=_token(c),
                                                        station=req.station, mappings=_rows(req.mappings)))
                else:
                    d = self.m.transfer(TransferRequest(model.GRANT, c.controller_id, c.controller_site, req.reason,
                                                        conn_token=_token(c), new_holder=req.new_holder,
                                                        new_holder_site=req.new_holder_site, mappings=_rows(req.mappings)))
                return self._reply(d, model.CONN)
            if req.role in ASSIGNMENT_ROLE:
                d = self.m.assign(ASSIGNMENT_ROLE[req.role], req.new_holder, c.controller_id, _revision(c), _token(c))
                return self._reply(d)
        # A typed limitation, never silence (serve_rpc's full-interface rule).
        return AuthorityReply(result=CommandResult.COMMAND_RESULT_UNSUPPORTED, refusal=Refusal.REFUSAL_UNSPECIFIED,
                              detail="an ENGINEER or PAYLOAD_AUTHORITY handover needs its gate and the inherited "
                                     "constraint snapshot; not served yet — release and take instead")

    def take_control(self, req) -> AuthorityReply:
        c = req.caller
        with self.lock:
            d = self.m.transfer(TransferRequest(model.TAKE_CONTROL, c.controller_id, c.controller_site, req.reason,
                                                assignment_revision=_revision(c), session_token=_token(c),
                                                mappings=_rows(req.mappings)))
            return self._reply(d, model.CONN)

    def abort_automation(self, req) -> AuthorityReply:
        c = req.caller
        with self.lock:
            d = self.m.transfer(TransferRequest(model.ABORT_AUTOMATION, c.controller_id, c.controller_site, req.reason,
                                                assignment_revision=_revision(c), session_token=_token(c)))
            return self._reply(d)

    def assume_command(self, req) -> AssumeCommandReply:
        c = req.caller
        if req.role not in ASSIGNMENT_ROLE:
            return AssumeCommandReply(result=CommandResult.COMMAND_RESULT_DENIED, refusal=Refusal.REFUSAL_MALFORMED,
                                      detail="succession is for OVERALL or NAVIGATIONAL_COMMAND")
        role = ASSIGNMENT_ROLE[req.role]
        with self.lock:
            d = self.m.assume_command(role, c.controller_id, req.reason, list(req.evidence), list(req.authorised_by))
            result, refusal = result_of(d.outcome)
            out = AssumeCommandReply(result=result, refusal=refusal, detail=d.detail)
            a = self.m.state.assignment(role)
            if d.outcome == model.ACCEPTED and a is not None:
                out.assignment.CopyFrom(codec._assignment_to_proto(a))
            return out

    def set_navigation_authority(self, req) -> SetNavigationAuthorityReply:
        c = req.caller
        fn = codec.nav_function_from_proto(req.authority) if req.HasField("authority") else None
        with self.lock:
            d = self.m.set_navigation_function(codec.function_name(req.function), fn, c.controller_id, _revision(c), _token(c))
            result, refusal = result_of(d.outcome)
            return SetNavigationAuthorityReply(result=result, refusal=refusal, detail=d.detail,
                                               document=codec.navigation_document(self.m.state))

    def get(self, req):
        with self.lock:
            return codec.authority_state(self.m.state, self.issuer_id, self.hb_s, self.feed_ttl_s)

    def history(self, req) -> HistoryReply:
        limit = req.limit or 1000
        with self.lock:
            rows = self.m.store.audit_after(req.after_seq, limit)
        return HistoryReply(records=[codec.audit_record(seq, at, rec) for seq, at, rec in rows])

    # ------------------------------------------------------------------ the periodic work

    def tick(self) -> None:
        with self.lock:
            self.m.tick()

    def heartbeat(self) -> None:
        with self.lock:
            self.m.heartbeat()
