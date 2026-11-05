"""The manager's sequence: boot, reconcile, requests, ticks. No Zenoh here -- the gate is a port and
publishing is injected, so the whole sequence runs in tests against a fake gate.

ONE THREAD DECIDES. Every request and tick is handled in order by the caller's single loop.

THE ORDER OF ONE DECISION (I13): decide (model.py), then ONE store transaction with the new state and
its audit records, then the effects -- the gate call, the publications, the alarms -- in order. A gate
reply is itself decided and committed before anything follows from it. So:

    begin_transfer  → commit PREPARED              → PREPARE to the gate
    gate ACK        → commit COMMITTED(map, fence) → COMMIT to the gate
    gate commit OK  → commit ACTIVE + the new conn lease → publish

A crash between any two lines leaves a state reconcile() resolves at the next boot.

THE GATE PORT (`GatePort`) is the `control_gate` RPC class of keelson's command_authority protocol:
`prepare`, `commit` and `exposed` (the reconcile read). Its interface is not yet in keelson's
interfaces.yaml (protocol `not_solved`); it arrives with keelson-processor-control-gate (crowsnest-dev
#85). Until then only fakes implement it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from . import attestation, model
from .model import Decision, Fence, Mapping, Policy, State, Transfer, TransferRequest

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateAck:
    accepted: bool
    fence: Fence | None = None
    reason: str = ""
    rejected_mapping_ids: tuple[str, ...] = ()


class GateUnreachable(Exception):
    pass


class GatePort(Protocol):
    def prepare(self, transfer: Transfer, prepare_ttl_ms: int) -> GateAck: ...

    def commit(self, transfer_id: str, fence: Fence) -> tuple[bool, str]: ...

    def exposed(self) -> tuple[Fence | None, tuple[Mapping, ...], str]: ...


def mono_ms() -> int:
    return int(time.monotonic() * 1000)


def wall_ms() -> int:
    """For audit timestamps only -- never for an expiry (§0)."""
    return int(time.time() * 1000)


class Manager:
    def __init__(self, *, store, policy: Policy, vessel_id: str, gate: GatePort,
                 publish: Callable[[object, State], None], now_ms: Callable[[], int] = mono_ms,
                 audit_ms: Callable[[], int] = wall_ms):
        self.store = store
        self.policy = policy
        self.vessel_id = vessel_id
        self.gate = gate
        self._publish = publish
        self.now_ms = now_ms
        self.audit_ms = audit_ms
        self.state: State | None = None
        # What the maintenance guards read, on local receipt. Never persisted (model.Heard).
        self.heard = model.Heard()
        self._published_guards: tuple | None = None
        # Attestation (crowsnest-dev#312): the challenges we issued and the sequences accepted,
        # volatile -- after a restart every attestation from before it is stale. `principal` is who
        # the RPC being decided was attested as, set by rpc.Handlers under the lock.
        self.challenges = attestation.Challenges(policy.challenge_ttl_ms)
        self.seen: dict = {}
        self.principal: dict | None = None

    # ------------------------------------------------------------------ boot

    def boot(self) -> State:
        """Restore (or initialise), rebase every lease onto this process's clock, then reconcile with
        the gate BEFORE accepting any transfer. The manager mints nothing here (§0)."""
        now = self.now_ms()
        stored = self.store.load()  # raises UntrustedStore: never guessed past
        st = stored if stored is not None else State(vessel_id=self.vessel_id)
        if st.vessel_id != self.vessel_id:
            raise ValueError(f"the store is for vessel {st.vessel_id!r}, this manager is for {self.vessel_id!r}")
        st = model.rebase_after_restart(st, now)
        self.store.commit(st, ({"event": "boot", "restored": stored is not None},), self.audit_ms())
        self.state = st
        self.challenges.rotate(now)
        self.reconcile()
        return self.state

    def reconcile(self) -> bool:
        try:
            fence, mappings, station = self.gate.exposed()
        except GateUnreachable as e:
            log.error("the gate is unreachable (%s): no transfer is accepted until it is read", e)
            return False
        self._apply(model.reconcile(self.state, self.policy, fence, mappings, station, self.now_ms()))
        return True

    # ------------------------------------------------------------------ transfers

    def transfer(self, req: TransferRequest) -> Decision:
        """Propose, and follow the gate's answers through to the outcome. Returns the last decision
        about this transfer: ACCEPTED (active), or the refusal."""
        if not self.state.reconciled:
            self.reconcile()
        d = self._apply(model.begin_transfer(self.state, self.policy, req, self.now_ms()))
        if d.outcome != model.PENDING:
            return d
        return self._follow(d)

    def _follow(self, d: Decision) -> Decision:
        last = d
        for effect in d.effects:
            if isinstance(effect, model.GatePrepare):
                try:
                    ack = self.gate.prepare(effect.transfer, effect.prepare_ttl_ms)
                except GateUnreachable as e:
                    return self._apply(model.abandon(self.state, effect.transfer.transfer_id, f"the gate is unreachable: {e}"))
                nxt = self._apply(model.on_ack(self.state, effect.transfer.transfer_id, ack.accepted, ack.fence,
                                               ack.reason, ack.rejected_mapping_ids))
                last = self._follow(nxt) if nxt.outcome == model.PENDING else nxt
            elif isinstance(effect, model.GateCommit):
                try:
                    ok, reason = self.gate.commit(effect.transfer_id, effect.fence)
                except GateUnreachable as e:
                    # COMMITTED stays recorded; whether the gate applied it is for reconcile() to find.
                    log.error("COMMIT %s unanswered (%s): reconcile before the next transfer", effect.transfer_id, e)
                    self.state = replace(self.state, reconciled=False)
                    return Decision(self.state, model.REFUSED_BY_GATE, f"the gate did not answer the commit: {e}",
                                    transfer_id=effect.transfer_id)
                last = self._apply(model.on_commit_reply(self.state, self.policy, effect.transfer_id, ok, self.now_ms(), reason))
        return last

    # ------------------------------------------------------------------ leases, assignments, navigation

    def take(self, role, principal, site, payload_id="", token=None) -> Decision:
        return self._apply(model.take_lease(self.state, self.policy, role, principal, site, self.now_ms(), payload_id, token))

    def renew(self, slot, token) -> Decision:
        return self._apply(model.renew(self.state, slot, token, self.now_ms()))

    def release(self, slot, token) -> Decision:
        return self._apply(model.release(self.state, slot, token))

    def assign(self, role, assignee, by, by_revision=None, session_token="") -> Decision:
        return self._apply(model.assign(self.state, self.policy, role, assignee, by, by_revision=by_revision,
                                        session_token=session_token, now_ms=self.now_ms()))

    def bootstrap_overall(self, principal, maintainer) -> Decision:
        return self._apply(model.bootstrap_overall(self.state, self.policy, principal, maintainer))

    def session(self, role, principal, site, token, assignment_id) -> Decision:
        return self._apply(model.session_heartbeat(self.state, self.policy, role, principal, site, token, assignment_id,
                                                   self.now_ms()))

    def assume_command(self, role, claimant, reason, evidence, authorised_by) -> Decision:
        return self._apply(model.assume_command(self.state, self.policy, role, claimant, reason, evidence, authorised_by,
                                                self.now_ms()))

    def set_navigation_function(self, function, fn, by, by_revision, session_token) -> Decision:
        return self._apply(model.set_navigation_function(self.state, function, fn, by, by_revision, session_token,
                                                         self.now_ms()))

    # ------------------------------------------------------------------ maintenance

    def observe_armed(self, armed: bool) -> None:
        self.heard = replace(self.heard, armed=bool(armed), armed_ms=self.now_ms())

    def observe_resolved(self, dofs: dict) -> None:
        """The gate's latest resolved_control, as {dof: DofReading}. Replaces the last one whole."""
        self.heard = replace(self.heard, resolved=dict(dofs), resolved_ms=self.now_ms())

    def guards(self) -> tuple:
        return model.maintenance_guards(self.state, self.policy, self.heard, self.now_ms())

    def request_maintenance(self, principal, revision, session_token, maintainer, reason, scopes) -> Decision:
        return self._apply(model.request_maintenance(self.state, principal, revision, session_token, maintainer, reason,
                                                     scopes, self.now_ms(), self.audit_ms()))

    def cancel_maintenance(self, principal, revision, session_token, reason) -> Decision:
        return self._apply(model.cancel_maintenance(self.state, principal, revision, session_token, reason,
                                                    self.now_ms(), self.audit_ms()))

    def finish_maintenance(self, principal, revision, session_token, detail) -> Decision:
        return self._apply(model.finish_maintenance(self.state, principal, revision, session_token, detail,
                                                    self.now_ms(), self.audit_ms()))

    def complete_maintenance_check(self, principal, revision, session_token, checks) -> Decision:
        return self._apply(model.complete_maintenance_check(self.state, self.policy, principal, revision, session_token,
                                                            checks, self.now_ms(), self.audit_ms()))

    # ------------------------------------------------------------------ the periodic work

    def tick(self) -> None:
        """Expiry, the legacy synthesis, the maintenance steps, and a republish when a guard changed."""
        now = self.now_ms()
        self._apply(model.expire(self.state, now))
        self._apply(model.synthesise_assignments(self.state, self.policy, now))
        self._apply(model.advance_maintenance(self.state, self.policy, self.heard, now, self.audit_ms()))
        if self.guards() != self._published_guards:
            self._publish_maintenance()

    def heartbeat(self) -> None:
        self.challenges.rotate(self.now_ms())
        self._publish(model.PublishAssignments(), self.state)
        self._publish_maintenance()

    def _publish_maintenance(self) -> None:
        guards = self.guards()
        self._published_guards = guards
        self._publish(model.PublishMaintenance(guards), self.state)

    def history(self, limit=1000) -> list[dict]:
        return self.store.audit(limit)

    # ------------------------------------------------------------------ internals

    def verify(self, attestation_pb, request, procedure: str):
        return attestation.verify(attestation_pb, request, procedure, self.policy.attestation_keys,
                                  self.challenges, self.seen, self.now_ms())

    def _stamp(self, d: Decision) -> Decision:
        """Put the attested principal on every lease and transfer this call created or changed, and
        on its audit records. A call with no attestation stamps nothing."""
        who = self.principal
        if not who:
            return d
        leases = {s: (replace(lease, principal=who) if self.state.leases.get(s) != lease else lease)
                  for s, lease in d.state.leases.items()}
        transfers = {t: (replace(x, principal=who) if self.state.transfers.get(t) != x else x)
                     for t, x in d.state.transfers.items()}
        st = replace(d.state, leases=leases, transfers=transfers)
        return replace(d, state=st, audits=tuple({**a, "principal": who} for a in d.audits))

    def _apply(self, d: Decision) -> Decision:
        """Commit, then run the non-gate effects. Gate effects are run by _follow, in order."""
        d = self._stamp(d)
        if d.state is not self.state or d.audits:
            self.store.commit(d.state, d.audits, self.audit_ms())
        self.state = d.state
        for effect in d.effects:
            if isinstance(effect, (model.GatePrepare, model.GateCommit)):
                continue
            if isinstance(effect, model.PublishMaintenance):
                self._publish_maintenance()  # with the guards of the state just committed
                continue
            self._publish(effect, self.state)
        return d
