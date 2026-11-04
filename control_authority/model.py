"""The onboard authority manager's decisions, and nothing else. Pure: no clock, no store, no Zenoh.

Normative sources: keelson `protocols/command_authority.yaml` and `ControlMapping.proto` (PR #331), and
crowsnest-dev docs/COMMAND-ARCHITECTURE.md §0, §4 ("I2", "I7", "I10", "I13" are its §8 invariants).

WHAT THE MANAGER OWNS: the role leases (CONN, ENGINEER, PAYLOAD_AUTHORITY), the OVERALL and
NAVIGATIONAL_COMMAND assignments and their sessions, the navigation authority document, the
`active_control_station`, and the record of every transfer it proposed.

WHAT IT NEVER DOES: mint a motion fence. It PROPOSES a transfer -- `PREPARE {transfer_id, mappings,
station, prepare_ttl_ms}`, with no fence -- and the gate validates against what it is emitting now,
reserves `(T, n+1)` and ACKs with it. The manager records COMMITTED(map, station, fence) and only then
sends COMMIT (I2, I13). The two revisions it does mint are not fences: `Assignment.revision` (each
change of assignee, a succession included) and `NavigationAuthority.revision` (each change to a
function's holder, lease, method or envelope).

Every function takes the current `State` and returns a `Decision`: the new state, an outcome, the
effects to run AFTER the new state is durable (gate calls, publications, alarms), and the audit
records that commit WITH it. Times are this process's monotonic milliseconds; nothing here reads a
remote clock (§0: "no remote absolute expiry anywhere").
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field, replace

# ---------------------------------------------------------------------------- vocabulary

CONN, OVERALL, NAVIGATIONAL, ENGINEER, PAYLOAD = "conn", "overall", "navigational", "engineer", "payload"
LEASE_ROLES = (CONN, ENGINEER, PAYLOAD)
PROFILES_ALLOWED = ("operational", "test", "legacy_single_operator")
ASSIGNMENT_ROLES = (OVERALL, NAVIGATIONAL)

GRANT, TAKE_CONTROL, STATION_HANDOVER, ABORT_AUTOMATION = "grant", "take_control", "station_handover", "abort_automation"
TRANSFER_KINDS = (GRANT, TAKE_CONTROL, STATION_HANDOVER, ABORT_AUTOMATION)

# A transfer's phases. PREPARED is written before the PREPARE is sent; COMMITTED before the COMMIT.
PREPARED, COMMITTED, ACTIVE, REJECTED, ABANDONED = "prepared", "committed", "active", "rejected", "abandoned"
IN_FLIGHT = (PREPARED, COMMITTED)

ACCEPTED = "ACCEPTED"
PENDING = "PENDING"  # a transfer was proposed; its outcome comes from the gate
REFUSED_HELD = "REFUSED_HELD"
REFUSED_UNAUTHORISED = "REFUSED_UNAUTHORISED"
REFUSED_STALE_REVISION = "REFUSED_STALE_REVISION"
REFUSED_SOFTWARE = "REFUSED_SOFTWARE"
REFUSED_NOT_RECONCILED = "REFUSED_NOT_RECONCILED"
REFUSED_BUSY = "REFUSED_BUSY"
REFUSED_MAP = "REFUSED_MAP"
REFUSED_MALFORMED = "REFUSED_MALFORMED"
REFUSED_NOT_UNREACHABLE = "REFUSED_NOT_UNREACHABLE"
REFUSED_BY_GATE = "REFUSED_BY_GATE"


def slot_of(role: str, payload_id: str = "") -> str:
    if role == PAYLOAD:
        return f"payload/{payload_id}"
    return role


def session_slot(role: str) -> str:
    return f"{role}/session"


def new_id() -> str:
    return secrets.token_hex(16)


# ---------------------------------------------------------------------------- records


@dataclass(frozen=True)
class Fence:
    """A gate's `(term, generation)`. The manager stores and forwards these; it never makes one."""

    term: str
    generation: int


@dataclass(frozen=True)
class Lease:
    """A lease or a session. `renewed_ms` is on THIS process's monotonic clock."""

    slot: str
    holder: str
    site: str
    token: str
    lease_id: str
    renewed_ms: int
    ttl_ms: int
    assignment_id: str = ""  # a session names the assignment it belongs to

    def live(self, now_ms: int) -> bool:
        return now_ms - self.renewed_ms <= self.ttl_ms


@dataclass(frozen=True)
class Succession:
    previous_assignee: str
    reason: str
    evidence: tuple[str, ...]
    authorised_by: tuple[str, ...]


@dataclass(frozen=True)
class Assignment:
    assignee: str
    assignment_id: str
    revision: int
    synthesised: bool = False
    succession: Succession | None = None


@dataclass(frozen=True)
class NavFunction:
    """One function's row of the navigation authority document. `method` is SARUMS's control
    method 1..5 (ControlMethod); `envelope` holds FunctionEnvelope's fields by their proto names."""

    holder: str
    holder_lease_id: str
    method: int
    envelope: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Mapping:
    """One DOF's row (`ControlMapping`). The holder and the executing source are different fields:
    that is how a human holds the conn while navigation/0 executes under it (§4)."""

    dof: str
    authority_holder: str
    authority_lease_id: str
    execution_source: str
    envelope: tuple = ()  # (min_pct, max_pct, max_rate_pct_per_s), or () for none
    group_id: str = ""
    mapping_id: str = ""


@dataclass(frozen=True)
class Transfer:
    transfer_id: str
    kind: str
    mappings: tuple[Mapping, ...]
    station: str
    requested_by: str
    reason: str
    phase: str
    fence: Fence | None = None
    detail: str = ""
    # The conn lease the transfer installs on COMMIT (none for an abort).
    conn_holder: str = ""
    conn_site: str = ""
    conn_token: str = ""
    conn_lease_id: str = ""


@dataclass(frozen=True)
class InForce:
    """What the gate is enforcing, as far as the manager knows: set from a confirmed COMMIT, and
    overwritten by the gate's exposed state at every reconciliation (the gate is the truth)."""

    mappings: tuple[Mapping, ...] = ()
    station: str = ""
    fence: Fence | None = None


@dataclass(frozen=True)
class State:
    vessel_id: str
    leases: dict = field(default_factory=dict)  # slot -> Lease (sessions included)
    overall: Assignment | None = None
    navigational: Assignment | None = None
    navigation: dict = field(default_factory=dict)  # function -> NavFunction
    navigation_revision: int = 0
    active_station: str = ""
    responsible_roc: str = ""
    in_force: InForce = field(default_factory=InForce)
    transfers: dict = field(default_factory=dict)  # transfer_id -> Transfer
    # Not persisted: false at every boot until the gate's exposed state has been read (I13).
    reconciled: bool = False

    def assignment(self, role: str) -> Assignment | None:
        return self.overall if role == OVERALL else self.navigational if role == NAVIGATIONAL else None

    def live_lease(self, slot: str, now_ms: int) -> Lease | None:
        lease = self.leases.get(slot)
        return lease if lease is not None and lease.live(now_ms) else None

    def in_flight(self) -> Transfer | None:
        return next((t for t in self.transfers.values() if t.phase in IN_FLIGHT), None)


# ---------------------------------------------------------------------------- effects


@dataclass(frozen=True)
class GatePrepare:
    transfer: Transfer
    prepare_ttl_ms: int


@dataclass(frozen=True)
class GateCommit:
    transfer_id: str
    fence: Fence


@dataclass(frozen=True)
class PublishLease:
    slot: str  # the service reads a live record from the state it has just committed
    released: bool = False
    lease: Lease | None = None  # a released lease is gone from the state: its identity travels here


@dataclass(frozen=True)
class PublishAssignments:
    """`command_assignment/{vessel}`: on every assignment change, and at the heartbeat."""


@dataclass(frozen=True)
class PublishNavigation:
    """The navigation authority document, republished on every revision."""


@dataclass(frozen=True)
class Alarm:
    text: str


@dataclass(frozen=True)
class Decision:
    state: State
    outcome: str
    detail: str = ""
    effects: tuple = ()
    audits: tuple = ()
    transfer_id: str = ""
    rejected_mapping_ids: tuple = ()  # the rows a gate NAK named


def _same(state: State, outcome: str, detail: str = "", audits=()) -> Decision:
    return Decision(state, outcome, detail, (), tuple(audits))


# ---------------------------------------------------------------------------- policy


@dataclass(frozen=True)
class Policy:
    """The vessel's control matrix and timings (platform registry; changes only in maintenance)."""

    command_profile: str = "test"  # operational | test | legacy_single_operator
    dofs: tuple[str, ...] = ()
    groups: dict = field(default_factory=dict)  # group_id -> tuple of dofs that move together
    automation_sources: frozenset = frozenset()  # e.g. {"navigation/0"}: never a human station's axis
    processes: frozenset = frozenset()  # principals that are software: never OVERALL / NAVIGATIONAL
    lease_ttl_ms: dict = field(default_factory=lambda: {CONN: 2000, ENGINEER: 60000, PAYLOAD: 5000})
    session_ttl_ms: int = 30000
    prepare_ttl_ms: int = 2000
    # role -> principals allowed to ASSUME it, or "*" for any human
    succession: dict = field(default_factory=dict)

    def ttl(self, role: str) -> int:
        return int(self.lease_ttl_ms.get(role, 30000))

    def is_software(self, principal: str) -> bool:
        return principal in self.processes


# ---------------------------------------------------------------------------- leases


def take_lease(state: State, policy: Policy, role: str, principal: str, site: str, now_ms: int,
               payload_id: str = "", token: str | None = None) -> Decision:
    """ENGINEER and PAYLOAD_AUTHORITY: a fresh lease on a free slot. The CONN is never taken here: a
    conn is a motion transfer (`begin_transfer` GRANT), because its DOFs move with it."""
    if role not in (ENGINEER, PAYLOAD) or (role == PAYLOAD and not payload_id):
        return _same(state, REFUSED_MALFORMED, "only ENGINEER and PAYLOAD_AUTHORITY are taken directly; the conn is a transfer")
    slot = slot_of(role, payload_id)
    held = state.live_lease(slot, now_ms)
    if held is not None and held.holder != principal:
        return _same(state, REFUSED_HELD, f"held by {held.holder}: request it from them")
    lease = Lease(slot, principal, site, token or new_id(), new_id(), now_ms, policy.ttl(role))
    st = replace(state, leases={**state.leases, slot: lease})
    return Decision(st, ACCEPTED, "", (PublishLease(slot),), (_audit("take", slot=slot, holder=principal, site=site),))


def renew(state: State, slot: str, token: str, now_ms: int) -> Decision:
    lease = state.leases.get(slot)
    if lease is None or lease.token != token:
        return _same(state, REFUSED_UNAUTHORISED, "not this lease's token")
    if not lease.live(now_ms):
        return _same(state, REFUSED_UNAUTHORISED, "the lease has lapsed; take it again")
    st = replace(state, leases={**state.leases, slot: replace(lease, renewed_ms=now_ms)})
    return Decision(st, ACCEPTED, "", (PublishLease(slot),))


def release(state: State, slot: str, token: str) -> Decision:
    """A release frees the slot. On the conn it does not propose anything: the mapping's holder lease
    is gone, and the GATE withdraws the DOFs into its transition state on its own (§4). On a session
    it ends the session; the assignment stands."""
    lease = state.leases.get(slot)
    if lease is None or lease.token != token:
        return _same(state, REFUSED_UNAUTHORISED, "not this lease's token")
    leases = dict(state.leases)
    del leases[slot]
    return Decision(replace(state, leases=leases), ACCEPTED, "", (PublishLease(slot, True, lease),),
                    (_audit("release", slot=slot, holder=lease.holder),))


def expire(state: State, now_ms: int) -> Decision:
    """Lapsed leases vacate their role and are published released. A lapsed SESSION means the
    assignee is unreachable: it alarms and vacates nothing (the assignment stands)."""
    lapsed = [s for s, lease in state.leases.items() if not lease.live(now_ms)]
    if not lapsed:
        return _same(state, ACCEPTED)
    leases = {s: lease for s, lease in state.leases.items() if s not in lapsed}
    effects, audits = [], []
    for s in lapsed:
        effects.append(PublishLease(s, True, state.leases[s]))
        audits.append(_audit("expire", slot=s, holder=state.leases[s].holder))
        if s.endswith("/session"):
            role = s.split("/")[0]
            effects.append(Alarm(f"{role.upper()} assignee {state.leases[s].holder} is unreachable: their session lapsed"))
    return Decision(replace(state, leases=leases), ACCEPTED, "", tuple(effects), tuple(audits))


def rebase_after_restart(state: State, now_ms: int) -> State:
    """A restart interrupts no healthy holder (§0): every lease and session gets a fresh TTL on the
    new process's clock; a holder still alive renews it, a dead one lapses one TTL later. Not
    reconciled until the gate has been read."""
    leases = {s: replace(lease, renewed_ms=now_ms) for s, lease in state.leases.items()}
    return replace(state, leases=leases, reconciled=False)


# ---------------------------------------------------------------------------- assignments


def assign(state: State, policy: Policy, role: str, assignee: str, by: str, *, by_revision: int | None = None,
           session_token: str = "", now_ms: int = 0) -> Decision:
    """Write an assignment by procedure or by grant. The master assigns NAVIGATIONAL; the OVERALL
    assignment changes by procedure (`by="procedure"`) or by the current master's own grant -- a
    privileged request like any other: the assignee, the current revision and a live session (I7).
    Each change of assignee increments the revision and draws a new assignment_id."""
    if role not in ASSIGNMENT_ROLES:
        return _same(state, REFUSED_MALFORMED, "not an assignment role")
    if policy.is_software(assignee):
        return _same(state, REFUSED_SOFTWARE, f"{role.upper()} is never software")
    if by != "procedure":
        outcome, detail, _ = authorise_privileged(state, (OVERALL,), by, by_revision, session_token, now_ms)
        if outcome != ACCEPTED:
            return _same(state, outcome, "only the OVERALL_COMMAND assignee assigns, or a procedure: " + detail)
    current = state.assignment(role)
    if current is not None and current.assignee == assignee and not current.synthesised:
        return _same(state, ACCEPTED, "already assigned")
    nxt = Assignment(assignee, new_id(), (current.revision if current else 0) + 1)
    st = replace(state, **{role: nxt})
    return Decision(st, ACCEPTED, "", (PublishAssignments(),),
                    (_audit("assign", role=role, assignee=assignee, by=by, revision=nxt.revision),))


def session_heartbeat(state: State, policy: Policy, role: str, principal: str, site: str, token: str, assignment_id: str,
                      now_ms: int) -> Decision:
    """The assignee's presence. A session must name the CURRENT assignment it belongs to."""
    current = state.assignment(role)
    if current is None or current.assignee != principal or current.assignment_id != assignment_id:
        return _same(state, REFUSED_UNAUTHORISED, "a session belongs to the current assignment and its assignee")
    slot = session_slot(role)
    lease = state.leases.get(slot)
    # A session of an EARLIER assignment is dead with it: it never blocks the current assignee's.
    ours = lease is not None and lease.assignment_id == assignment_id
    if ours and lease.token != token and lease.live(now_ms):
        return _same(state, REFUSED_HELD, "another session of this assignment is live")
    lease_id = lease.lease_id if ours and lease.token == token else new_id()
    nxt = Lease(slot, principal, site, token, lease_id, now_ms, policy.session_ttl_ms, assignment_id)
    return Decision(replace(state, leases={**state.leases, slot: nxt}), ACCEPTED, "", (PublishLease(slot),))


def authorise_privileged(state: State, roles: tuple[str, ...], principal: str, revision: int | None, session_token: str,
                         now_ms: int) -> tuple[str, str, str]:
    """Every privileged request -- a grant, a take-control, a station change -- carries the
    assignment_revision it acts under: assignee match ∧ revision match ∧ the session belongs to the
    assignment (I7). Returns (outcome, detail, role it was authorised under)."""
    why = "not the assignee of " + " or ".join(r.upper() for r in roles)
    for role in roles:
        a = state.assignment(role)
        if a is None or a.assignee != principal:
            continue
        if revision != a.revision:
            return REFUSED_STALE_REVISION, f"acting under revision {revision}, {role.upper()} is at {a.revision}", role
        session = state.live_lease(session_slot(role), now_ms)
        if session is None or session.token != session_token or session.assignment_id != a.assignment_id:
            return REFUSED_UNAUTHORISED, f"no live session of the {role.upper()} assignment", role
        return ACCEPTED, "", role
    return REFUSED_UNAUTHORISED, why, ""


def assume_command(state: State, policy: Policy, role: str, claimant: str, reason: str, evidence, authorised_by,
                   now_ms: int) -> Decision:
    """Succession for an unreachable assignee (§4, §5): reason, evidence and who authorises, under
    the vessel's succession policy. Touches no lease, no map and no fence; increments the
    assignment's revision, so every grant and RESET the old assignee sends under the old revision is
    refused (I7, I7′)."""
    if role not in ASSIGNMENT_ROLES:
        return _same(state, REFUSED_MALFORMED, "succession is for OVERALL or NAVIGATIONAL_COMMAND")
    if policy.is_software(claimant):
        return _same(state, REFUSED_SOFTWARE, f"{role.upper()} is never software")
    if not reason or not evidence:
        return _same(state, REFUSED_MALFORMED, "a succession records its reason and evidence")
    allowed = policy.succession.get(role)
    if allowed is None or (allowed != "*" and claimant not in allowed):
        return _same(state, REFUSED_UNAUTHORISED, f"the succession policy does not allow {claimant} to assume {role.upper()}")
    current = state.assignment(role)
    if current is not None and state.live_lease(session_slot(role), now_ms) is not None:
        return _same(state, REFUSED_NOT_UNREACHABLE, f"{current.assignee}'s session is live: ask them, or wait")
    nxt = Assignment(claimant, new_id(), (current.revision if current else 0) + 1, False,
                     Succession(current.assignee if current else "", reason, tuple(evidence), tuple(authorised_by)))
    st = replace(state, **{role: nxt})
    return Decision(st, ACCEPTED, "", (PublishAssignments(), Alarm(f"{claimant} assumed {role.upper()}: {reason}")),
                    (_audit("assume_command", role=role, assignee=claimant, previous=nxt.succession.previous_assignee,
                            reason=reason, evidence=list(evidence), authorised_by=list(authorised_by), revision=nxt.revision),))


def synthesise_assignments(state: State, policy: Policy, now_ms: int) -> Decision:
    """LEGACY_SINGLE_OPERATOR only: the OVERALL and NAVIGATIONAL assignments ARE the conn holder,
    marked synthesised. A change of holder increments the revision like any other change of
    assignee; no conn means no assignee."""
    if policy.command_profile != "legacy_single_operator":
        return _same(state, ACCEPTED)
    conn = state.live_lease(CONN, now_ms)
    holder = conn.holder if conn is not None and not policy.is_software(conn.holder) else None
    changes = {}
    for role in ASSIGNMENT_ROLES:
        cur = state.assignment(role)
        if holder is None:
            if cur is not None and cur.synthesised:
                changes[role] = None
            continue
        if cur is None or cur.assignee != holder or not cur.synthesised:
            changes[role] = Assignment(holder, new_id(), (cur.revision if cur else 0) + 1, True)
    if not changes:
        return _same(state, ACCEPTED)
    return Decision(replace(state, **changes), ACCEPTED, "", (PublishAssignments(),), (_audit("synthesise", assignee=holder),))


# ---------------------------------------------------------------------------- navigation authority


def set_navigation_function(state: State, function: str, fn: NavFunction | None, by: str, by_revision: int | None,
                            session_token: str, now_ms: int) -> Decision:
    """The navigational commander writes the navigation authority document. Every change to a
    function's holder, lease, method or envelope increments its revision; an identical write does not.
    The document is an assignment, not a lease: it has no TTL."""
    outcome, detail, _ = authorise_privileged(state, (NAVIGATIONAL, OVERALL), by, by_revision, session_token, now_ms)
    if outcome != ACCEPTED:
        return _same(state, outcome, detail)
    if state.navigation.get(function) == fn:
        return _same(state, ACCEPTED, "no change")
    nav = dict(state.navigation)
    if fn is None:
        nav.pop(function, None)
    else:
        nav[function] = fn
    st = replace(state, navigation=nav, navigation_revision=state.navigation_revision + 1)
    return Decision(st, ACCEPTED, "", (PublishNavigation(),),
                    (_audit("navigation_authority", function=function, by=by, revision=st.navigation_revision),))


# ---------------------------------------------------------------------------- transfers


@dataclass(frozen=True)
class TransferRequest:
    kind: str
    requester: str
    requester_site: str
    reason: str = ""
    # GRANT: the conn holder's token (or none, for a free conn); the new holder.
    conn_token: str = ""
    new_holder: str = ""
    new_holder_site: str = ""
    # TAKE_CONTROL / STATION_HANDOVER / ABORT_AUTOMATION: the assignment acted under.
    assignment_revision: int | None = None
    session_token: str = ""
    station: str = ""  # STATION_HANDOVER: the station that takes the helm
    # The proposed rows, without lease ids (the manager fills them in) and without fences (never).
    mappings: tuple[Mapping, ...] = ()


def _human_map_problems(policy: Policy, mappings, station: str) -> list[str]:
    """A human motion map moves the WHOLE helm, at one station, in coupled groups (§4.2, I10)."""
    problems = []
    dofs = [m.dof for m in mappings]
    if len(set(dofs)) != len(dofs):
        problems.append("a DOF is mapped twice")
    unknown = sorted(set(dofs) - set(policy.dofs))
    if unknown:
        problems.append(f"not a DOF of this vessel: {', '.join(unknown)}")
    # The whole helm, so every coupled group moves together by construction: no group check is needed
    # while a transfer must name every DOF. The groups stay in the policy for the gate's pick-up.
    missing = sorted(set(policy.dofs) - set(dofs))
    if missing:
        problems.append(f"a transfer moves every motion DOF together; missing: {', '.join(missing)}")
    for m in mappings:
        if m.execution_source in policy.automation_sources:
            continue
        if not station or not m.execution_source.startswith(f"{station}/"):
            problems.append(f"{m.dof}: a human source must be at the active control station {station or '(none)'}")
    return problems


def begin_transfer(state: State, policy: Policy, req: TransferRequest, now_ms: int, transfer_id: str | None = None) -> Decision:
    """Validate what the manager can -- lease, role, profile, the shape of the map -- record PREPARED,
    and propose. Neutral-or-match is NOT checked here: it is the gate's final word, against what it is
    emitting now (a manager-side check is a time-of-check / time-of-use race, §4)."""
    if req.kind not in TRANSFER_KINDS:
        return _same(state, REFUSED_MALFORMED, f"unknown transfer kind {req.kind!r}")
    if not state.reconciled:
        return _same(state, REFUSED_NOT_RECONCILED, "the gate's exposed state has not been read since start")
    busy = state.in_flight()
    if busy is not None:
        return _same(state, REFUSED_BUSY, f"transfer {busy.transfer_id} is {busy.phase}")

    station = state.active_station
    conn_holder, conn_site = "", ""
    if req.kind == GRANT:
        # Between humans: the holder hands the conn to a named station, or a claimant takes a free one.
        held = state.live_lease(CONN, now_ms)
        if held is not None:
            if held.token != req.conn_token or held.holder != req.requester:
                return _same(state, REFUSED_HELD, f"the conn is held by {held.holder}: request it from them")
            conn_holder, conn_site = req.new_holder or req.requester, req.new_holder_site or req.requester_site
        else:
            conn_holder, conn_site = req.requester, req.requester_site
        human = not policy.is_software(conn_holder)
        if human and not station and policy.command_profile != "operational":
            station = conn_site  # test / legacy: the first taker's station; in operational it is OVERALL's to set
        # Full automation (the conn handed to the navigator) is not held to a human station; its rows
        # must then name automation sources, which _human_map_problems checks.
        if human and conn_site != station:
            return _same(state, REFUSED_UNAUTHORISED,
                         f"{conn_site} is not the active control station {station or '(none)'}: the master moves it")
    elif req.kind in (TAKE_CONTROL, ABORT_AUTOMATION):
        outcome, detail, _ = authorise_privileged(state, (NAVIGATIONAL, OVERALL), req.requester, req.assignment_revision,
                                                  req.session_token, now_ms)
        if outcome != ACCEPTED:
            return _same(state, outcome, detail)
        if req.kind == TAKE_CONTROL:
            # The one place NAVIGATIONAL_COMMAND may move the station: to their own, in this transaction.
            station, conn_holder, conn_site = req.requester_site, req.requester, req.requester_site
    else:  # STATION_HANDOVER: OVERALL's, the whole human map in one transaction
        outcome, detail, _ = authorise_privileged(state, (OVERALL,), req.requester, req.assignment_revision,
                                                  req.session_token, now_ms)
        if outcome != ACCEPTED:
            return _same(state, outcome, detail)
        if not req.station or not req.new_holder:
            return _same(state, REFUSED_MALFORMED, "a handover names the station and the holder who takes the helm")
        station, conn_holder, conn_site = req.station, req.new_holder, req.new_holder_site or req.station

    if conn_holder and policy.is_software(conn_holder) and req.kind != GRANT:
        return _same(state, REFUSED_SOFTWARE, "take-control and handover give the helm to a human")

    lease_id = new_id() if conn_holder else ""
    if req.kind == ABORT_AUTOMATION:
        if req.mappings:
            return _same(state, REFUSED_MALFORMED, "an abort proposes no map: the gate enters its transition state")
        mappings = ()
    else:
        mappings = tuple(replace(m, authority_holder=conn_holder, authority_lease_id=lease_id,
                                 mapping_id=m.mapping_id or new_id()) for m in req.mappings)
        problems = _human_map_problems(policy, mappings, station)
        if problems:
            return _same(state, REFUSED_MAP, "; ".join(problems))

    tid = transfer_id or new_id()
    t = Transfer(tid, req.kind, mappings, station, req.requester, req.reason, PREPARED,
                 conn_holder=conn_holder, conn_site=conn_site, conn_token=new_id() if conn_holder else "",
                 conn_lease_id=lease_id)
    st = replace(state, transfers={**state.transfers, tid: t})
    return Decision(st, PENDING, "", (GatePrepare(t, policy.prepare_ttl_ms),),
                    (_audit("prepared", transfer_id=tid, kind=req.kind, by=req.requester, station=station,
                            old_map=_map_json(state.in_force.mappings), new_map=_map_json(mappings),
                            old_fence=_fence_json(state.in_force.fence), reason=req.reason),), tid)


def on_ack(state: State, transfer_id: str, accepted: bool, fence: Fence | None, reason: str = "",
           rejected_mapping_ids=()) -> Decision:
    """The gate's answer to a PREPARE. ACK: record COMMITTED(map, station, fence) -- durably, before
    the COMMIT is sent (I13). NAK: the transfer is rejected, all or nothing (I10)."""
    t = state.transfers.get(transfer_id)
    if t is None or t.phase != PREPARED:
        return _same(state, ACCEPTED, "an answer to no proposal in flight", (_audit("stray_ack", transfer_id=transfer_id),))
    if not accepted or fence is None:
        nt = replace(t, phase=REJECTED, detail=reason or "refused by the gate")
        return Decision(replace(state, transfers={**state.transfers, transfer_id: nt}), REFUSED_BY_GATE, nt.detail, (),
                        (_audit("rejected", transfer_id=transfer_id, reason=nt.detail,
                                rejected_mapping_ids=list(rejected_mapping_ids)),), transfer_id, tuple(rejected_mapping_ids))
    nt = replace(t, phase=COMMITTED, fence=fence)
    return Decision(replace(state, transfers={**state.transfers, transfer_id: nt}), PENDING, "",
                    (GateCommit(transfer_id, fence),),
                    (_audit("committed", transfer_id=transfer_id, fence=_fence_json(fence)),), transfer_id)


def on_commit_reply(state: State, policy: Policy, transfer_id: str, ok: bool, now_ms: int, reason: str = "") -> Decision:
    """The gate activated the transfer, or it no longer remembers it (it restarted after its ACK). On
    activation: station, map and fence change together, and the new conn lease is installed with the
    fence the gate minted. On a NAK: abandoned, and nothing is proposed again until reconciled."""
    t = state.transfers.get(transfer_id)
    if t is None or t.phase != COMMITTED:
        return _same(state, ACCEPTED, "a commit reply for no committed transfer", (_audit("stray_commit_reply", transfer_id=transfer_id),))
    if not ok:
        nt = replace(t, phase=ABANDONED, detail=reason or "the gate refused the commit")
        st = replace(state, transfers={**state.transfers, transfer_id: nt}, reconciled=False)
        return Decision(st, REFUSED_BY_GATE, nt.detail, (Alarm(f"transfer {transfer_id} not applied: {nt.detail}"),),
                        (_audit("abandoned", transfer_id=transfer_id, reason=nt.detail),), transfer_id)
    return _activate(state, policy, t, now_ms, "active")


def _activate(state: State, policy: Policy, t: Transfer, now_ms: int, how: str) -> Decision:
    fence = t.fence
    mappings = tuple(m for m in t.mappings)
    leases = dict(state.leases)
    effects = []
    if t.conn_holder:
        leases[CONN] = Lease(CONN, t.conn_holder, t.conn_site, t.conn_token, t.conn_lease_id, now_ms, policy.ttl(CONN))
        effects.append(PublishLease(CONN))
    elif t.kind == ABORT_AUTOMATION:
        conn = leases.get(CONN)
        if conn is not None and policy.is_software(conn.holder):
            del leases[CONN]  # the automation's conn is fenced; its lease ends with it
            effects.append(PublishLease(CONN, True, conn))
    nt = replace(t, phase=ACTIVE)
    st = replace(state, leases=leases, transfers={**state.transfers, t.transfer_id: nt},
                 in_force=InForce(mappings, t.station, fence), active_station=t.station)
    return Decision(st, ACCEPTED, "", tuple(effects),
                    (_audit(how, transfer_id=t.transfer_id, kind=t.kind, station=t.station, fence=_fence_json(fence),
                            new_map=_map_json(mappings)),), t.transfer_id)


def abandon(state: State, transfer_id: str, why: str) -> Decision:
    """A PREPARE nobody answered within its TTL. The gate drops its reservation on its own clock."""
    t = state.transfers.get(transfer_id)
    if t is None or t.phase != PREPARED:
        return _same(state, ACCEPTED)
    nt = replace(t, phase=ABANDONED, detail=why)
    return Decision(replace(state, transfers={**state.transfers, transfer_id: nt}), REFUSED_BY_GATE, why, (),
                    (_audit("abandoned", transfer_id=transfer_id, reason=why),), transfer_id)


def reconcile(state: State, policy: Policy, gate_fence: Fence | None, gate_mappings, gate_station: str, now_ms: int) -> Decision:
    """Before any transfer after a restart, or after a refused COMMIT (I13). The gate's exposed pair is
    the truth about what is in force; the manager's store is the truth about what was intended.
    Disagreement resolves toward the gate and is audited.

    - PREPARED without COMMITTED: abandoned, never half-applied (the gate dropped or will drop it).
    - COMMITTED whose fence the gate shows in force: the gate applied it -- it is active.
    - COMMITTED whose fence the gate does not show: the gate never activated it -- abandoned.
    The manager mints nothing here: the conn in force stays in force (§0)."""
    st, audits, effects = state, [], []
    for t in list(state.transfers.values()):
        if t.phase == PREPARED:
            nt = replace(t, phase=ABANDONED, detail="PREPARED without COMMITTED at reconciliation")
            st = replace(st, transfers={**st.transfers, t.transfer_id: nt})
            audits.append(_audit("abandoned", transfer_id=t.transfer_id, reason=nt.detail))
        elif t.phase == COMMITTED:
            if gate_fence is not None and t.fence == gate_fence:
                d = _activate(st, policy, t, now_ms, "active_on_reconcile")
                st, effects, audits = d.state, [*effects, *d.effects], [*audits, *d.audits]
            else:
                nt = replace(t, phase=ABANDONED, detail="COMMITTED but the gate shows another fence in force")
                st = replace(st, transfers={**st.transfers, t.transfer_id: nt})
                audits.append(_audit("abandoned", transfer_id=t.transfer_id, reason=nt.detail))
    gate = InForce(tuple(gate_mappings), gate_station, gate_fence)
    # A manager that has recorded nothing yet (a fresh store) has nothing to disagree with.
    if st.in_force.fence is not None and gate != st.in_force:
        audits.append(_audit("reconcile_disagreement", manager_fence=_fence_json(st.in_force.fence),
                             gate_fence=_fence_json(gate_fence), manager_station=st.in_force.station, gate_station=gate_station))
        effects.append(Alarm("the gate's state differed from the manager's record; the gate's was adopted"))
    st = replace(st, in_force=gate, active_station=gate_station or st.active_station, reconciled=True)
    return Decision(st, ACCEPTED, "", tuple(effects), (*audits, _audit("reconciled", fence=_fence_json(gate_fence))))


# ---------------------------------------------------------------------------- audit helpers


def _fence_json(f: Fence | None):
    return None if f is None else {"term": f.term, "generation": f.generation}


def _map_json(mappings):
    return [{"dof": m.dof, "authority_holder": m.authority_holder, "execution_source": m.execution_source} for m in mappings]


def _audit(event: str, **fields) -> dict:
    return {"event": event, **fields}
