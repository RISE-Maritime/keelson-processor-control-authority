#!/usr/bin/env python3
"""keelson-processor-control-authority — the onboard authority manager (COMMAND-ARCHITECTURE.md §4).

Exactly one per vessel. It serves `authority_manager/v1`, calls the motion gate over `control_gate/v1`,
and is the one writer of `command_authority` records and of `command_assignment/{vessel}`.

Boot: open the store (an untrusted one stops the process) → rebase every lease → reconcile with the
gate (until it answers, every transfer is refused NOT_RECONCILED) → republish every record → serve
the interface → liveliness → one loop for expiry and the feed's heartbeat. Every request is decided
under one lock (control_authority/rpc.py).
"""

import argparse
import functools
import logging
import pathlib
import sys
import time

import keelson
import zenoh
from google.protobuf.timestamp_pb2 import Timestamp
from keelson.interfaces import AuthorityManager_pb2 as pb
from keelson.payloads.Alarm_pb2 import Alarm
from keelson.scaffolding import add_common_arguments, create_zenoh_config, setup_logging
from keelson.scaffolding.liveliness import declare_liveliness
from keelson.scaffolding.rpc import RpcOp, serve_rpc
from keelson.scaffolding.signals import GracefulShutdown

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from control_authority import codec, model
from control_authority.policy import load
from control_authority.rpc import Handlers
from control_authority.service import Manager
from control_authority.store import Store
from control_authority.zenoh_gate import ZenohGate

log = logging.getLogger("authority-manager")

REQUESTS = {
    "request": pb.RequestRoleRequest,
    "renew": pb.RenewRequest,
    "release": pb.ReleaseRequest,
    "transfer": pb.TransferRoleRequest,
    "take_control": pb.TakeControlRequest,
    "abort_automation": pb.AbortAutomationRequest,
    "assume_command": pb.AssumeCommandRequest,
    "set_navigation_authority": pb.SetNavigationAuthorityRequest,
    "get": pb.GetAuthorityRequest,
    "history": pb.HistoryRequest,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="authority-manager2keelson", description=__doc__.split("\n")[0])
    add_common_arguments(p)
    p.add_argument("-r", "--realm", required=True, help="Keelson realm, e.g. rise")
    p.add_argument("-e", "--entity-id", required=True, help="The vessel this manager is onboard (the vessel_id)")
    p.add_argument("-s", "--source-id", default="authority-manager", help="Source id and RPC responder id")
    p.add_argument("--issuer-id", default="authority-manager/0", help="issued_by / issuer_id on every record it writes")
    p.add_argument("--gate-responder-id", default="control-gate", help="The motion gate's control_gate/v1 responder id")
    p.add_argument("--gate-timeout-s", type=float, default=2.0, help="Timeout of each gate call (default 2 s)")
    p.add_argument("--state-dir", type=pathlib.Path, required=True, help="Directory of the manager's store (a persistent volume)")
    p.add_argument("--policy", type=pathlib.Path, required=True, help="The vessel's control matrix and timings (JSON)")
    p.add_argument("--heartbeat-s", type=int, default=5, help="command_assignment heartbeat (default 5 s)")
    p.add_argument("--feed-ttl-s", type=int, default=15, help="feed_ttl_seconds stated on the feed (default 15 s)")
    return p


def main() -> int:
    args = build_parser().parse_args()
    setup_logging(level=args.log_level)
    zenoh.init_log_from_env_or(logging.getLevelName(args.log_level))
    realm, vessel = args.realm, args.entity_id
    base = f"{realm}/@v0/{vessel}/pubsub"
    policy = load(args.policy)
    log.info("profile %s, %d DOF(s): %s", policy.command_profile, len(policy.dofs), ", ".join(policy.dofs))

    conf = create_zenoh_config(args.mode, args.connect, args.listen, zenoh_config=args.zenoh_config)
    with zenoh.open(conf) as session:

        def now_ts() -> Timestamp:
            ts = Timestamp()
            ts.GetCurrentTime()
            return ts

        def put(subject_path: str, message) -> None:
            session.put(f"{base}/{subject_path}", keelson.enclose(message.SerializeToString()))

        def publish(effect, st) -> None:
            if isinstance(effect, model.PublishLease):
                rec = codec.lease_record(st, effect.slot, args.issuer_id, effect.released, now_ts(), effect.lease)
                put(f"command_authority/{codec.slot_path(vessel, effect.slot)}", rec)
            elif isinstance(effect, model.PublishAssignments):
                put(f"command_assignment/{vessel}", codec.assignment_feed(st, args.issuer_id, args.heartbeat_s, args.feed_ttl_s, now_ts()))
                for role in model.ASSIGNMENT_ROLES:
                    put(f"command_authority/{vessel}/{role}", codec.assignment_record(st, role, args.issuer_id))
            elif isinstance(effect, model.PublishNavigation):
                put(f"command_authority/{vessel}/navigation", codec.navigation_record(st, args.issuer_id))
            elif isinstance(effect, model.Alarm):
                log.warning("ALARM: %s", effect.text)
                put(f"alarm/{args.source_id}", Alarm(timestamp=now_ts(), identifier=f"{args.source_id}/authority",
                                                    description=effect.text, category=Alarm.Category.CATEGORY_NAVIGATION,
                                                    priority=Alarm.Priority.PRIORITY_WARNING))

        gate = ZenohGate(session, realm=realm, vessel_id=vessel, responder_id=args.gate_responder_id, timeout_s=args.gate_timeout_s)
        manager = Manager(store=Store(args.state_dir / "authority.sqlite"), policy=policy, vessel_id=vessel, gate=gate,
                          publish=publish)
        manager.boot()
        log.info("booted: %s, %d lease(s), fence %s", "reconciled" if manager.state.reconciled else "NOT reconciled (gate silent)",
                 len(manager.state.leases), manager.state.in_force.fence)
        # Every record is republished at boot, so a late joiner and the router's storage agree with the store.
        for slot in manager.state.leases:
            publish(model.PublishLease(slot), manager.state)
        publish(model.PublishAssignments(), manager.state)
        publish(model.PublishNavigation(), manager.state)

        handlers = Handlers(manager, args.issuer_id, args.heartbeat_s, args.feed_ttl_s)

        def serve(name: str, op: RpcOp) -> None:
            req = REQUESTS[name]()
            try:
                req.ParseFromString(op.request_bytes)
            except Exception as e:  # noqa: BLE001
                op.reply_err(f"malformed {name} request: {e}")
                return
            op.reply_ok(getattr(handlers, name)(req))

        serve_rpc(session, base_path=realm, entity_id=vessel, responder_id=args.source_id, interface="authority_manager",
                  version="v1", handlers={name: functools.partial(serve, name) for name in REQUESTS}, log=log)

        with declare_liveliness(session, realm, vessel, args.source_id,
                                pubsub_subjects=["command_authority", "command_assignment", "alarm"]), \
                GracefulShutdown() as shutdown:
            last_hb = last_reconcile = time.monotonic()
            while not shutdown.is_requested():
                time.sleep(0.2)
                try:
                    handlers.tick()
                    # A silent gate is retried at the heartbeat, not every pass: each try holds the lock
                    # for up to --gate-timeout-s.
                    if not manager.state.reconciled and time.monotonic() - last_reconcile >= args.heartbeat_s:
                        last_reconcile = time.monotonic()
                        with handlers.lock:
                            manager.reconcile()
                    if time.monotonic() - last_hb >= args.heartbeat_s:
                        handlers.heartbeat()
                        last_hb = time.monotonic()
                except Exception:  # pylint: disable=broad-except
                    log.exception("periodic work failed")
    manager.store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
