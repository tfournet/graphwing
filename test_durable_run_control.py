#!/usr/bin/env python3
"""Provider-free contract tests for dormant durable run control."""
import json
import threading
import unittest
from pathlib import Path

from run_control_model import DurableRunControlModel, RunControlConflict
import server

ROOT = Path(__file__).resolve().parent


def initialize_request(**overrides):
    request = {
        "root_workflow_id": "wf-1",
        "root_workflow_version_id": "wfv-1",
        "root_workflow_run_id": "run-1",
        "purpose": "implement_slice",
        "budgets": {"attempts": 4, "turns": 300, "wall_seconds": 7200,
                    "tokens": 500000, "provider_cost_usd": "25.000000000000"},
        "initial_route": {"route_version": "normal-v1", "launcher": "codex",
                          "provider": "openai", "model": "gpt-5.6-sol"},
    }
    request.update(overrides)
    return request


def evaluator_route():
    return {"route_version": "normal-v1", "launcher": "codex",
            "provider": "openai", "model": "gpt-5.6-sol"}


def evaluator_attempt(ordinal, turns, cost, fingerprint):
    envelope = {"attempt_id": f"att-{ordinal}", "turns": turns,
                "wall_seconds": 1800, "tokens": 150000,
                "provider_cost_usd": cost}
    return {"sequence": ordinal, "attempt_id": f"att-{ordinal}", **evaluator_route(),
            "usage": {key: envelope[key] for key in (
                "turns", "wall_seconds", "tokens", "provider_cost_usd")},
            "reservation": envelope, "terminal_state": "terminal",
            "progress": {"checkpoint": 1, "failing_regression_present": True,
                         "production_diff_bytes": 1200, "focused_tests_green": False,
                         "diff_fingerprint": fingerprint},
            "constraint_signals": [], "verified_outcome": False}


def reservation_request(route=None, handoff=None):
    value = {
        "candidate_route": route or evaluator_route(),
        "envelope": {"turns": 40, "wall_seconds": 600, "tokens": 100000,
                     "provider_cost_usd": "2.000000000000"},
        "endpoint": "/v1/agent/run", "launcher_fingerprint": "sha256:" + "1" * 64,
        "exact_request_body_sha256": "2" * 64, "repository": "scratch",
        "branch": "main", "head_sha": "3" * 40, "task_sha256": "4" * 64,
        "permission_profile": "workspace-write-v1",
        "callback_binding_sha256": "5" * 64,
    }
    if handoff is not None:
        value["handoff"] = handoff
    return value


class DurableRunControlGraphTests(unittest.TestCase):
    def test_initialize_graph_exists_as_dormant_native_workflow(self):
        graph = json.loads((ROOT / "graphs/run-control-state.json").read_text())
        self.assertEqual(graph["slug"], "graphwing-run-control-state")
        self.assertTrue(graph["spec"]["meta"]["dormant"])

    def test_concurrent_initialize_has_one_winner_and_exact_replay(self):
        store = DurableRunControlModel()
        request = initialize_request()
        barrier = threading.Barrier(8)
        results = []
        errors = []

        def initialize():
            barrier.wait()
            try:
                results.append(store.initialize(request))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=initialize) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual({result["state_sha256"] for result in results},
                         {results[0]["state_sha256"]})
        self.assertEqual(store.pointer["phase"], "stable")
        self.assertEqual(store.pointer["logical_revision"], 0)

    def test_immutable_budget_conflict_and_pending_recovery_are_fail_closed(self):
        store = DurableRunControlModel()
        original = initialize_request()
        store.initialize(original)
        changed = initialize_request(budgets={**original["budgets"], "turns": 301})
        with self.assertRaisesRegex(RunControlConflict, "immutable"):
            store.initialize(changed)

        stable = dict(store.pointer)
        state = store.load_verified()
        request_hash = "1" * 64
        target = {**state, "logical_revision": 1,
                  "last_transition": {"operation_id": "op1-" + "2" * 64,
                                      "type": "test", "from_logical_revision": 0,
                                      "request_sha256": request_hash}}
        pending = store.prepare_for_test(target, "test", request_hash)
        self.assertEqual(store.pointer["phase"], "pending")
        self.assertFalse(store.launch_authorized())
        recovered = store.recover()
        self.assertTrue(recovered["recovered"])
        self.assertEqual(store.load_verified()["logical_revision"], 1)
        self.assertEqual(store.recover()["state_sha256"], store.pointer["state_sha256"])

        # A content-addressed key containing different bytes is corruption, never overwrite.
        second = DurableRunControlModel()
        second.initialize(original)
        next_state = {**second.load_verified(), "logical_revision": 1,
                      "last_transition": {"operation_id": "op1-" + "3" * 64,
                                          "type": "test", "from_logical_revision": 0,
                                          "request_sha256": "4" * 64}}
        pending = second.prepare_for_test(next_state, "test", "4" * 64)
        second.records[pending["target_state_record_key"]] = {"conflict": True}
        with self.assertRaisesRegex(RunControlConflict, "conflicting"):
            second.recover()
        self.assertEqual(second.pointer["phase"], "fenced")
        self.assertEqual(stable["schema"], second.pointer["schema"])

    def test_evaluator_refuses_260_turns_and_5_37_before_reservation(self):
        same = "a" * 64
        request = {
            "run_id": "rc1-" + "b" * 64,
            "route": evaluator_route(),
            "budgets": {"attempts": 4, "turns": 260, "wall_seconds": 7200,
                        "tokens": 500000, "provider_cost_usd": "5.370000000000"},
            "attempts": [evaluator_attempt(1, 130, "2.680000000000", same),
                         evaluator_attempt(2, 130, "2.690000000000", same)],
            "next_attempt": 3,
            "next_envelope": {"attempt_id": "att-3", "turns": 1,
                              "wall_seconds": 1, "tokens": 1,
                              "provider_cost_usd": "0.000000000001"},
        }
        status, result = server.run_control_evaluate(
            json.dumps(request, separators=(",", ":")).encode())
        self.assertEqual(status, 200)
        self.assertEqual(result["decision"], "terminate")
        self.assertEqual(result["aggregate"]["turns"], 260)
        self.assertEqual(result["aggregate"]["provider_cost_usd"], "5.37")
        self.assertIn("turns_exhausted", result["evidence_codes"])
        self.assertIn("provider_cost_exhausted", result["evidence_codes"])

    def test_reserve_uses_only_stored_history_and_allows_one_outstanding(self):
        store = DurableRunControlModel()
        store.initialize(initialize_request())
        observed = []

        def evaluator(body):
            request = json.loads(body)
            observed.append(request)
            return 200, {"version": "run-control-v1",
                         "decision": "continue_same_model",
                         "classification": None, "retryable": None}

        reserved = store.reserve(reservation_request(), evaluator, lambda: "6" * 64)
        state = store.load_verified()
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["budgets"], state["immutable"]["budgets"])
        self.assertEqual(observed[0]["attempts"], [])
        self.assertEqual(state["outstanding_reservation"]["attempt_id"],
                         reserved["attempt_id"])
        self.assertTrue(state["same_model_continuation_used"])
        self.assertRegex(reserved["attempt_id"], r"^att1-[0-9a-f]{64}$")
        self.assertRegex(reserved["authorization_id"], r"^rca1-[0-9a-f]{64}-[0-9a-f]{64}$")
        self.assertEqual(reserved["server_instance_challenge"], "6" * 64)
        with self.assertRaisesRegex(RunControlConflict, "outstanding"):
            store.reserve(reservation_request(), evaluator, lambda: "7" * 64)
        self.assertEqual(len(observed), 1)

    def test_terminal_receipt_reconciles_once_and_unknown_charges_full_envelope(self):
        def allow(body):
            return 200, {"version": "run-control-v1", "decision": "continue_same_model",
                         "classification": None, "retryable": None}

        store = DurableRunControlModel()
        store.initialize(initialize_request())
        reservation = store.reserve(reservation_request(), allow, lambda: "6" * 64)
        store.mark_authorization_consumed("6" * 64, consumed_version=2,
                                          consumed_value_sha256="7" * 64)
        state = store.load_verified()
        held = state["outstanding_reservation"]
        receipt = {
            "receipt_id": "rec1-" + "8" * 64,
            "attempt_id": reservation["attempt_id"],
            "run_control_id": state["run_control_id"],
            "authorization_id": reservation["authorization_id"],
            "launch_descriptor_sha256": reservation["launch_descriptor_sha256"],
            "job_id": "job-1", "terminal_status": "failed",
            "route": held["route"],
            "usage": {"turns": 21, "wall_seconds": 300, "tokens": 88000,
                      "provider_cost_usd": "1.250000000000"},
            "progress": {"checkpoint": 1, "failing_regression_present": True,
                         "production_diff_bytes": 1200, "focused_tests_green": False,
                         "diff_fingerprint": "9" * 64},
            "constraint_signals": [], "verified_outcome": False,
        }
        first = store.reconcile_receipt(receipt)
        replay = store.reconcile_receipt(receipt)
        state = store.load_verified()
        self.assertTrue(first["reconciled"])
        self.assertFalse(replay["reconciled"])
        self.assertEqual(len(state["attempts"]), 1)
        self.assertEqual(len(state["evaluator_history"]), 1)
        self.assertEqual(state["attempts"][0]["charged_usage"]["provider_cost_usd"],
                         "1.250000000000")
        evaluator_request = {
            "run_id": state["run_control_id"], "route": state["current_route"],
            "budgets": state["immutable"]["budgets"],
            "attempts": state["evaluator_history"], "next_attempt": 2,
            "next_route": state["current_route"],
            "next_envelope": {"attempt_id": "att-next", "turns": 1,
                              "wall_seconds": 1, "tokens": 1,
                              "provider_cost_usd": "0.000000000001"},
        }
        self.assertEqual(server.run_control_evaluate(
            json.dumps(evaluator_request, separators=(",", ":")).encode())[0], 200)
        self.assertIsNone(state["outstanding_reservation"])
        with self.assertRaisesRegex(RunControlConflict, "conflicting"):
            store.reconcile_receipt({**receipt, "job_id": "different-job"})
        self.assertEqual(store.load_verified()["status"], "fenced")

        unknown = DurableRunControlModel()
        unknown.initialize(initialize_request(root_workflow_run_id="run-unknown"))
        held_result = unknown.reserve(reservation_request(), allow, lambda: "a" * 64)
        unknown.mark_authorization_consumed("a" * 64, 2, "b" * 64)
        unknown_receipt = {**receipt, "receipt_id": "rec1-" + "c" * 64,
                           "attempt_id": held_result["attempt_id"],
                           "run_control_id": unknown.load_verified()["run_control_id"],
                           "authorization_id": held_result["authorization_id"],
                           "launch_descriptor_sha256": held_result["launch_descriptor_sha256"],
                           "usage": {**receipt["usage"], "tokens": None}}
        unknown.reconcile_receipt(unknown_receipt)
        charged = unknown.load_verified()["attempts"][0]
        self.assertEqual(charged["reconciliation"]["kind"], "authority_lost")
        self.assertEqual(charged["charged_usage"], reservation_request()["envelope"])
        self.assertFalse(unknown.reconcile_authority_loss("launch_result_lost")["reconciled"])
        self.assertEqual(len(unknown.load_verified()["attempts"]), 1)

    def test_global_continuation_and_explicit_cross_model_guards(self):
        store = DurableRunControlModel()
        store.initialize(initialize_request(root_workflow_run_id="run-handoff"))
        same_decision = lambda body: (200, {"version": "run-control-v1",
                                             "decision": "continue_same_model",
                                             "classification": None, "retryable": None})
        first = store.reserve(reservation_request(), same_decision, lambda: "1" * 64)
        store.mark_authorization_consumed("1" * 64, 2, "2" * 64)
        held = store.load_verified()["outstanding_reservation"]
        receipt = {
            "receipt_id": "rec1-" + "3" * 64, "attempt_id": first["attempt_id"],
            "run_control_id": store.load_verified()["run_control_id"],
            "authorization_id": first["authorization_id"],
            "launch_descriptor_sha256": first["launch_descriptor_sha256"],
            "job_id": "job-handoff", "terminal_status": "failed", "route": held["route"],
            "usage": {"turns": 10, "wall_seconds": 100, "tokens": 1000,
                      "provider_cost_usd": "0.100000000000"},
            "progress": {"checkpoint": 1, "failing_regression_present": True,
                         "production_diff_bytes": 1, "focused_tests_green": False,
                         "diff_fingerprint": "4" * 64},
            "constraint_signals": [], "verified_outcome": False,
        }
        store.reconcile_receipt(receipt)
        with self.assertRaisesRegex(RunControlConflict, "same-model"):
            store.reserve(reservation_request(), same_decision, lambda: "5" * 64)

        cross = {"route_version": "availability-fallback-v1", "launcher": "claude",
                 "provider": "anthropic", "model": "claude-sonnet-5"}
        handoff_decision = lambda body: (200, {"version": "run-control-v1",
                                                "decision": "handoff_cross_model",
                                                "classification": None, "retryable": None})
        with self.assertRaisesRegex(RunControlConflict, "explicit cross-model"):
            store.reserve(reservation_request(cross), handoff_decision, lambda: "5" * 64)
        accepted = store.reserve(reservation_request(cross, {"reason_code": "provider_switch"}),
                                 handoff_decision, lambda: "5" * 64)
        self.assertEqual(accepted["decision"], "handoff_cross_model")
        self.assertEqual(store.load_verified()["current_route"], cross)
        self.assertTrue(store.load_verified()["same_model_continuation_used"])

        terminated = DurableRunControlModel()
        terminated.initialize(initialize_request(root_workflow_run_id="run-terminal"))
        challenged = []
        stop = lambda body: (200, {"version": "run-control-v1", "decision": "terminate",
                                   "classification": "budget_exhausted", "retryable": False})
        result = terminated.reserve(reservation_request(), stop,
                                    lambda: challenged.append(True) or "6" * 64)
        self.assertFalse(result["reserved"])
        self.assertEqual(challenged, [])
        self.assertEqual(terminated.load_verified()["status"], "terminal")

    def test_native_graphs_encode_exact_transition_protocol_and_no_launch_edge(self):
        controller = json.loads((ROOT / "graphs/run-control-state.json").read_text())
        transition = json.loads((ROOT / "graphs/run-control-transition.json").read_text())
        controller_nodes = {node["id"]: node for node in controller["spec"]["nodes"]}
        transition_nodes = {node["id"]: node for node in transition["spec"]["nodes"]}
        expected_transition = {
            "pointer_get": "action.datastore.kv.get",
            "load_state": "action.datastore.records.get",
            "state_hash": "transforms.hash",
            "prepare_cas": "action.datastore.kv.compareAndSwap",
            "pending_get": "action.datastore.kv.get",
            "target_get": "action.datastore.records.get",
            "target_upsert": "action.datastore.records.upsert",
            "readback": "action.datastore.records.get",
            "readback_hash": "transforms.hash",
            "publish_cas": "action.datastore.kv.compareAndSwap",
            "hard_failure": "transforms.regexReplace",
        }
        self.assertEqual({key: transition_nodes[key]["type"] for key in expected_transition},
                         expected_transition)
        for node_id in ("pointer_get", "prepare_cas", "pending_get", "publish_cas"):
            self.assertEqual(transition_nodes[node_id]["config"]["namespace"],
                             "graphwing_run_control_pointer_v1")
        for node_id in ("load_state", "target_get", "target_upsert", "readback"):
            self.assertEqual(transition_nodes[node_id]["config"]["collection"],
                             "graphwing_run_control_states_v1")
            self.assertEqual(transition_nodes[node_id]["config"]["scope"], "tenant")
        self.assertEqual(transition_nodes["target_upsert"]["config"]["ttlSeconds"], 0)
        self.assertEqual(transition_nodes["state_hash"]["config"]["inputKind"],
                         "json_stringify")
        self.assertEqual(transition_nodes["readback_hash"]["config"]["inputKind"],
                         "json_stringify")

        self.assertEqual(controller_nodes["evaluate"]["type"],
                         "action.graphwing.POST:/v1/run/control/evaluate")
        self.assertEqual(controller_nodes["challenge"]["type"],
                         "action.graphwing.GET:/v1/rewst/server-challenge")
        child = controller_nodes["commit_transition"]["config"]
        self.assertEqual(child["workflowVersionId"],
                         "$GRAPHWING_RUN_CONTROL_TRANSITION_VERSION_ID")
        self.assertNotIn("versionSelection", child)
        self.assertEqual(child["workflowId"], "$GRAPHWING_RUN_CONTROL_TRANSITION_WORKFLOW_ID")
        all_nodes = controller["spec"]["nodes"] + transition["spec"]["nodes"]
        types = {node["type"] for node in all_nodes}
        self.assertNotIn("transforms.codeExpression", types)
        self.assertFalse(any(node_type.startswith("action.graphwing.POST:/v1/agent")
                             or node_type.startswith("action.graphwing.POST:/v1/review")
                             for node_type in types))
        dumped = json.dumps(all_nodes)
        self.assertNotIn("versionSelection", dumped)
        self.assertNotRegex(dumped, r"{%-?\s*set\s+")

    def test_evaluator_openapi_is_closed_and_publisher_pins_child_versions(self):
        spec = json.loads((ROOT / "openapi.json").read_text())
        operation = spec["paths"]["/v1/run/control/evaluate"]["post"]
        self.assertEqual(operation["operationId"], "runControlEvaluate")
        request = spec["components"]["schemas"]["RunControlRequest"]
        response = spec["components"]["schemas"]["RunControlReceipt"]
        self.assertFalse(request["additionalProperties"])
        self.assertFalse(response["additionalProperties"])
        self.assertEqual(
            operation["requestBody"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/RunControlRequest"},
        )
        source = (ROOT / "scripts/publish_graphs.py").read_text()
        self.assertIn('"run-control-transition"', source)
        self.assertIn('"run-control-state"', source)
        self.assertIn("workflowVersionId", source)
        self.assertNotIn('"versionSelection": "latest"', source)

    def test_initialize_and_reconcile_are_native_pinned_and_dormant(self):
        graphs = {
            name: json.loads((ROOT / "graphs" / f"{name}.json").read_text())
            for name in ("run-control-initialize", "run-control-reconcile",
                         "run-control-consume")
        }
        initialize = {node["id"]: node for node in graphs["run-control-initialize"]["spec"]["nodes"]}
        reconcile = {node["id"]: node for node in graphs["run-control-reconcile"]["spec"]["nodes"]}
        consume = {node["id"]: node for node in graphs["run-control-consume"]["spec"]["nodes"]}
        self.assertEqual(initialize["run_id_hash"]["type"], "transforms.hash")
        self.assertEqual(initialize["commit_initialize"]["type"], "action.subworkflow")
        self.assertEqual(reconcile["receipt_hash"]["type"], "transforms.hash")
        self.assertEqual(reconcile["commit_receipt"]["type"], "action.subworkflow")
        self.assertEqual(reconcile["commit_authority_loss"]["type"], "action.subworkflow")
        self.assertEqual(consume["authorize"]["type"], "action.subworkflow")
        self.assertEqual(consume["commit_consumed"]["type"], "action.subworkflow")
        self.assertEqual(consume["authorize"]["config"]["workflowVersionId"],
                         "$GRAPHWING_RUN_CONTROL_AUTHORIZE_VERSION_ID")
        for graph in graphs.values():
            self.assertTrue(graph["spec"]["meta"]["dormant"])
            for node in graph["spec"]["nodes"]:
                self.assertNotEqual(node["type"], "transforms.codeExpression")
                self.assertFalse(node["type"].startswith("action.graphwing.POST:/v1/agent"))
                if node["type"] == "action.subworkflow":
                    self.assertIn("workflowVersionId", node["config"])
                    self.assertNotIn("versionSelection", node["config"])


if __name__ == "__main__":
    unittest.main()
