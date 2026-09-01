#!/usr/bin/env python3
"""Provider-free tests for the dormant Rewst HMAC/CAS foundation."""

import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GRAPHWING_HOME", str(Path(__file__).resolve().parent))
os.environ.setdefault("GRAPHWING_HERDR", "0")

import server
import install

ROOT = Path(__file__).resolve().parent
LAUNCH_PATHS = {
    "/v1/agent/run": "AgentRunRequest",
    "/v1/review/run": "ReviewRunRequest",
    "/v1/pr/continue": "PrContinueRequest",
    "/v1/slice/continue": "SliceContinueRequest",
}


def authorized_body(*, auth_overrides=None, descriptor_overrides=None, request=None):
    request = dict(request or {})
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    descriptor = {
        "descriptor_version": "graphwing-launch-descriptor-v1",
        "operation": "agent_run",
        "route_version": "normal-v1",
        "role": "writer",
        "work_kind": "go_coding",
        "work_class": "mechanical",
        "effective_size": "S",
        "profile_version": "route-execution-profile-v1",
        "launcher": "codex",
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "requested_effort": "medium",
        "effective_effort": "medium",
        "effort_source": "route",
        "launcher_version": "sha256:" + "1" * 64,
        "repo": "scratch",
        "branch": "main",
        "starting_head": "2" * 40,
        "prompt_sha256": "3" * 64,
        "diff_sha256": None,
        "resume_parent_job_id": None,
        "max_turns": 40,
        "wall_seconds": 600,
        "max_tokens": 200000,
        "max_cost_usd": "2.00",
        "callback_sha256": "4" * 64,
        "permission_profile": "workspace-write-v1",
        "authorization_id": "auth-1",
        "consumed_version": 2,
        "record_key": "run-1",
        "record_version": 7,
        "payload_sha256": "5" * 64,
        "request_sha256": hashlib.sha256(canonical).hexdigest(),
    }
    descriptor.update(descriptor_overrides or {})
    authorization = {
        "authorization_version": "graphwing-rewst-authorization-v1",
        "authorization_id": "auth-1",
        "state": "consumed",
        "ok": True,
        "swapped": True,
        "expected_version": 1,
        "consumed_version": 2,
        "issued_at": 1699999990,
        "expires_at": 1700000100,
        "collection": "graphwing_run_control_v1",
        "record_key": "run-1",
        "record_found": True,
        "record_version": 7,
        "payload_sha256": "5" * 64,
        "descriptor": descriptor,
    }
    authorization.update(auth_overrides or {})
    return json.dumps(
        {**request, "rewst_authorization": authorization},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def signed_headers(body, *, timestamp=1700000000, nonce="a" * 64, secret=b"r" * 32):
    signature = hmac.new(
        secret,
        str(timestamp).encode() + b"." + nonce.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Graphwing-Rewst-Timestamp": str(timestamp),
        "X-Graphwing-Rewst-Nonce": nonce,
        "X-Graphwing-Rewst-Signature": signature,
    }


class RewstAuthorizationFoundationTests(unittest.TestCase):
    def setUp(self):
        server.reset_rewst_authority_registry_for_test()

    def test_exact_body_hmac_timestamp_nonce_replay_and_saturation_fail_closed(self):
        body = authorized_body()
        headers = signed_headers(body)
        with mock.patch.object(server, "load_rewst_issuer_secret", return_value=b"r" * 32):
            authority, error = server.verify_rewst_issuer_request(
                "POST", "/v1/agent/run", body, headers, now=1700000000
            )
            self.assertIsNone(error)
            self.assertEqual(authority["body_sha256"], hashlib.sha256(body).hexdigest())
            for bad_body, bad_headers, code in (
                (body + b" ", headers, "rewst_authorization_invalid"),
                (body, signed_headers(body, timestamp=1699999699), "rewst_authorization_stale"),
                (body, signed_headers(body, nonce="short"), "rewst_authorization_invalid"),
                (body, signed_headers(body, nonce="A" * 64), "rewst_authorization_invalid"),
                (body, {**signed_headers(body, nonce="c" * 64),
                        "X-Graphwing-Rewst-Timestamp": " 1700000000 "},
                 "rewst_authorization_invalid"),
                (body, {**signed_headers(body, nonce="d" * 64),
                        "X-Graphwing-Rewst-Nonce": " " + "d" * 64},
                 "rewst_authorization_invalid"),
                (body, {**signed_headers(body, nonce="e" * 64),
                        "X-Graphwing-Rewst-Signature": signed_headers(body, nonce="e" * 64)["X-Graphwing-Rewst-Signature"] + " "},
                 "rewst_authorization_invalid"),
                (body, {**signed_headers(body), "X-Graphwing-Rewst-Timestamp": "01700000000"},
                 "rewst_authorization_invalid"),
            ):
                _, failure = server.verify_rewst_issuer_request(
                    "POST", "/v1/agent/run", bad_body, bad_headers, now=1700000000
                )
                self.assertEqual(failure[1]["code"], code)
            _, replay = server.verify_rewst_issuer_request(
                "POST", "/v1/agent/run", body, headers, now=1700000000
            )
            self.assertEqual(replay[1]["code"], "rewst_authorization_replayed")
            with mock.patch.object(server, "REWST_REPLAY_REGISTRY_MAX", 1):
                _, saturated = server.verify_rewst_issuer_request(
                    "POST", "/v1/agent/run", body,
                    signed_headers(body, nonce="b" * 64), now=1700000000
                )
            self.assertEqual(saturated[1]["code"], "rewst_authorization_registry_saturated")

    def test_consumed_tuple_descriptor_and_constant_time_comparison_are_closed(self):
        body = authorized_body()
        with (
            mock.patch.object(server, "load_rewst_issuer_secret", return_value=b"r" * 32),
            mock.patch.object(server.hmac, "compare_digest", wraps=hmac.compare_digest) as compare,
        ):
            authority, error = server.verify_rewst_issuer_request(
                "POST", "/v1/agent/run", body, signed_headers(body), now=1700000000
            )
            self.assertIsNone(error)
            self.assertTrue(server.rewst_descriptor_matches(authority, authority["descriptor"]))
            self.assertGreaterEqual(compare.call_count, 2)
        for auth_drift, descriptor_drift in (
            ({"state": "issued"}, {}),
            ({"consumed_version": 3}, {}),
            ({"expires_at": 1700001000}, {}),
            ({}, {"operation": "review_run"}),
            ({}, {"request_sha256": "0" * 64}),
            ({"extra": True}, {}),
        ):
            bad = authorized_body(
                auth_overrides=auth_drift, descriptor_overrides=descriptor_drift
            )
            with mock.patch.object(server, "load_rewst_issuer_secret", return_value=b"r" * 32):
                server.reset_rewst_authority_registry_for_test()
                validated, failure = server.verify_rewst_issuer_request(
                    "POST", "/v1/agent/run", bad, signed_headers(bad), now=1700000000
                )
            self.assertIsNone(validated)
            self.assertEqual(failure[1]["code"], "rewst_authorization_invalid")

    def test_process_local_claim_and_consume_are_opaque_one_winner_and_restart_closed(self):
        body = authorized_body()
        with mock.patch.object(server, "load_rewst_issuer_secret", return_value=b"r" * 32):
            authority, error = server.verify_rewst_issuer_request(
                "POST", "/v1/agent/run", body, signed_headers(body), now=1700000000
            )
        self.assertIsNone(error)
        tampered = json.loads(json.dumps(authority))
        tampered["descriptor"]["repo"] = "other"
        self.assertFalse(server.claim_rewst_launch_authority(tampered, "tampered-job"))
        barrier = threading.Barrier(8)
        results = []
        lock = threading.Lock()

        def claim(index):
            barrier.wait()
            won = server.claim_rewst_launch_authority(authority, f"job-{index}")
            with lock:
                results.append((won, index))

        threads = [threading.Thread(target=claim, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(won for won, _ in results), 1)
        winner = next(f"job-{index}" for won, index in results if won)
        self.assertTrue(server.consume_rewst_authority_for_job(winner))
        self.assertFalse(server.consume_rewst_authority_for_job(winner))
        server.reset_rewst_authority_registry_for_test()
        self.assertFalse(server.consume_rewst_launch_authority(authority, winner))

    def test_secret_loader_is_distinct_strong_and_env_or_mode_0600_seat_file(self):
        with tempfile.TemporaryDirectory() as td:
            secret_path = Path(td) / "rewst-hmac.key"
            with (
                mock.patch.object(server, "REWST_ISSUER_SECRET_PATH", secret_path),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                with self.assertRaises(RuntimeError):
                    server.load_rewst_issuer_secret()
                secret_path.write_bytes(b"x" * 31)
                with self.assertRaises(RuntimeError):
                    server.load_rewst_issuer_secret()
                secret_path.write_bytes(b"f" * 32 + b"\n")
                secret_path.chmod(0o644)
                with self.assertRaises(RuntimeError):
                    server.load_rewst_issuer_secret()
                secret_path.chmod(0o600)
                self.assertEqual(server.load_rewst_issuer_secret(), b"f" * 32)
                with mock.patch.dict(os.environ, {"GRAPHWING_REWST_HMAC_SECRET": "e" * 32}):
                    self.assertEqual(server.load_rewst_issuer_secret(), b"e" * 32)

    def test_installer_creates_and_preserves_a_private_strong_seat_secret(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {}, clear=True):
            home = Path(td)
            path = install.ensure_rewst_hmac_secret(home)
            original = path.read_bytes()
            self.assertGreaterEqual(len(original.strip()), 32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(install.ensure_rewst_hmac_secret(home).read_bytes(), original)
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "target").write_bytes(b"z" * 32)
            (home / "rewst-hmac.key").symlink_to(home / "target")
            with self.assertRaises(SystemExit):
                install.ensure_rewst_hmac_secret(home)

    def test_native_helper_has_exact_readback_hash_binding_and_issued_to_consumed_cas(self):
        graph = json.loads((ROOT / "graphs" / "run-control-authorize.json").read_text())
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        expected = {
            "load": "action.datastore.records.get",
            "upsert": "action.datastore.records.upsert",
            "readback": "action.datastore.records.get",
            "expected_hash": "transforms.hash",
            "readback_hash": "transforms.hash",
            "issue": "action.datastore.kv.compareAndSwap",
            "consume": "action.datastore.kv.compareAndSwap",
        }
        self.assertEqual({key: nodes[key]["type"] for key in expected}, expected)
        self.assertEqual(nodes["load"]["config"]["recordKey"], "{{ CTX.INPUT.record_key }}")
        self.assertEqual(nodes["upsert"]["config"]["recordKey"], "{{ CTX.state.record_key }}")
        self.assertEqual(nodes["readback"]["config"]["recordKey"], "{{ CTX.state.record_key }}")
        issue, consume = nodes["issue"]["config"], nodes["consume"]["config"]
        self.assertEqual(issue["namespace"], "graphwing_run_authorizations_v1")
        self.assertEqual((issue["expectedVersion"], consume["expectedVersion"]), (0, 1))
        self.assertEqual((issue["value"]["state"], consume["value"]["state"]), ("issued", "consumed"))
        self.assertLessEqual(max(issue["ttlSeconds"], consume["ttlSeconds"]), 300)

    def test_openapi_installer_publisher_and_docs_are_registered_but_launches_remain_dormant(self):
        spec = json.loads((ROOT / "openapi.json").read_text())
        schemes = spec["components"]["securitySchemes"]
        self.assertEqual(schemes["rewstTimestampHeader"]["name"], "X-Graphwing-Rewst-Timestamp")
        self.assertEqual(schemes["rewstNonceHeader"]["name"], "X-Graphwing-Rewst-Nonce")
        self.assertEqual(schemes["rewstSignatureHeader"]["name"], "X-Graphwing-Rewst-Signature")
        auth = spec["components"]["schemas"]["RewstConsumedAuthorization"]
        descriptor = spec["components"]["schemas"]["RewstLaunchDescriptor"]
        self.assertFalse(auth["additionalProperties"])
        self.assertFalse(descriptor["additionalProperties"])
        self.assertEqual(set(auth["required"]), server.REWST_AUTHORIZATION_FIELDS)
        self.assertEqual(set(descriptor["required"]), server.REWST_DESCRIPTOR_FIELDS)
        for path, schema_name in LAUNCH_PATHS.items():
            operation = spec["paths"][path]["post"]
            self.assertNotIn("rewstTimestampHeader", json.dumps(operation.get("security", [])))
            schema = spec["components"]["schemas"][schema_name]
            self.assertNotIn("rewst_authorization", schema.get("required", []))
            self.assertNotIn("rewst_authorization", schema["properties"])
        for name in ("implement-slice.json", "pr-drive.json", "code-off.json"):
            text = (ROOT / "graphs" / name).read_text()
            self.assertNotIn("run-control-authorize", text)
            self.assertNotIn("rewst_authorization", text)
            self.assertNotIn("$GRAPHWING_RUN_CONTROL_WORKFLOW", text)
        publisher = (ROOT / "scripts" / "publish_graphs.py").read_text()
        self.assertIn('"run-control-authorize"', publisher)
        installer = (ROOT / "install.py").read_text()
        self.assertIn('"rewst-hmac.key"', installer)
        docs = (ROOT / "docs" / "REWST-AUTHORIZATION.md").read_text().lower()
        self.assertIn("inert", docs)
        credential = json.loads((ROOT / "examples" / "rewst-request-hmac-credential.json").read_text())
        self.assertEqual(
            credential["fields"],
            [
                {"name": "graphwing_api_key", "type": "secret", "required": True},
                {"name": "rewst_hmac_secret", "type": "secret", "required": True},
            ],
        )
        self.assertNotIn('"value"', json.dumps(credential).lower())
        self.assertNotIn('"default"', json.dumps(credential).lower())


if __name__ == "__main__":
    unittest.main()
