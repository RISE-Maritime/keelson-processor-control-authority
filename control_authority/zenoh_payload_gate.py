"""The payload-gate port over `payload_control/v1 get_payload_mapping` (crowsnest-dev#270): the
payload_epoch a payload's own gate shows now, which the manager reads back onto the PAYLOAD_AUTHORITY
record. Built only when the policy names `payload_gates`.

Every call is bounded by `timeout_s`; no answer or an error reply is None, never a fence."""

from __future__ import annotations

import logging

import keelson

from .model import Fence

log = logging.getLogger(__name__)


class ZenohPayloadGates:
    def __init__(self, session, *, realm: str, vessel_id: str, responders: dict, timeout_s: float = 1.0):
        # PayloadControl is not in every SDK this manager is developed against: imported only when used.
        from keelson.interfaces.PayloadControl_pb2 import GetPayloadMappingRequest, PayloadMapping

        self._req, self._reply = GetPayloadMappingRequest, PayloadMapping
        self.session = session
        self.realm, self.vessel_id = realm, vessel_id
        self.responders = dict(responders)
        self.timeout_s = timeout_s

    def epoch(self, payload_id: str) -> Fence | None:
        responder = self.responders.get(payload_id)
        if not responder:
            return None
        key = keelson.construct_rpc_key(self.realm, self.vessel_id, "payload_control", "v1", "get_payload_mapping", responder)
        try:
            for reply in self.session.get(key, payload=self._req(payload_id=payload_id).SerializeToString(), timeout=self.timeout_s):
                if reply.ok is None:
                    return None
                m = self._reply()
                m.ParseFromString(reply.ok.payload.to_bytes())
                if not m.HasField("payload_epoch") or not m.payload_epoch.term:
                    return None
                return Fence(m.payload_epoch.term, int(m.payload_epoch.generation))
        except Exception as e:  # noqa: BLE001 - an unreachable gate is no fence, never a crash of the manager
            log.warning("payload gate %s: %s", responder, e)
        return None
