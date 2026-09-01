#!/usr/bin/env python3
"""Pure provider-free executable model of the dormant Rewst protocol.

This is a deterministic fixture/oracle, not a persistence service. Tenant Records and
KV remain the only durable authority when the native workflows are published.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any


_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,12})?$")
_HEX = re.compile(r"^[0-9a-f]{64}$")


class RunControlConflict(RuntimeError):
    """The durable evidence conflicts and the run must fence."""


def _bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _deep(value: Any) -> Any:
    return copy.deepcopy(value)


class DurableRunControlModel:
    """Provider-free executor grounded in the published native graph contracts.

    Records upsert remains deliberately unconditional here; safety comes from the
    graph's pointer journal, exact existing-byte check, and mandatory readback.
    """

    def __init__(self, graph_root: Path | None = None) -> None:
        self.pointer: dict[str, Any] | None = None
        self.pointer_version = 0
        self.records: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self.production_contract: dict[str, Any] = {}
        if graph_root is not None:
            required = {
                "transition.target_upsert": ("run-control-transition", "target_upsert"),
                "transition.prepare_cas": ("run-control-transition", "prepare_cas"),
                "state.evaluate": ("run-control-state", "evaluate"),
            }
            loaded: dict[str, dict[str, Any]] = {}
            for graph_name in {
                "run-control-initialize", "run-control-state", "run-control-transition",
                "run-control-reconcile", "run-control-consume",
                "run-control-consume-authorization",
            }:
                path = graph_root / f"{graph_name}.json"
                loaded[graph_name] = json.loads(path.read_text(encoding="utf-8"))
            for label, (graph_name, node_id) in required.items():
                graph_nodes = {node["id"]: node for node in loaded[graph_name]["spec"]["nodes"]}
                if node_id not in graph_nodes:
                    raise ValueError(f"production graph missing {graph_name}:{node_id}")
                self.production_contract[label] = graph_nodes[node_id]["type"]
            all_nodes = [node for value in loaded.values() for node in value["spec"]["nodes"]]
            self.production_contract["all_dormant"] = all(
                value["spec"]["meta"].get("dormant") is True for value in loaded.values()
            )
            self.production_contract["no_launch_nodes"] = not any(
                node["type"].startswith(("action.graphwing.POST:/v1/agent",
                                         "action.graphwing.POST:/v1/review"))
                for node in all_nodes
            )

    @staticmethod
    def _validate_budgets(budgets: Any) -> dict[str, Any]:
        required = {"attempts", "turns", "wall_seconds", "tokens", "provider_cost_usd"}
        if not isinstance(budgets, dict) or set(budgets) != required:
            raise ValueError("invalid budgets")
        for key in required - {"provider_cost_usd"}:
            value = budgets[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("invalid budgets")
        cost = budgets["provider_cost_usd"]
        if not isinstance(cost, str) or not _DECIMAL.fullmatch(cost):
            raise ValueError("invalid fixed-decimal cost")
        return _deep(budgets)

    @staticmethod
    def _validate_route(route: Any) -> dict[str, str]:
        required = {"route_version", "launcher", "provider", "model"}
        if (not isinstance(route, dict) or set(route) != required
                or any(not isinstance(route[key], str) or not route[key]
                       for key in required)):
            raise ValueError("invalid route")
        return _deep(route)

    @staticmethod
    def run_control_id(request: dict[str, Any]) -> str:
        material = {
            "id_version": "graphwing-run-id-v1",
            "root_workflow_id": request["root_workflow_id"],
            "root_workflow_version_id": request["root_workflow_version_id"],
            "root_workflow_run_id": request["root_workflow_run_id"],
            "purpose": request["purpose"],
        }
        return "rc1-" + _sha(material)

    def initialize(self, request: dict[str, Any]) -> dict[str, Any]:
        required = {"root_workflow_id", "root_workflow_version_id",
                    "root_workflow_run_id", "purpose", "budgets", "initial_route"}
        if not isinstance(request, dict) or set(request) != required:
            raise ValueError("invalid initialize request")
        if request["purpose"] not in {"implement_slice", "pr_drive", "code_off"}:
            raise ValueError("invalid purpose")
        budgets = self._validate_budgets(request["budgets"])
        route = self._validate_route(request["initial_route"])
        run_id = self.run_control_id(request)
        immutable = {
            "budgets": budgets,
            "policy_version": "run-control-v2",
            "evaluator_contract_sha256": "0" * 64,
            "initial_route": route,
        }
        request_hash = _sha(request)
        operation_id = "op1-" + _sha({"run_control_id": run_id,
                                      "operation_type": "initialize",
                                      "prior_revision": None,
                                      "transition_request_sha256": request_hash})
        state = {
            "schema": "graphwing-run-control-state-v1",
            "run_control_id": run_id,
            "logical_revision": 0,
            "origin": {key: request[key] for key in (
                "root_workflow_id", "root_workflow_version_id",
                "root_workflow_run_id", "purpose")},
            "immutable": immutable,
            "status": "active",
            "same_model_continuation_used": False,
            "current_route": route,
            "attempts": [],
            "evaluator_history": [],
            "handoffs": [],
            "outstanding_reservation": None,
            "terminal": None,
            "last_transition": {"operation_id": operation_id, "type": "initialize",
                                "from_logical_revision": None,
                                "request_sha256": request_hash},
        }
        state_hash = _sha(state)
        run_hash = run_id.removeprefix("rc1-")
        record_key = f"rcs1-{run_hash}-{state_hash}"
        pending = {
            "schema": "graphwing-run-control-pointer-v1", "run_control_id": run_id,
            "phase": "pending", "operation_id": operation_id,
            "operation_type": "initialize", "from_logical_revision": None,
            "previous_state_record_key": None, "previous_state_sha256": None,
            "target_state_record_key": record_key, "target_state_sha256": state_hash,
            "transition_delta": {"initialize_request": _deep(request)},
            "transition_request_sha256": request_hash,
            "owner_workflow_run_id": request["root_workflow_run_id"],
            "recovery_rule": "rebuild_or_fence",
        }
        with self._lock:
            if self.pointer is not None:
                current = self.load_verified()
                if (self.pointer.get("last_operation_id") == operation_id
                        and current == state):
                    return {"created": False, "run_control_id": run_id,
                            "state_sha256": state_hash,
                            "state_record_key": record_key}
                raise RunControlConflict("immutable initialize conflict")
            self.pointer = pending
            self.pointer_version = 1
            existing = self.records.get(record_key)
            if existing is not None and _sha(existing) != state_hash:
                self.pointer = {**pending, "phase": "fenced"}
                raise RunControlConflict("conflicting content-addressed record")
            self.records[record_key] = _deep(state)
            readback = self.records[record_key]
            if _sha(readback) != state_hash:
                self.pointer = {**pending, "phase": "fenced"}
                raise RunControlConflict("record readback mismatch")
            self.pointer = {
                "schema": "graphwing-run-control-pointer-v1",
                "run_control_id": run_id, "phase": "stable", "logical_revision": 0,
                "state_record_key": record_key, "state_sha256": state_hash,
                "observed_record_version": 1, "last_operation_id": operation_id,
            }
            self.pointer_version = 2
        return {"created": True, "run_control_id": run_id,
                "state_sha256": state_hash, "state_record_key": record_key}

    def reserve(self, request: dict[str, Any], evaluator: Any,
                challenge_provider: Any) -> dict[str, Any]:
        """Evaluate stored history, enforce local guards, and persist one reservation."""
        allowed = {"candidate_route", "envelope", "endpoint", "launcher_fingerprint",
                   "exact_request_body_sha256", "repository", "branch", "head_sha",
                   "task_sha256", "permission_profile", "callback_binding_sha256",
                   "handoff"}
        required = allowed - {"handoff"}
        if not isinstance(request, dict) or not required <= set(request) or set(request) - allowed:
            raise ValueError("invalid reservation request")
        with self._lock:
            state = self.load_verified()
            if state.get("status") != "active":
                raise RunControlConflict("run is not active")
            if state.get("outstanding_reservation") is not None:
                raise RunControlConflict("outstanding reservation exists")
            route = self._validate_route(request["candidate_route"])
            envelope = self._validate_budgets({"attempts": 1, **request["envelope"]})
            envelope.pop("attempts")
            if any(envelope[key] <= 0 for key in ("turns", "wall_seconds", "tokens")):
                raise ValueError("zero envelope cannot authorize launch")
            ordinal = len(state["attempts"]) + 1
            run_hash = str(state["run_control_id"]).removeprefix("rc1-")
            attempt_hash = _sha({"run_control_id": state["run_control_id"],
                                 "ordinal": ordinal, "route": route,
                                 "task_sha256": request["task_sha256"]})
            attempt_id = "att1-" + attempt_hash
            evaluator_envelope = {"attempt_id": attempt_id, **envelope}
            evaluator_request = {
                "run_id": state["run_control_id"], "route": state["current_route"],
                "budgets": _deep(state["immutable"]["budgets"]),
                "attempts": _deep(state["evaluator_history"]),
                "next_attempt": ordinal, "next_route": route,
                "next_envelope": evaluator_envelope,
            }
            evaluator_bytes = _bytes(evaluator_request)
            status, response = evaluator(evaluator_bytes)
            if status != 200 or not isinstance(response, dict):
                raise RunControlConflict("evaluator unavailable")
            decision = response.get("decision")
            if decision not in {"continue_same_model", "handoff_cross_model",
                                "terminate", "restructure"}:
                raise RunControlConflict("invalid evaluator decision")
            same_model = (route["provider"], route["model"]) == (
                state["current_route"]["provider"], state["current_route"]["model"])
            if decision == "continue_same_model":
                if not same_model or state["same_model_continuation_used"]:
                    raise RunControlConflict("same-model continuation guard")
            elif decision == "handoff_cross_model":
                handoff = request.get("handoff")
                if same_model or not isinstance(handoff, dict) or set(handoff) != {"reason_code"}:
                    raise RunControlConflict("explicit cross-model handoff guard")
            evaluator_request_hash = _sha(evaluator_request)
            evaluator_response_hash = _sha(response)
            transition_request_hash = _sha({"request": request,
                                            "evaluator_request_sha256": evaluator_request_hash,
                                            "evaluator_response_sha256": evaluator_response_hash})
            operation_id = "op1-" + _sha({
                "run_control_id": state["run_control_id"], "operation_type": "reserve",
                "prior_revision": state["logical_revision"],
                "transition_request_sha256": transition_request_hash,
            })
            target = _deep(state)
            target["logical_revision"] += 1
            target["last_transition"] = {
                "operation_id": operation_id, "type": "reserve",
                "from_logical_revision": state["logical_revision"],
                "request_sha256": transition_request_hash,
            }
            if decision in {"terminate", "restructure"}:
                target["status"] = "terminal"
                target["terminal"] = {"decision": decision,
                                      "classification": response.get("classification"),
                                      "evaluator_response_sha256": evaluator_response_hash}
                self.prepare_for_test(target, decision, transition_request_hash)
                self.recover()
                return {"decision": decision, "reserved": False}
            challenge = challenge_provider()
            if not isinstance(challenge, str) or not _HEX.fullmatch(challenge):
                raise RunControlConflict("invalid daemon challenge")
            authorization_id = f"rca1-{run_hash}-{attempt_hash}"
            descriptor = {
                "descriptor_version": "graphwing-launch-descriptor-v2",
                "run_control_id": state["run_control_id"],
                "state_revision": target["logical_revision"], "attempt_id": attempt_id,
                "authorization_id": authorization_id,
                "server_instance_challenge": challenge, "route": route,
                "launcher_fingerprint": request["launcher_fingerprint"],
                "endpoint": request["endpoint"],
                "exact_request_body_sha256": request["exact_request_body_sha256"],
                "repository": request["repository"], "branch": request["branch"],
                "head_sha": request["head_sha"], "task_sha256": request["task_sha256"],
                "permission_profile": request["permission_profile"],
                "callback_binding_sha256": request["callback_binding_sha256"],
                "envelope": envelope,
            }
            descriptor_hash = _sha(descriptor)
            reservation = {
                "attempt_id": attempt_id, "ordinal": ordinal, "phase": "reserved",
                "route": route, "envelope": envelope,
                "evaluator": {"request_sha256": evaluator_request_hash,
                              "response_sha256": evaluator_response_hash,
                              "decision": decision},
                "launch_descriptor": descriptor,
                "launch_descriptor_sha256": descriptor_hash, "authorization": None,
            }
            target["outstanding_reservation"] = reservation
            if decision == "continue_same_model":
                target["same_model_continuation_used"] = True
            else:
                handoff = {
                    "handoff_id": "hof1-" + _sha({"attempt_id": attempt_id,
                                                   "from": state["current_route"],
                                                   "to": route}),
                    "from_attempt_id": (state["attempts"][-1]["attempt_id"]
                                        if state["attempts"] else None),
                    "from_route": state["current_route"], "to_route": route,
                    "reason_code": request["handoff"]["reason_code"],
                    "evaluator_response_sha256": evaluator_response_hash,
                }
                target["handoffs"].append(handoff)
                target["current_route"] = route
            self.prepare_for_test(target, "reserve", transition_request_hash)
            self.recover()
            return {"decision": decision, "reserved": True, "attempt_id": attempt_id,
                    "authorization_id": authorization_id,
                    "server_instance_challenge": challenge,
                    "launch_descriptor_sha256": descriptor_hash}

    def mark_authorization_consumed(self, challenge: str, consumed_version: int,
                                    consumed_value_sha256: str, *,
                                    authorization_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            state = self.load_verified()
            reservation = state.get("outstanding_reservation")
            if not isinstance(reservation, dict):
                raise RunControlConflict("no reserved authorization")
            descriptor = reservation["launch_descriptor"]
            expected_authorization_id = descriptor.get("authorization_id")
            supplied_authorization_id = authorization_id or expected_authorization_id
            evidence_hash = _sha({"authorization_id": supplied_authorization_id,
                                  "challenge": challenge,
                                  "consumed_version": consumed_version,
                                  "consumed_value_sha256": consumed_value_sha256})
            if supplied_authorization_id != expected_authorization_id:
                self._persist_fence(state, "authorization_id_drift", evidence_hash)
                raise RunControlConflict("authorization id mismatch")
            if reservation.get("phase") == "authorization_consumed":
                existing = reservation.get("authorization")
                expected = {
                    "authorization_id": expected_authorization_id,
                    "challenge": challenge, "consumed_kv_version": consumed_version,
                    "consumed_value_sha256": consumed_value_sha256,
                }
                if existing == expected:
                    return {"consumed": False, "attempt_id": reservation["attempt_id"]}
                self._persist_fence(state, "authorization_consumption_drift", evidence_hash)
                raise RunControlConflict("authorization consumption mismatch")
            if reservation.get("phase") != "reserved":
                self._persist_fence(state, "authorization_state_drift", evidence_hash)
                raise RunControlConflict("authorization state mismatch")
            if (challenge != descriptor.get("server_instance_challenge")
                    or consumed_version != 2 or not _HEX.fullmatch(consumed_value_sha256)):
                self._persist_fence(state, "authorization_consumption_drift", evidence_hash)
                raise RunControlConflict("authorization consumption mismatch")
            request_hash = _sha({"attempt_id": reservation["attempt_id"],
                                 "challenge": challenge,
                                 "consumed_version": consumed_version,
                                 "consumed_value_sha256": consumed_value_sha256})
            operation_id = "op1-" + _sha({"run_control_id": state["run_control_id"],
                                           "operation_type": "mark_authorization_consumed",
                                           "prior_revision": state["logical_revision"],
                                           "transition_request_sha256": request_hash})
            target = _deep(state)
            target["logical_revision"] += 1
            target["outstanding_reservation"]["phase"] = "authorization_consumed"
            target["outstanding_reservation"]["authorization"] = {
                "authorization_id": descriptor["authorization_id"],
                "challenge": challenge, "consumed_kv_version": consumed_version,
                "consumed_value_sha256": consumed_value_sha256,
            }
            target["last_transition"] = {
                "operation_id": operation_id, "type": "mark_authorization_consumed",
                "from_logical_revision": state["logical_revision"],
                "request_sha256": request_hash,
            }
            self.prepare_for_test(target, "mark_authorization_consumed", request_hash)
            self.recover()
            return {"consumed": True, "attempt_id": reservation["attempt_id"]}

    def reconcile_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        required = {"receipt_id", "attempt_id", "run_control_id", "authorization_id",
                    "launch_descriptor_sha256", "job_id", "terminal_status", "route",
                    "usage", "progress", "constraint_signals", "verified_outcome"}
        if not isinstance(receipt, dict) or set(receipt) != required:
            with self._lock:
                if self.load_verified().get("outstanding_reservation") is not None:
                    return self.reconcile_authority_loss("malformed_terminal_receipt")
            raise ValueError("terminal receipt must be closed")
        receipt_hash = _sha(receipt)
        with self._lock:
            state = self.load_verified()
            reservation = state.get("outstanding_reservation")
            if reservation is None:
                matches = [item for item in state["attempts"]
                           if item["attempt_id"] == receipt.get("attempt_id")]
                if (len(matches) == 1
                        and matches[0]["reconciliation"].get("receipt_id") == receipt["receipt_id"]
                        and matches[0]["reconciliation"].get("receipt_sha256") == receipt_hash):
                    return {"reconciled": False, "receipt_sha256": receipt_hash}
                self._persist_fence(state, "conflicting_reconciled_receipt", receipt_hash)
                raise RunControlConflict("conflicting reconciled receipt")
            if not isinstance(reservation, dict) or reservation.get("phase") != "authorization_consumed":
                raise RunControlConflict("receipt without consumed authority")
            descriptor = reservation["launch_descriptor"]
            bindings = {
                "attempt_id": reservation["attempt_id"],
                "run_control_id": state["run_control_id"],
                "authorization_id": descriptor["authorization_id"],
                "launch_descriptor_sha256": reservation["launch_descriptor_sha256"],
                "route": reservation["route"],
            }
            if any(receipt.get(key) != value for key, value in bindings.items()):
                self._persist_fence(state, "receipt_binding_mismatch", receipt_hash)
                raise RunControlConflict("receipt binding mismatch")
            if receipt["terminal_status"] not in {"succeeded", "failed", "cancelled", "timed_out"}:
                return self.reconcile_authority_loss("malformed_terminal_receipt")
            progress = receipt["progress"]
            if (not isinstance(progress, dict) or set(progress) != {
                    "checkpoint", "failing_regression_present", "production_diff_bytes",
                    "focused_tests_green", "diff_fingerprint"}
                    or not isinstance(receipt["constraint_signals"], list)
                    or not isinstance(receipt["verified_outcome"], bool)):
                return self.reconcile_authority_loss("malformed_terminal_receipt")
            usage = receipt["usage"]
            if not isinstance(usage, dict) or set(usage) != {
                    "turns", "wall_seconds", "tokens", "provider_cost_usd"}:
                return self.reconcile_authority_loss("unknown_or_partial_usage")
            if any(value is None for value in usage.values()):
                return self.reconcile_authority_loss("unknown_or_partial_usage")
            envelope = reservation["envelope"]
            for key in ("turns", "wall_seconds", "tokens"):
                if (isinstance(usage[key], bool) or not isinstance(usage[key], int)
                        or usage[key] < 0 or usage[key] > envelope[key]):
                    return self.reconcile_authority_loss("receipt_usage_outside_reservation")
            cost = usage["provider_cost_usd"]
            if not isinstance(cost, str) or not _DECIMAL.fullmatch(cost):
                return self.reconcile_authority_loss("receipt_usage_outside_reservation")
            if Decimal(cost) > Decimal(envelope["provider_cost_usd"]):
                return self.reconcile_authority_loss("receipt_usage_outside_reservation")
            return self._append_reconciliation(
                state, reservation, receipt_hash,
                {"kind": "terminal_receipt", "receipt_id": receipt["receipt_id"],
                 "receipt_sha256": receipt_hash,
                 "terminal_status": receipt["terminal_status"]},
                _deep(usage), receipt["progress"], receipt["constraint_signals"],
                receipt["verified_outcome"], terminal_state="terminal",
                operation_material={"receipt_id": receipt["receipt_id"],
                                    "receipt_sha256": receipt_hash},
            )

    def reconcile_authority_loss(self, reason: str) -> dict[str, Any]:
        with self._lock:
            state = self.load_verified()
            reservation = state.get("outstanding_reservation")
            if reservation is None:
                if state["attempts"] and state["attempts"][-1]["reconciliation"]["kind"] == "authority_lost":
                    return {"reconciled": False,
                            "attempt_id": state["attempts"][-1]["attempt_id"]}
                raise RunControlConflict("no outstanding authority")
            if not isinstance(reservation, dict):
                raise RunControlConflict("malformed reservation")
            return self._append_reconciliation(
                state, reservation, None, {"kind": "authority_lost", "reason": reason},
                _deep(reservation["envelope"]),
                {"checkpoint": 0, "failing_regression_present": True,
                 "production_diff_bytes": 0, "focused_tests_green": False,
                 "diff_fingerprint": "0" * 64},
                [], False, terminal_state="authority_lost",
                operation_material={"attempt_id": reservation["attempt_id"], "reason": reason},
            )

    def _append_reconciliation(
        self, state: dict[str, Any], reservation: dict[str, Any],
        receipt_hash: str | None, reconciliation: dict[str, Any],
        charged_usage: dict[str, Any], progress: dict[str, Any],
        constraint_signals: list[str], verified_outcome: bool, *, terminal_state: str,
        operation_material: dict[str, Any],
    ) -> dict[str, Any]:
        request_hash = _sha(operation_material)
        operation_id = "op1-" + _sha({"run_control_id": state["run_control_id"],
                                       "operation_type": "reconcile",
                                       "prior_revision": state["logical_revision"],
                                       "transition_request_sha256": request_hash})
        envelope = reservation["envelope"]
        evaluator_usage = (_deep(charged_usage) if terminal_state == "terminal"
                           else {key: None for key in envelope})
        evaluator_evidence = {
            "sequence": reservation["ordinal"], "attempt_id": reservation["attempt_id"],
            **reservation["route"], "usage": evaluator_usage,
            "reservation": {"attempt_id": reservation["attempt_id"], **envelope},
            "terminal_state": terminal_state, "progress": _deep(progress),
            "constraint_signals": _deep(constraint_signals),
            "verified_outcome": bool(verified_outcome),
        }
        attempt = {
            "attempt_id": reservation["attempt_id"], "ordinal": reservation["ordinal"],
            "route": _deep(reservation["route"]), "reservation": _deep(reservation),
            "launch": {"descriptor_sha256": reservation["launch_descriptor_sha256"]},
            "reconciliation": _deep(reconciliation), "charged_usage": _deep(charged_usage),
            "progress": _deep(progress), "constraint_signals": _deep(constraint_signals),
            "verified_outcome": bool(verified_outcome),
        }
        target = _deep(state)
        target["logical_revision"] += 1
        target["attempts"].append(attempt)
        target["evaluator_history"].append(evaluator_evidence)
        target["outstanding_reservation"] = None
        if reconciliation["kind"] == "terminal_receipt" and reconciliation["terminal_status"] == "succeeded":
            target["status"] = "terminal"
            target["terminal"] = {"reason": "succeeded", "attempt_id": reservation["attempt_id"]}
        target["last_transition"] = {
            "operation_id": operation_id, "type": "reconcile",
            "from_logical_revision": state["logical_revision"],
            "request_sha256": request_hash,
        }
        self.prepare_for_test(target, "reconcile", request_hash)
        self.recover()
        return {"reconciled": True, "attempt_id": reservation["attempt_id"],
                "receipt_sha256": receipt_hash}

    def _persist_fence(self, state: dict[str, Any], reason: str,
                       evidence_sha256: str) -> None:
        request_hash = _sha({"reason": reason, "evidence_sha256": evidence_sha256})
        operation_id = "op1-" + _sha({"run_control_id": state["run_control_id"],
                                       "operation_type": "fence",
                                       "prior_revision": state["logical_revision"],
                                       "transition_request_sha256": request_hash})
        target = _deep(state)
        target["logical_revision"] += 1
        target["status"] = "fenced"
        target["terminal"] = {"reason": reason, "evidence_sha256": evidence_sha256}
        target["last_transition"] = {
            "operation_id": operation_id, "type": "fence",
            "from_logical_revision": state["logical_revision"],
            "request_sha256": request_hash,
        }
        self.prepare_for_test(target, "fence", request_hash)
        self.recover()

    def prepare_for_test(self, target: dict[str, Any], operation_type: str,
                         request_hash: str) -> dict[str, Any]:
        """Prepare a deterministic transition; mirrors the native CAS journal."""
        if not _HEX.fullmatch(request_hash):
            raise ValueError("invalid request hash")
        with self._lock:
            prior = self.load_verified()
            if target.get("run_control_id") != prior.get("run_control_id"):
                raise RunControlConflict("run id drift")
            if _bytes(target.get("immutable")) != _bytes(prior.get("immutable")):
                raise RunControlConflict("immutable state drift")
            if target.get("logical_revision") != prior.get("logical_revision") + 1:
                raise RunControlConflict("noncontiguous logical revision")
            operation_id = target.get("last_transition", {}).get("operation_id")
            if not isinstance(operation_id, str) or not operation_id.startswith("op1-"):
                raise ValueError("invalid operation id")
            target_hash = _sha(target)
            run_hash = str(prior["run_control_id"]).removeprefix("rc1-")
            target_key = f"rcs1-{run_hash}-{target_hash}"
            delta = {"target_state": _deep(target)}
            if len(_bytes(delta)) > 24 * 1024:
                raise ValueError("transition delta exceeds 24 KiB")
            pending = {
                "schema": "graphwing-run-control-pointer-v1",
                "run_control_id": prior["run_control_id"], "phase": "pending",
                "operation_id": operation_id, "operation_type": operation_type,
                "from_logical_revision": prior["logical_revision"],
                "previous_state_record_key": self.pointer["state_record_key"],
                "previous_state_sha256": self.pointer["state_sha256"],
                "target_state_record_key": target_key,
                "target_state_sha256": target_hash,
                "transition_delta": delta,
                "transition_request_sha256": request_hash,
                "owner_workflow_run_id": prior["origin"]["root_workflow_run_id"],
                "recovery_rule": "rebuild_or_fence",
            }
            self.pointer = pending
            self.pointer_version += 1
            return _deep(pending)

    def recover(self) -> dict[str, Any]:
        """Recover exact pending materialization, readback, and publish."""
        with self._lock:
            if not isinstance(self.pointer, dict):
                raise RunControlConflict("missing pointer")
            if self.pointer.get("phase") == "stable":
                self.load_verified()
                return {"recovered": False,
                        "state_sha256": self.pointer["state_sha256"]}
            if self.pointer.get("phase") != "pending":
                raise RunControlConflict("pointer cannot recover")
            pending = _deep(self.pointer)
            target = pending.get("transition_delta", {}).get("target_state")
            target_hash = pending.get("target_state_sha256")
            target_key = pending.get("target_state_record_key")
            if (not isinstance(target, dict) or not isinstance(target_hash, str)
                    or not isinstance(target_key, str) or _sha(target) != target_hash):
                self.pointer = {**pending, "phase": "fenced"}
                raise RunControlConflict("incomplete deterministic delta")
            existing = self.records.get(target_key)
            if existing is not None and _sha(existing) != target_hash:
                self.pointer = {**pending, "phase": "fenced"}
                raise RunControlConflict("conflicting target record")
            if existing is None:
                self.records[target_key] = _deep(target)
            readback = self.records.get(target_key)
            if not isinstance(readback, dict) or _sha(readback) != target_hash:
                self.pointer = {**pending, "phase": "fenced"}
                raise RunControlConflict("target readback mismatch")
            if self.pointer != pending:
                self.pointer = {**pending, "phase": "fenced"}
                raise RunControlConflict("pending pointer changed")
            self.pointer = {
                "schema": "graphwing-run-control-pointer-v1",
                "run_control_id": pending["run_control_id"], "phase": "stable",
                "logical_revision": target["logical_revision"],
                "state_record_key": target_key, "state_sha256": target_hash,
                "observed_record_version": 1,
                "last_operation_id": pending["operation_id"],
            }
            self.pointer_version += 1
            return {"recovered": True, "state_sha256": target_hash,
                    "state_record_key": target_key}

    def launch_authorized(self) -> bool:
        try:
            state = self.load_verified()
        except RunControlConflict:
            return False
        reservation = state.get("outstanding_reservation")
        return bool(state.get("status") == "active" and isinstance(reservation, dict)
                    and reservation.get("phase") == "authorization_consumed")

    def load_verified(self) -> dict[str, Any]:
        if not isinstance(self.pointer, dict) or self.pointer.get("phase") != "stable":
            raise RunControlConflict("pointer is not stable")
        key = self.pointer.get("state_record_key")
        state = self.records.get(key)
        if (not isinstance(state, dict) or _sha(state) != self.pointer.get("state_sha256")
                or state.get("run_control_id") != self.pointer.get("run_control_id")
                or state.get("logical_revision") != self.pointer.get("logical_revision")):
            raise RunControlConflict("stable pointer record mismatch")
        return _deep(state)
