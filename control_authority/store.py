"""The manager's own durable store: the state (leases, assignments, the navigation document, the
station, what is in force, every transfer) and the audit trail, in one sqlite database (stdlib, WAL,
synchronous=FULL).

THE TRANSACTION IS THE POINT (I13). A decision's new state and its audit records commit together, and
the service runs the decision's effects -- the PREPARE, the COMMIT, the publications -- only after the
commit. So PREPARED is durable before the gate hears the proposal, and COMMITTED(map, station, fence)
before the gate hears the COMMIT; a crash at any point leaves something reconcile() can resolve.

A store that cannot be trusted -- corrupt, a schema it does not know, a state that fails its hash --
is refused, never read as "nobody holds anything". Unlike the safety supervisor there is no safe
default to fall back to: the service will not start until an operator decides (README, "A lost
store"). `reconciled` is never persisted: every boot starts unreconciled.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

from .model import (
    MAINTENANCE_STATES,
    Assignment,
    Check,
    Fence,
    InForce,
    Lease,
    Maintenance,
    Mapping,
    NavFunction,
    Offer,
    State,
    Succession,
    Transfer,
)

SCHEMA_VERSION = 1


class UntrustedStore(Exception):
    pass


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _digest(state_json: str) -> str:
    return hashlib.sha256(f"v{SCHEMA_VERSION}:{state_json}".encode()).hexdigest()


# ---------------------------------------------------------------------------- codec


def state_to_json(st: State) -> dict:
    d = asdict(replace(st, reconciled=False))
    d.pop("reconciled")
    return d


def _fence(d):
    return None if d is None else Fence(d["term"], int(d["generation"]))


def _mapping(d):
    return Mapping(d["dof"], d["authority_holder"], d["authority_lease_id"], d["execution_source"],
                   tuple(d.get("envelope") or ()), d.get("group_id", ""), d.get("mapping_id", ""))


def _assignment(d):
    if d is None:
        return None
    s = d.get("succession")
    succ = None if s is None else Succession(s["previous_assignee"], s["reason"], tuple(s["evidence"]), tuple(s["authorised_by"]))
    return Assignment(d["assignee"], d["assignment_id"], int(d["revision"]), bool(d.get("synthesised")), succ)


def _maintenance(d):
    # A store written before maintenance existed had no procedure in force: INACTIVE.
    if d is None:
        return Maintenance()
    if d["state"] not in MAINTENANCE_STATES:
        raise ValueError(f"unknown maintenance state {d['state']!r}")
    return Maintenance(**{**d, "scopes": tuple(d["scopes"]), "checks": tuple(Check(**c) for c in d["checks"])})


def state_from_json(d: dict) -> State:
    return State(
        vessel_id=d["vessel_id"],
        # `fence` and `offers` arrived with crowsnest-dev#270; a store written before them has neither.
        leases={k: Lease(**{**v, "principal": dict(v.get("principal") or {}), "fence": _fence(v.get("fence"))})
                for k, v in d["leases"].items()},
        overall=_assignment(d["overall"]),
        navigational=_assignment(d["navigational"]),
        navigation={k: NavFunction(v["holder"], v["holder_lease_id"], int(v["method"]), dict(v.get("envelope") or {}))
                    for k, v in d["navigation"].items()},
        navigation_revision=int(d["navigation_revision"]),
        active_station=d["active_station"],
        responsible_roc=d["responsible_roc"],
        in_force=InForce(tuple(_mapping(m) for m in d["in_force"]["mappings"]), d["in_force"]["station"],
                         _fence(d["in_force"]["fence"])),
        transfers={k: Transfer(**{**v, "mappings": tuple(_mapping(m) for m in v["mappings"]), "fence": _fence(v["fence"]),
                                  "principal": dict(v.get("principal") or {})})
                   for k, v in d["transfers"].items()},
        maintenance=_maintenance(d.get("maintenance")),
        offers={k: Offer(**v) for k, v in (d.get("offers") or {}).items()},
        reconciled=False,
    )


# ---------------------------------------------------------------------------- the store


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Explicit BEGIN/COMMIT below. The RPC handlers run on zenoh's queryable threads, so the
        # connection crosses threads -- safely, because every access is under rpc.Handlers' one lock.
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              at_ms INTEGER NOT NULL,
              record TEXT NOT NULL
            );
            """
        )

    def close(self):
        self.db.close()

    def load(self) -> State | None:
        """The stored state, or None for a store that was never written. Raises UntrustedStore."""
        try:
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise UntrustedStore("the store failed its integrity check")
            rows = dict(self.db.execute("SELECT k, v FROM meta").fetchall())
        except sqlite3.DatabaseError as e:
            raise UntrustedStore(f"the store is unreadable: {e}") from e
        if "state" not in rows:
            return None
        if rows.get("schema") != str(SCHEMA_VERSION):
            raise UntrustedStore(f"unknown store schema {rows.get('schema')!r}")
        if _digest(rows["state"]) != rows.get("state_hash"):
            raise UntrustedStore("the stored state does not match its hash")
        try:
            return state_from_json(json.loads(rows["state"]))
        except (ValueError, KeyError, TypeError) as e:
            raise UntrustedStore(f"the stored state is inconsistent: {e}") from e

    def commit(self, st: State, audits=(), at_ms: int = 0) -> None:
        """The state and its audit records, in ONE transaction."""
        state_json = _canonical(state_to_json(st))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.executemany(
                "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                [("schema", str(SCHEMA_VERSION)), ("state", state_json), ("state_hash", _digest(state_json))],
            )
            self.db.executemany("INSERT INTO audit (at_ms, record) VALUES (?, ?)",
                                [(at_ms, _canonical(a)) for a in audits])
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def audit(self, limit: int = 1000) -> list[dict]:
        rows = self.db.execute("SELECT record FROM audit ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def audit_after(self, after_seq: int = 0, limit: int = 1000) -> list[tuple[int, int, dict]]:
        """(seq, at_ms, record), oldest first. `at_ms` is the wall clock the service stamped."""
        rows = self.db.execute("SELECT seq, at_ms, record FROM audit WHERE seq > ? ORDER BY seq LIMIT ?",
                               (after_seq, limit)).fetchall()
        return [(int(s), int(t), json.loads(r)) for s, t, r in rows]
