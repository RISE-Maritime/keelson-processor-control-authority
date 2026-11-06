"""ENGINEER and PAYLOAD_AUTHORITY holder changes (crowsnest-dev#270): the holder offers, the named holder
accepts with their own request, and the new lease's token is the change its gate observes and mints
for. The manager mints nothing; it reads the gate's fence back onto the record. No network."""

from dataclasses import replace

import pytest

from control_authority import codec, model
from control_authority.model import Fence, HeardConstraint
from control_authority.service import Manager
from control_authority.store import Store
from fakegate import FakeGate
from test_manager import POLICY, V, Bus, Clock

ANNA, BO, CY = "anna@ECR", "bo@ECR", "cy@ROC-1"
ENG = model.slot_of(model.ENGINEER)
GIMBAL = model.slot_of(model.PAYLOAD, "gimbal")
TERM = "p" * 32


class FakePayloadGate:
    """A payload gate's get_payload_mapping, as far as the manager reads it: the epoch it shows now."""

    def __init__(self):
        self.epoch_now: Fence | None = Fence("g" * 32, 1)
        self.calls = 0

    def epoch(self, payload_id):
        self.calls += 1
        return self.epoch_now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gate(clock):
    return FakeGate(clock)


def make(tmp_path, clock, gate, policy=POLICY, payload_gates=None, bus=None):
    m = Manager(store=Store(tmp_path / "authority.sqlite"), policy=policy, vessel_id=V, gate=gate, publish=bus or Bus(),
                now_ms=clock, payload_gates=payload_gates)
    m.boot()
    return m


def derate(holder=ANNA, generation=2, revision=1):
    return HeardConstraint(Fence(TERM, generation), revision, holder, (("stbd", 40.0, False, "bearing temperature"),))


def offer(m, slot=ENG, to=BO, by=ANNA):
    role, pid = (model.ENGINEER, "") if slot == ENG else (model.PAYLOAD, slot.split("/", 1)[1])
    return m.offer(role, by, pid, m.state.leases[slot].token, to, "ECR")


# ---------------------------------------------------------------------------- offer, then accept


def test_the_holder_offers_and_only_the_named_holders_request_takes_it(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    assert m.take(model.ENGINEER, ANNA, "ECR").outcome == model.ACCEPTED
    old = m.state.leases[ENG]
    assert m.take(model.ENGINEER, BO, "ECR").outcome == model.REFUSED_HELD
    assert offer(m).outcome == model.ACCEPTED
    assert m.state.leases[ENG] == old  # an offer moves nothing
    assert m.take(model.ENGINEER, CY, "ROC-1").outcome == model.REFUSED_HELD  # not offered to them
    d = m.take(model.ENGINEER, BO, "ECR")
    assert d.outcome == model.ACCEPTED and "taken over from anna@ECR" in d.detail
    new = m.state.leases[ENG]
    assert new.holder == BO and new.token != old.token and new.fence is None and not m.state.offers
    assert [a["event"] for a in m.history()][-1] == "handed_over"


@pytest.mark.parametrize("bad, outcome", [
    (dict(token="not-hers"), model.REFUSED_UNAUTHORISED),
    (dict(requester=BO), model.REFUSED_UNAUTHORISED),
    (dict(to=ANNA), model.REFUSED_MALFORMED),
    (dict(to=""), model.REFUSED_MALFORMED),
    (dict(role=model.CONN), model.REFUSED_MALFORMED),
])
def test_only_the_holder_offers_and_to_someone_else(tmp_path, clock, gate, bad, outcome):
    m = make(tmp_path, clock, gate)
    m.take(model.ENGINEER, ANNA, "ECR")
    args = dict(role=model.ENGINEER, requester=ANNA, payload_id="", token=m.state.leases[ENG].token, to=BO, to_site="ECR")
    args.update(bad)
    assert model.offer_lease(m.state, POLICY, args["role"], args["payload_id"], args["requester"], args["token"], args["to"],
                             args["to_site"], clock()).outcome == outcome


def test_an_offer_expires_and_ends_with_the_lease_that_made_it(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    m.take(model.ENGINEER, ANNA, "ECR")
    offer(m)
    clock.t += POLICY.offer_ttl_ms + 1
    m.renew(ENG, m.state.leases[ENG].token)
    m.tick()
    assert not m.state.offers and m.take(model.ENGINEER, BO, "ECR").outcome == model.REFUSED_HELD
    offer(m)
    assert m.release(ENG, m.state.leases[ENG].token).outcome == model.ACCEPTED
    assert not m.state.offers


def test_an_expired_offer_is_refused_at_accept_even_before_a_tick_drops_it(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    m.take(model.ENGINEER, ANNA, "ECR")
    offer(m)
    clock.t += POLICY.offer_ttl_ms + 1
    m.renew(ENG, m.state.leases[ENG].token)  # Anna is still there; no tick has run
    assert m.take(model.ENGINEER, BO, "ECR").outcome == model.REFUSED_HELD


def test_a_payload_offer_is_refused_in_maintenance_and_the_offer_survives_a_restart(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    m.take(model.PAYLOAD, CY, "ROC-1", "gimbal")
    offer(m, GIMBAL, to=BO, by=CY)
    m.store.close()
    again = make(tmp_path, clock, gate)
    assert again.state.offers[GIMBAL].to_holder == BO
    again.state = replace(again.state, maintenance=replace(again.state.maintenance, state=model.M_PREPARING))
    assert offer(again, GIMBAL, to=BO, by=CY).outcome == model.REFUSED_MAINTENANCE


def test_a_free_slot_is_taken_as_before(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    d = m.take(model.ENGINEER, ANNA, "ECR")
    assert d.outcome == model.ACCEPTED and m.state.leases[ENG].holder == ANNA


def test_a_payload_handover_proposes_nothing_to_the_motion_gate(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    before = (gate.generation, len(gate.prepares))
    m.take(model.PAYLOAD, CY, "ROC-1", "gimbal")
    offer(m, GIMBAL, to=BO, by=CY)
    assert m.take(model.PAYLOAD, BO, "ECR", "gimbal").outcome == model.ACCEPTED
    m.take(model.ENGINEER, ANNA, "ECR")
    assert (gate.generation, len(gate.prepares)) == before


# ---------------------------------------------------------------------------- the inherited constraint


def test_the_incoming_engineer_is_told_what_they_inherit(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    assert "not read yet" in m.take(model.ENGINEER, ANNA, "ECR").detail
    m.observe_constraint(derate())
    offer(m)
    d = m.take(model.ENGINEER, BO, "ECR")
    assert "inherits stbd 40 % (revision 1 under plant_generation 2, by anna@ECR)" in d.detail
    assert m.history()[-1]["inherits"].startswith("stbd 40 %")


def test_no_constraint_in_storage_is_said_as_none_not_as_unread(tmp_path, clock, gate):
    m = make(tmp_path, clock, gate)
    m.observe_constraint(None)
    assert "no plant constraint in force" in m.take(model.ENGINEER, ANNA, "ECR").detail


# ---------------------------------------------------------------------------- the gate's fence, read back


def test_the_engineer_record_carries_the_plant_generation_the_gate_published_for_them(tmp_path, clock, gate):
    bus = Bus()
    m = make(tmp_path, clock, gate, bus=bus)
    m.take(model.ENGINEER, ANNA, "ECR")
    m.observe_constraint(derate(ANNA, 2))
    assert m.state.leases[ENG].fence == Fence(TERM, 2)
    offer(m)
    m.take(model.ENGINEER, BO, "ECR")
    m.observe_constraint(derate(ANNA, 2, revision=5))  # the outgoing holder's: not Bo's fence
    assert m.state.leases[ENG].fence is None
    m.observe_constraint(derate(BO, 3))  # the gate's rebase for Bo, at revision 1
    assert m.state.leases[ENG].fence == Fence(TERM, 3)
    m.observe_constraint(derate(BO, 2))  # a stale replay never replaces a newer fence
    assert m.state.leases[ENG].fence == Fence(TERM, 3)
    rec = codec.lease_record(m.state, ENG, "authority-manager/0")
    assert (rec.fence.term, rec.fence.generation) == (TERM, 3)
    assert {"fence_recorded"} <= {a["event"] for a in m.history()}


def test_a_payload_holders_epoch_is_read_back_only_once_the_gate_shows_a_newer_one(tmp_path, clock, gate):
    pol = replace(POLICY, payload_gates={"gimbal": "payload-gimbal"})
    pg = FakePayloadGate()
    m = make(tmp_path, clock, gate, policy=pol, payload_gates=pg)
    m.take(model.PAYLOAD, CY, "ROC-1", "gimbal")  # the baseline is (g, 1), taken before the lease went out
    m.tick()
    assert m.state.leases[GIMBAL].fence is None  # the gate still shows the old epoch
    pg.epoch_now = Fence("g" * 32, 2)  # it has seen Cy's token
    m.tick()
    assert m.state.leases[GIMBAL].fence == Fence("g" * 32, 2)
    calls = pg.calls
    m.tick()
    assert pg.calls == calls  # recorded: not asked again
    offer(m, GIMBAL, to=BO, by=CY)
    m.take(model.PAYLOAD, BO, "ECR", "gimbal")  # baseline (g, 2): Cy's epoch is never Bo's
    m.tick()
    assert m.state.leases[GIMBAL].fence is None
    pg.epoch_now = Fence("g" * 32, 3)
    m.tick()
    assert m.state.leases[GIMBAL].fence == Fence("g" * 32, 3)
    rec = codec.lease_record(m.state, GIMBAL, "authority-manager/0")
    assert rec.fence.generation == 3 and rec.payload_id == "gimbal"


def test_with_no_baseline_no_payload_epoch_is_ever_called_theirs(tmp_path, clock, gate):
    pol = replace(POLICY, payload_gates={"gimbal": "payload-gimbal"})
    pg = FakePayloadGate()
    pg.epoch_now = None  # the gate does not answer when the lease goes out
    m = make(tmp_path, clock, gate, policy=pol, payload_gates=pg)
    m.take(model.PAYLOAD, CY, "ROC-1", "gimbal")
    pg.epoch_now = Fence("g" * 32, 7)
    m.tick()
    assert m.state.leases[GIMBAL].fence is None


def test_without_payload_gates_nothing_is_asked(tmp_path, clock, gate):
    pg = FakePayloadGate()
    m = make(tmp_path, clock, gate, payload_gates=pg)  # POLICY names no payload gate
    m.take(model.PAYLOAD, CY, "ROC-1", "gimbal")
    m.tick()
    assert pg.calls == 0
