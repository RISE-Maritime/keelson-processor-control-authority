# keelson-processor-control-authority

The onboard **authority manager**, one per vessel. It owns the role leases, the OVERALL and
NAVIGATIONAL_COMMAND assignments and their sessions, the navigation authority document,
`active_control_station`, and the two-phase motion transfer with the control gate. It **proposes a
transfer, never a fence**: the gate validates it against what it is emitting now, reserves `(T, n+1)`
and ACKs with it, and only a COMMIT that the manager has already written to its own store activates.

- **Protocol:** keelson `protocols/command_authority.yaml`, `CommandAuthority.proto`,
  `ControlMapping.proto` and `NavigationAuthority.proto` (keelson PR #331). The interfaces are
  `authority_manager/v1` (served here) and `control_gate/v1` (called here), on the keelson branch
  `feat/authority-manager-and-control-gate`, which is stacked on #331.
- **Design:** crowsnest-dev `docs/COMMAND-ARCHITECTURE.md` §0, §4 and §5. The work item is crowsnest-dev #84.
- **Replaces** `keelson-processor-supervisor`'s `authority.py` (`open` / `exclusive`, single station).

> **Status: built and tested against a fake gate. It actuates nothing.** The motion gate it calls
> (crowsnest-dev #85) does not exist yet. Until it does, every transfer is refused NOT_RECONCILED,
> because the gate never answers. Never run this against a real vessel or a shared network.

## What it does

- **Every conn change is a motion transfer.** Taking a free conn, a grant between humans, Take
  control from automation and a station handover all go through `PREPARE → ACK → COMMIT`. A
  release proposes nothing: the mapping's holder lease is gone, and the gate withdraws into its
  transition state on its own.
- **Durable on both sides (I13).** PREPARED is committed before the gate hears the proposal, and
  COMMITTED(map, station, fence) before it hears the COMMIT. Each new state commits together with
  its audit records in one sqlite transaction, and the effects run after it.
- **Reconcile before any transfer.** Every boot starts unreconciled, and so does every COMMIT the
  gate refused or left unanswered. Reading the gate's exposed `(fence, map, station)` resolves the
  transfers in flight as follows:
  - PREPARED is abandoned;
  - COMMITTED whose fence the gate shows is active;
  - any other COMMITTED is abandoned.

  If the gate disagrees with the manager's record, the gate's state is adopted, and the
  disagreement is audited and raised as an alarm. A restart mints nothing, and every lease gets a
  fresh TTL on the new process's clock, so a healthy conn is never interrupted.
- **All or nothing (I10).** A transfer moves the whole helm at one station: every DOF and every
  coupled group, with human sources at the active control station. The manager checks the shape,
  the lease, the role and the profile. **Neutral-or-match is the gate's final word**, and one
  unmatched lever NAKs the whole transfer.
- **The revision revokes (I7).** Every privileged request (take control, abort automation, a station
  handover, an assignment, a navigation-document change) must pass all three of these:
  - the requester is the assignee;
  - the request carries the current `assignment_revision`;
  - it comes with a live session of that assignment.

  `assume_command` records a succession for an assignee whose session has lapsed, under the
  vessel's succession policy and with a reason and evidence. It increments the revision, so the
  old master's stale grants are refused. It touches no lease, no map and no fence.
- **Take control and Abort take back the delegation** (crowsnest-dev#271, decided by Ted on
  2026-11-05). When either becomes active, every navigation function a process holds reverts to the
  human who acted, at method 2 (Directed).
  - **Durable together:** it happens in the same decision as the CONN, map and station, with one
    `NavigationAuthority.revision` bump and an audit `navigation_reverted`.
  - **What changes:** a human's functions are untouched, the envelope is kept, and re-delegating is
    a new grant.
  - **Why:** the CONN fence alone leaves the delegation in the document. It would become executable
    again through a later carry-out envelope. The revision bump also refuses a stale `NAV_FUNCTION`
    candidate on the DECISION check.
  - **Not yet covered:** an abort the gate performs alone, while the manager is down, is not
    recognised at reconcile yet (crowsnest-dev#290).
- **Never software:** OVERALL and NAVIGATIONAL_COMMAND are never assigned to a process.
- **`legacy_single_operator`:** the two assignments are synthesised for the conn holder and marked
  `synthesised`.
- **Who may take the conn:**
  - `operational`: only once the master has set a station;
  - `test`: the first taker's station becomes the active control station.
- **Revisions it mints:** `Assignment.revision` and `NavigationAuthority.revision` are the only
  ones. The motion fence is the gate's. `plant_generation` and `payload_epoch` are their gates' too:
  Ted decided that the motion gate mints `plant_generation` in both halves, which supersedes the
  older wording in #84.
- **No `force_safety_release` anywhere:** the manager is not the supervisor.
- **It verifies who is acting** (keelson#383, crowsnest-dev#312; trust chain B). `controller_id` is
  a label. Every procedure that changes anything checks `Caller.attestation` first, under the one
  lock, in the protocol's order, and refuses `REFUSAL_ATTESTATION` with the check that failed:
  1. the claim parses;
  2. the key is one this vessel trusts (and belongs to the service it names);
  3. the Ed25519 signature is good;
  4. the challenge is one this manager issued within `challenge_ttl_ms`, on its own clock;
  5. the sequence rises for that key and session;
  6. the claim is for this procedure;
  7. the request digest is this request's;
  8. `controller_id` / `controller_site` equal the claim's operator and site.

  How it is enforced:
  - **A present but bad attestation is always refused.** A missing one is refused only where the
    policy requires it: `attestation.required`, which defaults to `command_profile == "operational"`.
  - **No key, no start.** An operational policy with no trusted key refuses to load, because
    nobody could command.
  - **What is recorded.** The attested principal (operator, site, station, service, key, session)
    is stamped on every lease and transfer the call creates or changes, and on its audit records
    (`history` → `detail_json`).
  - **The challenge.** A new one is drawn at every heartbeat and published on
    `command_assignment` as `attestation_challenge`, and the accepted ones are in `get`. Challenges
    are never persisted, so after a restart every older attestation is stale.
  - **The keys** (`attestation.keys`: `key_id` → `service_id`, base64 raw 32-byte public key) are
    maintenance configuration.
- **It runs the maintenance procedure** (keelson `protocols/maintenance.yaml`, crowsnest-dev#91). It
  is the one writer of `maintenance_state/{vessel}`. The procedure goes INACTIVE → REQUESTED →
  PREPARING → MAINTENANCE → READY_FOR_CHECK → INACTIVE.
  - **OVERALL_COMMAND only:** `request_maintenance`, `cancel_maintenance` (from REQUESTED or
    PREPARING only), `finish_maintenance` and `complete_maintenance_check`, each under the current
    revision and a live session.
  - **The manager's own steps**, at every tick, on what it has heard (stamped on local receipt and
    never persisted):
    - REQUESTED → PREPARING when `vehicle_armed` (the key `maintenance.vehicle_armed_key` names) is
      false and fresh, and `resolved_control/{vessel}` reports every DOF at rest. At rest means at
      zero within the tolerance, or holding and not driven where the policy says `hold`, from VALID
      feedback no older than `feedback_max_age_ms`. Absent, STALE, ESTIMATED or NONE feedback is
      never at rest.
    - PREPARING vacates every PAYLOAD_AUTHORITY lease, publishes it released, and refuses new ones.
      PREPARING → MAINTENANCE when every payload in `maintenance.payloads` reports its safe state.
      **No subject carries that yet (keelson#372), so a vessel that declares payloads stays in
      PREPARING.** It fails closed.
  - **From PREPARING until INACTIVE:** every transfer but abort_automation is refused
    (REFUSAL_MAINTENANCE).
  - **Out only through the checks:** every one of `maintenance.required_checks` (default
    `self_test`, `configuration_valid`) reported passed returns her to INACTIVE. Any failed check
    returns her to MAINTENANCE. A missing one refuses the call.
  - **It never arms and never commands the vehicle.** A request made while she is armed waits in
    REQUESTED until whoever holds the conn disarms her. The supervisor's and the gate's
    ACTUATION_ALLOWED, the vehicle connector's arming, and the config-write gating read the record;
    those are their changes, not this one.

**A lost store.** A store that fails its integrity check, its schema or its hash is refused, and the
service does not start. Unlike the safety supervisor, there is no "more inhibited" state to fall
back to that would be safe here: who holds which role is not a thing to guess. The procedure for
re-initialising a store, which would reconcile everything from the gate, is not written yet.

## Keys

| Serves / calls | Writes |
| --- | --- |
| serves `authority_manager/v1` as responder `authority-manager` | `command_authority/{vessel}` (the conn), `…/{vessel}/engineer`, `…/{vessel}/payload/{id}`, `…/{vessel}/overall[/session]`, `…/{vessel}/navigational[/session]`, `…/{vessel}/navigation` |
| calls `control_gate/v1` on responder `control-gate` (`--gate-responder-id`) | `command_assignment/{vessel}`, on every change and at `--heartbeat-s` |
| hears `resolved_control/{vessel}` and the `maintenance.vehicle_armed_key` | `maintenance_state/{vessel}`, on every transition and guard change, and at `--heartbeat-s` |
| | `alarm/authority-manager`: a lapsed session, a succession, a reconcile disagreement, an unapplied transfer |
| | liveliness: the source token, the RPC interface token, and the pubsub subjects |

Every record is republished at boot, so the router's storage agrees with the store. One lock decides:
each RPC is handled whole, including its gate round trips, before the next one starts.

## Running (development only)

```bash
authority-manager2keelson -r rise -e <vessel> --state-dir ./state --policy config/policy.json \
  --gate-responder-id control-gate --connect tcp/<router>:7447
```

**Bootstrapping the master** is a maintenance act, not an RPC:
`--assign-overall PRINCIPAL --maintainer WHO` assigns OVERALL_COMMAND by procedure at boot, **only
when nobody holds it**.
- The same principal again changes nothing.
- Anyone else holding it makes the manager **refuse to start**. A flag left in a compose file would
  otherwise re-assign its principal on every restart, silently undoing a succession: the stale
  master coming back that the revision exists to stop (I7). Ted and I reviewed this on 2026-11-04.
- It is refused for a process.
- It is audited as `bootstrap_overall` with the maintainer.

Every later change of master goes through a grant or `assume_command`.

## Developing before the build

The pin is an experimental build that carries both #331 and the interfaces branch. That build
includes only open PRs, so the interfaces branch must be opened as a PR before the build is cut.
Until the build exists, `uv sync` cannot resolve the pin. In the meantime, develop against the
interfaces worktree's SDK **without committing that**:

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ../keelson.worktrees/authority-interfaces/sdks/python pytest==8.3.4 ruff==0.14.11
.venv/bin/python -m pytest -q          # unit + e2e (e2e: loopback, multicast scouting off)
.venv/bin/ruff check .
```

When the build exists: set its exact version in `pyproject.toml`, run `uv lock`, and use
`uv sync --locked --extra dev`.

**Attestation needs keelson#383** (`feat/authority-attestation`, stacked on #377) and
`cryptography`. Develop against that worktree's SDK in the same way.

**Maintenance needs a newer build than the pin.** `MaintenanceState`, the four maintenance calls and
`ResolvedControl` are on keelson's `feat/maintenance-state` branch, stacked on #361 and #359, and
`0.6.0.dev21` does not carry them. Until an experimental build carries all three, develop against
that worktree's SDK, as above. CI on the pin fails the maintenance tests, and only those, until the
pin moves.

## Not in this cut

- Audit records on a subject of their own: `history` serves them from the store.
- The ENGINEER and PAYLOAD_AUTHORITY holder changes as gate transfers: today they are plain
  leases. Those transfers need the inherited constraint snapshot and the plant and payload gates.
- Fencing NAVIGATION_AUTHORITY on take-control and abort. The CONN is fenced; what happens to the
  automation's function assignments is not specified yet.
- During migration, compiling the map into `vehicle_control/v1 set_control_mapping`.
- The principal binding (`controller_id` to an authenticated principal).
