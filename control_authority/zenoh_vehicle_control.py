"""MIGRATION (crowsnest-dev#269): the vehicle_control port over `vehicle_control/v1 set_control_mapping`,
called only with `--compile-to-vehicle-control` (vehicle_control.py).

Every call is bounded by `timeout_s`. No answer, or an ErrorResponse, is `VehicleControlUnreachable`:
the service alarms and retries at the next tick (service._sync_vehicle)."""

from __future__ import annotations

import keelson
from keelson.interfaces.ErrorResponse_pb2 import ErrorResponse

from . import vehicle_control
from .service import VehicleControlUnreachable

INTERFACE, VERSION, PROCEDURE = "vehicle_control", "v1", "set_control_mapping"


class ZenohVehicleControl:
    def __init__(self, session, *, realm: str, vessel_id: str, vcp: vehicle_control.VehicleControlPolicy,
                 timeout_s: float = 1.0):
        self.session = session
        self.vcp = vcp
        self.timeout_s = timeout_s
        self.key = keelson.construct_rpc_key(realm, vessel_id, INTERFACE, VERSION, PROCEDURE, vcp.responder)

    def set(self, target: vehicle_control.Target) -> None:
        payload = vehicle_control.to_proto(target, self.vcp).SerializeToString()
        for reply in self.session.get(self.key, payload=payload, timeout=self.timeout_s):
            if reply.ok is not None:
                return
            err = ErrorResponse()
            try:
                err.ParseFromString(reply.err.payload.to_bytes())
            except Exception:  # noqa: BLE001 - an unparseable error is still an error
                pass
            raise VehicleControlUnreachable(err.error_description or "error reply")
        raise VehicleControlUnreachable(f"no answer from {self.vcp.responder} within {self.timeout_s} s")
