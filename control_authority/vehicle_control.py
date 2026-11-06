"""MIGRATION: the committed map compiled into the vessel's own `vehicle_control/v1` mapping
(crowsnest-dev#269, #84).

Until the motion gate's output reaches an actuator, a vessel's `vehicle_control/v1 set_control_mapping`
(mavlink2keelson, Slipway's `sim`) is the only enforcement there is. With `--compile-to-vehicle-control`
the manager installs on it exactly the rows it has committed, and clears them when they stop being in
force. It is the one path by which the manager makes a vessel's helm follow a station, so it is off by
default.

THE VESSEL'S MAPPING IS A FUNCTION OF COMMITTED STATE, never an effect of one decision: `target()` reads
the CONN lease and the map in force, so a release, a lapse and an abort need no special case -- each
leaves no live conn holding the rows, and the target is empty.

A MAP THAT DOES NOT COMPILE WHOLE IS CLEARED, never partly installed (Ted, 2026-11-06): one row without
a source in the table, a DOF the vessel has no axis for, or an envelope vehicle_control cannot enforce
empties the whole target, and the problems are named. The gate's transfer stands; the vessel is just
not driven through vehicle_control. Nothing is guessed from a name: the table is maintenance
configuration (policy.py)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import CONN, State


@dataclass(frozen=True)
class AxisSource:
    """`ControlAxis`'s fields: where the vessel subscribes for this row's samples."""

    entity_id: str
    subject: str
    source_id: str
    unipolar: bool = False
    invert: bool = False


@dataclass(frozen=True)
class VehicleControlPolicy:
    responder: str
    axes: dict = field(default_factory=dict)  # dof -> the vessel's axis name ("steering", "throttle")
    sources: dict = field(default_factory=dict)  # (execution_source, dof) -> AxisSource
    min_interval_s: float = 0.0
    max_axis_age_s: float = 0.0  # 0: the vessel's own default dead-man


@dataclass(frozen=True)
class Target:
    """What the vessel's mapping should be: (axis, AxisSource) pairs sorted by axis, empty to stop
    driving. `problems` names why a map in force was emptied instead of installed."""

    axes: tuple = ()
    problems: tuple = ()


EMPTY = Target()


def _full_travel(envelope: tuple, unipolar: bool) -> bool:
    """vehicle_control has no envelope and no rate limit: a row may carry neither narrower than the
    device's travel. Rate 0 is no limit, as the gate reads it."""
    if not envelope:
        return True
    lo, hi, rate = envelope
    return lo <= (0.0 if unipolar else -100.0) and hi >= 100.0 and not rate


def target(state: State, vcp: VehicleControlPolicy, now_ms: int) -> Target:
    conn = state.live_lease(CONN, now_ms)
    rows = state.in_force.mappings
    # Only rows a LIVE conn holds drive: the lease the rows name must be the one in force, so a map
    # adopted from the gate under an older lease is never installed.
    if conn is None or not rows or any(m.authority_lease_id != conn.lease_id for m in rows):
        return EMPTY
    axes, problems = {}, []
    for m in rows:
        axis = vcp.axes.get(m.dof)
        src = vcp.sources.get((m.execution_source, m.dof))
        if axis is None:
            problems.append(f"{m.dof}: the vessel has no vehicle_control axis for it")
        elif src is None:
            problems.append(f"{m.dof}: no vehicle_control source for {m.execution_source}")
        elif not _full_travel(m.envelope, src.unipolar):
            problems.append(f"{m.dof}: envelope {list(m.envelope)} is narrower than vehicle_control can enforce")
        else:
            axes[axis] = src
    if problems:
        return Target((), tuple(problems))
    return Target(tuple(sorted(axes.items())))


def to_proto(t: Target, vcp: VehicleControlPolicy):
    from keelson.interfaces.VehicleControl_pb2 import ControlAxis, ControlAxisMapping

    out = ControlAxisMapping(min_interval_s=vcp.min_interval_s, max_axis_age_s=vcp.max_axis_age_s)
    for axis, s in t.axes:
        out.axes[axis].CopyFrom(ControlAxis(entity_id=s.entity_id, subject=s.subject, source_id=s.source_id,
                                            unipolar=s.unipolar, invert=s.invert))
    return out


def axes_json(t: Target) -> dict:
    return {axis: {"entity_id": s.entity_id, "subject": s.subject, "source_id": s.source_id} for axis, s in t.axes}
