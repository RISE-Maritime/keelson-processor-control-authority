"""The gate port over `control_gate/v1`: the manager's client of the onboard motion gate.

Every call is bounded by `timeout_s`. No answer, or an ErrorResponse, is `GateUnreachable` -- never an
ACK and never a NAK: the service abandons an unanswered PREPARE and leaves an unanswered COMMIT
COMMITTED for reconcile() to resolve (service.py)."""

from __future__ import annotations

import keelson
from keelson.interfaces.ControlGate_pb2 import CommitResponse, GateState, GetGateStateRequest
from keelson.interfaces.ErrorResponse_pb2 import ErrorResponse
from keelson.payloads.ControlMapping_pb2 import TransferAck

from . import codec
from .service import GateUnreachable

INTERFACE, VERSION = "control_gate", "v1"


class ZenohGate:
    def __init__(self, session, *, realm: str, vessel_id: str, responder_id: str, timeout_s: float = 2.0):
        self.session = session
        self.realm = realm
        self.vessel_id = vessel_id
        self.responder_id = responder_id
        self.timeout_s = timeout_s

    def _call(self, procedure: str, request, reply_cls):
        key = keelson.construct_rpc_key(self.realm, self.vessel_id, INTERFACE, VERSION, procedure, self.responder_id)
        for reply in self.session.get(key, payload=request.SerializeToString(), timeout=self.timeout_s):
            if reply.ok is not None:
                out = reply_cls()
                out.ParseFromString(reply.ok.payload.to_bytes())
                return out
            err = ErrorResponse()
            try:
                err.ParseFromString(reply.err.payload.to_bytes())
            except Exception:  # noqa: BLE001 - an unparseable error is still an error
                pass
            raise GateUnreachable(f"{procedure}: {err.error_description or 'error reply'}")
        raise GateUnreachable(f"{procedure}: no answer from {self.responder_id} within {self.timeout_s} s")

    def prepare(self, transfer, prepare_ttl_ms):
        ack = self._call("prepare", codec.transfer_prepare(transfer, prepare_ttl_ms, self.vessel_id), TransferAck)
        return codec.ack_from_proto(ack)

    def commit(self, transfer_id, fence):
        resp = self._call("commit", codec.transfer_commit(transfer_id, fence), CommitResponse)
        return resp.applied, resp.reason

    def exposed(self):
        st = self._call("get_gate_state", GetGateStateRequest(), GateState)
        fence = codec.fence_from_proto(st.fence) if st.HasField("fence") else None
        return fence, tuple(codec.mapping_from_proto(m) for m in st.mappings), st.active_control_station
