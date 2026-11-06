"""A gate that follows COMMAND-ARCHITECTURE §4 exactly as far as the manager can see it: it owns both
halves of the fence, reserves (T, n+1) on an ACK, changes authority ONLY on COMMIT, drops an orphaned
PREPARE on its own clock, can fence alone, and NAKs a whole transfer if one DOF is not neutral or
matched (I10). It is a test double, not the gate (crowsnest-dev #85)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, replace

from control_authority.model import Fence
from control_authority.service import GateAck, GateUnreachable


@dataclass
class Reservation:
    transfer_id: str
    fence: object
    mappings: tuple
    station: str
    deadline_ms: int


class FakeGate:
    def __init__(self, clock):
        self.clock = clock
        self.term = secrets.token_hex(16)
        self.generation = 1
        self.mappings: tuple = ()
        self.station = ""
        self.reservation: Reservation | None = None
        self.unready: set[str] = set()  # DOFs whose incoming source is neither neutral nor matched
        self.down = False
        self.prepares: list = []  # what it was sent, for assertions
        self.last_cause = None  # GateState.last_fence_cause (#290): set by fence_alone, cleared by a COMMIT
        self.on_prepare = None  # hooks a test can use to look at the manager's store mid-call
        self.on_commit = None

    @property
    def fence(self) -> Fence:
        return Fence(self.term, self.generation)

    def _up(self):
        if self.down:
            raise GateUnreachable("gate down")

    def _expire(self):
        if self.reservation is not None and self.clock() > self.reservation.deadline_ms:
            self.reservation = None

    def prepare(self, transfer, prepare_ttl_ms):
        self._up()
        self.prepares.append(transfer)
        if self.on_prepare:
            self.on_prepare(transfer)
        self._expire()
        if self.reservation is not None:
            return GateAck(False, None, f"transfer {self.reservation.transfer_id} is reserved")
        bad = [m.mapping_id for m in transfer.mappings if m.dof in self.unready]
        if bad:
            return GateAck(False, None, "not neutral or matched", tuple(bad))  # all or nothing
        fence = Fence(self.term, self.generation + 1)
        self.reservation = Reservation(transfer.transfer_id, fence, transfer.mappings, transfer.station,
                                       self.clock() + prepare_ttl_ms)
        return GateAck(True, fence)

    def commit(self, transfer_id, fence):
        self._up()
        if self.on_commit:
            self.on_commit(transfer_id)
        self._expire()
        r = self.reservation
        if r is None or r.transfer_id != transfer_id or r.fence != fence:
            return False, "no such reservation"
        self.generation = fence.generation
        self.mappings = tuple(replace(m) for m in r.mappings)
        self.last_cause = None  # a transfer moved the fence: no cause of an older fence survives it
        self.station = r.station
        self.reservation = None
        return True, ""

    def exposed(self):
        self._up()
        return self.fence, self.mappings, self.station, self.last_cause

    # ---- what happens to a gate without the manager

    def fence_alone(self, kind="conn_lapse", **who):
        """A CONN lapse, an invalid source, an abort with the manager dead: the gate increments by
        itself, enters its transition map, and records why (`last_cause`, #290)."""
        from control_authority.model import GateCause

        self.generation += 1
        self.mappings = ()
        self.last_cause = GateCause(kind, self.fence, **who)

    def restart(self):
        """The gate restarts with its store intact: the fence it activated survives, any reservation
        does not."""
        self.reservation = None

    def reinitialise(self):
        self.term = secrets.token_hex(16)
        self.generation = 1
        self.mappings = ()
        self.reservation = None
