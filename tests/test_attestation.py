"""The ROC service's attestation, verified onboard (crowsnest-dev#312, keelson#383): each check in its
order, refusing with its own reason; freshness by our own challenge; the principal on the lease, the
transfer and the audit. A throwaway Ed25519 key; no network."""

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from keelson.interfaces import AuthorityManager_pb2 as pb
from keelson.interfaces.VehicleCommon_pb2 import CommandResult
from keelson.payloads.CommandAuthority_pb2 import Role
from keelson.payloads.ControlMapping_pb2 import ControlMapping

from control_authority import attestation, model
from control_authority.policy import parse
from control_authority.rpc import Handlers
from control_authority.service import Manager
from control_authority.store import Store
from fakegate import FakeGate
from test_manager import DOFS, Bus, Clock

ACCEPTED, DENIED = CommandResult.COMMAND_RESULT_ACCEPTED, CommandResult.COMMAND_RESULT_DENIED
KEY = Ed25519PrivateKey.generate()
OTHER = Ed25519PrivateKey.generate()


def b64(priv) -> str:
    return base64.b64encode(priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def policy(profile="operational", **att):
    return parse({
        "command_profile": profile, "dofs": DOFS, "groups": {"throttles": ["throttle_port", "throttle_stbd"]},
        "attestation": {"keys": {"roc1-a": {"service_id": "roc1-authority", "public_key": b64(KEY)}}, **att},
    })


@pytest.fixture
def h(tmp_path):
    return make(tmp_path)


def make(tmp_path, pol=None):
    clock = Clock()
    gate = FakeGate(clock)
    m = Manager(store=Store(tmp_path / "a.sqlite"), policy=pol or policy(), vessel_id="usv-alpha", gate=gate,
                publish=Bus(), now_ms=clock)
    m.boot()
    handlers = Handlers(m, "authority-manager/0", 5, 15)
    handlers.gate, handlers.clock = gate, clock
    return handlers


class Service:
    """The ROC service: signs a claim for one call, its sequence rising per session."""

    def __init__(self, h, key=KEY, key_id="roc1-a", service_id="roc1-authority", session="sess-9"):
        self.h, self.key, self.key_id, self.service_id, self.session = h, key, key_id, service_id, session
        self.seq = 0

    def sign(self, req, procedure, *, sequence=None, challenge=None, operator="ted@ROC-1", site="ROC-1"):
        self.seq = sequence if sequence is not None else self.seq + 1
        live = self.h.get(pb.GetAuthorityRequest()).attestation_challenges
        claim = pb.AttestationClaim(
            service_id=self.service_id, key_id=self.key_id, operator_id=operator, site=site, station="ROC-1/console-2",
            session_id=self.session, sequence=self.seq, challenge=challenge if challenge is not None else live[0],
            procedure=procedure, request_sha256=attestation.request_digest(req)).SerializeToString()
        req.caller.attestation.claim = claim
        req.caller.attestation.signature = self.key.sign(claim)
        return req


def take_conn(who="ted@ROC-1", site="ROC-1"):
    rows = [ControlMapping(dof=d, execution_source=f"{site}/helm-1/{d}") for d in DOFS]
    return pb.RequestRoleRequest(caller=pb.Caller(controller_id=who, controller_site=site), role=Role.ROLE_CONN,
                                 mappings=rows, reason="taking the helm")


# ---------------------------------------------------------------------------- accepted


def test_a_good_attestation_is_accepted_and_its_principal_is_on_lease_transfer_and_audit(tmp_path):
    h = make(tmp_path, policy("test"))
    svc = Service(h)
    r = h.request(svc.sign(take_conn(), "request"))
    assert r.result == ACCEPTED, r.detail
    st = h.m.state
    who = st.leases[model.CONN].principal
    assert (who["operator"], who["site"], who["service"], who["session"]) == ("ted@ROC-1", "ROC-1", "roc1-authority", "sess-9")
    assert next(iter(st.transfers.values())).principal == who
    audits = h.m.history()
    assert audits and all(a.get("principal") == who for a in audits if a["event"] in ("prepared", "committed", "active"))
    hist = h.history(pb.HistoryRequest())
    assert any('"principal"' in r.detail_json and "sess-9" in r.detail_json for r in hist.records)


def test_the_challenge_is_on_the_feed_and_in_get_and_rotates_at_the_heartbeat(h):
    first = list(h.get(pb.GetAuthorityRequest()).attestation_challenges)
    assert len(first) == 1 and len(first[0]) == 16
    h.clock.t += 5000
    h.heartbeat()
    now = list(h.get(pb.GetAuthorityRequest()).attestation_challenges)
    assert now[0] != first[0] and first[0] in now  # the newest first, the last still accepted
    assert h.get(pb.GetAuthorityRequest()).assignments.attestation_challenge == now[0]


# ---------------------------------------------------------------------------- refused, each on its own


def refused(reply, why):
    return reply.result == DENIED and reply.refusal == pb.Refusal.REFUSAL_ATTESTATION and why in reply.detail


def test_missing_where_required(h):
    assert refused(h.request(take_conn()), "requires an attestation")


def test_an_unknown_key_or_a_key_claimed_by_another_service(h):
    assert refused(h.request(Service(h, key_id="roc9").sign(take_conn(), "request")), "not one this vessel trusts")
    assert refused(h.request(Service(h, service_id="impostor").sign(take_conn(), "request")), "belongs to roc1-authority")


def test_a_bad_signature(h):
    assert refused(h.request(Service(h, key=OTHER).sign(take_conn(), "request")), "signature does not verify")


def test_a_stale_challenge_after_its_ttl_on_our_clock(h):
    req = Service(h).sign(take_conn(), "request")
    h.clock.t += h.m.policy.challenge_ttl_ms + 1
    h.heartbeat()  # a fresh one now; the claim names the old
    assert refused(h.request(req), "challenge is not one this manager issued")


def test_a_challenge_this_manager_never_issued_as_after_a_restart(h):
    assert refused(h.request(Service(h).sign(take_conn(), "request", challenge=b"\x00" * 16)), "challenge is not one")


def test_a_replay_and_a_sequence_that_does_not_rise(tmp_path):
    h = make(tmp_path, policy("test"))
    svc = Service(h)
    first = svc.sign(take_conn(), "request")
    assert h.request(first).result == ACCEPTED
    assert refused(h.request(first), "a replay")  # the same attestation again
    assert refused(h.request(svc.sign(take_conn(), "request", sequence=1)), "not above 1")


def test_another_procedures_attestation(h):
    assert refused(h.request(Service(h).sign(take_conn(), "take_control")), "is for 'take_control', not 'request'")


def test_a_request_changed_after_it_was_signed(h):
    req = Service(h).sign(take_conn(), "request")
    req.reason = "something else"
    assert refused(h.request(req), "another request")


def test_a_label_that_disagrees_with_the_claim(h):
    # Signed for anna, then the label says ted: the label never outranks the attested principal.
    req = take_conn("ted@ROC-1")
    Service(h).sign(req, "request", operator="anna@ROC-1")
    assert refused(h.request(req), "is not the attested anna@ROC-1")


def test_refused_on_every_mutating_procedure_with_its_own_reply_type(h):
    caller = pb.Caller(controller_id="ted@ROC-1", controller_site="ROC-1")
    for call, req in [
        (h.renew, pb.RenewRequest(caller=caller, role=Role.ROLE_CONN)),
        (h.release, pb.ReleaseRequest(caller=caller, role=Role.ROLE_CONN)),
        (h.take_control, pb.TakeControlRequest(caller=caller)),
        (h.abort_automation, pb.AbortAutomationRequest(caller=caller)),
        (h.assume_command, pb.AssumeCommandRequest(caller=caller, role=Role.ROLE_OVERALL_COMMAND)),
        (h.set_navigation_authority, pb.SetNavigationAuthorityRequest(caller=caller)),
        (h.request_maintenance, pb.RequestMaintenanceRequest(caller=caller)),
        (h.complete_maintenance_check, pb.CompleteMaintenanceCheckRequest(caller=caller)),
    ]:
        r = call(req)
        assert refused(r, "requires an attestation"), call.__name__
    assert h.get(pb.GetAuthorityRequest()).vessel_id == "usv-alpha"  # reads need none


# ---------------------------------------------------------------------------- not required


def test_where_not_required_a_missing_one_passes_and_a_bad_one_is_still_refused(tmp_path):
    h = make(tmp_path, policy("test"))
    assert h.request(take_conn()).result == ACCEPTED
    assert h.m.state.leases[model.CONN].principal == {}  # nothing attested, nothing claimed
    assert refused(h.request(Service(h, key=OTHER).sign(take_conn("anna@ROC-1"), "request")), "signature")


# ---------------------------------------------------------------------------- policy and store


@pytest.mark.parametrize("keys,why", [
    ({"k": {"service_id": "s", "public_key": "not base64!"}}, "not base64"),
    ({"k": {"service_id": "s", "public_key": base64.b64encode(b"short").decode()}}, "32 bytes"),
    ({"k": {"public_key": b64(KEY)}}, "needs service_id"),
])
def test_a_malformed_key_is_refused(keys, why):
    with pytest.raises(ValueError, match=why):
        parse({"command_profile": "test", "dofs": DOFS, "attestation": {"keys": keys}})


def test_operational_with_nobody_to_vouch_refuses_to_load():
    with pytest.raises(ValueError, match="names no trusted key"):
        parse({"command_profile": "operational", "dofs": DOFS})
    assert parse({"command_profile": "test", "dofs": DOFS}).attestation_required is False
    assert policy().attestation_required is True


def test_the_principal_survives_a_restart_and_the_challenges_do_not(tmp_path):
    h = make(tmp_path, policy("test"))
    svc = Service(h)
    req = svc.sign(take_conn(), "request")
    assert h.request(req).result == ACCEPTED
    who = h.m.state.leases[model.CONN].principal
    h.m.store.close()
    h2 = make(tmp_path, policy("test"))
    assert h2.m.state.leases[model.CONN].principal == who
    stale = Service(h2, session="sess-10")
    assert refused(h2.request(stale.sign(take_conn(), "request", challenge=svc_challenge(req))), "challenge is not one")


def svc_challenge(req) -> bytes:
    return pb.AttestationClaim.FromString(req.caller.attestation.claim).challenge
