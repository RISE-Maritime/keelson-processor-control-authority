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

**A lost store.** A store that fails its integrity check, its schema or its hash is refused, and the
service does not start. Unlike the safety supervisor, there is no "more inhibited" state to fall
back to that would be safe here: who holds which role is not a thing to guess. The procedure for
re-initialising a store, which would reconcile everything from the gate, is not written yet.

## Keys

| Serves / calls | Writes |
| --- | --- |
| serves `authority_manager/v1` as responder `authority-manager` | `command_authority/{vessel}` (the conn), `…/{vessel}/engineer`, `…/{vessel}/payload/{id}`, `…/{vessel}/overall[/session]`, `…/{vessel}/navigational[/session]`, `…/{vessel}/navigation` |
| calls `control_gate/v1` on responder `control-gate` (`--gate-responder-id`) | `command_assignment/{vessel}`, on every change and at `--heartbeat-s` |
| | `alarm/authority-manager`: a lapsed session, a succession, a reconcile disagreement, an unapplied transfer |
| | liveliness: the source token, the RPC interface token, and the pubsub subjects |

Every record is republished at boot, so the router's storage agrees with the store. One lock decides:
each RPC is handled whole, including its gate round trips, before the next one starts.

## Running (development only)

```bash
authority-manager2keelson -r rise -e <vessel> --state-dir ./state --policy config/policy.json \
  --gate-responder-id control-gate --connect tcp/<router>:7447
```

**Bootstrapping the master** is a maintenance act, not an RPC: `--assign-overall PRINCIPAL` assigns
OVERALL_COMMAND by procedure, once, at boot. It is audited as `assign … by procedure`, refused for a
process, and changes nothing when that principal already holds it. Every later change of master goes
through a grant or `assume_command`.

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

## Not in this cut

- Audit records on a subject of their own: `history` serves them from the store.
- The ENGINEER and PAYLOAD_AUTHORITY holder changes as gate transfers: today they are plain
  leases. Those transfers need the inherited constraint snapshot and the plant and payload gates.
- Fencing NAVIGATION_AUTHORITY on take-control and abort. The CONN is fenced; what happens to the
  automation's function assignments is not specified yet.
- During migration, compiling the map into `vehicle_control/v1 set_control_mapping`.
- The principal binding (`controller_id` to an authenticated principal).
