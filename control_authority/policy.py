"""The vessel's control matrix and timings, from a local JSON file. Like the supervisor's policy it is
platform-registry data that changes only in maintenance (command_authority `not_solved`, "The values")."""

from __future__ import annotations

import json
from pathlib import Path

from .model import CONN, ENGINEER, PAYLOAD, PROFILES_ALLOWED, Policy


def parse(d: dict) -> Policy:
    profile = d.get("command_profile", "test")
    if profile not in PROFILES_ALLOWED:
        raise ValueError(f"unknown command_profile {profile!r}")
    dofs = tuple(d.get("dofs") or ())
    if not dofs:
        raise ValueError("a policy names the vessel's motion DOFs")
    groups = {str(k): tuple(v) for k, v in (d.get("groups") or {}).items()}
    for g, members in groups.items():
        if not set(members) <= set(dofs):
            raise ValueError(f"group {g} names a DOF the vessel does not have")
    ttl = d.get("lease_ttl_ms") or {}
    succession = {}
    for role, who in (d.get("succession") or {}).items():
        succession[role] = "*" if who == "*" else frozenset(who)
    return Policy(
        command_profile=profile,
        dofs=dofs,
        groups=groups,
        automation_sources=frozenset(d.get("automation_sources") or ()),
        processes=frozenset(d.get("processes") or ()),
        lease_ttl_ms={CONN: int(ttl.get(CONN, 2000)), ENGINEER: int(ttl.get(ENGINEER, 60000)), PAYLOAD: int(ttl.get(PAYLOAD, 5000))},
        session_ttl_ms=int(d.get("session_ttl_ms", 30000)),
        prepare_ttl_ms=int(d.get("prepare_ttl_ms", 2000)),
        succession=succession,
    )


def load(path: Path) -> Policy:
    return parse(json.loads(Path(path).read_text()))
