# CLAUDE.md

The onboard authority manager (README). **Safety-critical: every change is reviewed by a human before
it is merged or run anywhere but the isolated tests.** Every commit message carries a
`Safety-review: required` trailer naming what changed.

## Rules that must not be broken

- **It never mints a motion fence.** It proposes; the gate reserves and ACKs; the manager records
  COMMITTED before it sends COMMIT. No code path builds a `Fence` from the manager's own numbers.
- **Durable before effective.** A decision's state and audit commit in one transaction; gate calls
  and publications run after it (service.py). Never send first and record later.
- **Reconcile before any transfer**, toward the gate. A restart interrupts no healthy holder and mints
  nothing.
- **Maintenance never arms and never commands the vehicle.** Its guards read what the manager has
  heard, and an unheard, stale or non-VALID input never satisfies one. The procedure leaves
  MAINTENANCE only through the required checks.
- **A bad attestation is never ignored.** If one is present it is verified, whether or not the
  profile requires it, and a refusal names the check. The principal is recorded from the claim,
  never from `controller_id`.
- **Pure decisions.** `control_authority/model.py` has no clock, no store and no Zenoh; every
  decision is tested there or through service.py against `tests/fakegate.py`.
- **One lock decides** (rpc.Handlers): every request, tick and heartbeat, gate round trips included.
- **Tests never touch a network**, and nothing runs against a vessel or the shared Zenoh network.
- **keelson is an exact published pin** (README, "Developing before the build"). Never commit a path
  or a local build.

## Commands

```bash
.venv/bin/python -m pytest -q          # all
.venv/bin/python -m pytest -m e2e    # the CLI against a fake gate on an isolated loopback peer
.venv/bin/ruff check .
```
