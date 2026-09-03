#!/usr/bin/env python3
"""Provider-free tests for the dormant Rewst HMAC/CAS foundation."""

import hashlib
import hmac
import json
import os
import re
import stat
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


def authorized_body(
    *, challenge=None, auth_overrides=None, descriptor_overrides=None, request=None,
):
    challenge = challenge or server.REWST_SERVER_INSTANCE_CHALLENGE
    request = dict(request or {})
    request_hash = hashlib.sha256(json.dumps(
        request, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    descriptor = dict(
        descriptor_version="graphwing-launch-descriptor-v1", operation="agent_run",
        route_version="normal-v1", role="writer", work_kind="go_coding",
        work_class="mechanical", effective_size="S",
        profile_version="route-execution-profile-v1", launcher="codex",
        provider="openai", model="gpt-5.6-sol", requested_effort="medium",
        effective_effort="medium", effort_source="route",
        launcher_version="sha256:" + "1" * 64, repo="scratch", branch="main",
        starting_head="2" * 40, prompt_sha256="3" * 64, diff_sha256=None,
        resume_parent_job_id=None, max_turns=40, wall_seconds=600,
        max_tokens=200000, max_cost_usd="2.00", callback_sha256="4" * 64,
        permission_profile="workspace-write-v1", authorization_id="auth-1",
        consumed_version=2, record_key="run-1", record_version=7,
        payload_sha256="5" * 64, request_sha256=request_hash,
        server_instance_challenge=challenge,
    )
    descriptor.update(descriptor_overrides or {})
    authorization = dict(
        authorization_version="graphwing-rewst-authorization-v1",
        authorization_id="auth-1", state="consumed", ok=True, swapped=True,
        expected_version=1, consumed_version=2, issued_at=1699999990,
        expires_at=1700000100, collection="graphwing_run_control_v1",
        record_key="run-1", record_found=True, record_version=7,
        payload_sha256="5" * 64, descriptor=descriptor,
        server_instance_challenge=challenge,
    )
    authorization.update(auth_overrides or {})
    return json.dumps({**request, "rewst_authorization": authorization},
                      sort_keys=True, separators=(",", ":")).encode()


def signed_headers(
    body, *, challenge=None, timestamp=1700000000, nonce="a" * 64, secret=b"r" * 32,
):
    challenge = challenge or server.REWST_SERVER_INSTANCE_CHALLENGE
    prefix = (
        b"graphwing-rewst-request-v2\n"
        + challenge.encode("ascii") + b"\n"
        + str(timestamp).encode("ascii") + b"\n"
        + nonce.encode("ascii") + b"\n"
        + body
    )
    return {
        "X-Graphwing-Rewst-Timestamp": str(timestamp),
        "X-Graphwing-Rewst-Nonce": nonce,
        "X-Graphwing-Rewst-Signature": hmac.new(secret, prefix, hashlib.sha256).hexdigest(),
    }


def swapping_open(secret, moved, target):
    real_open, swapped = os.open, []
    def open_once(path, flags, mode=0o777):
        descriptor = real_open(path, flags, mode)
        if Path(path) == secret and not swapped:
            secret.rename(moved)
            secret.symlink_to(target)
            swapped.append(True)
        return descriptor
    return swapped, open_once


def verify(body, headers=None):
    with mock.patch.object(server, "load_rewst_issuer_secret", return_value=b"r" * 32):
        return server.verify_rewst_issuer_request(
            "POST", "/v1/agent/run", body, headers or signed_headers(body), now=1700000000
        )


class RewstAuthorizationFoundationTests(unittest.TestCase):
    def setUp(self):
        server.reset_rewst_authority_registry_for_test()

    def test_server_instance_challenge_is_256_bit_and_endpoint_requires_only_api_key(self):
        self.assertRegex(server.REWST_SERVER_INSTANCE_CHALLENGE, r"^[0-9a-f]{64}$")
        with mock.patch.object(server.secrets, "token_hex", return_value="c" * 64) as token_hex:
            self.assertEqual(server._new_rewst_server_instance_challenge(), "c" * 64)
        token_hex.assert_called_once_with(32)

        with mock.patch.object(
            server, "load_rewst_issuer_secret", side_effect=AssertionError("HMAC not required")
        ):
            denied = server.dispatch("GET", "/v1/rewst/server-challenge", {}, False, b"")
            allowed = server.dispatch("GET", "/v1/rewst/server-challenge", {}, True, b"")
        self.assertEqual(denied[:2], (
            401, {"error": "invalid or missing X-Graphwing-Key", "code": "unauthorized"},
        ))
        self.assertEqual(allowed[:2], (200, {
            "challenge_version": "graphwing-server-instance-challenge-v1",
            "server_instance_challenge": server.REWST_SERVER_INSTANCE_CHALLENGE,
        }))

    def test_restart_rotation_rejects_identical_still_fresh_request_before_claim(self):
        first_challenge = "a" * 64
        next_challenge = "b" * 64
        body = authorized_body(challenge=first_challenge)
        headers = signed_headers(body, challenge=first_challenge, nonce="d" * 64)
        with mock.patch.object(server, "REWST_SERVER_INSTANCE_CHALLENGE", first_challenge):
            authority, error = verify(body, headers)
        self.assertIsNone(error)
        self.assertEqual(authority["server_instance_challenge"], first_challenge)

        server.reset_rewst_authority_registry_for_test()
        with mock.patch.object(server, "REWST_SERVER_INSTANCE_CHALLENGE", next_challenge):
            replayed, failure = verify(body, headers)
        self.assertIsNone(replayed)
        self.assertEqual(failure[1]["code"], "rewst_authorization_invalid")
        self.assertEqual(server._REWST_AUTHORITIES, {})

    def test_challenge_equality_is_rechecked_at_claim_and_both_consume_boundaries(self):
        first_challenge = "6" * 64
        next_challenge = "7" * 64
        with mock.patch.object(server, "REWST_SERVER_INSTANCE_CHALLENGE", first_challenge):
            first, error = verify(authorized_body(challenge=first_challenge))
        self.assertIsNone(error)
        with mock.patch.object(server, "REWST_SERVER_INSTANCE_CHALLENGE", next_challenge):
            self.assertFalse(server.claim_rewst_launch_authority(first, "prelaunch-job"))

        server.reset_rewst_authority_registry_for_test()
        with mock.patch.object(server, "REWST_SERVER_INSTANCE_CHALLENGE", first_challenge):
            claimed, error = verify(authorized_body(
                challenge=first_challenge,
            ), signed_headers(
                authorized_body(challenge=first_challenge),
                challenge=first_challenge,
                nonce="8" * 64,
            ))
            self.assertIsNone(error)
            self.assertTrue(server.claim_rewst_launch_authority(claimed, "claimed-job"))
        with mock.patch.object(server, "REWST_SERVER_INSTANCE_CHALLENGE", next_challenge):
            self.assertFalse(server.consume_rewst_authority_for_job("claimed-job"))
            self.assertFalse(server.consume_rewst_launch_authority(claimed, "claimed-job"))

    def test_challenge_drift_and_malformed_challenges_fail_closed(self):
        current = "c" * 64
        cases = (
            ({"server_instance_challenge": "d" * 64}, {}),
            ({}, {"server_instance_challenge": "d" * 64}),
            ({"server_instance_challenge": "short"},
             {"server_instance_challenge": "short"}),
            ({"server_instance_challenge": "D" * 64},
             {"server_instance_challenge": "D" * 64}),
        )
        with mock.patch.object(server, "REWST_SERVER_INSTANCE_CHALLENGE", current):
            for index, (auth_drift, descriptor_drift) in enumerate(cases):
                body = authorized_body(
                    challenge=current,
                    auth_overrides=auth_drift,
                    descriptor_overrides=descriptor_drift,
                )
                server.reset_rewst_authority_registry_for_test()
                _, failure = verify(
                    body, signed_headers(body, challenge=current, nonce=f"{index + 1:064x}")
                )
                self.assertEqual(failure[1]["code"], "rewst_authorization_invalid")

    def test_exact_body_hmac_timestamp_nonce_replay_and_saturation_fail_closed(self):
        body = authorized_body()
        authority, error = verify(body)
        self.assertIsNone(error)
        self.assertEqual(authority["body_sha256"], hashlib.sha256(body).hexdigest())
        cases = ((body + b" ", signed_headers(body), "rewst_authorization_invalid"),
                 (body, signed_headers(body, timestamp=1699999699), "rewst_authorization_stale"),
                 (body, signed_headers(body, nonce="short"), "rewst_authorization_invalid"))
        for candidate, headers, code in cases:
            self.assertEqual(verify(candidate, headers)[1][1]["code"], code)
        self.assertEqual(verify(body)[1][1]["code"], "rewst_authorization_replayed")
        with mock.patch.object(server, "REWST_REPLAY_REGISTRY_MAX", 1):
            second = authorized_body(
                auth_overrides={"authorization_id": "auth-2"},
                descriptor_overrides={"authorization_id": "auth-2"},
            )
            saturated = verify(second, signed_headers(second, nonce="b" * 64))[1]
        self.assertEqual(saturated[1]["code"], "rewst_authorization_registry_saturated")

    def test_signed_lone_surrogates_and_canonicalization_failures_are_sanitized(self):
        expected = (401, {"error": "invalid Rewst authorization",
                          "code": "rewst_authorization_invalid"})
        body = authorized_body(request={"hostile": "\ud800"})
        self.assertEqual(verify(body), (None, expected))
        body = authorized_body()
        authority, error = verify(body, signed_headers(body, nonce="e" * 64))
        self.assertIsNone(error)
        assert authority is not None
        with mock.patch.object(server.json, "dumps", side_effect=RuntimeError("secret")):
            self.assertEqual(verify(body, signed_headers(body, nonce="f" * 64)),
                             (None, expected))
            self.assertFalse(server.rewst_descriptor_matches(authority,
                                                              authority["descriptor"]))
            self.assertIsNone(server._rewst_authority_digest(authority))

    def test_consumed_tuple_descriptor_and_constant_time_comparison_are_closed(self):
        body = authorized_body()
        with mock.patch.object(server.hmac, "compare_digest", wraps=hmac.compare_digest) as compare:
            authority, error = verify(body)
            self.assertIsNone(error)
            self.assertTrue(server.rewst_descriptor_matches(authority, authority["descriptor"]))
            self.assertGreaterEqual(compare.call_count, 2)
        for auth_drift, descriptor_drift in (
            ({"state": "issued"}, {}),
            ({}, {"operation": "review_run"}),
            ({}, {"request_sha256": "0" * 64}),
            ({"extra": True}, {}),
        ):
            bad = authorized_body(
                auth_overrides=auth_drift, descriptor_overrides=descriptor_drift
            )
            server.reset_rewst_authority_registry_for_test()
            validated, failure = verify(bad)
            self.assertIsNone(validated)
            self.assertEqual(failure[1]["code"], "rewst_authorization_invalid")

    def test_process_local_claim_and_consume_are_opaque_one_winner_and_restart_closed(self):
        authority, error = verify(authorized_body())
        self.assertIsNone(error)
        tampered = json.loads(json.dumps(authority))
        tampered["descriptor"]["repo"] = "other"
        self.assertFalse(server.claim_rewst_launch_authority(tampered, "tampered-job"))
        barrier = threading.Barrier(8)
        results = [False] * 8

        def claim(index):
            barrier.wait()
            results[index] = server.claim_rewst_launch_authority(authority, f"job-{index}")

        threads = [threading.Thread(target=claim, args=(index,)) for index in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(results), 1)
        winner = f"job-{results.index(True)}"
        self.assertTrue(server.consume_rewst_authority_for_job(winner))
        self.assertFalse(server.consume_rewst_authority_for_job(winner))
        server.reset_rewst_authority_registry_for_test()
        self.assertFalse(server.consume_rewst_launch_authority(authority, winner))

    def test_secret_loader_is_distinct_strong_and_env_or_mode_0600_seat_file(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {}, clear=True):
            path = Path(td) / "rewst-hmac.key"
            with mock.patch.object(server, "REWST_ISSUER_SECRET_PATH", path):
                with self.assertRaises(RuntimeError): server.load_rewst_issuer_secret()
                for raw, mode in ((b"x" * 31, 0o600), (b"f" * 32 + b"\n", 0o644)):
                    path.write_bytes(raw); path.chmod(mode)
                    with self.assertRaises(RuntimeError): server.load_rewst_issuer_secret()
                path.chmod(0o600)
                self.assertEqual(server.load_rewst_issuer_secret(), b"f" * 32)
                with mock.patch.dict(os.environ, {"GRAPHWING_REWST_HMAC_SECRET": "e" * 32}):
                    self.assertEqual(server.load_rewst_issuer_secret(), b"e" * 32)

    def test_secret_loader_reads_one_no_follow_descriptor_across_path_substitution(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {}, clear=True):
            root = Path(td)
            secret, target = root / "rewst-hmac.key", root / "target"
            secret.write_bytes(b"f" * 32 + b"\n"); secret.chmod(0o600)
            target.write_bytes(b"z" * 64 + b"\n"); target.chmod(0o600)
            swapped, race = swapping_open(secret, root / "opened", target)
            with (
                mock.patch.object(server, "REWST_ISSUER_SECRET_PATH", secret),
                mock.patch.object(server.os, "open", side_effect=race),
            ):
                self.assertEqual(server.load_rewst_issuer_secret(), b"f" * 32)
            self.assertTrue(swapped); self.assertEqual(target.read_bytes(), b"z" * 64 + b"\n")

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
            target = home / "target"; target.write_bytes(b"z" * 32)
            (home / "rewst-hmac.key").symlink_to(target)
            with self.assertRaises(SystemExit): install.ensure_rewst_hmac_secret(home)

    def test_installer_fails_closed_on_existing_secret_path_substitution(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {}, clear=True):
            home = Path(td)
            secret, target = home / "rewst-hmac.key", home / "target"
            secret.write_bytes(b"f" * 32 + b"\n"); secret.chmod(0o600)
            target.write_bytes(b"z" * 64 + b"\n"); target.chmod(0o640)
            swapped, race = swapping_open(secret, home / "opened", target)
            with mock.patch.object(install.os, "open", side_effect=race):
                with self.assertRaises(SystemExit):
                    install.ensure_rewst_hmac_secret(home)
            self.assertTrue(swapped); self.assertEqual(target.read_bytes(), b"z" * 64 + b"\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_native_helper_has_exact_readback_hash_binding_and_issued_to_consumed_cas(self):
        graph = json.loads((ROOT / "graphs/run-control-authorize.json").read_text())
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        expected = {"load": "action.datastore.records.get",
                    "upsert": "action.datastore.records.upsert",
                    "readback": "action.datastore.records.get",
                    "issue": "action.datastore.kv.compareAndSwap",
                    "consume": "action.datastore.kv.compareAndSwap"}
        self.assertEqual({name: nodes[name]["type"] for name in expected}, expected)
        for name in ("load", "upsert", "readback"):
            self.assertEqual(nodes[name]["config"]["recordKey"], "{{ CTX.INPUT.record_key }}")
        mappings = lambda node: {item["output"]: item["expression"]
                                 for item in nodes[node]["config"]["mappings"]}
        initial, final = mappings("initial_check"), mappings("readback_check")
        self.assertEqual(initial["found_matches"]["left"]["path"], "TASKS.load.found")
        self.assertTrue(initial["found_matches"]["right"]["value"])
        self.assertEqual((initial["key_matches"]["left"]["path"],
                          initial["key_matches"]["right"]["path"]),
                         ("TASKS.load.recordKey", "CTX.INPUT.record_key"))
        self.assertEqual(final["key_matches"]["right"]["path"], "CTX.INPUT.record_key")
        self.assertEqual({rule["path"] for rule in nodes["readback_gate"]["config"]["rules"]},
                         {"found_matches", "key_matches", "version_matches", "hash_matches"})
        edges = {(e["source"], e["sourceHandle"], e["target"])
                 for e in graph["spec"]["edges"]}
        self.assertTrue({("load", "success", "initial_check"),
                         ("initial_gate", "pass", "state"),
                         ("initial_gate", "fail", "authorization_failed")} <= edges)
        self.assertNotIn(("load", "success", "state"), edges)
        issue, consume = nodes["issue"]["config"], nodes["consume"]["config"]
        self.assertEqual((issue["namespace"], issue["expectedVersion"], consume["expectedVersion"]),
                         ("graphwing_run_authorizations_v1", 0, 1))
        self.assertLessEqual(max(issue["ttlSeconds"], consume["ttlSeconds"]), 300)

    def test_openapi_has_exact_authenticated_non_launching_challenge_contract(self):
        spec = json.loads((ROOT / "openapi.json").read_text())
        operation = spec["paths"]["/v1/rewst/server-challenge"]["get"]
        self.assertEqual(operation["operationId"], "rewstServerChallenge")
        self.assertNotIn("security", operation)
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/RewstServerChallenge"},
        )
        self.assertEqual(operation["responses"]["401"],
                         {"$ref": "#/components/responses/Unauthorized"})
        self.assertEqual(spec["components"]["schemas"]["RewstServerChallenge"], {
            "type": "object",
            "additionalProperties": False,
            "required": ["challenge_version", "server_instance_challenge"],
            "properties": {
                "challenge_version": {
                    "type": "string",
                    "enum": ["graphwing-server-instance-challenge-v1"],
                },
                "server_instance_challenge": {
                    "type": "string", "pattern": "^[0-9a-f]{64}$",
                },
            },
        })
        for name in ("RewstConsumedAuthorization", "RewstLaunchDescriptor"):
            schema = spec["components"]["schemas"][name]
            self.assertIn("server_instance_challenge", schema["required"])
            self.assertEqual(schema["properties"]["server_instance_challenge"],
                             {"type": "string", "pattern": "^[0-9a-f]{64}$"})

    def test_native_helper_binds_challenge_input_into_descriptor_cas_and_output(self):
        graph = json.loads((ROOT / "graphs/run-control-authorize.json").read_text())
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        mappings = lambda node: {item["output"]: item["expression"]
                                 for item in nodes[node]["config"]["mappings"]}
        initial = mappings("initial_check")
        self.assertEqual(initial["challenge_matches"], {
            "kind": "binary", "operator": "==",
            "left": {"kind": "getField",
                     "path": "TASKS.load.data.descriptor.server_instance_challenge"},
            "right": {"kind": "getField", "path": "CTX.INPUT.server_instance_challenge"},
        })
        self.assertIn(
            {"path": "challenge_matches", "op": "equals", "value": True},
            nodes["initial_gate"]["config"]["rules"],
        )
        self.assertEqual(mappings("state")["server_instance_challenge"],
                         {"kind": "getField", "path": "CTX.INPUT.server_instance_challenge"})
        for node in ("issue", "consume"):
            self.assertEqual(nodes[node]["config"]["value"]["serverInstanceChallenge"],
                             "{{ CTX.state.server_instance_challenge }}")
        self.assertEqual(mappings("authorization")["server_instance_challenge"],
                         {"kind": "getField", "path": "CTX.state.server_instance_challenge"})

    def test_request_credential_uses_exact_challenge_bound_v2_canonical_bytes(self):
        credential = json.loads(
            (ROOT / "examples/rewst-request-hmac-credential.json").read_text()
        )
        steps = {step["output"]: step for step in credential["steps"] if "output" in step}
        self.assertEqual(steps["server_instance_challenge"], {
            "kind": "request_value",
            "path": "request.body.rewst_authorization.server_instance_challenge",
            "output": "server_instance_challenge",
        })
        self.assertEqual(steps["signed_bytes"], {
            "kind": "concat",
            "parts": [
                "graphwing-rewst-request-v2\n",
                "server_instance_challenge", "\n",
                "timestamp", "\n",
                "nonce", "\n",
                {"kind": "request", "path": "request.body"},
            ],
            "output": "signed_bytes",
        })

    def test_openapi_rewst_identifiers_have_exact_runtime_grammar_parity(self):
        spec = json.loads((ROOT / "openapi.json").read_text())
        schemas = spec["components"]["schemas"]
        identifier = schemas["RewstIdentifier"]
        ref = {"$ref": "#/components/schemas/RewstIdentifier"}
        for schema in ("RewstConsumedAuthorization", "RewstLaunchDescriptor"):
            for field in ("authorization_id", "record_key"):
                self.assertEqual(schemas[schema]["properties"][field], ref)
        self.assertEqual(schemas["RewstLaunchDescriptor"]["properties"]["resume_parent_job_id"],
                         {"oneOf": [ref, {"type": "null"}]})
        first = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        rest = first + "._:-"
        candidates = ({"", "-bad", "a..b", "a/b", "a b", "é", "a" * 129,
                       "SeCrEt", "ToKeN", "KeYsafe"} | set(first)
                      | {a + b for a in first for b in rest}
                      | {"a" + c * 127 for c in rest}
                      | {p + s for p in ("secret", "token", "key")
                         for s in ("", "safe")} | {p.upper() for p in ("secret", "token", "key")})
        pattern = re.compile(identifier["pattern"])
        for candidate in candidates:
            self.assertEqual(bool(pattern.fullmatch(candidate)),
                             server._valid_rewst_id(candidate), candidate)



if __name__ == "__main__":
    unittest.main()
