"""The CLI as a real process, against the fake gate served over `control_gate/v1`, on an ISOLATED
loopback Zenoh pair: multicast and gossip scouting off on both sides, so nothing here can reach a
shared network or a vessel."""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import keelson
import pytest
import zenoh
from keelson.interfaces import AuthorityManager_pb2 as pb
from keelson.interfaces.ControlGate_pb2 import AbortAutomationResponse, CommitResponse, GateState
from keelson.interfaces.VehicleCommon_pb2 import CommandResult
from keelson.payloads.Alarm_pb2 import Alarm
from keelson.payloads.CommandAuthority_pb2 import CommandAssignment, CommandAuthority, Role
from keelson.payloads.ControlMapping_pb2 import ControlMapping, TransferAck, TransferCommit, TransferPrepare
from keelson.scaffolding.rpc import serve_rpc

from control_authority import codec
from fakegate import FakeGate

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parent.parent
REALM, V = "e2e", "testvessel"
BASE = f"{REALM}/@v0/{V}/pubsub"
DOFS = ["rudder", "throttle"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def isolated(conf: zenoh.Config, **kv):
    conf.insert_json5("scouting/multicast/enabled", "false")
    conf.insert_json5("scouting/gossip/enabled", "false")
    for k, v in kv.items():
        conf.insert_json5(k, json.dumps(v))
    return conf


def mono_ms():
    return int(time.monotonic() * 1000)


class GateServer:
    """FakeGate behind control_gate/v1, in this test process."""

    def __init__(self, session):
        self.session = session
        self.gate = FakeGate(mono_ms)
        self.server = None

    def _parse(self, cls, op):
        msg = cls()
        msg.ParseFromString(op.request_bytes)
        return msg

    def _prepare(self, op):
        p = self._parse(TransferPrepare, op)
        t = SimpleNamespace(transfer_id=p.transfer_id, station=p.active_control_station,
                            mappings=tuple(codec.mapping_from_proto(m) for m in p.mappings))
        ack = self.gate.prepare(t, p.prepare_ttl_ms)
        out = TransferAck(transfer_id=p.transfer_id, accepted=ack.accepted, reason=ack.reason,
                          rejected_mapping_ids=list(ack.rejected_mapping_ids))
        if ack.fence is not None:
            out.proposed_fence.CopyFrom(codec.fence_to_proto(ack.fence))
        op.reply_ok(out)

    def _commit(self, op):
        c = self._parse(TransferCommit, op)
        applied, reason = self.gate.commit(c.transfer_id, codec.fence_from_proto(c.fence))
        op.reply_ok(CommitResponse(transfer_id=c.transfer_id, applied=applied, reason=reason,
                                   fence=codec.fence_to_proto(self.gate.fence)))

    def _state(self, op):
        st = GateState(vessel_id=V, fence=codec.fence_to_proto(self.gate.fence), active_control_station=self.gate.station,
                       mappings=[codec.mapping_to_proto(m, self.gate.fence) for m in self.gate.mappings])
        op.reply_ok(st)

    def _abort(self, op):
        op.reply_ok(AbortAutomationResponse(result=CommandResult.COMMAND_RESULT_UNSUPPORTED, detail="fake"))

    def serve(self):
        self.server = serve_rpc(self.session, base_path=REALM, entity_id=V, responder_id="control-gate", interface="control_gate",
                                version="v1", handlers={"prepare": self._prepare, "commit": self._commit,
                                                        "get_gate_state": self._state, "abort_automation": self._abort})

    def stop(self):
        for q in self.server.queryables:
            q.undeclare()
        self.server.liveliness_token.undeclare()
        self.server = None


class Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.port = free_port()
        self.proc = None
        self.session = zenoh.open(isolated(zenoh.Config(), mode="peer", **{"listen/endpoints": [f"tcp/127.0.0.1:{self.port}"],
                                                                         "connect/endpoints": []}))
        self.gate = GateServer(self.session)
        self.records, self.feeds, self.alarms = {}, [], []
        self._subs = [
            self.session.declare_subscriber(f"{BASE}/command_authority/**", self._on_record),
            self.session.declare_subscriber(f"{BASE}/command_assignment/{V}", lambda s: self.feeds.append(self._decode(s, CommandAssignment))),
            self.session.declare_subscriber(f"{BASE}/alarm/**", lambda s: self.alarms.append(self._decode(s, Alarm))),
        ]

    def _decode(self, sample, cls):
        msg = cls()
        msg.ParseFromString(keelson.uncover(sample.payload.to_bytes())[2])
        return msg

    def _on_record(self, s):
        self.records[str(s.key_expr)] = self._decode(s, CommandAuthority)

    def start(self):
        zcfg = self.tmp / "zenoh.json5"
        zcfg.write_text(json.dumps({"mode": "peer", "scouting": {"multicast": {"enabled": False}, "gossip": {"enabled": False}},
                                    "connect": {"endpoints": [f"tcp/127.0.0.1:{self.port}"]}, "listen": {"endpoints": []}}))
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({"command_profile": "test", "dofs": DOFS, "lease_ttl_ms": {"conn": 30000}}))
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bin/authority-manager2keelson.py"), "-r", REALM, "-e", V, "--zenoh-config", str(zcfg),
             "--state-dir", str(self.tmp / "state"), "--policy", str(policy), "--heartbeat-s", "1", "--gate-timeout-s", "1",
             *getattr(self, "start_args", [])],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        token = keelson.construct_source_liveliness_key(REALM, V, "authority-manager")
        self.wait(lambda: any(True for _ in self.session.liveliness().get(token, timeout=0.5)), what="the manager's liveliness token")

    def stop_proc(self):
        if self.proc:
            self.proc.terminate()
            out, _ = self.proc.communicate(timeout=10)
            self.proc = None
            return out
        return ""

    def wait(self, pred, timeout=20.0, what="condition"):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc and self.proc.poll() is not None:
                raise AssertionError(f"manager exited:\n{self.proc.stdout.read()}")
            if pred():
                return
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {what}")

    def call(self, procedure, request, reply_cls):
        key = keelson.construct_rpc_key(REALM, V, "authority_manager", "v1", procedure, "authority-manager")
        for reply in self.session.get(key, payload=request.SerializeToString(), timeout=10):
            assert reply.ok is not None, reply.err.payload.to_bytes()
            out = reply_cls()
            out.ParseFromString(reply.ok.payload.to_bytes())
            return out
        raise AssertionError(f"no answer to {procedure}")

    def take_conn(self, who="ted@ROC-1", site="ROC-1"):
        rows = [ControlMapping(dof=d, execution_source=f"{site}/helm-1/{d}") for d in DOFS]
        return self.call("request", pb.RequestRoleRequest(caller=pb.Caller(controller_id=who, controller_site=site),
                                                          role=Role.ROLE_CONN, mappings=rows), pb.AuthorityReply)

    def close(self):
        out = self.stop_proc()
        for s in self._subs:
            s.undeclare()
        self.session.close()
        return out


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def test_the_conn_is_taken_through_the_gate_and_published_with_the_gates_fence(h):
    h.gate.serve()
    h.start()
    st = h.call("get", pb.GetAuthorityRequest(), pb.AuthorityState)
    assert st.reconciled and st.fence.generation == h.gate.gate.generation
    r = h.take_conn()
    assert r.result == CommandResult.COMMAND_RESULT_ACCEPTED, r.detail
    assert (r.fence.term, r.fence.generation) == (h.gate.gate.term, h.gate.gate.generation)
    key = f"{BASE}/command_authority/{V}"
    h.wait(lambda: key in h.records, what="the conn record")
    rec = h.records[key]
    assert rec.controller_id == "ted@ROC-1" and rec.fence.generation == r.fence.generation and rec.issued_by == "authority-manager/0"
    h.wait(lambda: h.feeds, what="the command_assignment feed")
    assert h.feeds[-1].issuer_id == "authority-manager/0" and h.feeds[-1].feed_ttl_seconds == 15


def test_a_gate_nak_is_a_typed_refusal_and_nothing_moves(h):
    h.gate.serve()
    h.start()
    h.gate.gate.unready = {"throttle"}
    r = h.take_conn()
    assert (r.result, r.refusal) == (CommandResult.COMMAND_RESULT_DENIED, pb.Refusal.REFUSAL_GATE)
    assert h.gate.gate.mappings == () and h.gate.gate.generation == 1


def test_a_silent_gate_refuses_every_transfer_until_it_answers(h):
    h.start()  # no gate yet
    r = h.take_conn()
    assert r.refusal == pb.Refusal.REFUSAL_NOT_RECONCILED
    h.gate.serve()
    h.wait(lambda: h.call("get", pb.GetAuthorityRequest(), pb.AuthorityState).reconciled, what="the reconcile")
    assert h.take_conn().result == CommandResult.COMMAND_RESULT_ACCEPTED


def test_a_restart_reconciles_toward_a_gate_that_fenced_alone(h):
    h.gate.serve()
    h.start()
    h.take_conn()
    h.stop_proc()
    h.gate.gate.fence_alone()  # the conn lapsed while the manager was down
    h.alarms.clear()
    h.start()
    st = h.call("get", pb.GetAuthorityRequest(), pb.AuthorityState)
    assert st.reconciled and st.fence.generation == h.gate.gate.generation and list(st.mappings) == []
    h.wait(lambda: h.alarms, what="the disagreement alarm")
    hist = h.call("history", pb.HistoryRequest(), pb.HistoryReply)
    assert "reconcile_disagreement" in [r.event for r in hist.records]


def test_the_master_is_bootstrapped_by_a_maintenance_flag_once(h):
    h.gate.serve()
    h.start_args = ["--assign-overall", "ted@ROC-1"]
    h.start()
    st = h.call("get", pb.GetAuthorityRequest(), pb.AuthorityState)
    assert st.assignments.overall.assignee == "ted@ROC-1" and st.assignments.overall.revision == 1
    h.stop_proc()
    h.start()  # same flag again: already assigned, the revision does not move
    st = h.call("get", pb.GetAuthorityRequest(), pb.AuthorityState)
    assert st.assignments.overall.revision == 1
    hist = h.call("history", pb.HistoryRequest(), pb.HistoryReply)
    assert [r.event for r in hist.records].count("assign") == 1


def test_a_process_is_never_bootstrapped_as_master(h, tmp_path):
    policy = tmp_path / "p.json"
    policy.write_text(json.dumps({"command_profile": "test", "dofs": DOFS, "processes": ["navigation/0@testvessel"]}))
    p = subprocess.run([sys.executable, str(ROOT / "bin/authority-manager2keelson.py"), "-r", REALM, "-e", V,
                        "--state-dir", str(tmp_path / "s"), "--policy", str(policy), "--assign-overall", "navigation/0@testvessel",
                        "--mode", "peer", "--connect", f"tcp/127.0.0.1:{h.port}", "--gate-timeout-s", "0.5"],
                       capture_output=True, text=True, timeout=30, check=False)
    assert p.returncode == 2 and "never software" in (p.stdout + p.stderr)
