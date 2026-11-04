"""The model's records as keelson #331's protobufs, and back."""

from control_authority import codec, model
from control_authority.model import Assignment, Fence, InForce, Lease, Mapping, NavFunction, State, Transfer
from keelson.payloads.CommandAuthority_pb2 import RecordKind, Role
from keelson.payloads.ControlMapping_pb2 import TransferAck, TransferKind


def _state():
    f = Fence("ab" * 16, 7)
    row = Mapping("rudder", "ted@ROC-1", "L1", "ROC-1/helm-1/rudder", (-100.0, 100.0, 20.0), "", "m1")
    return State(vessel_id="usv-alpha", active_station="ROC-1",
                 leases={"conn": Lease("conn", "ted@ROC-1", "ROC-1", "tok", "L1", 0, 2000)},
                 overall=Assignment("ted@ROC-1", "as-1", 3), in_force=InForce((row,), "ROC-1", f),
                 navigation={"track_keeping": NavFunction("navigation/0@usv-alpha", "L9", 4, {"reroute_allowed": True})},
                 navigation_revision=2)


def test_a_prepare_carries_the_kind_the_rows_and_no_fence():
    st = _state()
    t = Transfer("t1", model.TAKE_CONTROL, st.in_force.mappings, "ROC-1", "anna@ROC-2", "pick up", model.PREPARED)
    pb = codec.transfer_prepare(t, 2000, "usv-alpha")
    assert pb.kind == TransferKind.TRANSFER_KIND_TAKE_CONTROL and pb.prepare_ttl_ms == 2000
    assert [r.dof for r in pb.mappings] == ["rudder"] and not pb.mappings[0].HasField("fence")
    assert codec.mapping_from_proto(pb.mappings[0]) == st.in_force.mappings[0]


def test_an_ack_with_and_without_a_fence():
    ok = codec.ack_from_proto(TransferAck(transfer_id="t1", accepted=True, proposed_fence={"term": "cd" * 16, "generation": 8}))
    assert ok.accepted and ok.fence == Fence("cd" * 16, 8)
    nak = codec.ack_from_proto(TransferAck(transfer_id="t1", accepted=False, reason="not neutral", rejected_mapping_ids=["m1"]))
    assert not nak.accepted and nak.fence is None and nak.rejected_mapping_ids == ("m1",)


def test_the_conn_record_carries_the_gates_fence():
    st = _state()
    rec = codec.lease_record(st, "conn", "authority-manager/0")
    assert rec.role == Role.ROLE_CONN and rec.kind == RecordKind.RECORD_KIND_LEASE
    assert rec.controller_id == "ted@ROC-1" and rec.token == "tok" and rec.active_control_station == "ROC-1"
    assert (rec.fence.term, rec.fence.generation) == ("ab" * 16, 7)


def test_the_assignment_feed_and_the_navigation_document():
    st = _state()
    feed = codec.assignment_feed(st, "authority-manager/0", 5, 15)
    assert feed.overall.assignee == "ted@ROC-1" and feed.overall.revision == 3 and not feed.HasField("navigational")
    doc = codec.navigation_document(st)
    assert doc.revision == 2 and doc.functions[0].control_method == 4 and doc.functions[0].envelope.reroute_allowed
