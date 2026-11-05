"""`authority_manager/v1`: each procedure's request protobuf → one Manager call → its reply protobuf.
No Zenoh here (bin/ serves these), so every procedure is tested without a network.

ONE LOCK DECIDES. zenoh-python runs each procedure's queryable on its own thread; every handler takes
the same lock for the whole call -- a transfer's gate round trips included -- so the manager decides
one request at a time, in arrival order, exactly as service.py requires. A transfer holds the lock
for at most two gate calls, each bounded by the gate client's timeout.

ATTESTED FIRST (crowsnest-dev#312). Every procedure that changes anything checks the caller's
attestation under that same lock before the manager decides: a present attestation is always
verified and a bad one always refused (REFUSAL_ATTESTATION, `detail` naming the check), and a
missing one is refused where the policy requires it (operational, by default). An accepted one
stamps its principal on what the call creates and on its audit records.
"""

from __future__ import annotations

import threading

from keelson.interfaces.AuthorityManager_pb2 import (
    AssumeCommandReply,
    AuthorityReply,
    HistoryReply,
    MaintenanceReply,
    Refusal,
    SetNavigationAuthorityReply,
)
from functools import wraps
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
    model.REFUSED_MAINTENANCE: Refusal.REFUSAL_MAINTENANCE,
    model.REFUSED_GUARD: Refusal.REFUSAL_GUARD,
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


def attested(procedure: str, reply_cls):
    """Check the call's attestation first, under the one lock (reentrant, so the handler's own
    `with self.lock` still holds). The refusal is the procedure's own reply type."""

    def deco(fn):
        @wraps(fn)
        def run(self, req):
            with self.lock:
                refused = self._attest(req, procedure, reply_cls)
                if refused is not None:
                    return refused
                try:
                    return fn(self, req)
                finally:
                    self.m.principal = None

        return run

    return deco


class Handlers:
    def __init__(self, manager, issuer_id: str, heartbeat_interval_seconds: int, feed_ttl_seconds: int):
        self.m = manager
        self.issuer_id = issuer_id
        self.hb_s = heartbeat_interval_seconds
        self.feed_ttl_s = feed_ttl_seconds
        self.lock = threading.RLock()

    def _attest(self, req, procedure: str, reply_cls):
        """None when the call may proceed (its principal on the manager), else the refusal."""
        self.m.principal = None
        caller = req.caller
        if caller.HasField("attestation"):
            v = self.m.verify(caller.attestation, req, procedure)
            if not v.ok:
                return reply_cls(result=CommandResult.COMMAND_RESULT_DENIED, refusal=Refusal.REFUSAL_ATTESTATION,
                                 detail=v.outcome)
            self.m.principal = v.principal
            return None
        if self.m.policy.attestation_required:
            return reply_cls(result=CommandResult.COMMAND_RESULT_DENIED, refusal=Refusal.REFUSAL_ATTESTATION,
                             detail="this vessel requires an attestation from a ROC service it trusts")
        return None

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

    @attested("request", AuthorityReply)
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

    @attested("renew", AuthorityReply)
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

    @attested("release", AuthorityReply)
    def release(self, req) -> AuthorityReply:
        c = req.caller
        with self.lock:
            if req.role in LEASE_ROLE:
                return self._reply(self.m.release(model.slot_of(LEASE_ROLE[req.role], req.payload_id), _token(c)))
            if req.role in ASSIGNMENT_ROLE:
                return self._reply(self.m.release(model.session_slot(ASSIGNMENT_ROLE[req.role]), _token(c)))
        return _denied(Refusal.REFUSAL_MALFORMED, "not a lease or a session")

    @attested("transfer", AuthorityReply)
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

    @attested("take_control", AuthorityReply)
    def take_control(self, req) -> AuthorityReply:
        c = req.caller
        with self.lock:
            d = self.m.transfer(TransferRequest(model.TAKE_CONTROL, c.controller_id, c.controller_site, req.reason,
                                                assignment_revision=_revision(c), session_token=_token(c),
                                                mappings=_rows(req.mappings)))
            return self._reply(d, model.CONN)

    @attested("abort_automation", AuthorityReply)
    def abort_automation(self, req) -> AuthorityReply:
        c = req.caller
        with self.lock:
            d = self.m.transfer(TransferRequest(model.ABORT_AUTOMATION, c.controller_id, c.controller_site, req.reason,
                                                assignment_revision=_revision(c), session_token=_token(c)))
            return self._reply(d)

    @attested("assume_command", AssumeCommandReply)
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

    @attested("set_navigation_authority", SetNavigationAuthorityReply)
    def set_navigation_authority(self, req) -> SetNavigationAuthorityReply:
        c = req.caller
        fn = codec.nav_function_from_proto(req.authority) if req.HasField("authority") else None
        with self.lock:
            d = self.m.set_navigation_function(codec.function_name(req.function), fn, c.controller_id, _revision(c), _token(c))
            result, refusal = result_of(d.outcome)
            return SetNavigationAuthorityReply(result=result, refusal=refusal, detail=d.detail,
                                               document=codec.navigation_document(self.m.state))

    # ------------------------------------------------------------------ maintenance (OVERALL_COMMAND only)

    def _maintenance_reply(self, d: Decision) -> MaintenanceReply:
        result, refusal = result_of(d.outcome)
        return MaintenanceReply(result=result, refusal=refusal, detail=d.detail,
                                record=codec.maintenance_record(self.m.state, self.m.guards(), self.issuer_id))

    @attested("request_maintenance", MaintenanceReply)
    def request_maintenance(self, req) -> MaintenanceReply:
        c = req.caller
        with self.lock:
            return self._maintenance_reply(self.m.request_maintenance(
                c.controller_id, _revision(c), _token(c), req.maintainer, req.reason, codec.scopes_from_proto(req.scopes)))

    @attested("cancel_maintenance", MaintenanceReply)
    def cancel_maintenance(self, req) -> MaintenanceReply:
        c = req.caller
        with self.lock:
            return self._maintenance_reply(self.m.cancel_maintenance(c.controller_id, _revision(c), _token(c), req.reason))

    @attested("finish_maintenance", MaintenanceReply)
    def finish_maintenance(self, req) -> MaintenanceReply:
        c = req.caller
        with self.lock:
            return self._maintenance_reply(self.m.finish_maintenance(c.controller_id, _revision(c), _token(c), req.detail))

    @attested("complete_maintenance_check", MaintenanceReply)
    def complete_maintenance_check(self, req) -> MaintenanceReply:
        c = req.caller
        with self.lock:
            return self._maintenance_reply(self.m.complete_maintenance_check(
                c.controller_id, _revision(c), _token(c), codec.checks_from_proto(req.checks)))

    def get(self, req):
        with self.lock:
            return codec.authority_state(self.m.state, self.issuer_id, self.hb_s, self.feed_ttl_s, self.m.guards(),
                                         self.m.challenges.live(self.m.now_ms()))

    def history(self, req) -> HistoryReply:
        limit = req.limit or 1000
        with self.lock:
            rows = self.m.store.audit_after(req.after_seq, limit)
        return HistoryReply(records=[codec.audit_record(seq, at, rec) for seq, at, rec in rows])

    # ------------------------------------------------------------------ the periodic work

    def tick(self) -> None:
        with self.lock:
            self.m.tick()

    # What the maintenance guards read: from the subscriber threads, so under the same lock.

    def observe_armed(self, armed: bool) -> None:
        with self.lock:
            self.m.observe_armed(armed)

    def observe_resolved(self, rc) -> None:
        dofs = codec.resolved_from_proto(rc)
        with self.lock:
            self.m.observe_resolved(dofs)

    def heartbeat(self) -> None:
        with self.lock:
            self.m.heartbeat()
