"""The vessel's control matrix and timings, from a local JSON file. Like the supervisor's policy it is
platform-registry data that changes only in maintenance (command_authority `not_solved`, "The values")."""

from __future__ import annotations

import json
from pathlib import Path

from .attestation import parse_keys
from .model import CONN, ENGINEER, HOLD, PAYLOAD, PROFILES_ALLOWED, ZERO, MaintenancePolicy, Policy
from .vehicle_control import AxisSource, VehicleControlPolicy


def _maintenance(d: dict, dofs: tuple) -> MaintenancePolicy:
    rest = {str(k): str(v) for k, v in (d.get("rest") or {}).items()}
    for dof, how in rest.items():
        if dof not in dofs:
            raise ValueError(f"maintenance.rest names a DOF the vessel does not have: {dof}")
        if how not in (ZERO, HOLD):
            raise ValueError(f"maintenance.rest.{dof} is {how!r}: zero or hold")
    default = MaintenancePolicy()
    checks = tuple(d.get("required_checks", default.required_checks))
    # A vessel that requires no check would leave maintenance on nobody's word.
    if not checks:
        raise ValueError("maintenance.required_checks names at least one check")
    return MaintenancePolicy(
        rest=rest,
        zero_tolerance_pct=float(d.get("zero_tolerance_pct", default.zero_tolerance_pct)),
        feedback_max_age_ms=int(d.get("feedback_max_age_ms", default.feedback_max_age_ms)),
        input_max_age_ms=int(d.get("input_max_age_ms", default.input_max_age_ms)),
        payloads=tuple(d.get("payloads") or ()),
        required_checks=checks,
        armed_key=str(d.get("vehicle_armed_key", "")),
    )


def _vehicle_control(d: dict | None, dofs: tuple) -> VehicleControlPolicy | None:
    """The migration table (vehicle_control.py). Optional: only `--compile-to-vehicle-control` reads it."""
    if d is None:
        return None
    responder = str(d.get("responder") or "")
    if not responder:
        raise ValueError("vehicle_control.responder names the vessel's vehicle_control/v1 responder")
    axes = {str(k): str(v) for k, v in (d.get("axes") or {}).items()}
    for dof in axes:
        if dof not in dofs:
            raise ValueError(f"vehicle_control.axes names a DOF the vessel does not have: {dof}")
    shared = sorted({a for a in axes.values() if list(axes.values()).count(a) > 1})
    if shared:
        raise ValueError(f"vehicle_control.axes drives one axis from two DOFs: {', '.join(shared)}")
    sources = {}
    for row in d.get("sources") or ():
        key = (str(row.get("execution_source") or ""), str(row.get("dof") or ""))
        if not key[0] or key[1] not in dofs:
            raise ValueError(f"vehicle_control.sources: {key} needs an execution_source and a DOF of this vessel")
        if key in sources:
            raise ValueError(f"vehicle_control.sources names {key} twice")
        if not row.get("subject"):
            raise ValueError(f"vehicle_control.sources: {key} names no subject")
        sources[key] = AxisSource(str(row.get("entity_id") or ""), str(row["subject"]), str(row.get("source_id") or ""),
                                  bool(row.get("unipolar", False)), bool(row.get("invert", False)))
    return VehicleControlPolicy(responder, axes, sources, float(d.get("min_interval_s", 0.0)),
                                float(d.get("max_axis_age_s", 0.0)))


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
    att = d.get("attestation") or {}
    keys = parse_keys(att.get("keys") or {})
    required = bool(att.get("required", profile == "operational"))
    # Required with nobody to vouch: every command would be refused. Say so at load, not at sea.
    if required and not keys:
        raise ValueError("attestation is required (operational) but the policy names no trusted key")
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
        maintenance=_maintenance(d.get("maintenance") or {}, dofs),
        attestation_keys=keys,
        attestation_required=required,
        challenge_ttl_ms=int(att.get("challenge_ttl_ms", 15000)),
        vehicle_control=_vehicle_control(d.get("vehicle_control"), dofs),
        offer_ttl_ms=int(d.get("offer_ttl_ms", 30000)),
        payload_gates={str(k): str(v) for k, v in (d.get("payload_gates") or {}).items()},
    )


def load(path: Path) -> Policy:
    return parse(json.loads(Path(path).read_text()))
