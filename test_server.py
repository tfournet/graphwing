#!/usr/bin/env python3
import fcntl
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

os.environ["GRAPHWING_HOME"] = str(Path(__file__).resolve().parent)
os.environ["GRAPHWING_HERDR"] = "0"

import server  # noqa: E402


def _fixture_execution_profile(
    launcher, provider, model, binary=None, requested="default", source="launcher_default"
):
    profile, error = server.normalize_effort(launcher, provider, model, requested, source)
    if error:
        # Adapter-unit fixtures may deliberately use an immutable sentinel model
        # outside the public AgentRun contract.
        profile = {
            "requested_effort": requested,
            "effective_effort": "high" if launcher in {"codex", "grok"} else "default",
            "effort_source": source,
        }
    version_bytes = (
        Path(binary).read_bytes()
        if binary is not None
        else f"fixture:{launcher}:{provider}:{model}".encode()
    )
    return {**profile, "launcher_version": "sha256:" + hashlib.sha256(version_bytes).hexdigest()}


def _fixture_identity_profile(profile):
    return {
        "requested_effort": profile["requested_effort"],
        "effective_effort": profile["effective_effort"],
        "effort_source": profile["effort_source"],
        "launcher_version": profile["launcher_version"],
    }


def _stamp_fixture_execution_profile(job, binary=None, requested="default", source="launcher_default"):
    job.setdefault("kind", "agent")
    job.setdefault("repo", job["session_identity"]["repo"])
    profile = _fixture_execution_profile(
        job["launcher"], job["provider"], job["model"], binary, requested, source
    )
    job.update(profile)
    job["session_identity"].update(_fixture_identity_profile(profile))
    return profile


def _minimal_agent_record(job_id, status="completed", repo="scratch"):
    profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
    identity = {
        "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
        **_fixture_identity_profile(profile), "repo": repo, "branch": "main",
        "starting_head": "a" * 40, "native_session_id": "fixture-session",
    }
    return {
        "job_id": job_id, "kind": "agent", "status": status, "repo": repo,
        "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
        **profile, "session_identity": identity, "receipt": {"status": "ok"},
    }


def _replace_persisted_job_fixture(job_id, job):
    """Model an out-of-protocol same-user file mutation in corruption tests."""
    path = server.job_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job))


def _stamp_complete_writer_evidence(job):
    """Complete intentionally minimal writer fixtures with trusted profile evidence."""
    job.setdefault("launcher", "codex")
    job.setdefault("kind", "agent")
    job.setdefault("provider", "openai")
    job.setdefault("model", "gpt-5.6-sol")
    profile = _fixture_execution_profile(job["launcher"], job["provider"], job["model"])
    job.update(profile)
    identity = job.setdefault("session_identity", {})
    identity.update({
        "launcher": job["launcher"], "provider": job["provider"], "model": job["model"],
        **_fixture_identity_profile(profile), "repo": job["repo"],
        "branch": identity["branch"], "starting_head": identity["starting_head"],
        "native_session_id": identity.get("native_session_id") or "fixture-session",
    })
    job.setdefault("receipt", {}).update({
        "status": "ok", "job_id": job["job_id"],
        "launcher": job["launcher"], "provider": job["provider"], "model": job["model"],
        **profile, "session_identity": identity,
        "failure_class": "none", "failure_code": "none", "failover_eligible": False,
    })
    return job


class DispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._scratch_td = tempfile.TemporaryDirectory()
        root = Path(cls._scratch_td.name)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "gw@test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "graphwing-test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True, capture_output=True)
        (repo / "README").write_text("hi\n")
        subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
        cls.scratch = repo

    @classmethod
    def tearDownClass(cls):
        cls._scratch_td.cleanup()

    @contextmanager
    def _native_review_context(
        self, launcher, provider, model, binary, *, prompt="ticket", diff_text="d",
        requested_effort="default", effort_source="launcher_default",
        route_execution_profile=None, job_id=None, resolved=None,
        run_budget_seconds=server.REVIEW_DEFAULT_BUDGET_SECONDS,
        max_turns=server.REVIEW_MAX_TURNS,
    ):
        """Author a complete direct-review authority for adapter-unit tests."""
        resolved = (resolved or self.scratch).resolve()
        branch, head, err = server.current_branch_head(resolved)
        self.assertIsNone(err)
        if effort_source == "route":
            profile, effort_err = server.normalize_effort(
                launcher, provider, model, requested_effort, effort_source
            )
        else:
            profile, effort_err = server.normalize_review_effort(
                launcher, provider, model, requested_effort, effort_source
            )
        self.assertIsNone(effort_err)
        self.assertIsNotNone(profile)
        assert profile is not None and branch is not None and head is not None
        pinned = server.pin_launcher(binary)
        identity = {
            "version": server.REVIEW_EXECUTION_VERSION,
            "kind": "review",
            "launcher": launcher,
            "provider": provider,
            "model": model,
            **profile,
            "launcher_version": pinned.fingerprint,
            "repo": "scratch",
            "branch": branch,
            "starting_head": head,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
            "run_budget_seconds": run_budget_seconds,
            "max_turns": max_turns,
            "permission_profile": server.REVIEW_PERMISSION_PROFILES[launcher],
        }
        if route_execution_profile is not None:
            identity["route_execution_profile"] = route_execution_profile
        try:
            context = server.NativeReviewExecutionContext(
                authority_identity=dict(identity), execution_identity=identity,
                repo="scratch", resolved=resolved, prompt=prompt, diff_text=diff_text,
                launcher=launcher, provider=provider, model=model,
                requested_effort=profile["requested_effort"],
                effective_effort=profile["effective_effort"],
                effort_source=profile["effort_source"],
                route_execution_profile=route_execution_profile,
                run_budget_seconds=run_budget_seconds, max_turns=max_turns,
                permission_profile=server.REVIEW_PERMISSION_PROFILES[launcher],
                pinned_launcher=pinned, job_id=job_id,
            )
            with mock.patch.object(
                server, "current_branch_head", return_value=(branch, head, None)
            ):
                yield context
        finally:
            pinned.close()

    def setUp(self):
        p = mock.patch.object(server, "load_repos", return_value={"scratch": str(self.scratch)})
        p.start()
        self.addCleanup(p.stop)
        runs = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        runs.close()
        self.runs_path = Path(runs.name)
        rp = mock.patch.object(server, "RUNS_PATH", self.runs_path)
        rp.start()
        self.addCleanup(rp.stop)
        self.addCleanup(lambda: self.runs_path.unlink(missing_ok=True))

    def _doorbell(self, claims=None, body=None, headers=None, authed=False, install=None):
        claims = claims or {"repository": "RewstApp/riftwing", "actor": "tfournet"}
        body = {"pr": 42} if body is None else body
        headers = {"Authorization": "Bearer oidc-token"} if headers is None else headers
        install = install or {
            "pr_drive_hook_url": "https://rewst.example/hooks/pr-drive",
            "hook_secret": "rewst-secret",
        }
        with (
            mock.patch.object(server, "verify_github_oidc", return_value=claims),
            mock.patch.object(server, "load_rewst_install", return_value=install),
        ):
            return server.dispatch(
                "POST",
                "/v1/doorbell/pr-drive",
                {},
                authed,
                json.dumps(body).encode(),
                headers,
            )

    def test_doorbell_requires_bearer_even_with_api_key_auth(self):
        with mock.patch.object(server, "verify_github_oidc") as verify:
            status, payload, _ = server.dispatch(
                "POST", "/v1/doorbell/pr-drive", {}, True, b'{"pr":42}', {}
            )
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "missing_oidc")
        verify.assert_not_called()

    def test_doorbell_riftwing_maps_and_rings_rewst(self):
        captured = {}

        class FakeResp:
            status = 202

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=20):
            captured["request"] = req
            captured["timeout"] = timeout
            return FakeResp()

        with mock.patch.object(server, "urlopen", fake_urlopen):
            status, payload, _ = self._doorbell()
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["mapped_repo"], "riftwing-drive")
        self.assertEqual(payload["rewst_status"], 202)
        self.assertEqual(captured["timeout"], 20)
        headers = {k.lower(): v for k, v in captured["request"].header_items()}
        self.assertEqual(headers["x-rewst-secret"], "rewst-secret")
        sent = json.loads(captured["request"].data)
        self.assertEqual(
            sent,
            {
                "repo": "riftwing-drive",
                "pr": 42,
                "test": "go-fmt",
                "prompt": "Doorbell: checks or review changed. If checks red, one fix slice. If mergeable, stop. Do not merge.",
                "commit_message": "pr-drive doorbell",
            },
        )

    def test_doorbell_graphwing_maps_to_graphwing(self):
        claims = {"repository": "tfournet/graphwing", "actor": "tfournet"}
        with mock.patch.object(server, "urlopen", return_value=mock.MagicMock(status=200)):
            status, payload, _ = self._doorbell(claims=claims)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["mapped_repo"], "graphwing")

    def test_doorbell_rejects_bad_actor_without_post(self):
        claims = {"repository": "RewstApp/riftwing", "actor": "mallory"}
        with mock.patch.object(server, "urlopen") as post:
            status, payload, _ = self._doorbell(claims=claims)
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "actor_not_allowed")
        post.assert_not_called()

    def test_doorbell_rejects_unknown_repo(self):
        claims = {"repository": "tfournet/other", "actor": "tfournet"}
        with mock.patch.object(server, "urlopen") as post:
            status, payload, _ = self._doorbell(claims=claims)
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "repo_not_allowed")
        post.assert_not_called()

    def test_doorbell_missing_pr_is_skipped(self):
        with mock.patch.object(server, "urlopen") as post:
            status, payload, _ = self._doorbell(body={})
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "skipped": True, "reason": "missing_pr"})
        post.assert_not_called()

    def test_doorbell_body_repo_cannot_override_oidc_repo(self):
        with mock.patch.object(server, "urlopen", return_value=mock.MagicMock(status=204)) as post:
            status, payload, _ = self._doorbell(body={"pr": 7, "repo": "tfournet/graphwing"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["mapped_repo"], "riftwing-drive")
        sent = json.loads(post.call_args.args[0].data)
        self.assertEqual(sent["repo"], "riftwing-drive")

    def test_doorbell_missing_hook_is_unavailable(self):
        with mock.patch.object(server, "urlopen") as post:
            status, payload, _ = self._doorbell(install={"org_id": "unused"})
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "hook_unconfigured")
        post.assert_not_called()

    def test_doorbell_legacy_pr_drive_hook_secret_key(self):
        with mock.patch.object(server, "urlopen", return_value=mock.MagicMock(status=200)) as post:
            status, payload, _ = self._doorbell(
                install={
                    "pr_drive_hook_url": "https://rewst.example/hooks/pr-drive",
                    "pr_drive_hook_secret": "legacy-secret",
                }
            )
        self.assertEqual(status, 200, payload)
        headers = {k.lower(): v for k, v in post.call_args.args[0].header_items()}
        self.assertEqual(headers["x-rewst-secret"], "legacy-secret")

    def test_health_no_auth(self):
        status, payload, _ = server.dispatch("GET", "/v1/health", {}, False, b"")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIsInstance(payload["repos"], list)

    def test_git_requires_key(self):
        status, payload, _ = server.dispatch("GET", "/v1/git/status", {}, False, b"")
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "unauthorized")

    def test_unknown_repo(self):
        status, payload, _ = server.dispatch(
            "GET", "/v1/git/status", {"repo": ["nope"]}, True, b""
        )
        self.assertEqual(status, 400)
        self.assertIn(payload["code"], ("unknown_repo", "no_repos"))

    def test_status_scratch_repo(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "GET", "/v1/git/status", {"repo": ["scratch"]}, True, b""
                )
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["repo"], "scratch")
            self.assertIn("branch", payload)

    def test_log_scratch_repo(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "GET", "/v1/git/log", {"repo": ["scratch"], "n": ["2"]}, True, b""
                )
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["commits"])
            self.assertIn("sha", payload["commits"][0])

    def test_branch_default_no_repos(self):
        with mock.patch.object(server, "load_repos", return_value={}):
            status, payload, _ = server.dispatch("GET", "/v1/git/branch", {}, True, b"")
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "no_repos")

    def test_show_requires_rev(self):
        status, payload, _ = server.dispatch("GET", "/v1/git/show", {}, True, b"")
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "missing_rev")

    def test_show_head(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "GET", "/v1/git/show", {"repo": ["scratch"], "rev": ["HEAD"]}, True, b""
                )
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["show"])

    def test_agent_profiles_endpoint_is_removed(self):
        status, payload, _ = server.dispatch("GET", "/v1/agent/profiles", {}, True, b"")
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["code"], "not_found")

    def test_agent_run_requires_auth(self):
        status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, False, b'{"prompt":"x"}')
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "unauthorized")

    def test_agent_run_missing_prompt(self):
        status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, b"{}")
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "missing_prompt")

    def test_agent_run_requires_explicit_native_identity(self):
        required = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
        }
        for missing in required:
            body = {"prompt": "x", **required}
            del body[missing]
            status, payload, _ = server.dispatch(
                "POST", "/v1/agent/run", {}, True, json.dumps(body).encode()
            )
            self.assertEqual(status, 400, missing)
            self.assertEqual(payload["code"], f"missing_{missing}", missing)

    def test_agent_run_rejects_unknown_fields(self):
        body = {
            "prompt": "x", "launcher": "codex", "provider": "openai",
            "model": "gpt-5.6-sol", "seat": "legacy",
        }
        status, payload, _ = server.dispatch(
            "POST", "/v1/agent/run", {}, True, json.dumps(body).encode()
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "unexpected_fields")

    def test_agent_run_accepts_and_persists_effort(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            binary = Path(td) / "codex"
            binary.write_text("codex fixture")
            body = json.dumps({
                "prompt": "bounded fixture", "cwd": "scratch", "launcher": "codex",
                "provider": "openai", "model": "gpt-5.6-sol", "effort": "high",
            }).encode()
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=binary), \
                 mock.patch.object(server, "enqueue_agent", lambda job: None):
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
                job = server.read_job(payload.get("job_id", "")) if status == 202 else None
        self.assertEqual(status, 202, payload)
        self.assertEqual(
            (job["requested_effort"], job["effective_effort"], job["effort_source"]),
            ("high", "high", "explicit"),
        )
        self.assertEqual(
            tuple(job["session_identity"][key] for key in ("effective_effort",)),
            ("high",),
        )

    def test_agent_run_rejects_unknown_effort(self):
        body = json.dumps({
            "prompt": "bounded fixture", "cwd": "scratch", "launcher": "codex",
            "provider": "openai", "model": "gpt-5.6-sol", "effort": "extreme",
        }).encode()
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "enqueue_agent", side_effect=AssertionError("provider execution forbidden")):
            status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "bad_effort")

    def test_agent_run_rejects_unsupported_effort(self):
        body = json.dumps({
            "prompt": "bounded fixture", "cwd": "scratch", "launcher": "grok",
            "provider": "xai", "model": "grok-4.6", "effort": "max",
        }).encode()
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "enqueue_agent", side_effect=AssertionError("provider execution forbidden")):
            status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "unsupported_effort")

    def test_agent_run_omitted_effort_uses_explicit_launcher_default_profile(self):
        cases = (
            ("codex", "openai", "gpt-5.6-sol", "high"),
            ("claude", "anthropic", "claude-opus-5", "default"),
            ("grok", "xai", "grok-4.6", "high"),
        )
        for launcher, provider, model, effective in cases:
            with self.subTest(launcher=launcher), tempfile.TemporaryDirectory() as td:
                jobs = Path(td) / "jobs"
                binary = Path(td) / launcher
                binary.write_text(f"{launcher} fixture")
                body = json.dumps({
                    "prompt": "bounded fixture", "cwd": "scratch", "launcher": launcher,
                    "provider": provider, "model": model,
                }).encode()
                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.object(server, "resolve_launcher_binary_now", return_value=binary), \
                     mock.patch.object(server, "enqueue_agent", lambda job: None):
                    status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
                    job = server.read_job(payload.get("job_id", "")) if status == 202 else None
                self.assertEqual(status, 202, payload)
                self.assertEqual(
                    (job["requested_effort"], job["effective_effort"], job["effort_source"]),
                    ("default", effective, "launcher_default"),
                )
                self.assertEqual(job["session_identity"]["effective_effort"], effective)

        profile, error = server.normalize_effort(
            "codex", "openai", "gpt-5.6-sol", "high", "route"
        )
        self.assertIsNone(error)
        self.assertEqual(profile["effort_source"], "route")

    def test_resume_rejects_effort_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "codex"
            binary.write_text("codex fixture")
            version = "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest()
            branch = subprocess.run(
                ["git", "-C", str(self.scratch), "rev-parse", "--abbrev-ref", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            head = subprocess.run(
                ["git", "-C", str(self.scratch), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "requested_effort": "default", "effective_effort": "high",
                "effort_source": "launcher_default", "launcher_version": version,
                "repo": "scratch", "branch": branch, "starting_head": head,
                "native_session_id": "codex-fixture",
            }
            parsed, parse_error = server.parse_session_identity(identity)
            self.assertIsNone(parse_error)
            self.assertEqual(parsed, identity)
            identities = (
                identity,
                {**identity, "requested_effort": "high", "effort_source": "explicit",
                 "launcher_version": "sha256:" + "f" * 64},
            )
            with mock.patch.object(server, "resolve_launcher_binary_now", return_value=binary), \
                 mock.patch.object(server, "enqueue_agent", side_effect=AssertionError("provider execution forbidden")):
                for mismatched_identity in identities:
                    body = json.dumps({
                        "prompt": "continue", "cwd": "scratch", "launcher": "codex",
                        "provider": "openai", "model": "gpt-5.6-sol", "effort": "high",
                        "session_identity": mismatched_identity, "resume_job_id": "ab" * 16,
                    }).encode()
                    status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["code"], "session_identity_mismatch")

    def test_agent_run_binds_launcher_version_without_executing_binary(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            binary = Path(td) / "codex"
            binary.write_bytes(b"phase-1 launcher fixture")
            expected = "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest()
            body = json.dumps({
                "prompt": "bounded fixture", "cwd": "scratch", "launcher": "codex",
                "provider": "openai", "model": "gpt-5.6-sol",
            }).encode()
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=binary), \
                 mock.patch.object(server.subprocess, "run", wraps=server.subprocess.run) as run, \
                 mock.patch.object(server, "enqueue_agent", lambda job: None):
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
                job = server.read_job(payload.get("job_id", "")) if status == 202 else None
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["session_identity"]["launcher_version"], expected)
        self.assertEqual(job["launcher_version"], expected)
        self.assertFalse(any(str(binary) in call.args[0] for call in run.call_args_list))

    def test_agent_run_path_cwd_rejected(self):
        status, payload, _ = server.dispatch(
            "POST",
            "/v1/agent/run",
            {},
            True,
            b'{"prompt":"x","cwd":"/tmp/graphwing-not-a-repo"}',
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "bad_cwd")

    def test_agent_run_omitted_cwd_uses_server_default_repo_policy(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(server, "JOBS_DIR", Path(td)), \
             mock.patch.object(server, "load_repos", return_value={"other": str(self.scratch), "scratch": str(self.scratch)}), \
             mock.patch.dict(os.environ, {"GRAPHWING_DEFAULT_REPO": "scratch"}), \
             mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "enqueue_agent"):
            status, payload, _ = server.dispatch(
                "POST", "/v1/agent/run", {}, True,
                b'{"prompt":"x","launcher":"codex","provider":"openai","model":"gpt-5.6-sol"}',
            )
            job = server.read_job(payload["job_id"])
        self.assertEqual(status, 202, payload)
        self.assertEqual((job["repo"], job["cwd"]), ("scratch", str(self.scratch)))

    def test_agent_run_bad_webhook_url(self):
        status, payload, _ = server.dispatch(
            "POST",
            "/v1/agent/run",
            {},
            True,
            b'{"prompt":"x","response_webhook_url":"http://127.0.0.1/x"}',
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "bad_webhook_url")

    def test_agent_run_resume_url_alias_rejected_if_http(self):
        status, payload, _ = server.dispatch(
            "POST",
            "/v1/agent/run",
            {},
            True,
            b'{"prompt":"x","resume_url":"http://127.0.0.1/x"}',
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "bad_webhook_url")

    def test_agent_run_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            jobs = Path(td) / "jobs"
            codex = Path(td) / "codex"
            codex.write_text("fixture")
            with mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                with mock.patch.object(server, "JOBS_DIR", jobs):
                    with mock.patch.object(server, "enqueue_agent", lambda job: None):
                        status, payload, _ = server.dispatch(
                            "POST",
                            "/v1/agent/run",
                            {},
                            True,
                            b'{"prompt":"ping","cwd":"scratch","launcher":"codex","provider":"openai","model":"gpt-5.6-sol"}',
                        )
            self.assertEqual(status, 202, payload)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "queued")
            self.assertEqual(payload["repo"], "scratch")
            job_id = payload["job_id"]
            self.assertRegex(job_id, r"^[0-9a-f]{32}$")
            self.assertEqual(payload["poll"], f"/v1/agent/jobs/{job_id}")
            with mock.patch.object(server, "JOBS_DIR", jobs):
                gstatus, gp, _ = server.dispatch(
                    "GET", f"/v1/agent/jobs/{job_id}", {}, True, b""
                )
            self.assertEqual(gstatus, 200, gp)
            self.assertEqual(gp["status"], "queued")
            self.assertNotIn("prompt", gp)
            self.assertNotIn("cwd", gp)
            self.assertNotIn("log_ref", gp)
            self.assertNotIn("response_webhook_token", gp)
            self.assertNotIn("resume_url", gp)

    def test_agent_run_preflight_uses_request_time_launcher_resolver(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            current = root / "current-codex"
            current.write_text("fixture")
            body = json.dumps({
                "prompt": "ping", "cwd": "scratch", "launcher": "codex",
                "provider": "openai", "model": "gpt-5.6-sol",
            }).encode()
            with mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(current)}), \
                 mock.patch.object(server, "resolve_launcher_binary_now", wraps=server.resolve_launcher_binary_now) as resolve_now, \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "enqueue_agent", lambda job: None), \
                 mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")):
                status, payload, _ = server.dispatch(
                    "POST", "/v1/agent/run", {}, True, body
                )
        self.assertEqual(status, 202, payload)
        resolve_now.assert_called_once_with("codex")

    def test_run_agent_job_launches_the_binary_from_the_shared_current_resolver(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs"
            current = root / "current-codex"
            current.write_text("fixture")
            job_id = "ab" * 16
            job = {
                "job_id": job_id,
                "status": "queued",
                "cwd": str(root),
                "prompt": "ping",
                "launcher": "codex",
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "max_turns": 2,
                "run_budget_seconds": 30,
                "session_identity": {
                    "launcher": "codex", "provider": "openai",
                    "model": "gpt-5.6-sol", "repo": "scratch",
                    "branch": "main", "starting_head": "a" * 40,
                    "native_session_id": None,
                },
                "response_webhook_url": None,
                "response_webhook_token": None,
                "resume_url": None,
                "created_at": "2026-08-26T12:00:00Z",
                "started_at": None,
                "finished_at": None,
                "receipt": None,
                "log_ref": str(jobs / job_id / "stdout.log"),
                "error": None,
                "webhook": None,
            }
            _stamp_fixture_execution_profile(job, current)
            with mock.patch.object(server, "JOBS_DIR", jobs):
                server.job_dir(job_id).mkdir(parents=True)
                server.job_dir(job_id).joinpath("prompt.txt").write_text("ping")
                server.write_job(job)
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(current)}), \
                 mock.patch.object(server, "resolve_launcher_binary_now", wraps=server.resolve_launcher_binary_now) as resolve_now, \
                 mock.patch.object(
                     server, "spawn_writer",
                     return_value=(None, {"error": "fixture stop", "code": "spawn_failed"}),
                 ) as spawn, \
                 mock.patch.object(server, "post_receipt", side_effect=AssertionError("network forbidden")), \
                 mock.patch.object(server, "herdr_job_done", lambda job: None):
                server.run_agent_job(job_id)
        resolve_now.assert_called_once_with("codex")
        spawn.assert_called_once()
        self.assertRegex(str(spawn.call_args.args[1]), r"^/proc/self/fd/[0-9]+$")

    def test_request_time_launcher_resolver_delegates_to_shared_current_resolver(self):
        cases = {
            "codex": ("GRAPHWING_CODEX_BIN", Path.home() / ".local" / "bin" / "codex"),
            "claude": ("GRAPHWING_CLAUDE_BIN", Path.home() / ".local" / "bin" / "claude"),
            "grok": ("GRAPHWING_GROK_BIN", Path.home() / ".local" / "bin" / "grok"),
        }
        for launcher, (env_var, fallback) in cases.items():
            with self.subTest(launcher=launcher), \
                 mock.patch.object(
                     server, "resolve_executable", return_value=Path(f"/current/{launcher}")
                 ) as shared:
                self.assertEqual(
                    server.resolve_launcher_binary_now(launcher),
                    Path(f"/current/{launcher}"),
                )
                shared.assert_called_once_with(launcher, env_var, fallback)

    def test_request_time_launcher_resolver_uses_env_then_current_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            configured = root / "configured-codex"
            configured.write_text("configured")
            current_path = root / "path-codex"
            current_path.write_text("path")
            with mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": ""}), \
                 mock.patch.object(server.shutil, "which", return_value=str(current_path)):
                self.assertEqual(server.resolve_launcher_binary_now("codex"), current_path)
            with mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(configured)}), \
                 mock.patch.object(server.shutil, "which", return_value="/provider/cli/must-not-run"):
                self.assertEqual(server.resolve_launcher_binary_now("codex"), configured)

    def test_shared_resolver_prefers_mise_selected_native_binary(self):
        with tempfile.TemporaryDirectory() as td:
            native = Path(td) / "native-gh"
            native.write_text("fixture")
            native.chmod(0o755)
            selected = subprocess.CompletedProcess(
                ["/usr/bin/mise", "which", "gh"], 0, stdout=f"{native}\n", stderr=""
            )
            with mock.patch.dict(os.environ, {"GRAPHWING_GH_BIN": ""}), mock.patch.object(
                server.shutil,
                "which",
                side_effect=lambda name: "/usr/bin/mise" if name == "mise" else "/shim/gh",
            ), mock.patch.object(server.subprocess, "run", return_value=selected):
                self.assertEqual(
                    server.resolve_executable("gh", "GRAPHWING_GH_BIN", Path("/fallback/gh")),
                    native,
                )

    def test_native_reviews_use_request_time_launcher_resolution(self):
        class FakeCompleted:
            returncode = 0
            stdout = b"VERDICT: PASS\n"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs"
            codex = root / "current-codex"
            codex.write_text("fixture")
            captured = []

            def fake_run(command, **kwargs):
                captured.append((command, kwargs))
                output_index = command.index("--output-last-message") + 1
                Path(command[output_index]).write_text(
                    '{"status":"ok","sha":null,"pr_url":null,"summary":"VERDICT: PASS"}'
                )
                return FakeCompleted()

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}):
                binary = server.resolve_launcher_binary_now("codex")
                with self._native_review_context(
                    "codex", "openai", "gpt-5.6-sol", binary
                ) as context, mock.patch.object(server.subprocess, "run", fake_run):
                    result = server.native_review_result(context)
        self.assertTrue(result["ok"], result)
        command, kwargs = captured[0]
        self.assertEqual(Path(command[0]).parent, Path("/proc/self/fd"))
        self.assertEqual(kwargs["pass_fds"], (int(Path(command[0]).name),))

    def test_codex_agent_run_records_git_bound_session_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            codex = root / "codex"
            codex.write_text("fixture")
            body = json.dumps({
                "prompt": "ping", "cwd": "scratch", "launcher": "codex",
                "provider": "openai", "model": "gpt-5.6-sol",
            }).encode()
            with mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "enqueue_agent", lambda job: None):
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
            self.assertEqual(status, 202, payload)
            identity = payload["session_identity"]
            self.assertEqual(identity["launcher"], "codex")
            self.assertEqual(identity["provider"], "openai")
            self.assertEqual(identity["model"], "gpt-5.6-sol")
            self.assertEqual(identity["repo"], "scratch")
            self.assertIsNone(identity["native_session_id"])

    def test_claude_agent_run_records_git_bound_session_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            claude = root / "claude"
            claude.write_text("fixture")
            with mock.patch.dict(os.environ, {"GRAPHWING_CLAUDE_BIN": str(claude)}), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "enqueue_agent", lambda job: None):
                for model in ("claude-opus-5", "claude-sonnet-5"):
                    body = json.dumps({
                        "prompt": "ping", "cwd": "scratch", "launcher": "claude",
                        "provider": "anthropic", "model": model,
                    }).encode()
                    status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
                    self.assertEqual(status, 202, payload)
                    self.assertEqual(payload["session_identity"]["launcher"], "claude")
                    self.assertEqual(payload["session_identity"]["provider"], "anthropic")
                    self.assertEqual(payload["session_identity"]["model"], model)
                    self.assertEqual(payload["session_identity"]["repo"], "scratch")
                    self.assertIsNone(payload["session_identity"]["native_session_id"])
                    stored = server.read_job(payload["job_id"])
                    stored["status"] = "completed"
                    server.write_job(stored)
                for provider, model in (
                    ("openai", "claude-opus-5"),
                    ("anthropic", "gpt-5.6-sol"),
                    ("anthropic", "claude-unknown"),
                ):
                    body = json.dumps({
                        "prompt": "ping", "cwd": "scratch", "launcher": "claude",
                        "provider": provider, "model": model,
                    }).encode()
                    status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["code"], "bad_model_identity")

    def test_grok_agent_run_records_exact_git_bound_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            grok = root / "grok"
            grok.write_text("fixture")
            expected_profile = _fixture_execution_profile("grok", "xai", "grok-4.6", grok)
            branch = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            body = json.dumps({
                "prompt": "ping", "cwd": "scratch", "launcher": "grok",
                "provider": "xai", "model": "grok-4.6",
            }).encode()
            with mock.patch.dict(os.environ, {"GRAPHWING_GROK_BIN": str(grok)}), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "enqueue_agent", lambda job: None):
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["launcher"], "grok")
        self.assertEqual(payload["provider"], "xai")
        self.assertEqual(payload["model"], "grok-4.6")
        self.assertEqual(payload["session_identity"], {
            "launcher": "grok", "provider": "xai", "model": "grok-4.6",
            **_fixture_identity_profile(expected_profile),
            "repo": "scratch", "branch": branch, "starting_head": head,
            "native_session_id": None,
        })

    def test_grok_resume_rejects_every_identity_mismatch_dimension(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            grok = root / "grok"
            grok.write_text("fixture")
            branch = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            profile = _fixture_execution_profile("grok", "xai", "grok-4.6", grok)
            identity = {
                "launcher": "grok", "provider": "xai", "model": "grok-4.6",
                **_fixture_identity_profile(profile),
                "repo": "scratch", "branch": branch, "starting_head": head,
                "native_session_id": "grok-123",
            }
            prior_job_id = "ab" * 16
            prior_dir = jobs / prior_job_id
            prior_dir.mkdir(parents=True)
            prior = {
                "job_id": prior_job_id, "kind": "agent", "status": "completed", "launcher": "grok",
                "provider": "xai", "model": "grok-4.6",
                "repo": "scratch", "cwd": str(repo), **profile, "session_identity": identity,
                "receipt": {"status": "ok", "job_id": prior_job_id, "launcher": "grok",
                            "provider": "xai", "model": "grok-4.6",
                            "session_identity": identity, **profile, "summary": "done"},
            }
            (prior_dir / "job.json").write_text(json.dumps(prior))
            replacements = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "repo": "other", "branch": "other", "starting_head": "f" * 40,
            }
            base = {
                "prompt": "continue", "cwd": "scratch", "launcher": "grok",
                "provider": "xai", "model": "grok-4.6", "resume_job_id": prior_job_id,
            }
            with mock.patch.dict(os.environ, {"GRAPHWING_GROK_BIN": str(grok)}), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "enqueue_agent", lambda job: None):
                for key, value in replacements.items():
                    body = json.dumps({**base, "session_identity": dict(identity, **{key: value})}).encode()
                    status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
                    self.assertEqual(status, 400, key)
                    self.assertEqual(
                        payload["code"],
                        "bad_session_identity" if key in {"launcher", "provider", "model"} else "session_identity_mismatch",
                        key,
                    )
                valid = json.dumps({**base, "session_identity": identity}).encode()
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, valid)
                self.assertEqual(status, 202, payload)
                self.assertEqual(payload["session_identity"], identity)
                changed_session = json.dumps({
                    **base, "session_identity": dict(identity, native_session_id="grok-other"),
                }).encode()
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, changed_session)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "untraceable_resume_session")
                bad_priors = [
                    ("failed job", dict(prior, status="failed")),
                    ("wrong job launcher", dict(prior, launcher="codex")),
                    ("error receipt", dict(prior, receipt=dict(prior["receipt"], status="error"))),
                    ("timeout receipt", dict(prior, receipt=dict(prior["receipt"], status="timeout"))),
                    ("wrong stored native session", dict(
                        prior, session_identity=dict(identity, native_session_id="grok-other"),
                    )),
                    ("wrong receipt native session", dict(
                        prior, receipt=dict(prior["receipt"], session_identity=dict(
                            identity, native_session_id="grok-other",
                        )),
                    )),
                ]
                bad_priors.extend(
                    (f"stored {key}", dict(
                        prior, session_identity=dict(identity, **{key: value}),
                    ))
                    for key, value in replacements.items()
                )
                for name, bad_prior in bad_priors:
                    (prior_dir / "job.json").write_text(json.dumps(bad_prior))
                    status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, valid)
                    self.assertEqual(status, 400, name)
                    self.assertEqual(payload["code"], "untraceable_resume_session", name)

                for provider, model in (("openai", "grok-4.6"), ("xai", "grok-other")):
                    body = json.dumps({
                        "prompt": "x", "cwd": "scratch", "launcher": "grok",
                        "provider": provider, "model": model,
                    }).encode()
                    status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
                    self.assertEqual(status, 400, (provider, model))
                    self.assertEqual(payload["code"], "bad_model_identity")

    def test_codex_resume_requires_graphwing_recorded_successful_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            codex = root / "codex"
            codex.write_text("fixture")
            branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol", codex)
            identity = {"launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", **_fixture_identity_profile(profile), "repo": "scratch", "branch": branch, "starting_head": head, "native_session_id": "codex-123"}
            prior_job_id = "ab" * 16
            prior_dir = jobs / prior_job_id
            prior_dir.mkdir(parents=True)
            prior = {
                "job_id": prior_job_id, "kind": "agent", "status": "completed", "launcher": "codex",
                "provider": "openai", "model": "gpt-5.6-sol",
                "repo": "scratch", "cwd": str(repo), **profile, "session_identity": identity,
                "receipt": {"status": "ok", "job_id": prior_job_id, "launcher": "codex",
                            "provider": "openai", "model": "gpt-5.6-sol",
                            "session_identity": identity, **profile, "summary": "done"},
            }
            (prior_dir / "job.json").write_text(json.dumps(prior))
            bad_identity_job = dict(prior, job_id="cd" * 16, receipt={"status": "ok", "session_identity": dict(identity, native_session_id="codex-999")})
            (jobs / bad_identity_job["job_id"]).mkdir(parents=True)
            (jobs / bad_identity_job["job_id"] / "job.json").write_text(json.dumps(bad_identity_job))
            failed_job = dict(prior, job_id="ef" * 16, status="failed", receipt={"status": "error", "session_identity": identity})
            (jobs / failed_job["job_id"]).mkdir(parents=True)
            (jobs / failed_job["job_id"] / "job.json").write_text(json.dumps(failed_job))
            base = {"prompt": "ping", "cwd": "scratch", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol"}
            with mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "enqueue_agent", lambda job: None):
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, json.dumps({**base, "session_identity": dict(identity, native_session_id=None)}).encode())
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "missing_native_session")
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, json.dumps({**base, "session_identity": identity}).encode())
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "missing_resume_job_id")
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, json.dumps({**base, "session_identity": identity, "resume_job_id": "not-a-job"}).encode())
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "bad_resume_job_id")
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, json.dumps({**base, "session_identity": identity, "resume_job_id": "11" * 16}).encode())
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "untraceable_resume_session")
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, json.dumps({**base, "session_identity": identity, "resume_job_id": failed_job["job_id"]}).encode())
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "untraceable_resume_session")
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, json.dumps({**base, "session_identity": identity, "resume_job_id": bad_identity_job["job_id"]}).encode())
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "untraceable_resume_session")
                valid_resume = json.dumps({**base, "session_identity": identity, "resume_job_id": prior_job_id}).encode()
                for mismatch in ({"repo": "other"}, {"cwd": str(root / "other-checkout")}):
                    (prior_dir / "job.json").write_text(json.dumps({**prior, **mismatch}))
                    status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, valid_resume)
                    self.assertEqual(status, 400, mismatch)
                    self.assertEqual(
                        payload["code"],
                        "untraceable_resume_session" if "repo" in mismatch else "resume_repository_mismatch",
                        mismatch,
                    )
                (prior_dir / "job.json").write_text(json.dumps(prior))
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, valid_resume)
            self.assertEqual(status, 202, payload)
            self.assertEqual(payload["session_identity"], identity)

    def test_claude_resume_requires_matching_successful_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            claude = root / "claude"
            claude.write_text("fixture")
            branch = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            profile = _fixture_execution_profile("claude", "anthropic", "claude-opus-5", claude)
            identity = {
                "launcher": "claude", "provider": "anthropic", "model": "claude-opus-5",
                **_fixture_identity_profile(profile),
                "repo": "scratch", "branch": branch, "starting_head": head,
                "native_session_id": "claude-123",
            }
            prior_job_id = "ab" * 16
            prior_dir = jobs / prior_job_id
            prior_dir.mkdir(parents=True)
            prior = {
                "job_id": prior_job_id, "kind": "agent", "status": "completed", "launcher": "claude",
                "provider": "anthropic", "model": "claude-opus-5",
                "repo": "scratch", "cwd": str(repo), **profile, "session_identity": identity,
                "receipt": {"status": "ok", "job_id": prior_job_id, "launcher": "claude",
                            "provider": "anthropic", "model": "claude-opus-5",
                            "session_identity": identity, **profile, "summary": "done"},
            }
            (prior_dir / "job.json").write_text(json.dumps(prior))
            body = json.dumps({
                "prompt": "continue", "cwd": "scratch", "launcher": "claude",
                "provider": "anthropic", "model": "claude-opus-5",
                "session_identity": identity, "resume_job_id": prior_job_id,
            }).encode()
            with mock.patch.dict(os.environ, {"GRAPHWING_CLAUDE_BIN": str(claude)}), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "enqueue_agent", lambda job: None):
                prior["session_identity"] = dict(identity, native_session_id="claude-other")
                (prior_dir / "job.json").write_text(json.dumps(prior))
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "untraceable_resume_session")
                prior["session_identity"] = identity
                (prior_dir / "job.json").write_text(json.dumps(prior))
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
            self.assertEqual(status, 202, payload)
            self.assertEqual(payload["session_identity"], identity)

    def test_codex_resume_rejects_stale_git_identity_with_valid_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            codex = root / "codex"
            codex.write_text("fixture")
            branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            old_head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol", codex)
            identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                **_fixture_identity_profile(profile),
                "repo": "scratch", "branch": branch, "starting_head": old_head,
                "native_session_id": "codex-123",
            }
            prior_job_id = "ab" * 16
            prior_dir = jobs / prior_job_id
            prior_dir.mkdir(parents=True)
            prior = {
                "job_id": prior_job_id, "kind": "agent", "status": "completed", "launcher": "codex",
                "provider": "openai", "model": "gpt-5.6-sol",
                "repo": "scratch", "cwd": str(repo), **profile, "session_identity": identity,
                "receipt": {"status": "ok", "job_id": prior_job_id, "launcher": "codex",
                            "provider": "openai", "model": "gpt-5.6-sol",
                            "session_identity": identity, **profile, "summary": "done"},
            }
            (prior_dir / "job.json").write_text(json.dumps(prior))
            (repo / "CHANGE").write_text("new head\n")
            subprocess.run(["git", "-C", str(repo), "add", "CHANGE"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "change"], check=True, capture_output=True)
            body = json.dumps({
                "prompt": "ping", "cwd": "scratch", "launcher": "codex",
                "provider": "openai", "model": "gpt-5.6-sol",
                "session_identity": identity, "resume_job_id": prior_job_id,
            }).encode()
            with mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "enqueue_agent", lambda job: None):
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "session_identity_mismatch")

    def test_compact_cmd_signal_shortens_failure(self):
        noise = "\n".join(f"ok line {i}" for i in range(80))
        result = {
            "ok": False,
            "stdout": noise + "\nFAIL: test_foo\nAssertionError: no\n" + noise,
            "stderr": "",
        }
        compact = server.compact_cmd_signal(result)
        self.assertIn("FAIL: test_foo", compact)
        self.assertLessEqual(len(compact), server.COMPACT_MAX_CHARS)
        self.assertEqual(server.compact_cmd_signal({"ok": True, "stdout": "lots"}), "ok")

    def test_agent_job_public_projection_is_exact_and_sanitized_for_every_state(self):
        schemas = json.loads(server.openapi_bytes())["components"]["schemas"]
        job_schema = schemas["AgentJob"]
        receipt_schema = schemas["AgentReceipt"]
        profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "b" * 40, "native_session_id": None,
        }
        expected = {
            "ok", "kind", "job_id", "status", "repo", "launcher", "provider", "model",
            "requested_effort", "effective_effort", "effort_source", "launcher_version",
            "session_identity", "created_at", "started_at", "finished_at", "receipt",
        }
        hostile = "PROMPT_FRAGMENT /home/private TOKEN_SECRET https://private.example provider-output"
        for state in ("queued", "running", "completed", "failed"):
            with self.subTest(state=state):
                state_identity = dict(identity)
                if state == "completed":
                    state_identity["native_session_id"] = "codex-fixture"
                job = {
                    "job_id": "ab" * 16, "kind": "agent", "status": state, "repo": "scratch", "cwd": "/home/private",
                    "prompt": hostile, "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                    **profile, "session_identity": state_identity, "created_at": "created",
                    "started_at": None, "finished_at": None, "receipt": None,
                    "log_ref": "/home/private/stdout.log", "error": hostile,
                    "webhook": {"error": hostile}, "response_webhook_url": "https://private.example",
                    "response_webhook_token": "TOKEN_SECRET",
                }
                if state == "completed":
                    job["receipt"] = server.normalize_receipt(
                        job, {"status": "ok", "summary": hostile}, 0, False,
                    )
                elif state == "failed":
                    job["receipt"] = server.normalize_receipt(
                        job, {"status": "error", "summary": hostile}, 1, False,
                    )
                public = server.public_job(job)
                self.assertEqual(set(public), expected)
                self.assertEqual(set(public), set(job_schema["required"]))
                self.assertEqual(set(public), set(job_schema["properties"]))
                if public["receipt"] is not None:
                    self.assertEqual(set(public["receipt"]), set(receipt_schema["required"]))
                    self.assertEqual(set(public["receipt"]), set(receipt_schema["properties"]) - {"role", "execution_identity"})
                dumped = json.dumps(public, sort_keys=True)
                for fragment in ("PROMPT_FRAGMENT", "/home/private", "TOKEN_SECRET", "private.example", "provider-output"):
                    self.assertNotIn(fragment, dumped)
                self.assertEqual(
                    {key: public[key] for key in ("requested_effort", "effective_effort", "effort_source", "launcher_version")},
                    profile,
                )
        incomplete = deepcopy(job)
        incomplete["receipt"].pop("launcher_version")
        fail_closed = server.public_job(incomplete)["receipt"]
        self.assertEqual(fail_closed["failure_code"], "session_provenance_invalid")
        self.assertTrue(all(fail_closed[key] is None for key in server.AGENT_PROFILE_FIELDS))

    def test_interrupted_agent_receipt_is_sanitized_and_fails_closed_without_profile(self):
        hostile = "PROMPT_FRAGMENT /home/private TOKEN_SECRET https://private.example provider-output"
        legacy = {
            "job_id": "ab" * 16, "launcher": "codex", "provider": "openai",
            "model": "gpt-5.6-sol", "session_identity": {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "repo": "scratch", "branch": "main", "starting_head": "b" * 40,
                "native_session_id": "native-old",
            },
            "log_ref": "/home/private/stdout.log", "error": hostile,
        }
        receipt = server.interrupted_job_receipt(legacy, "agent")
        receipt_schema = json.loads(server.openapi_bytes())["components"]["schemas"]["AgentReceipt"]
        self.assertEqual(set(receipt), set(receipt_schema["required"]))
        self.assertEqual(receipt["failure_code"], "session_provenance_invalid")
        self.assertIsNone(receipt["session_identity"])
        for key in ("launcher", "provider", "model", "requested_effort", "effective_effort", "effort_source", "launcher_version"):
            self.assertIn(key, receipt)
            self.assertIsNone(receipt[key])
        dumped = json.dumps(receipt, sort_keys=True)
        for fragment in ("PROMPT_FRAGMENT", "/home/private", "TOKEN_SECRET", "private.example", "provider-output"):
            self.assertNotIn(fragment, dumped)
        self.assertNotIn("log_ref", receipt)
        self.assertNotIn("sha", receipt)
        self.assertNotIn("pr_url", receipt)

    def test_agent_job_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(server, "JOBS_DIR", Path(td) / "jobs"):
                status, payload, _ = server.dispatch(
                    "GET",
                    "/v1/agent/jobs/" + ("ab" * 16),
                    {},
                    True,
                    b"",
                )
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "not_found")

    def test_agent_job_bad_id(self):
        status, payload, _ = server.dispatch("GET", "/v1/agent/jobs/not-a-job", {}, True, b"")
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "bad_job_id")

    def test_agent_job_endpoint_rejects_adversarial_non_agent_records(self):
        hostile = {
            "log_ref": "/home/private/log", "path": "/home/private/credential",
            "token": "TOKEN_SECRET", "callback": "https://private.example/callback",
            "error": "raw provider error",
        }
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            for index, kind in enumerate(("test", "script", "rr", "review"), start=1):
                job_id = f"{index:02x}" * 16
                record = {
                    "job_id": job_id, "kind": kind, "status": "completed",
                    "repo": "scratch", "receipt": {"status": "ok", **hostile}, **hostile,
                }
                path = jobs / job_id / "job.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(record))
                with self.subTest(kind=kind), mock.patch.object(server, "JOBS_DIR", jobs):
                    status, payload, _ = server.dispatch(
                        "GET", f"/v1/agent/jobs/{job_id}", {}, True, b""
                    )
                self.assertEqual((status, payload["code"]), (404, "not_found"), payload)
                self.assertNotIn("TOKEN_SECRET", json.dumps(payload))
                self.assertNotIn("private.example", json.dumps(payload))

            allowed = (
                (5, "codex", "openai", "gpt-5.6-sol", {}),
                (6, "codex", "openai", "gpt-5.6-terra", {
                    "codeoff_workspace": {"experiment_id": "experiment-0001", "slot": "author-1"},
                }),
            )
            for index, launcher, provider, model, extra in allowed:
                profile = _fixture_execution_profile(launcher, provider, model)
                identity = {
                    "launcher": launcher, "provider": provider, "model": model,
                    **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
                    "starting_head": "b" * 40, "native_session_id": None,
                }
                job_id = f"{index:02x}" * 16
                record = {
                    "job_id": job_id, "kind": "agent", "status": "queued", "repo": "scratch",
                    "launcher": launcher, "provider": provider, "model": model,
                    **profile, "session_identity": identity, **extra,
                }
                path = jobs / job_id / "job.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(record))
                with self.subTest(agent_extra=extra), mock.patch.object(server, "JOBS_DIR", jobs):
                    status, payload, _ = server.dispatch(
                        "GET", f"/v1/agent/jobs/{job_id}", {}, True, b""
                    )
                self.assertEqual(status, 200, payload)
                self.assertIsInstance(payload, dict)
                assert isinstance(payload, dict)
                self.assertEqual(payload["job_id"], job_id)
                self.assertNotIn("codeoff_workspace", payload)

    def test_agent_job_wait_blocks_until_terminal_without_relaunching(self):
        job_id = "ab" * 16
        profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "b" * 40, "native_session_id": None,
        }
        agent_shape = {
            "kind": "agent", "repo": "scratch", "launcher": "codex",
            "provider": "openai", "model": "gpt-5.6-sol",
            **profile, "session_identity": identity,
        }
        queued = {"job_id": job_id, "status": "running", **agent_shape}
        completed = {"job_id": job_id, "status": "completed", "receipt": {"status": "ok"}, **agent_shape}
        with mock.patch.object(server, "read_job", side_effect=[queued, completed]), \
             mock.patch.object(server.time, "sleep") as sleep:
            status, payload, _ = server.dispatch(
                "POST", "/v1/agent/jobs/wait", {}, True,
                json.dumps({"job_id": job_id, "timeout_seconds": 30}).encode(),
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "completed")
        sleep.assert_called_once()

    def test_agent_job_wait_returns_not_found_for_missing_job(self):
        with mock.patch.object(server, "read_job", return_value=None):
            status, payload, _ = server.dispatch(
                "POST", "/v1/agent/jobs/wait", {}, True,
                json.dumps({"job_id": "ab" * 16, "timeout_seconds": 30}).encode(),
            )
        self.assertEqual((status, payload["code"]), (404, "not_found"))

    def test_agent_job_wait_rejects_non_ascii_numeric_string(self):
        status, payload, _ = server.dispatch(
            "POST", "/v1/agent/jobs/wait", {}, True,
            json.dumps({"job_id": "ab" * 16, "timeout_seconds": "²"}).encode(),
        )
        self.assertEqual((status, payload["code"]), (400, "bad_timeout"))

    def test_agent_job_wait_timeout_is_bounded(self):
        job_id = "ab" * 16
        profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "b" * 40, "native_session_id": None,
        }
        running = {
            "job_id": job_id, "kind": "agent", "repo": "scratch", "status": "running", "launcher": "codex",
            "provider": "openai", "model": "gpt-5.6-sol", **profile,
            "session_identity": identity,
        }
        with mock.patch.object(server, "read_job", return_value=running), \
             mock.patch.object(server.time, "monotonic", side_effect=[0, 31]):
            status, payload, _ = server.dispatch(
                "POST", "/v1/agent/jobs/wait", {}, True,
                json.dumps({"job_id": job_id, "timeout_seconds": 30}).encode(),
            )
        self.assertEqual(status, 408, payload)
        self.assertEqual(payload["code"], "job_wait_timeout")

    def test_pinned_launcher_executes_exact_hashed_bytes_after_path_and_inode_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs"
            marker = root / "marker"
            original = root / "original"
            configured = root / "codex"
            replacement = root / "replacement"
            original.write_text(f"#!/bin/sh\nprintf original > {marker}\ncat >/dev/null\n")
            replacement.write_text(f"#!/bin/sh\nprintf replacement > {marker}\ncat >/dev/null\n")
            original.chmod(0o755)
            replacement.chmod(0o755)
            configured.symlink_to(original)
            job_id = "ad" * 16
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            (jdir / "prompt.txt").write_text("bounded")
            job = {
                "job_id": job_id, "cwd": str(self.scratch), "model": "gpt-5.6-sol",
                "launcher": "codex", "provider": "openai",
                "requested_effort": "default", "effective_effort": "high",
                "effort_source": "launcher_default", "max_turns": 1,
                "session_identity": {"native_session_id": None},
            }
            with mock.patch.object(server, "JOBS_DIR", jobs):
                with server.pinned_launcher(configured) as pinned:
                    expected = pinned.fingerprint
                    configured.unlink()
                    configured.symlink_to(replacement)
                    original.write_text(replacement.read_text())
                    original.chmod(0o755)
                    proc, err = server.spawn_codex(job, pinned.path)
                    self.assertIsNone(err)
                    self.assertEqual(proc.wait(timeout=5), 0)
            self.assertEqual(marker.read_text(), "original")
            self.assertEqual(expected, "sha256:" + hashlib.sha256(
                f"#!/bin/sh\nprintf original > {marker}\ncat >/dev/null\n".encode()
            ).hexdigest())

    def test_pinned_launcher_rejects_missing_and_nonregular_and_closes_descriptors(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            regular = root / "launcher"
            regular.write_text("#!/bin/sh\nexit 0\n")
            regular.chmod(0o755)
            unreadable = root / "unreadable"
            unreadable.write_text("#!/bin/sh\nexit 0\n")
            unreadable.chmod(0)
            before = len(list(Path("/proc/self/fd").iterdir()))
            for _ in range(100):
                with server.pinned_launcher(regular) as pinned:
                    self.assertTrue(pinned.path.exists())
            with self.assertRaises(RuntimeError):
                with server.pinned_launcher(regular):
                    raise RuntimeError("fixture context failure")
            for bad in (root / "missing", root):
                with self.subTest(path=bad):
                    with self.assertRaises(OSError):
                        with server.pinned_launcher(bad):
                            pass
            with self.assertRaises(PermissionError):
                with server.pinned_launcher(unreadable):
                    pass
            after = len(list(Path("/proc/self/fd").iterdir()))
            self.assertEqual(after, before)

    def test_agent_worker_rejects_launcher_version_mismatch_before_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            job_id = "ab" * 16
            binary = Path(td) / "codex"
            binary.write_text("changed launcher fixture")
            identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "requested_effort": "high", "effective_effort": "high", "effort_source": "explicit",
                "launcher_version": "sha256:" + "a" * 64,
                "repo": "scratch", "branch": "main", "starting_head": "b" * 40,
                "native_session_id": None,
            }
            job = {
                "job_id": job_id, "kind": "agent", "status": "queued", "repo": "scratch", "cwd": str(self.scratch),
                "prompt": "bounded", "launcher": "codex", "provider": "openai",
                "model": "gpt-5.6-sol", "requested_effort": "high",
                "effective_effort": "high", "effort_source": "explicit",
                "launcher_version": identity["launcher_version"], "session_identity": identity,
                "max_turns": 1, "run_budget_seconds": 30, "response_webhook_url": None,
                "response_webhook_token": None, "resume_url": None, "created_at": "fixture",
                "started_at": None, "finished_at": None, "receipt": None,
                "log_ref": str(jobs / job_id / "stdout.log"), "error": None, "webhook": None,
            }
            with mock.patch.object(server, "JOBS_DIR", jobs):
                server.write_job(job)
                with mock.patch.object(server, "resolve_launcher_binary_now", return_value=binary), \
                     mock.patch.object(server, "spawn_writer", side_effect=AssertionError("provider execution forbidden")), \
                     mock.patch.object(server, "herdr_job_done", lambda job: None):
                    server.run_agent_job(job_id)
                saved = server.read_job(job_id)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["receipt"]["failure_code"], "launcher_version_mismatch")
        self.assertEqual(saved["receipt"]["diagnostic"]["code"], "launcher_version_mismatch")

    def test_public_agent_receipt_is_allowlisted_and_cannot_copy_launcher_text(self):
        hostile = (
            "PROMPT_FRAGMENT /home/private/provider TOKEN_SECRET "
            "https://private.example/callback model-provider-output"
        )
        profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "b" * 40, "native_session_id": "codex-fixture",
        }
        job = {
            "job_id": "ab" * 16, "kind": "agent", "repo": "scratch",
            "launcher": "codex", "provider": "openai",
            "model": "gpt-5.6-sol", "session_identity": identity, **profile,
        }
        for status in ("ok", "error", "timeout"):
            with self.subTest(status=status):
                receipt = server.normalize_receipt(
                    job,
                    {"status": status, "summary": hostile, "sha": hostile, "pr_url": hostile},
                    0,
                    False,
                )
                dumped = json.dumps(receipt, sort_keys=True)
                self.assertNotIn(hostile, dumped)
                for fragment in ("PROMPT_FRAGMENT", "/home/private", "TOKEN_SECRET", "private.example", "model-provider-output"):
                    self.assertNotIn(fragment, dumped)
                self.assertNotIn("sha", receipt)
                self.assertNotIn("pr_url", receipt)
                self.assertEqual(receipt["session_identity"]["starting_head"], "b" * 40)
                self.assertEqual(receipt["summary"], receipt["diagnostic"]["summary"])
        for doc_path in (Path("docs/HUMAN-LOOP.md"), Path("docs/USING.md")):
            text = doc_path.read_text()
            self.assertIn("validated `starting_head`", text)
            self.assertIn("launcher/model-supplied SHA/PR URL", text)

    def test_launcher_fingerprint_docs_bound_downstream_package_resolution(self):
        for doc_path in (Path("docs/HUMAN-LOOP.md"), Path("docs/USING.md")):
            with self.subTest(doc=doc_path.name):
                text = doc_path.read_text()
                self.assertIn("exact selected wrapper artifact bytes", text)
                self.assertIn("does not attest", text)
                self.assertIn("interpreter", text)
                self.assertIn("PATH-resolved mise/shim", text)
                self.assertIn("provider package/tool bytes", text)

    def test_worker_callback_cans_raw_spawn_failure(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            binary = Path(td) / "codex"
            binary.write_text("fixture")
            hostile = "spawn failed /home/private/codex TOKEN_SECRET https://private.example provider text"
            job_id = "ac" * 16
            profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol", binary)
            identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
                "starting_head": "b" * 40, "native_session_id": None,
            }
            job = {
                "job_id": job_id, "kind": "agent", "status": "queued", "repo": "scratch", "cwd": str(self.scratch),
                "prompt": "PROMPT_FRAGMENT", "launcher": "codex", "provider": "openai",
                "model": "gpt-5.6-sol", **profile, "session_identity": identity,
                "max_turns": 1, "run_budget_seconds": 30, "created_at": "fixture",
                "started_at": None, "finished_at": None, "receipt": None,
                "response_webhook_url": "https://example.invalid/resume",
                "response_webhook_token": "TOKEN_SECRET", "resume_url": None,
                "log_ref": str(jobs / job_id / "stdout.log"), "error": None, "webhook": None,
            }
            with mock.patch.object(server, "JOBS_DIR", jobs):
                server.write_job(job)
                with mock.patch.object(server, "resolve_launcher_binary_now", return_value=binary), \
                     mock.patch.object(server, "spawn_writer", return_value=(None, {"error": hostile, "code": "spawn_failed"})), \
                     mock.patch.object(server, "post_receipt", return_value={"ok": True}) as posted, \
                     mock.patch.object(server, "herdr_job_done"):
                    server.run_agent_job(job_id)
            callback = posted.call_args.args[1]
            dumped = json.dumps(callback, sort_keys=True)
            for fragment in ("PROMPT_FRAGMENT", "/home/private", "TOKEN_SECRET", "private.example", "provider text"):
                self.assertNotIn(fragment, dumped)
            self.assertEqual(callback["summary"], callback["diagnostic"]["summary"])

    def test_agent_receipt_binds_effective_effort(self):
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            "requested_effort": "high", "effective_effort": "high",
            "effort_source": "explicit", "launcher_version": "sha256:" + "a" * 64,
            "repo": "scratch", "branch": "main", "starting_head": "b" * 40,
            "native_session_id": "codex-fixture",
        }
        job = {
            "job_id": "ab" * 16, "kind": "agent", "repo": "scratch",
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", "session_identity": identity,
            "requested_effort": "high", "effective_effort": "high",
            "effort_source": "explicit", "launcher_version": identity["launcher_version"],
            "log_ref": "/private/runtime/stdout.log",
        }
        receipt = server.normalize_receipt(
            job, {"status": "ok", "summary": "bounded"}, 0, False
        )
        self.assertEqual(
            {key: receipt[key] for key in (
                "requested_effort", "effective_effort", "effort_source", "launcher_version"
            )},
            {
                "requested_effort": "high", "effective_effort": "high",
                "effort_source": "explicit", "launcher_version": "sha256:" + "a" * 64,
            },
        )
        self.assertNotIn("log_ref", receipt)
        self.assertNotIn("/private", json.dumps(receipt, sort_keys=True))

    def test_session_identity_binds_requested_effective_source_and_version(self):
        profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "b" * 40, "native_session_id": "native-123",
        }
        parsed, error = server.parse_session_identity(identity)
        self.assertIsNone(error)
        self.assertEqual(parsed, identity)
        schema = json.loads(server.openapi_bytes())["components"]["schemas"]["SessionIdentity"]
        self.assertEqual(set(schema["required"]), set(identity))
        self.assertEqual(
            set(schema["properties"]), set(identity) | {"route_execution_profile"}
        )
        self.assertEqual(
            schema["properties"]["route_execution_profile"]["$ref"],
            "#/components/schemas/RouteExecutionProfile",
        )

    def test_session_identity_provenance_has_closed_bounded_formats(self):
        profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
        base = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "Graphwing_catalog-1",
            "branch": "feature/model-routing_hardening.v1",
            "starting_head": "b" * 40, "native_session_id": "native-123",
        }
        for head in ("b" * 40, "c" * 64):
            with self.subTest(valid_head_length=len(head)):
                parsed, error = server.parse_session_identity({**base, "starting_head": head})
                self.assertIsNone(error)
                self.assertEqual(parsed["starting_head"], head)
        codeoff_repo = "codeoff:experiment-0001:author-1"
        parsed, error = server.parse_session_identity({**base, "repo": codeoff_repo})
        self.assertIsNone(error)
        self.assertEqual(parsed["repo"], codeoff_repo)
        hostile = (
            ("repo", "../private"),
            ("repo", "https://private.example/repo"),
            ("repo", "bad\nrepo"),
            ("repo", "r" * 129),
            ("branch", "/private/branch"),
            ("branch", "../private"),
            ("branch", "https://private.example/branch"),
            ("branch", "bad\x00branch"),
            ("branch", "b" * 256),
            ("starting_head", "/home/tim/private"),
            ("starting_head", "TOKEN_SECRET"),
            ("starting_head", "https://private.example/token"),
            ("starting_head", "a" * 39),
            ("starting_head", "A" * 40),
            ("starting_head", "a" * 65),
        )
        for field, value in hostile:
            with self.subTest(field=field, value=value[:30]):
                parsed, error = server.parse_session_identity({**base, field: value})
                self.assertIsNone(parsed)
                self.assertEqual(error["code"], "bad_session_identity")
        schemas = json.loads(server.openapi_bytes())["components"]["schemas"]
        props = schemas["SessionIdentity"]["properties"]
        self.assertEqual((props["repo"]["minLength"], props["repo"]["maxLength"]), (1, 128))
        self.assertEqual(props["branch"], {"$ref": "#/components/schemas/BranchName"})
        self.assertEqual((schemas["BranchName"]["minLength"], schemas["BranchName"]["maxLength"]), (1, 255))
        self.assertEqual(props["starting_head"]["pattern"], "^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

    def test_invalid_session_provenance_is_not_public_or_callback_data(self):
        hostile = "https://private.example/callback?token=TOKEN_SECRET"
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            job_id = "bc" * 16
            job = _stamp_complete_writer_evidence({
                "job_id": job_id, "status": "completed", "repo": "scratch",
                "cwd": str(self.scratch.resolve()), "session_identity": {
                    "branch": "main", "starting_head": "b" * 40,
                },
                "receipt": {"status": "ok"},
            })
            job["session_identity"]["starting_head"] = hostile
            job["response_webhook_url"] = "https://example.invalid/resume"
            with mock.patch.object(server, "JOBS_DIR", jobs):
                server.write_job(job)
                get_status, get_payload, _ = server.dispatch(
                    "GET", f"/v1/agent/jobs/{job_id}", {}, True, b""
                )
                wait_status, wait_payload, _ = server.dispatch(
                    "POST", "/v1/agent/jobs/wait", {}, True,
                    json.dumps({"job_id": job_id, "timeout_seconds": 1}).encode(),
                )
            normalized = server.normalize_receipt(
                job, {"status": "ok", "summary": "done"}, 0, False
            )
            with mock.patch.object(server, "post_receipt", return_value={"ok": True}) as posted:
                server.deliver_webhook(job, normalized)
            rendered = json.dumps({
                "get": get_payload, "wait": wait_payload,
                "callback": posted.call_args.args[1],
            })
            self.assertEqual((get_status, wait_status), (404, 404))
            self.assertIsNone(normalized["session_identity"])
            self.assertNotIn(hostile, rendered)
            self.assertNotIn("TOKEN_SECRET", rendered)

    def test_resume_rejects_top_level_profile_drift_from_identity(self):
        profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "b" * 40, "native_session_id": "native-123",
        }
        prior = {
            "job_id": "ab" * 16, "status": "completed",
            "launcher": "codex", "provider": "anthropic", "model": "claude-opus-5",
            **profile, "session_identity": identity,
            "receipt": {
                "status": "ok", "job_id": "ab" * 16,
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                **profile, "session_identity": identity,
            },
        }
        with mock.patch.object(server, "read_job", return_value=prior):
            error = server.validate_native_resume_provenance(
                {"resume_job_id": "ab" * 16}, identity, "codex"
            )
        self.assertEqual(error["code"], "untraceable_resume_session")

    def test_session_identity_rejects_unknown_fields_in_parser_and_endpoint(self):
        profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "b" * 40, "native_session_id": "native-123",
            "native_token": "TOKEN_SECRET",
        }
        parsed, error = server.parse_session_identity(identity)
        self.assertIsNone(parsed)
        self.assertEqual(error["code"], "bad_session_identity")
        body = json.dumps({
            "prompt": "bounded", "cwd": "scratch", "launcher": "codex",
            "provider": "openai", "model": "gpt-5.6-sol", "session_identity": identity,
            "resume_job_id": "ab" * 16,
        }).encode()
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)):
            status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
        self.assertEqual((status, payload["code"]), (400, "bad_session_identity"))

    def test_parse_receipt_json(self):
        parsed = server.parse_receipt_text(
            'noise\n{"status":"ok","sha":"abc","pr_url":null,"summary":"done"}\n'
        )
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["sha"], "abc")

    def test_parse_codex_jsonl_receipt_and_session(self):
        text = "\n".join([
            '{"type":"thread.started","thread_id":"codex-123"}',
            json.dumps({"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}'}}),
        ])
        self.assertEqual(server.parse_native_session_id(text), "codex-123")
        self.assertEqual(server.parse_receipt_text(text)["summary"], "done")

    def test_parse_claude_json_receipt_and_session(self):
        text = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "session_id": "claude-123",
            "result": 'Test execution was unavailable.\n\n{"status":"ok","sha":null,"pr_url":null,"summary":"done"}',
        })
        self.assertEqual(server.parse_native_session_id(text, "claude"), "claude-123")
        self.assertEqual(server.parse_receipt_text(text)["summary"], "done")
        self.assertIsNone(server.parse_native_session_id("session_id: invented", "claude"))
        failed = json.dumps({
            "type": "result", "subtype": "error", "is_error": True,
            "session_id": "claude-123",
            "result": '{"status":"ok","summary":"must not pass"}',
        })
        self.assertIsNone(server.parse_receipt_text(failed))

    def test_receipt_and_codex_session_fail_closed(self):
        job = {"job_id": "ab" * 16, "launcher": "codex", "log_ref": "/tmp/log", "session_identity": {"native_session_id": "codex-123"}}
        parsed = {"status": "ok", "summary": "done"}
        self.assertEqual(server.normalize_receipt(job, None, 0, False)["status"], "error")
        self.assertEqual(server.normalize_receipt(job, parsed, 2, False)["status"], "error")
        missing = dict(job, session_identity={"native_session_id": None})
        self.assertEqual(server.normalize_receipt(missing, parsed, 0, False)["status"], "error")
        self.assertIn("changed", server.record_native_session(job, "codex-456"))
        self.assertEqual(job["session_identity"]["native_session_id"], "codex-123")

    def test_receipt_and_claude_session_fail_closed(self):
        job = {
            "job_id": "ab" * 16, "launcher": "claude",
            "log_ref": "/tmp/log", "session_identity": {"native_session_id": "claude-123"},
        }
        parsed = {"status": "ok", "summary": "done"}
        missing = dict(job, session_identity={"native_session_id": None})
        self.assertEqual(server.normalize_receipt(missing, parsed, 0, False)["status"], "error")
        self.assertIn("changed", server.record_native_session(job, "claude-456"))
        self.assertEqual(job["session_identity"]["native_session_id"], "claude-123")

    def test_launcher_failure_contract_is_closed_and_failover_is_narrow(self):
        expected = {
            "none": {"success"}, "provider_availability": {"missing_binary"},
            "timeout_budget": {"run_budget_exceeded", "reported_timeout"},
            "model_execution": {"process_exit", "spawn_failed", "model_stopped", "model_reported_error"},
            "malformed_contract": {"invalid_receipt", "adapter_contract_invalid", "missing_prompt"},
            "session_provenance": {
                "session_identity_missing", "session_identity_mismatch", "session_provenance_invalid",
                "launcher_version_mismatch",
            },
            "repository_state": {"repository_mismatch", "branch_mismatch", "head_mismatch"},
            "cancelled": {"cancelled"}, "unknown": {"unknown_failure"},
        }
        self.assertEqual(server.FAILURE_CLASS_CODES, expected)
        for failure_class, codes in expected.items():
            for evidence in codes:
                got = server.classify_agent_failure(evidence)
                self.assertEqual((got["failure_class"], got["failure_code"], got["failover_eligible"]),
                                 (failure_class, "none" if evidence == "success" else evidence,
                                  failure_class == "provider_availability"))

    def test_diagnostic_v1_is_closed_sanitized_and_evidence_scoped(self):
        expected = {
            "missing_binary": ("provider_availability", "binary", "deterministic_binary_check"),
            "local_binary_missing": ("local_infrastructure", "binary", "deterministic_binary_check"),
            "not_configured": ("local_infrastructure", "config", "deterministic_config_check"),
            "provider_authentication": ("provider_availability", "auth", "structured_provider_error"),
            "provider_network": ("provider_availability", "network", "structured_provider_error"),
            "provider_rate_limit": ("provider_availability", "provider", "structured_provider_error"),
            "provider_http_5xx": ("provider_availability", "provider", "structured_provider_error"),
            "stack_unhealthy": ("local_infrastructure", "stack", "compose_snapshot"),
            "port_not_listening": ("local_infrastructure", "port", "bounded_port_probe"),
            "health_bad_status": ("local_infrastructure", "health", "bounded_loopback_probe"),
            "cmd_failed": ("local_infrastructure", "command", "bounded_command"),
            "timeout": ("local_infrastructure", "command", "bounded_command"),
            "process_exit": ("workflow_failure", "execution", "closed_failure_code"),
            "primary_execution_profile_mismatch": ("workflow_failure", "execution", "closed_failure_code"),
            "none": ("none", "none", "success"),
        }
        for code, want in expected.items():
            with self.subTest(code=code):
                got = server.compact_diagnostic(code)
                self.assertEqual(got["diagnostic_version"], "diagnostic-v1")
                self.assertEqual(
                    (got["category"], got["component"], got["evidence"]), want
                )
                self.assertEqual(got["code"], code)
                self.assertEqual(
                    set(got),
                    {"diagnostic_version", "category", "component", "code", "evidence", "summary"},
                )
                self.assertLessEqual(len(json.dumps(got, separators=(",", ":"))), 300)

        expected_codes = (
            (set(server.FAILURE_CLASS_BY_CODE) - {"success"})
            | set(server.PROVIDER_AVAILABILITY_CODES)
            | {
                "none", "not_configured", "bad_health_config", "health_unreachable",
                "health_bad_status", "stack_unhealthy", "port_not_listening", "cmd_failed",
                "timeout", "local_binary_missing", "missing_port", "bad_port", "unknown_port",
                "unknown_stack", "primary_execution_profile_mismatch",
            }
        )
        self.assertEqual(set(server.DIAGNOSTIC_METADATA), expected_codes)
        for code in server.PROVIDER_AVAILABILITY_CODES:
            self.assertEqual(server.compact_diagnostic(code)["category"], "provider_availability", code)

        hostile = server.compact_diagnostic(
            "tok_secret /home/user/.config/provider/auth.json raw provider output"
        )
        dumped = json.dumps(hostile)
        self.assertEqual(hostile["code"], "unknown_failure")
        for secret in ("tok_secret", "/home/user", "provider output"):
            self.assertNotIn(secret, dumped)

    def test_agent_diagnostic_adds_origin_without_changing_failure_taxonomy(self):
        cases = (
            ("missing_binary", "", "provider_availability", "missing_binary", True, "provider_availability"),
            (
                "process_exit",
                json.dumps({"type": "error", "error": {"code": "rate_limit_exceeded"}}),
                "provider_availability",
                "provider_rate_limit",
                True,
                "provider_availability",
            ),
            (
                "process_exit",
                json.dumps({"type": "error", "error": {"code": "authentication_error"}}),
                "provider_availability",
                "provider_authentication",
                True,
                "provider_availability",
            ),
            ("process_exit", "raw authentication network prose", "model_execution", "process_exit", False, "workflow_failure"),
        )
        for evidence, output, failure_class, failure_code, eligible, category in cases:
            with self.subTest(evidence=evidence, failure_code=failure_code):
                got = server.classify_agent_failure(evidence, output)
                self.assertEqual(
                    (got["failure_class"], got["failure_code"], got["failover_eligible"]),
                    (failure_class, failure_code, eligible),
                )
                self.assertEqual(got["diagnostic"]["category"], category)
                self.assertEqual(got["diagnostic"]["code"], failure_code)
                if eligible:
                    self.assertEqual(got["diagnostic"]["category"], got["failure_class"])

    def test_structured_provider_failures_and_receipts_are_closed(self):
        fixtures = (
            ("codex", {"type": "error", "error": {"code": "rate_limit_exceeded"}}, "provider_rate_limit"),
            ("codex-quota", {"type": "error", "error": {"code": "quota_exceeded"}}, "provider_quota"),
            ("claude", {"type": "result", "is_error": True, "error": {"type": "authentication_error"}}, "provider_authentication"),
            ("claude-network", {"type": "error", "error": {"type": "network_error"}}, "provider_network"),
            ("grok", {"jsonrpc": "2.0", "error": {"data": {"status": 503}}}, "provider_http_5xx"),
            ("grok-overload", {"jsonrpc": "2.0", "error": {"data": {"code": "overloaded_error"}}}, "provider_overloaded"),
        )
        for launcher, event, code in fixtures:
            with self.subTest(launcher=launcher):
                got = server.classify_agent_failure("process_exit", json.dumps(event))
                self.assertEqual((got["failure_class"], got["failure_code"], got["failover_eligible"]),
                                 ("provider_availability", code, True))
        structured = json.dumps({"type": "error", "error": {"status_code": 503}})
        for evidence, expected in (("run_budget_exceeded", "timeout_budget"), ("session_identity_mismatch", "session_provenance")):
            got = server.classify_agent_failure(evidence, structured)
            self.assertEqual((got["failure_class"], got["failover_eligible"]), (expected, False))
        prose = server.classify_agent_failure("process_exit", "authentication quota network overload")
        self.assertEqual((prose["failure_class"], prose["failover_eligible"]), ("model_execution", False))
        profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "0" * 40, "native_session_id": "native-123",
        }
        job = {
            "job_id": "ab" * 16, "kind": "agent", "repo": "scratch",
            "launcher": "codex", "provider": "openai",
            "model": "gpt-5.6-sol", **profile, "log_ref": "/tmp/log",
            "session_identity": identity,
        }
        receipts = (
            (server.normalize_receipt(job, {"status": "ok", "summary": "done"}, 0, False), "none", "none"),
            (server.normalize_receipt(job, None, 0, False), "malformed_contract", "invalid_receipt"),
            (server.normalize_receipt(job, {"status": "error", "summary": "specific"}, 0, False), "model_execution", "model_reported_error"),
            (server.normalize_receipt(job, {"status": "timeout", "summary": "budget"}, 0, False), "timeout_budget", "reported_timeout"),
        )
        for receipt, failure_class, failure_code in receipts:
            self.assertEqual((receipt["failure_class"], receipt["failure_code"]), (failure_class, failure_code))
        self.assertEqual(receipts[-2][0]["summary"], receipts[-2][0]["diagnostic"]["summary"])
        self.assertNotIn("specific", json.dumps(receipts[-2][0]))

    def test_agent_run_missing_binary_codes_match_for_all_launchers(self):
        cases = (
            ("codex", "openai", "gpt-5.6-sol", "GRAPHWING_CODEX_BIN"),
            ("claude", "anthropic", "claude-opus-5", "GRAPHWING_CLAUDE_BIN"),
            ("grok", "xai", "grok-4.6", "GRAPHWING_GROK_BIN"),
        )
        for launcher, provider, model, env_var in cases:
            body = json.dumps({
                "prompt": "x", "cwd": "scratch", "launcher": launcher,
                "provider": provider, "model": model,
            }).encode()
            with self.subTest(launcher=launcher), \
                 mock.patch.dict(os.environ, {env_var: f"/nope/{launcher}"}):
                status, payload, _ = server.dispatch(
                    "POST", "/v1/agent/run", {}, True, body,
                )
            self.assertEqual(status, 501)
            self.assertEqual(
                (payload["code"], payload["failure_code"], payload["diagnostic"]["code"]),
                ("missing_binary", "missing_binary", "missing_binary"),
            )
            self.assertEqual(payload["failure_class"], "provider_availability")
            self.assertTrue(payload["failover_eligible"])
            self.assertEqual(payload["diagnostic"]["category"], "provider_availability")
            self.assertNotIn("provider_unavailable", json.dumps(payload))

    def test_webhook_agent_run_missing_binary_posts_classified_receipt(self):
        cases = (
            ("codex", "openai", "gpt-5.6-sol", "GRAPHWING_CODEX_BIN", "go_coding"),
            ("claude", "anthropic", "claude-opus-5", "GRAPHWING_CLAUDE_BIN", "typescript_coding"),
            ("grok", "xai", "grok-4.6", "GRAPHWING_GROK_BIN", "research_ops"),
        )
        for launcher, provider, model, env_var, work_kind in cases:
            with self.subTest(launcher=launcher), tempfile.TemporaryDirectory() as td:
                jobs = Path(td) / "jobs"
                primary_route = server.slice_route_lookup("mechanical", "M", work_kind=work_kind)
                fallback_result = {}
                body = json.dumps({
                    "prompt": "x", "cwd": "scratch", "launcher": launcher,
                    "provider": provider, "model": model,
                    "effort": primary_route["effort"],
                    "route_execution_profile": primary_route["writer_execution_profile"],
                    "response_webhook_url": "https://example.invalid/resume",
                }).encode()

                def receive_receipt(_url, receipt, token=None):
                    stored = server.read_job(receipt["job_id"])
                    self.assertEqual(stored["status"], "failed")
                    self.assertEqual(stored["receipt"], receipt)
                    self.assertEqual(stored["session_identity"], receipt["session_identity"])
                    fallback_result["status"], fallback_result["payload"] = server.slice_route_fallback(json.dumps({
                        "class": "mechanical", "size": "M", "work_kind": work_kind,
                        "primary_route": primary_route,
                        "primary_receipt": {**receipt, "role": "primary"},
                    }).encode())
                    return {"ok": True, "status": 200}

                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.dict(os.environ, {env_var: f"/nope/{launcher}"}), \
                     mock.patch.object(server, "enqueue_agent", lambda job: server.run_agent_job(job["job_id"])), \
                     mock.patch.object(server, "spawn_writer", side_effect=AssertionError("no model process")), \
                     mock.patch.object(server, "run_grok_acp", side_effect=AssertionError("no model process")), \
                     mock.patch.object(server, "post_receipt", side_effect=receive_receipt) as posted:
                    status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
                self.assertEqual(status, 202, payload)
                self.assertEqual(fallback_result["status"], 200, fallback_result)
                posted.assert_called_once()
                receipt = posted.call_args.args[1]
                self.assertEqual(receipt["status"], "error")
                self.assertEqual(receipt["failure_class"], "provider_availability")
                self.assertEqual(receipt["failure_code"], "missing_binary")
                self.assertTrue(receipt["failover_eligible"])
                self.assertEqual(receipt["diagnostic"]["category"], "provider_availability")
                self.assertEqual(receipt["diagnostic"]["component"], "binary")
                self.assertEqual(receipt["summary"], "writer binary unavailable")
                self.assertIsNone(receipt["usage"])
                self.assertEqual(receipt["usage_diagnostic"], "usage_not_reported")
                self.assertNotIn(launcher, receipt["summary"])

    def test_wrap_prompt_locks_cwd_and_leaves_staging_to_graph(self):
        text = server.wrap_prompt("ab" * 16, "ping", "/home/tim/work/gw-real-slice")
        self.assertIn("/home/tim/work/gw-real-slice", text)
        self.assertIn("only inside that directory", text)
        self.assertNotIn("git add", text)
        self.assertIn("Graphwing stages", text)
        self.assertIn("Do not git commit, git push", text)
        self.assertIn("Do not `git checkout`", text)

    def test_writer_execution_profile_mismatch_blocks_commit_and_merge_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            binary = root / "codex"
            binary.write_text("fixture")
            profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol", binary)
            branch = subprocess.run(
                ["git", "-C", str(repo), "branch", "--show-current"], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                **_fixture_identity_profile(profile), "repo": "scratch", "branch": branch,
                "starting_head": head, "native_session_id": "native-writer",
            }
            baseline = server.capture_writer_baseline(repo)[0]
            (repo / "writer.txt").write_text("writer\n")
            job_id = "ae" * 16
            pristine = {
                "job_id": job_id, "kind": "agent", "status": "completed", "repo": "scratch", "cwd": str(repo),
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", **profile,
                "session_identity": identity, "git_baseline": baseline,
                "writer_parent_paths": [],
                "receipt": {
                    "status": "ok", "job_id": job_id, "launcher": "codex", "provider": "openai",
                    "model": "gpt-5.6-sol", **profile, "session_identity": identity,
                    "failure_class": "none", "failure_code": "none", "failover_eligible": False,
                    "diagnostic": server.compact_diagnostic("none"), "summary": "ok",
                },
            }
            self.assertIsNone(server.stage_writer_changes(pristine))
            mutations = {
                "launcher": "claude", "provider": "anthropic", "model": "claude-opus-5",
                "effective_effort": "default", "launcher_version": "sha256:" + "f" * 64,
                "requested_effort": "high", "effort_source": "explicit",
            }
            surfaces = ("job", "session_identity", "receipt")
            with mock.patch.object(server, "JOBS_DIR", jobs):
                for surface in surfaces:
                    for field, value in mutations.items():
                        with self.subTest(surface=surface, field=field):
                            job = deepcopy(pristine)
                            target = job if surface == "job" else job[surface]
                            target[field] = value
                            server.write_job(job)
                            error = server.validate_writer_evidence(job_id, "scratch", repo, branch)
                            self.assertEqual(error["code"], "writer_evidence_mismatch")
                            status, payload = server.git_commit(json.dumps({
                                "repo": "scratch", "message": "must not commit", "writer_job_id": job_id,
                            }).encode(), {"scratch": str(repo)})
                            self.assertEqual((status, payload["code"]), (409, "writer_job_mismatch"))
                coordinated = deepcopy(pristine)
                coordinated["requested_effort"] = "high"
                coordinated["effort_source"] = "explicit"
                coordinated["receipt"]["requested_effort"] = "high"
                coordinated["receipt"]["effort_source"] = "explicit"
                server.write_job(coordinated)
                error = server.validate_writer_evidence(job_id, "scratch", repo, branch)
                self.assertEqual(error["code"], "writer_evidence_mismatch")
                for field, value in (
                    ("repo", "other"), ("branch", "other"), ("starting_head", "f" * 40),
                ):
                    with self.subTest(surface="top_level_provenance", field=field):
                        hostile = deepcopy(pristine)
                        hostile[field] = value
                        server.write_job(hostile)
                        error = server.validate_writer_evidence(job_id, "scratch", repo, branch)
                        self.assertEqual(error["code"], "writer_evidence_mismatch")
                        status, payload = server.git_commit(json.dumps({
                            "repo": "scratch", "message": "must not commit",
                            "writer_job_id": job_id,
                        }).encode(), {"scratch": str(repo)})
                        self.assertEqual((status, payload["code"]), (409, "writer_job_mismatch"))
                        self.assertEqual(
                            subprocess.run(
                                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                capture_output=True, text=True,
                            ).stdout.strip(),
                            head,
                        )

    def test_unknown_non_agent_job_cannot_authorize_commit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            branch = subprocess.run(
                ["git", "-C", str(repo), "branch", "--show-current"], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            baseline = server.capture_writer_baseline(repo)[0]
            (repo / "alien.txt").write_text("not an agent writer\n")
            job_id = "ad" * 16
            job = _stamp_complete_writer_evidence({
                "job_id": job_id, "kind": "agent", "status": "completed",
                "repo": "scratch", "cwd": str(repo.resolve()), "git_baseline": baseline,
                "writer_parent_paths": [], "session_identity": {
                    "branch": branch, "starting_head": head,
                },
                "receipt": {"status": "ok"},
            })
            self.assertIsNone(server.stage_writer_changes(job))
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                for kind in ("alien-non-agent", "test", "script", "rr", "review", None, 7):
                    candidate = deepcopy(job)
                    if kind is None:
                        candidate.pop("kind")
                    else:
                        candidate["kind"] = kind
                    server.write_job(candidate)
                    evidence_error = server.validate_writer_evidence(job_id, "scratch", repo, branch)
                    status, payload, _ = server.dispatch(
                        "POST", "/v1/git/commit", {}, True,
                        json.dumps({
                            "repo": "scratch", "message": "must not commit",
                            "writer_job_id": job_id,
                        }).encode(),
                    )
                    with self.subTest(kind=kind):
                        self.assertEqual(
                            evidence_error.get("code") if isinstance(evidence_error, dict) else None,
                            "writer_evidence_mismatch",
                        )
                        self.assertEqual(
                            (status, payload.get("code") if isinstance(payload, dict) else None),
                            (409, "writer_job_mismatch"),
                            payload,
                        )
            after = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(after, head)

    def test_writer_staging_and_commit_preserve_dirty_operator_state_in_linked_worktree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            primary = self._scratch_git(root)
            for name in ("modified.txt", "deleted.txt", "operator-staged.txt", "operator-worktree.txt", "note1.md", "index.json"):
                (primary / name).write_text("base\n")
            subprocess.run(["git", "-C", str(primary), "add", "--", "modified.txt", "deleted.txt", "operator-staged.txt", "operator-worktree.txt", "note1.md", "index.json"], check=True)
            subprocess.run(["git", "-C", str(primary), "commit", "-m", "fixtures"], check=True, capture_output=True)
            linked = root / "linked"
            subprocess.run(["git", "-C", str(primary), "worktree", "add", "-b", "writer", str(linked), "HEAD"], check=True, capture_output=True)
            (linked / "operator-staged.txt").write_text("operator staged\n")
            subprocess.run(["git", "-C", str(linked), "add", "--", "operator-staged.txt"], check=True)
            (linked / "operator-worktree.txt").write_text("operator unstaged\n")
            (linked / "note1.md").write_text("operator glob collision\n")
            baseline, err = server.capture_writer_baseline(linked)
            self.assertIsNone(err)
            (linked / "modified.txt").write_text("writer modified\n")
            (linked / "new.txt").write_text("writer new\n")
            (linked / "note[1].md").write_text("literal writer path\n")
            (linked / "deleted.txt").unlink()
            job_id = "ab" * 16
            job = {
                "job_id": job_id, "repo": "scratch", "cwd": str(linked),
                "status": "completed", "git_baseline": baseline,
                "session_identity": {"branch": "writer", "starting_head": subprocess.run(
                    ["git", "-C", str(linked), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
                ).stdout.strip()},
                "receipt": {"status": "ok"},
            }
            _stamp_complete_writer_evidence(job)
            staged_err = server.stage_writer_changes(job)
            self.assertIsNone(staged_err)
            self.assertEqual(job["writer_paths"], ["deleted.txt", "modified.txt", "new.txt", "note[1].md"])
            (linked / "index.json").write_text("graph complete\n")
            with mock.patch.object(server, "JOBS_DIR", root / "jobs"):
                server.write_job(job)
                status, payload = server.git_commit(json.dumps({
                    "repo": "scratch", "message": "writer result", "writer_job_id": job_id,
                    "add": "index.json",
                }).encode(), {"scratch": str(linked)})
            self.assertEqual(status, 200, payload)
            committed = subprocess.run(
                ["git", "-C", str(linked), "show", "--format=", "--name-only", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.splitlines()
            self.assertEqual(set(committed), {"deleted.txt", "index.json", "modified.txt", "new.txt", "note[1].md"})
            cached = subprocess.run(
                ["git", "-C", str(linked), "diff", "--cached", "--name-only"],
                check=True, capture_output=True, text=True,
            ).stdout.splitlines()
            self.assertEqual(cached, ["operator-staged.txt"])
            self.assertEqual((linked / "operator-worktree.txt").read_text(), "operator unstaged\n")
            self.assertEqual((linked / "note1.md").read_text(), "operator glob collision\n")
            self.assertIn("note1.md", subprocess.run(
                ["git", "-C", str(linked), "diff", "--name-only"], check=True, capture_output=True, text=True,
            ).stdout.splitlines())

    def test_writer_commit_stops_before_commit_when_temp_index_seed_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            baseline = server.capture_writer_baseline(repo)[0]
            (repo / "writer.txt").write_text("writer\n")
            job_id = "ef" * 16
            branch = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], check=True, capture_output=True, text=True).stdout.strip()
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            job = {
                "job_id": job_id, "repo": "scratch", "cwd": str(repo), "status": "completed",
                "git_baseline": baseline, "writer_parent_paths": [], "receipt": {"status": "ok"},
                "session_identity": {"branch": branch, "starting_head": head},
            }
            _stamp_complete_writer_evidence(job)
            self.assertIsNone(server.stage_writer_changes(job))
            original = server.run_cmd
            commit_attempted = False
            temp_parent = None
            temp_mode = None

            def fail_read_tree(args, **kwargs):
                nonlocal commit_attempted, temp_parent, temp_mode
                if "GIT_INDEX_FILE" in (kwargs.get("env") or {}) and "read-tree" in args:
                    temp_parent = Path(kwargs["env"]["GIT_INDEX_FILE"]).parent
                    temp_mode = stat.S_IMODE(temp_parent.stat().st_mode)
                    return {"ok": False, "returncode": 1, "stderr": "seed failed", "stdout": "", "truncated": False}
                if "GIT_INDEX_FILE" in (kwargs.get("env") or {}) and "commit" in args:
                    commit_attempted = True
                return original(args, **kwargs)

            with mock.patch.object(server, "JOBS_DIR", root / "jobs"), mock.patch.object(server, "run_cmd", side_effect=fail_read_tree):
                server.write_job(job)
                status, _ = server.git_commit(json.dumps({
                    "repo": "scratch", "message": "must not commit", "writer_job_id": job_id,
                }).encode(), {"scratch": str(repo)})
            self.assertEqual(status, 400)
            self.assertFalse(commit_attempted)
            self.assertEqual(temp_mode, 0o700)
            self.assertFalse(temp_parent.exists())
            self.assertEqual(subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
            ).stdout.strip(), head)

    def test_writer_snapshot_rejects_truncated_status(self):
        with mock.patch.object(server, "run_git", return_value={"ok": True, "stdout": "", "truncated": True}):
            baseline, err = server.capture_writer_baseline(Path("/unused"))
        self.assertIsNone(baseline)
        self.assertEqual(err["code"], "repository_mismatch")

    def test_staging_exception_finishes_job_and_delivers_failure_webhook(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            job_id = "91" * 16
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            binary = root / "codex"
            binary.write_text("fixture")
            job = {
                "job_id": job_id, "status": "queued", "repo": "scratch", "cwd": str(repo),
                "prompt": "x", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "session_identity": {"launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", "repo": "scratch", "branch": "main", "starting_head": head, "native_session_id": None},
                "created_at": "t", "started_at": None, "finished_at": None, "max_turns": 1,
                "run_budget_seconds": 30, "receipt": None, "log_ref": str(jdir / "stdout.log"),
                "error": None, "webhook": None, "response_webhook_url": "https://example.invalid/resume",
                "git_baseline": {"dirty": {}}, "writer_parent_paths": [],
            }
            _stamp_fixture_execution_profile(job, binary)
            (jdir / "job.json").write_text(json.dumps(job))
            (jdir / "prompt.txt").write_text("x")

            class FakeProc:
                pid = 42
                returncode = 0
                def wait(self, timeout=None):
                    (jdir / "stdout.log").write_text(
                        '{"type":"thread.started","thread_id":"codex-123"}\n'
                        + '{"type":"turn.started"}\n'
                        + json.dumps({"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}'}})
                        + '\n'
                        + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 25, "cached_input_tokens": 5, "cache_write_input_tokens": 2, "output_tokens": 8, "reasoning_output_tokens": 3}})
                    )
                    return 0

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=binary), \
                 mock.patch.object(server, "spawn_writer", return_value=(FakeProc(), None)), \
                 mock.patch.object(server, "stage_writer_changes", side_effect=OSError("private detail")), \
                 mock.patch.object(server, "post_receipt", return_value={"ok": True, "status": 200}) as posted:
                server.run_agent_job(job_id)
                saved = server.read_job(job_id)
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["receipt"]["failure_class"], "repository_state")
            self.assertEqual(saved["receipt"]["failure_code"], "repository_mismatch")
            self.assertNotIn("private detail", json.dumps(saved["receipt"]))
            self.assertEqual(saved["receipt"]["usage"], {
                "usage_version": "normalized-usage-v1",
                "fresh_input_tokens": 18,
                "cached_input_tokens": 5,
                "cache_write_tokens": 2,
                "output_tokens": 8,
                "reasoning_tokens": 3,
                "provider_cost_usd": None,
                "wall_seconds": saved["receipt"]["usage"]["wall_seconds"],
                "turns_observed": None,
            })
            self.assertIsNone(saved["receipt"]["usage_diagnostic"])
            self.assertNotIn("turn.completed", json.dumps(saved["receipt"]))
            posted.assert_called_once()
            self.assertEqual(posted.call_args.args[1], saved["receipt"])

    def test_dirty_conflict_receipt_summary_names_only_bounded_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            (repo / "dirty.txt").write_text("base\n")
            subprocess.run(["git", "-C", str(repo), "add", "--", "dirty.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "dirty"], check=True, capture_output=True)
            (repo / "dirty.txt").write_text("operator private bytes\n")
            baseline = server.capture_writer_baseline(repo)[0]
            jobs = root / "jobs"
            job_id = "99" * 16
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            job = {
                "job_id": job_id, "status": "queued", "repo": "scratch", "cwd": str(repo), "prompt": "x",
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "session_identity": {"launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", "repo": "scratch", "branch": "main", "starting_head": head, "native_session_id": None},
                "created_at": "t", "started_at": None, "finished_at": None, "max_turns": 1, "run_budget_seconds": 30,
                "receipt": None, "log_ref": str(jdir / "stdout.log"), "error": None, "webhook": None,
                "response_webhook_url": None, "git_baseline": baseline, "writer_parent_paths": [],
            }
            (jdir / "job.json").write_text(json.dumps(job))
            class FakeProc:
                pid = 42
                returncode = 0
                def wait(self, timeout=None):
                    (repo / "dirty.txt").write_text("writer payload must not leak\n")
                    (jdir / "stdout.log").write_text(
                        '{"type":"thread.started","thread_id":"codex-123"}\n'
                        + json.dumps({"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}'}})
                    )
                    return 0
            binary = root / "codex"
            binary.write_text("fixture")
            _stamp_fixture_execution_profile(job, binary)
            (jdir / "job.json").write_text(json.dumps(job))
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=binary), \
                 mock.patch.object(server, "spawn_writer", return_value=(FakeProc(), None)):
                server.run_agent_job(job_id)
                saved = server.read_job(job_id)
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["error"], "writer changed pre-existing dirty paths: dirty.txt")
            self.assertNotIn("payload", json.dumps(saved["receipt"]))
            self.assertNotIn("operator private", json.dumps(saved["receipt"]))

    def test_writer_pathspec_file_is_bounded_and_nul_literal(self):
        paths = [f"dir/file-{i:05d}[x].txt" for i in range(5000)]
        captured = {}
        def fake_run(args, **kwargs):
            captured["args"] = args
            option = next(arg for arg in args if arg.startswith("--pathspec-from-file="))
            source = Path(option.split("=", 1)[1])
            captured["mode"] = stat.S_IMODE(source.parent.stat().st_mode)
            captured["data"] = source.read_bytes()
            return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "truncated": False}
        with mock.patch.object(server, "run_cmd", side_effect=fake_run):
            result = server.run_git_pathspec(Path("/repo"), ["add"], paths)
        self.assertTrue(result["ok"])
        self.assertLess(len(captured["args"]), 10)
        self.assertEqual(captured["mode"], 0o700)
        self.assertEqual(captured["data"].count(b"\0"), len(paths))
        self.assertIn(b":(literal)dir/file-00000[x].txt\0", captured["data"])

    def test_agent_run_rejects_overlapping_ordinary_writer_for_same_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            codex = root / "codex"
            codex.write_text("fixture")
            body = json.dumps({"prompt": "x", "cwd": "scratch", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol"}).encode()
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                 mock.patch.object(server, "enqueue_agent", lambda job: None):
                first_status, first, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
                second_status, second, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
            self.assertEqual(first_status, 202, first)
            self.assertEqual(second_status, 409, second)
            self.assertEqual(second["code"], "writer_checkout_busy")
            self.assertEqual(json.loads((jobs / first["job_id"] / "job.json").read_text())["status"], "queued")

    def test_two_thread_same_checkout_admission_allows_exactly_one_writer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            codex = root / "codex"
            codex.write_text("fixture")
            body = json.dumps({"prompt": "x", "cwd": "scratch", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol"}).encode()
            barrier = threading.Barrier(3)
            results = []
            def launch():
                barrier.wait()
                results.append(server.dispatch("POST", "/v1/agent/run", {}, True, body)[:2])
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                 mock.patch.object(server, "enqueue_agent", lambda job: None):
                threads = [threading.Thread(target=launch) for _ in range(2)]
                for thread in threads: thread.start()
                barrier.wait()
                for thread in threads: thread.join(timeout=5)
            self.assertEqual(sorted(status for status, _ in results), [202, 409])
            self.assertEqual(len(list(jobs.glob("*/job.json"))), 1)
            blocker = next(payload for status, payload in results if status == 409)
            accepted = next(payload for status, payload in results if status == 202)
            self.assertEqual(blocker["blocker_kind"], "agent")
            self.assertEqual(blocker["blocker_job_id"], accepted["job_id"])

    def test_script_job_blocker_is_identified_without_reaping_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            blocker_id = "93" * 16
            (jobs / blocker_id).mkdir(parents=True)
            (jobs / blocker_id / "job.json").write_text(json.dumps({
                "job_id": blocker_id, "kind": "script", "status": "running", "cwd": str(repo),
            }))
            codex = root / "codex"
            codex.write_text("fixture")
            body = json.dumps({"prompt": "x", "cwd": "scratch", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol"}).encode()
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}):
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
            self.assertEqual(status, 409, payload)
            self.assertEqual((payload["blocker_kind"], payload["blocker_job_id"]), ("script", blocker_id))
            self.assertEqual(json.loads((jobs / blocker_id / "job.json").read_text())["status"], "running")

    def test_startup_recovery_cancels_interrupted_agents_delivers_webhook_and_unblocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            interrupted_id = "94" * 16
            jdir = jobs / interrupted_id
            jdir.mkdir(parents=True)
            job = {
                "job_id": interrupted_id, "kind": "agent", "status": "running", "repo": "scratch", "cwd": str(repo),
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "session_identity": {"launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", "repo": "scratch", "branch": "main", "starting_head": "0" * 40, "native_session_id": "codex-old"},
                "created_at": "t", "started_at": "t", "finished_at": None, "receipt": None,
                "log_ref": str(jdir / "stdout.log"), "error": "raw secret must disappear",
                "response_webhook_url": "https://example.invalid/resume", "response_webhook_token": "tok_secret", "webhook": None,
            }
            _stamp_fixture_execution_profile(job)
            (jdir / "job.json").write_text(json.dumps(job))
            queued_id = "90" * 16
            queued_dir = jobs / queued_id
            queued_dir.mkdir(parents=True)
            queued = {**job, "job_id": queued_id, "status": "queued", "started_at": None,
                      "response_webhook_url": None, "response_webhook_token": None,
                      "log_ref": str(queued_dir / "stdout.log")}
            (queued_dir / "job.json").write_text(json.dumps(queued))
            codex = root / "codex"
            codex.write_text("fixture")
            body = json.dumps({"prompt": "x", "cwd": "scratch", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol"}).encode()
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "post_receipt", return_value={"ok": True, "status": 200}) as posted:
                recovered = server.recover_interrupted_agent_jobs()
                saved = server.read_job(interrupted_id)
            self.assertEqual(recovered, 2)
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["receipt"]["status"], "error")
            self.assertEqual((saved["receipt"]["failure_class"], saved["receipt"]["failure_code"]), ("cancelled", "cancelled"))
            self.assertEqual(saved["error"], saved["receipt"]["diagnostic"]["summary"])
            self.assertNotIn("secret", json.dumps(saved["receipt"]))
            posted.assert_called_once()
            self.assertEqual(posted.call_args.args[1], saved["receipt"])
            with mock.patch.object(server, "JOBS_DIR", jobs):
                queued_saved = server.read_job(queued_id)
            self.assertEqual(queued_saved["status"], "failed")
            self.assertEqual(queued_saved["receipt"]["failure_code"], "cancelled")
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                 mock.patch.object(server, "enqueue_agent", lambda job: None):
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
            self.assertEqual(status, 202, payload)

    def test_main_binds_single_instance_socket_before_recovery(self):
        events = []
        class FakeHTTPD:
            def __init__(self, *args): events.append("bind")
            def serve_forever(self): events.append("serve")
        with mock.patch.object(server, "load_key", return_value=b"key"), \
             mock.patch.object(server, "load_repos", return_value={}), \
             mock.patch.object(server, "load_scripts", return_value={}), \
             mock.patch.object(server, "load_tests", return_value={}), \
             mock.patch.object(server, "load_rr", return_value={}), \
             mock.patch.object(server, "load_stacks", return_value=({}, set())), \
             mock.patch.object(server, "recover_interrupted_agent_jobs", side_effect=lambda: events.append("recover") or 0), \
             mock.patch.object(server, "ThreadingHTTPServer", FakeHTTPD), \
             mock.patch("builtins.print"):
            server.main()
        self.assertEqual(events, ["bind", "recover", "serve"])

    def test_startup_recovery_closes_every_process_job_kind_including_codeoff(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            agent_profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
            agent_identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                **_fixture_identity_profile(agent_profile), "repo": "scratch", "branch": "main",
                "starting_head": "0" * 40, "native_session_id": "codex-old",
            }
            fixtures = {
                "95" * 16: {"kind": "agent", "launcher": "codex", "provider": "openai",
                              "model": "gpt-5.6-sol", **agent_profile,
                              "session_identity": agent_identity},
                "96" * 16: {"kind": "script", "script": "lint"},
                "97" * 16: {"kind": "test", "script": "unit"},
                "98" * 16: {"kind": "review", "launcher": "claude", "provider": "anthropic", "model": "claude-opus-5"},
                "99" * 16: {"kind": "rr", "script": "rr"},
            }
            for job_id, extra in fixtures.items():
                path = jobs / job_id
                path.mkdir(parents=True)
                (path / "job.json").write_text(json.dumps({
                    "job_id": job_id, "status": "running", "repo": "scratch", "cwd": str(repo),
                    "log_ref": str(path / "stdout.log"),
                    "response_webhook_url": "https://example.invalid/resume",
                    "response_webhook_token": "secret", **extra,
                }))
            codeoff_id = "9a" * 16
            codeoff_dir = jobs / codeoff_id
            codeoff_dir.mkdir(parents=True)
            codeoff_profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-terra")
            codeoff_identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-terra",
                **_fixture_identity_profile(codeoff_profile),
                "repo": "codeoff:experiment-0001:author-1", "branch": "main",
                "starting_head": "0" * 40, "native_session_id": "codex-codeoff",
            }
            (codeoff_dir / "job.json").write_text(json.dumps({
                "job_id": codeoff_id, "kind": "agent", "status": "running",
                "repo": codeoff_identity["repo"], "cwd": str(root / "codeoff"),
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-terra",
                **codeoff_profile, "session_identity": codeoff_identity,
                "codeoff_workspace": {"experiment_id": "experiment-0001", "slot": "author-1"},
            }))
            persisted_before_delivery = []
            def post(_url, receipt, token=None):
                persisted_before_delivery.append(json.loads((jobs / receipt["job_id"] / "job.json").read_text())["status"])
                return {"ok": True, "status": 200}
            with mock.patch.object(server, "JOBS_DIR", jobs), mock.patch.object(server, "post_receipt", side_effect=post) as posted:
                self.assertEqual(server.recover_interrupted_agent_jobs(), 6)
            self.assertEqual(persisted_before_delivery, ["failed"] * 4)
            self.assertEqual(posted.call_count, 4)
            for job_id, extra in fixtures.items():
                saved = json.loads((jobs / job_id / "job.json").read_text())
                self.assertEqual(saved["status"], "failed", extra)
                self.assertLessEqual(len(saved["error"]), 500)
                self.assertNotIn("secret", json.dumps(saved["receipt"]))
                if extra.get("kind") == "review":
                    self.assertIsNone(saved["receipt"])
                    self.assertEqual(
                        saved["review_recovery_code"], "review_authority_unavailable"
                    )
                elif extra.get("kind") in {"script", "test", "rr"}:
                    self.assertEqual(saved["receipt"]["status"], "error")
                    self.assertEqual(saved["receipt"]["log_ref"], saved["log_ref"])
                else:
                    self.assertEqual(saved["receipt"]["failure_code"], "cancelled")
                    self.assertIsNotNone(saved["receipt"]["session_identity"])
            codeoff_saved = json.loads((codeoff_dir / "job.json").read_text())
            self.assertEqual(codeoff_saved["status"], "failed")
            self.assertEqual(codeoff_saved["receipt"]["failure_code"], "cancelled")
            codex = root / "codex"
            codex.write_text("fixture")
            body = json.dumps({"prompt": "x", "cwd": "scratch", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol"}).encode()
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                 mock.patch.object(server, "enqueue_agent", lambda job: None):
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
            self.assertEqual(status, 202, payload)

    def test_startup_recovery_ignores_malformed_records_without_crashing(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            malformed = {
                "a1" * 16: "not json",
                "a2" * 16: json.dumps(["not", "an", "object"]),
                "a3" * 16: json.dumps({"status": "running", "kind": "script"}),
                "a4" * 16: json.dumps({"job_id": "ff" * 16, "status": "queued", "kind": "test"}),
                "a5" * 16: json.dumps({"job_id": "a5" * 16, "status": "running", "kind": "unknown"}),
                "a6" * 16: json.dumps({"job_id": "a6" * 16, "status": "running", "kind": ["agent"]}),
                "a7" * 16: json.dumps({"job_id": "a7" * 16, "status": "queued", "kind": "agent", "launcher": {"name": "codex"}}),
            }
            for job_id, body in malformed.items():
                path = jobs / job_id
                path.mkdir(parents=True)
                (path / "job.json").write_text(body)
            with mock.patch.object(server, "JOBS_DIR", jobs), mock.patch.object(server, "post_receipt") as posted:
                self.assertEqual(server.recover_interrupted_agent_jobs(), 0)
            posted.assert_not_called()

    def test_agent_run_rejects_repo_alias_below_git_top_level(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            nested = repo / "nested"
            nested.mkdir()
            codex = root / "codex"
            codex.write_text("fixture")
            body = json.dumps({"prompt": "x", "cwd": "nested", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol"}).encode()
            with mock.patch.object(server, "load_repos", return_value={"nested": str(nested)}), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}):
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
            self.assertEqual(status, 400, payload)
            self.assertEqual(payload["code"], "repo_not_toplevel")

    def test_ordinary_commit_requires_nonempty_writer_job_id(self):
        for writer_value in (None, ""):
            with self.subTest(writer_value=writer_value), tempfile.TemporaryDirectory() as td:
                repo = self._scratch_git(Path(td))
                (repo / "staged.txt").write_text("staged\n")
                subprocess.run(["git", "-C", str(repo), "add", "--", "staged.txt"], check=True)
                body = {"repo": "scratch", "message": "staged"}
                if writer_value is not None:
                    body["writer_job_id"] = writer_value
                status, payload = server.git_commit(
                    json.dumps(body).encode(), {"scratch": str(repo)}
                )
                self.assertEqual((status, payload["code"]), (400, "writer_job_required"), payload)
                self.assertEqual(
                    subprocess.run(
                        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
                        check=True, capture_output=True, text=True,
                    ).stdout.strip(),
                    "1",
                )

    def test_writer_commit_rejects_cyclic_resume_descendants(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            job_id = "9b" * 16
            branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            job = {
                "job_id": job_id, "writer_parent_job_id": job_id, "status": "completed",
                "repo": "scratch", "cwd": str(repo), "receipt": {"status": "ok"},
                "session_identity": {"branch": branch, "starting_head": head}, "writer_paths": [],
            }
            with mock.patch.object(server, "JOBS_DIR", jobs):
                server.write_job(job)
                status, payload = server.git_commit(json.dumps({
                    "repo": "scratch", "message": "must not hang", "writer_job_id": job_id,
                }).encode(), {"scratch": str(repo)})
            self.assertEqual((status, payload["code"]), (409, "writer_job_mismatch"))

    def test_writer_commit_rejects_ambiguous_successful_resume_children(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            parent_id, child1_id, child2_id = "9c" * 16, "9d" * 16, "9e" * 16
            (repo / "operator.txt").write_text("staged\n")
            subprocess.run(["git", "-C", str(repo), "add", "--", "operator.txt"], check=True)
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            index = subprocess.run(
                ["git", "-C", str(repo), "write-tree"], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            parent = _minimal_agent_record(parent_id)
            children = [
                {**_minimal_agent_record(job_id), "writer_parent_job_id": parent_id}
                for job_id in (child1_id, child2_id)
            ]
            with mock.patch.object(server, "JOBS_DIR", jobs):
                for job in (parent, *children):
                    server.write_job(job)
                status, payload = server.git_commit(json.dumps({
                    "repo": "scratch", "message": "must stay ambiguous", "writer_job_id": parent_id,
                }).encode(), {"scratch": str(repo)})
            self.assertEqual((status, payload["code"]), (409, "writer_job_mismatch"))
            self.assertEqual(payload["error"], "writer resume chain is ambiguous")
            self.assertEqual(subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True,
            ).stdout.strip(), head)
            self.assertEqual(subprocess.run(
                ["git", "-C", str(repo), "write-tree"], check=True,
                capture_output=True, text=True,
            ).stdout.strip(), index)

    def test_writer_commit_rejects_post_receipt_mutation_and_dirty_add_overlap(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            (repo / "operator.txt").write_text("base\n")
            subprocess.run(["git", "-C", str(repo), "add", "--", "operator.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "operator fixture"], check=True, capture_output=True)
            (repo / "operator.txt").write_text("operator staged\n")
            subprocess.run(["git", "-C", str(repo), "add", "--", "operator.txt"], check=True)
            baseline = server.capture_writer_baseline(repo)[0]
            (repo / "writer.txt").write_text("receipt bytes\n")
            job_id = "92" * 16
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            job = {"job_id": job_id, "repo": "scratch", "cwd": str(repo), "status": "completed", "git_baseline": baseline, "writer_parent_paths": [], "receipt": {"status": "ok"}, "session_identity": {"branch": "main", "starting_head": head}}
            _stamp_complete_writer_evidence(job)
            self.assertIsNone(server.stage_writer_changes(job))
            operator_index = subprocess.run(["git", "-C", str(repo), "show", ":operator.txt"], check=True, capture_output=True).stdout
            jobs = root / "jobs"
            with mock.patch.object(server, "JOBS_DIR", jobs):
                server.write_job(job)
                overlap_status, overlap = server.git_commit(json.dumps({"repo": "scratch", "message": "no", "writer_job_id": job_id, "add": "operator.txt"}).encode(), {"scratch": str(repo)})
                (repo / "writer.txt").write_text("mutated after receipt\n")
                changed_status, changed = server.git_commit(json.dumps({"repo": "scratch", "message": "no", "writer_job_id": job_id}).encode(), {"scratch": str(repo)})
            self.assertEqual((overlap_status, overlap["code"]), (409, "writer_dirty_conflict"))
            self.assertEqual(overlap["paths"], ["operator.txt"])
            self.assertEqual(overlap["more_paths"], 0)
            self.assertEqual((changed_status, changed["code"]), (409, "writer_paths_changed"))
            self.assertEqual(changed["paths"], ["writer.txt"])
            self.assertEqual(changed["more_paths"], 0)
            self.assertEqual(subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip(), head)
            self.assertEqual(subprocess.run(["git", "-C", str(repo), "show", ":operator.txt"], check=True, capture_output=True).stdout, operator_index)

    def test_real_agent_resume_inherits_paths_and_ancestor_commit_uses_child_tip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            codex = root / "codex"
            codex.write_text("fixture")
            attempt = 0
            class FakeProc:
                pid = 42
                returncode = 0
                def __init__(self, job, number): self.job, self.number = job, number
                def wait(self, timeout=None):
                    target = repo / "writer.txt"
                    target.write_text("first\n" if self.number == 1 else "corrected\n")
                    if self.number == 1:
                        (repo / "ephemeral.txt").write_text("remove me\n")
                        (repo / "helper.py").write_text("rename me\n")
                    else:
                        (repo / "ephemeral.txt").unlink()
                        (repo / "helper.py").rename(repo / "util.py")
                        (repo / "child.txt").write_text("child\n")
                    jdir = jobs / self.job["job_id"]
                    (jdir / "stdout.log").write_text(
                        '{"type":"thread.started","thread_id":"codex-123"}\n'
                        + json.dumps({"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}'}})
                    )
                    return 0
            def spawn(job, binary):
                nonlocal attempt
                attempt += 1
                return FakeProc(job, attempt), None
            base = {"prompt": "x", "cwd": "scratch", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol"}
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                 mock.patch.object(server, "enqueue_agent", lambda job: server.run_agent_job(job["job_id"])), \
                 mock.patch.object(server, "spawn_writer", side_effect=spawn):
                status1, first, _ = server.dispatch("POST", "/v1/agent/run", {}, True, json.dumps(base).encode())
                first_job = server.read_job(first["job_id"])
                resume = {**base, "session_identity": first_job["session_identity"], "resume_job_id": first["job_id"]}
                status2, second, _ = server.dispatch("POST", "/v1/agent/run", {}, True, json.dumps(resume).encode())
                second_job = server.read_job(second["job_id"])
                commit_status, committed = server.git_commit(json.dumps({"repo": "scratch", "message": "corrected", "writer_job_id": first["job_id"]}).encode(), {"scratch": str(repo)})
            self.assertEqual((status1, status2, commit_status), (202, 202, 200), (first, second, committed))
            self.assertEqual(second_job["writer_parent_job_id"], first["job_id"])
            self.assertEqual(second_job["writer_paths"], ["child.txt", "util.py", "writer.txt"])
            self.assertEqual((repo / "writer.txt").read_text(), "corrected\n")
            self.assertFalse((repo / "ephemeral.txt").exists())
            self.assertFalse((repo / "helper.py").exists())
            self.assertEqual(subprocess.run(["git", "-C", str(repo), "show", "HEAD:writer.txt"], check=True, capture_output=True, text=True).stdout, "corrected\n")
            self.assertEqual(subprocess.run(["git", "-C", str(repo), "show", "HEAD:util.py"], check=True, capture_output=True, text=True).stdout, "rename me\n")

    def test_writer_staging_fails_closed_on_dirty_conflict_and_unsafe_path(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            (repo / "dirty.txt").write_text("base\n")
            subprocess.run(["git", "-C", str(repo), "add", "--", "dirty.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "dirty fixture"], check=True, capture_output=True)
            (repo / "dirty.txt").write_text("operator\n")
            subprocess.run(["git", "-C", str(repo), "add", "--", "dirty.txt"], check=True)
            baseline, err = server.capture_writer_baseline(repo)
            self.assertIsNone(err)
            before = subprocess.run(["git", "-C", str(repo), "show", ":dirty.txt"], check=True, capture_output=True).stdout
            (repo / "dirty.txt").write_text("writer collision\n")
            job = {"cwd": str(repo), "git_baseline": baseline, "writer_parent_paths": []}
            conflict = server.stage_writer_changes(job)
            self.assertEqual(conflict["code"], "writer_dirty_conflict")
            after = subprocess.run(["git", "-C", str(repo), "show", ":dirty.txt"], check=True, capture_output=True).stdout
            self.assertEqual(after, before)

            (repo / "dirty.txt").write_text("operator\n")
            outside = Path(td) / "outside.txt"
            outside.write_text("outside\n")
            baseline2 = server.capture_writer_baseline(repo)[0]
            (repo / "unsafe").symlink_to(outside)
            unsafe = server.stage_writer_changes({"cwd": str(repo), "git_baseline": baseline2, "writer_parent_paths": []})
            self.assertEqual(unsafe["code"], "bad_path")

    def test_resume_writer_reowns_and_restages_corrected_paths(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            (repo / "fix.txt").write_text("base\n")
            (repo / "remove.txt").write_text("base\n")
            subprocess.run(["git", "-C", str(repo), "add", "--", "fix.txt", "remove.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "resume fixtures"], check=True, capture_output=True)
            first = {"cwd": str(repo), "git_baseline": server.capture_writer_baseline(repo)[0], "writer_parent_paths": []}
            (repo / "fix.txt").write_text("first attempt\n")
            self.assertIsNone(server.stage_writer_changes(first))
            second = {
                "cwd": str(repo), "git_baseline": server.capture_writer_baseline(repo)[0],
                "writer_parent_paths": first["writer_paths"],
            }
            (repo / "fix.txt").write_text("corrected\n")
            (repo / "new.txt").write_text("new\n")
            (repo / "remove.txt").unlink()
            self.assertIsNone(server.stage_writer_changes(second))
            self.assertEqual(second["writer_paths"], ["fix.txt", "new.txt", "remove.txt"])
            self.assertEqual(subprocess.run(
                ["git", "-C", str(repo), "show", ":fix.txt"], check=True, capture_output=True, text=True
            ).stdout, "corrected\n")
            branch = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], check=True, capture_output=True, text=True).stdout.strip()
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            for job, job_id in ((first, "ab" * 16), (second, "cd" * 16)):
                job.update({
                    "job_id": job_id, "repo": "scratch", "status": "completed",
                    "receipt": {"status": "ok"},
                    "session_identity": {"branch": branch, "starting_head": head},
                })
                _stamp_complete_writer_evidence(job)
            second["writer_parent_job_id"] = first["job_id"]
            jobs = Path(td) / "jobs"
            with mock.patch.object(server, "JOBS_DIR", jobs):
                server.write_job(first)
                server.write_job(second)
                status, payload = server.git_commit(json.dumps({
                    "repo": "scratch", "message": "corrected writer", "writer_job_id": first["job_id"],
                }).encode(), {"scratch": str(repo)})
            self.assertEqual(status, 200, payload)
            committed = subprocess.run(
                ["git", "-C", str(repo), "show", "--format=", "--name-only", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.splitlines()
            self.assertEqual(set(committed), {"fix.txt", "new.txt", "remove.txt"})

    def test_spawn_codex_uses_supported_fixture_contract(self):
        captured = {}
        class FakePopen:
            pid = 7
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = list(cmd)
                captured["stdin"] = kwargs["stdin"].read().decode()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs"
            job_id = "ab" * 16
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            (jdir / "prompt.txt").write_text("fixture prompt")
            codex = root / "codex"
            codex.write_text("fixture")
            job = {
                "job_id": job_id, "cwd": td, "model": "gpt-5.6-sol",
                "launcher": "codex", "provider": "openai",
                "requested_effort": "default", "effective_effort": "high",
                "effort_source": "launcher_default",
                "session_identity": {"native_session_id": "codex-123"},
            }
            with mock.patch.object(server, "JOBS_DIR", jobs), mock.patch.object(server.subprocess, "Popen", FakePopen):
                proc, err = server.spawn_codex(job, codex)
                resume_cmd = captured["cmd"]
                initial_job = dict(job, session_identity={"native_session_id": None})
                initial_proc, initial_err = server.spawn_codex(initial_job, codex)
                initial_cmd = captured["cmd"]
        self.assertIsNone(err)
        self.assertIsNotNone(proc)
        cmd = resume_cmd
        self.assertEqual(cmd[:2], [str(codex), "exec"])
        self.assertIn("--json", cmd)
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "workspace-write")
        self.assertIn("model_reasoning_effort=high", cmd)
        self.assertNotIn("--cwd", cmd)
        self.assertNotIn("--prompt-file", cmd)
        self.assertEqual(cmd[cmd.index("resume") + 1], "codex-123")
        self.assertEqual(cmd[-1], "-")
        self.assertEqual(captured["stdin"], "fixture prompt")
        self.assertIsNone(initial_err)
        self.assertIsNotNone(initial_proc)
        self.assertNotIn("resume", initial_cmd)
        self.assertEqual(initial_cmd[-1], "-")

    def _adapter_contract_job(
        self, launcher, provider, model, requested, effective, source=None
    ):
        return {
            "job_id": "ab" * 16,
            "cwd": str(self.scratch),
            "launcher": launcher,
            "provider": provider,
            "model": model,
            "requested_effort": requested,
            "effective_effort": effective,
            "effort_source": source or (
                "explicit" if requested != "default" else "launcher_default"
            ),
            "max_turns": 12,
            "session_identity": {"native_session_id": None},
        }

    def test_codex_command_uses_effective_effort(self):
        for effort in ("low", "medium", "high", "max"):
            with self.subTest(effort=effort):
                job = self._adapter_contract_job(
                    "codex", "openai", "gpt-5.6-sol", effort, effort
                )
                cmd = server.codex_command(
                    job, Path("/fixture/job/prompt.txt"), "/fixture/repo", Path("/fixture/codex")
                )
                settings = [
                    cmd[index + 1]
                    for index, value in enumerate(cmd[:-1])
                    if value == "-c" and cmd[index + 1].startswith("model_reasoning_effort=")
                ]
                self.assertEqual(settings, [f"model_reasoning_effort={effort}"])

    def test_codex_review_uses_reviewer_effort(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            output = b'{"status":"ok","sha":null,"pr_url":null,"summary":"VERDICT: PASS"}\n'
            return subprocess.CompletedProcess(cmd, 0, output, b"")

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(server, "JOBS_DIR", Path(td) / "jobs"):
            with self._native_review_context(
                "codex", "openai", "gpt-5.6-sol", Path(__file__),
                diff_text="fixture", requested_effort="medium", effort_source="explicit",
            ) as context, mock.patch.object(server.subprocess, "run", side_effect=fake_run):
                result = server.native_review_result(context)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["requested_effort"], "medium")
        self.assertEqual(result["effective_effort"], "medium")
        self.assertEqual(
            [value for value in captured["cmd"] if value.startswith("model_reasoning_effort=")],
            ["model_reasoning_effort=medium"],
        )
        self.assertEqual(captured["cmd"][captured["cmd"].index("--sandbox") + 1], "read-only")

    def test_claude_command_uses_effective_effort(self):
        for effort in ("low", "medium", "high", "max"):
            with self.subTest(effort=effort):
                job = self._adapter_contract_job(
                    "claude", "anthropic", "claude-opus-5", effort, effort
                )
                cmd = server.claude_command(
                    job, "fixture prompt", "/fixture/repo", Path("/fixture/claude")
                )
                self.assertEqual(cmd[cmd.index("--effort") + 1], effort)
                self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
                self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Bash")
        default_job = self._adapter_contract_job(
            "claude", "anthropic", "claude-opus-5", "default", "default"
        )
        default_cmd = server.claude_command(
            default_job, "fixture prompt", "/fixture/repo", Path("/fixture/claude")
        )
        self.assertNotIn(
            "--effort",
            default_cmd,
        )
        self.assertEqual(default_cmd.count("--allowedTools"), 1)
        self.assertEqual(default_cmd[default_cmd.index("--allowedTools") + 1], "Bash")

    def test_claude_review_uses_reviewer_effort(self):
        job = self._adapter_contract_job(
            "claude", "anthropic", "claude-sonnet-5", "high", "high"
        )
        cmd = server.claude_command(
            job, "review fixture", "/fixture/repo", Path("/fixture/claude"),
            permission_mode="plan", output_format="text",
        )
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")
        self.assertNotIn("--allowedTools", cmd)

    def test_grok_acp_command_uses_effective_effort(self):
        job = self._adapter_contract_job(
            "grok", "xai", "grok-4.6", "default", "high"
        )
        cmd = server.grok_acp_command(job, Path("/fixture/grok"), mode="writer")
        self.assertEqual(
            cmd,
            [
                "/fixture/grok", "agent", "--always-approve", "--model", "grok-4.6",
                "--reasoning-effort", "high", "stdio",
            ],
        )
        self.assertNotIn("default", cmd)

    def test_grok_review_uses_reviewer_effort(self):
        job = self._adapter_contract_job(
            "grok", "xai", "grok-4.6", "default", "high"
        )
        cmd = server.grok_acp_command(job, Path("/fixture/grok"), mode="review")
        self.assertEqual(
            cmd,
            [
                "/fixture/grok", "agent", "--model", "grok-4.6",
                "--reasoning-effort", "high", "stdio",
            ],
        )
        self.assertNotIn("default", cmd)

    def test_grok_unsupported_effort_fails_before_process_launch(self):
        job = self._adapter_contract_job("grok", "xai", "grok-4.6", "max", "max")
        with mock.patch.object(
            server.subprocess, "Popen", side_effect=AssertionError("process must not launch")
        ) as popen:
            result = server.run_grok_acp(job, Path("/fixture/grok"), mode="writer")
        self.assertEqual(result, (1, False, None, "unsupported effort profile"))
        self.assertEqual(job["_adapter_failure_code"], "unsupported_effort")
        popen.assert_not_called()

    def test_all_native_adapters_bind_execution_profile(self):
        # Independent contract: do not derive expected rows from implementation tables.
        expected_profiles = {
            ("codex", "openai", "gpt-5.6-sol"): (
                ("default", "high", "launcher_default"),
                ("default", "high", "explicit"),
                ("low", "low", "explicit"), ("medium", "medium", "explicit"),
                ("high", "high", "explicit"), ("max", "max", "explicit"),
            ),
            ("codex", "openai", "gpt-5.6-terra"): (
                ("default", "high", "launcher_default"),
                ("default", "high", "explicit"),
                ("low", "low", "explicit"), ("medium", "medium", "explicit"),
                ("high", "high", "explicit"), ("max", "max", "explicit"),
            ),
            ("claude", "anthropic", "claude-opus-5"): (
                ("default", "default", "launcher_default"),
                ("default", "default", "explicit"),
                ("low", "low", "explicit"), ("medium", "medium", "explicit"),
                ("high", "high", "explicit"), ("max", "max", "explicit"),
            ),
            ("claude", "anthropic", "claude-sonnet-5"): (
                ("default", "default", "launcher_default"),
                ("default", "default", "explicit"),
                ("low", "low", "explicit"), ("medium", "medium", "explicit"),
                ("high", "high", "explicit"), ("max", "max", "explicit"),
            ),
            ("claude", "anthropic", "claude-fable-5"): (
                ("default", "default", "launcher_default"),
                ("default", "default", "explicit"),
                ("low", "low", "explicit"), ("medium", "medium", "explicit"),
                ("high", "high", "explicit"), ("max", "max", "explicit"),
            ),
            ("grok", "xai", "grok-4.6"): (
                ("default", "high", "launcher_default"),
                ("default", "high", "explicit"),
                ("low", "low", "explicit"), ("medium", "medium", "explicit"),
                ("high", "high", "explicit"),
            ),
        }
        self.assertEqual(
            server.EFFORT_PROFILES,
            {
                identity: {requested: effective for requested, effective, _ in rows}
                for identity, rows in expected_profiles.items()
            },
        )
        observed = 0
        for (launcher, provider, model), rows in expected_profiles.items():
            for requested, effective, source in rows:
                for mode in ("writer", "review"):
                    observed += 1
                    with self.subTest(
                        launcher=launcher, model=model, requested=requested,
                        source=source, mode=mode,
                    ):
                        job = self._adapter_contract_job(
                            launcher, provider, model, requested, effective, source
                        )
                        if launcher == "codex":
                            cmd = server.codex_command(
                                job, Path("/fixture/job/prompt.txt"), "/fixture/repo",
                                Path("/fixture/codex"),
                                sandbox="workspace-write" if mode == "writer" else "read-only",
                            )
                            tokens = [
                                (index, cmd[index + 1].split("=", 1)[1])
                                for index, value in enumerate(cmd[:-1])
                                if value == "-c" and cmd[index + 1].startswith("model_reasoning_effort=")
                            ]
                            permission = cmd[cmd.index("--sandbox") + 1]
                            self.assertEqual(len(tokens), 1)
                            self.assertLess(tokens[0][0], cmd.index("-C"))
                        elif launcher == "claude":
                            cmd = server.claude_command(
                                job, "fixture", "/fixture/repo", Path("/fixture/claude"),
                                permission_mode="acceptEdits" if mode == "writer" else "plan",
                                output_format="json" if mode == "writer" else "text",
                            )
                            positions = [i for i, value in enumerate(cmd) if value == "--effort"]
                            tokens = [(i, cmd[i + 1]) for i in positions]
                            permission = cmd[cmd.index("--permission-mode") + 1]
                            self.assertEqual(len(tokens), 0 if effective == "default" else 1)
                            if tokens:
                                self.assertLess(tokens[0][0], len(cmd) - 1)
                            allowed_tools = [i for i, value in enumerate(cmd) if value == "--allowedTools"]
                            self.assertEqual(len(allowed_tools), 1 if mode == "writer" else 0)
                            if allowed_tools:
                                self.assertEqual(cmd[allowed_tools[0] + 1], "Bash")
                        else:
                            cmd = server.grok_acp_command(
                                job, Path("/fixture/grok"), mode=mode
                            )
                            positions = [
                                i for i, value in enumerate(cmd) if value == "--reasoning-effort"
                            ]
                            tokens = [(i, cmd[i + 1]) for i in positions]
                            permission = (
                                "always-approve" if "--always-approve" in cmd else "read-only-acp"
                            )
                            self.assertEqual(len(tokens), 1)
                            self.assertLess(tokens[0][0], cmd.index("stdio"))
                            self.assertNotEqual(tokens[0][1], "default")
                        self.assertEqual(cmd[cmd.index("--model") + 1], model)
                        self.assertEqual(
                            [value for _, value in tokens],
                            [] if launcher == "claude" and effective == "default" else [effective],
                        )
                        self.assertEqual(job["provider"], provider)
                        self.assertEqual(job["requested_effort"], requested)
                        self.assertEqual(job["effective_effort"], effective)
                        self.assertEqual(job["effort_source"], source)
                        self.assertEqual(
                            permission,
                            {
                                ("codex", "writer"): "workspace-write",
                                ("codex", "review"): "read-only",
                                ("claude", "writer"): "acceptEdits",
                                ("claude", "review"): "plan",
                                ("grok", "writer"): "always-approve",
                                ("grok", "review"): "read-only-acp",
                            }[(launcher, mode)],
                        )
        self.assertEqual(observed, 70)

    def test_native_adapter_source_has_no_dead_hardcoded_effort_setting(self):
        source = Path(server.__file__).read_text()
        self.assertNotIn("model_reasoning_effort=high", source)
        self.assertEqual(source.count('f"model_reasoning_effort={effort}"'), 1)

    def test_spawn_claude_uses_json_resume_contract(self):
        captured = {}

        class FakePopen:
            pid = 7

            def __init__(self, cmd, **kwargs):
                captured["cmd"] = list(cmd)
                captured["env"] = kwargs["env"]

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs"
            job_id = "ab" * 16
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            (jdir / "prompt.txt").write_text("fixture prompt")
            claude = root / "claude"
            claude.write_text("fixture")
            job = {
                "job_id": job_id, "cwd": td, "model": "claude-opus-5",
                "launcher": "claude", "provider": "anthropic", "max_turns": 12,
                "requested_effort": "default", "effective_effort": "default",
                "effort_source": "launcher_default",
                "session_identity": {"native_session_id": "claude-123"},
                "codeoff_workspace": {"experiment_id": "experiment-0001", "slot": "author-1"},
            }
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server.subprocess, "Popen", FakePopen):
                proc, err = server.spawn_claude(job, claude)
                resume_cmd = captured["cmd"]
                initial_proc, initial_err = server.spawn_claude(
                    dict(job, session_identity={"native_session_id": None}), claude
                )
                initial_cmd = captured["cmd"]
        self.assertIsNone(err)
        self.assertIsNotNone(proc)
        self.assertEqual(resume_cmd[:2], [str(claude), "-p"])
        self.assertEqual(resume_cmd[resume_cmd.index("--output-format") + 1], "json")
        self.assertEqual(resume_cmd[resume_cmd.index("--resume") + 1], "claude-123")
        self.assertEqual(resume_cmd[-1], "fixture prompt")
        self.assertIsNone(initial_err)
        self.assertIsNotNone(initial_proc)
        self.assertNotIn("--resume", initial_cmd)
        self.assertEqual(captured["env"]["PYTHONDONTWRITEBYTECODE"], "1")

    def _write_grok_acp_fixture(self, root: Path) -> Path:
        fixture = root / "grok-fixture"
        fixture.write_text("""#!/usr/bin/env python3
import json, os, sys, tempfile, time
cfg = json.loads(os.environ.get("ACP_TEST_FIXTURE", "{}"))
capture = os.environ["ACP_TEST_CAPTURE"]
seen = {"pid":os.getpid(), "argv":sys.argv[1:], "cwd":os.getcwd(), "env":{key:os.environ.get(key) for key in ["GROK_DISABLE_AUTOUPDATER","PWD","TERMINAL_CWD","GRAPHWING_JOB_ID","GIT_TERMINAL_PROMPT","GH_PROMPT_DISABLED"]}, "requests":[]}
def save():
    with tempfile.NamedTemporaryFile("w", dir=os.path.dirname(capture), prefix=os.path.basename(capture) + ".", delete=False) as fh:
        json.dump(seen, fh)
    os.replace(fh.name, capture)
def send(value):
    print(json.dumps(value), flush=True)
def response_id(request):
    if cfg.get("response_id_method") == request["method"]:
        return cfg["response_id"]
    return request["id"]
save()
if cfg.get("stderr_bytes"):
    sys.stderr.write("x" * cfg["stderr_bytes"]); sys.stderr.flush()
while True:
    if cfg.get("stop_reading_after") == len(seen["requests"]):
        time.sleep(2); continue
    line = sys.stdin.readline()
    if not line: break
    request = json.loads(line)
    seen["requests"].append(request); save()
    method = request["method"]
    if cfg.get("exit_method") == method: sys.exit(cfg.get("exit_code", 7))
    if method == "authenticate" and cfg.get("wire_burst_record_bytes"):
        target = cfg["wire_burst_record_bytes"]
        notification = {"jsonrpc":"2.0","method":"_x.ai/models/update","params":{"padding":""}}
        raw = json.dumps(notification)
        notification["params"]["padding"] = "x" * max(0, target - len(raw))
        raw = json.dumps(notification)
        assert len(raw) == target
        response = {"jsonrpc":"2.0","id":response_id(request),"result":{}}
        sys.stdout.write(raw + "\\n" + json.dumps(response) + "\\n"); sys.stdout.flush()
        continue
    if cfg.get("malformed_method") == method:
        print("not-json", flush=True); continue
    if cfg.get("missing_result_method") == method:
        send({"jsonrpc":"2.0","id":response_id(request)}); continue
    if cfg.get("error_method") == method:
        error = {"code":cfg.get("error_code", -32000),"message":"fixture error"}
        if "error_status" in cfg: error["data"] = {"status":cfg["error_status"]}
        send({"jsonrpc":"2.0","id":response_id(request),"error":error})
        if cfg.get("exit_after_method") == method: sys.exit(cfg.get("exit_code", 7))
        continue
    if cfg.get("session_update_before_method") == method:
        params = {"update":{"sessionUpdate":"available_commands_update","availableCommands":[]}}
        if not cfg.get("omit_early_update_session_id"):
            params["sessionId"] = cfg.get("early_update_session_id", "grok-123")
        send({"jsonrpc":"2.0","method":"session/update","params":params})
    if method == "initialize":
        auth_methods = cfg["auth_methods"] if "auth_methods" in cfg else [{"id":"xai.api_key"},{"id":"cached_token"}]
        result = {"protocolVersion":cfg.get("protocol_version", 1),"authMethods":auth_methods,"agentCapabilities":{"loadSession":cfg.get("load_session", True)}}
        if cfg.get("omit_auth_methods"):
            result.pop("authMethods")
        if cfg.get("omit_protocol_version"):
            result.pop("protocolVersion")
        if cfg.get("initialize_padding_bytes"):
            result["fixturePadding"] = "x" * cfg["initialize_padding_bytes"]
    elif method == "authenticate": result = {}
    elif method == "session/new":
        for sid in cfg.get("session_new_update_ids", []):
            update = {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"available_commands_update","availableCommands":[]}}}
            if cfg.get("session_new_update_has_id"): update["id"] = 999
            send(update)
        result = {} if cfg.get("missing_session") else {"sessionId":cfg.get("session_id", "grok-123")}
    elif method == "session/load":
        for text in cfg.get("replay_chunks", []):
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":request["params"]["sessionId"],"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":text}}}})
        result = cfg.get("load_result", {})
    elif method == "session/prompt":
        if cfg.get("sleep"): time.sleep(cfg["sleep"])
        sid = cfg.get("update_session_id", request["params"]["sessionId"])
        if cfg.get("permission_request"):
            send({"jsonrpc":"2.0","id":99,"method":"session/request_permission","params":{"sessionId":sid,"toolCall":{"toolCallId":"tool-1","title":"write fixture file","kind":"edit","status":"pending"},"options":[{"optionId":"allow","name":"Allow","kind":"allow_once"}]}})
            response = sys.stdin.readline()
            if response:
                seen["permission_response"] = json.loads(response); save()
            sys.exit(0)
        if cfg.get("malformed_update"):
            send({"jsonrpc":"2.0","method":"session/update","params":{}})
        if cfg.get("foreign_update"):
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"grok-other","update":{"sessionUpdate":"plan"}}})
        if cfg.get("invalid_update_envelope"):
            send({"jsonrpc":"2.0","id":999,"method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"plan"}}})
        if cfg.get("untyped_update"):
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{}}})
        for _ in range(cfg.get("thought_chunks", 0)):
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"agent_thought_chunk","content":{"type":"text","text":"x"}}}})
        for text in cfg.get("chunks", ['{"status":"ok","sha":null,"pr_url":null,"summary":"done"}']):
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"agent_message_chunk","content":{"type":cfg.get("chunk_content_type", "text"),"text":text}}}})
        result = {"stopReason":cfg.get("stop_reason", "end_turn")}
        if "usage" in cfg: result["usage"] = cfg["usage"]
    else: result = {}
    send({"jsonrpc":"2.0","id":response_id(request),"result":result})
    if cfg.get("notification_after_method") == method:
        notification = {"jsonrpc":"2.0","method":cfg.get("notification_method", "_x.ai/mcp/servers_updated"), **({"id":99} if cfg.get("notification_has_id") else {})}
        notification.update(cfg.get("notification_fields", {}))
        send(notification)
    if cfg.get("exit_after_method") == method: sys.exit(cfg.get("exit_code", 7))
    if cfg.get("hang_after_method") == method:
        while True: time.sleep(1)
""")
        fixture.chmod(0o755)
        return fixture

    def _run_grok_fixture(
        self, cfg=None, resume=False, api_key=True, prompt="fixture prompt", model="grok-4.6",
        adapter_results=None,
    ):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        jobs = root / "jobs"
        job_id = "12" * 16
        jdir = jobs / job_id
        jdir.mkdir(parents=True)
        fixture = self._write_grok_acp_fixture(root)
        capture = root / "capture.json"
        native_session_id = "grok-123" if resume else None
        profile = _fixture_execution_profile("grok", "xai", model, fixture)
        identity = {
            "launcher": "grok", "provider": "xai", "model": model,
            **_fixture_identity_profile(profile),
            "repo": "scratch", "branch": "main", "starting_head": "0" * 40,
            "native_session_id": native_session_id,
        }
        job = {
            "job_id": job_id, "kind": "agent", "status": "queued", "repo": "scratch",
            "cwd": str(root), "prompt": prompt,
            "launcher": "grok", "provider": "xai", "model": model,
            **profile,
            "session_identity": identity, "created_at": "t",
            "started_at": None, "finished_at": None, "max_turns": 1,
            "run_budget_seconds": (cfg or {}).get("budget", 30), "receipt": None,
            "log_ref": str(jdir / "stdout.log"), "error": None, "webhook": None,
            "response_webhook_url": None,
        }
        (jdir / "job.json").write_text(json.dumps(job))
        (jdir / "prompt.txt").write_text(prompt)
        env = {
            "ACP_TEST_FIXTURE": json.dumps(cfg or {}),
            "ACP_TEST_CAPTURE": str(capture),
        }
        if api_key:
            env["XAI_API_KEY"] = "fixture-key"
        env["GRAPHWING_GROK_BIN"] = str(fixture)
        native_launchers = deepcopy(server.NATIVE_LAUNCHERS)
        native_launchers["grok"]["models"] = tuple(dict.fromkeys(
            (*native_launchers["grok"]["models"], model)
        ))
        effort_profiles = {
            **server.EFFORT_PROFILES,
            ("grok", "xai", model): {
                "default": "high", "low": "low", "medium": "medium", "high": "high",
            },
        }
        with mock.patch.object(server, "JOBS_DIR", jobs), \
             mock.patch.object(server, "NATIVE_LAUNCHERS", native_launchers), \
             mock.patch.object(server, "EFFORT_PROFILES", effort_profiles), \
             mock.patch.dict(os.environ, env, clear=False):
            if not api_key:
                os.environ.pop("XAI_API_KEY", None)
            if adapter_results is None:
                server.run_agent_job(job_id)
            else:
                run_grok_acp = server.run_grok_acp

                def capture_adapter_result(*args, **kwargs):
                    result = run_grok_acp(*args, **kwargs)
                    adapter_results.append(result)
                    return result

                with mock.patch.object(server, "run_grok_acp", side_effect=capture_adapter_result):
                    server.run_agent_job(job_id)
        return json.loads((jdir / "job.json").read_text()), json.loads(capture.read_text()), jdir

    def _run_grok_review_fixture(self, cfg=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        jobs = root / "jobs"
        fixture = self._write_grok_acp_fixture(root)
        capture = root / "capture.json"
        fixture_cfg = {
            "chunks": [
                '{"status":"ok","sha":null,"pr_url":null,"summary":"VERDICT: PASS"}'
            ],
            **(cfg or {}),
        }
        env = {
            "ACP_TEST_FIXTURE": json.dumps(fixture_cfg),
            "ACP_TEST_CAPTURE": str(capture),
            "XAI_API_KEY": "fixture-key",
        }
        env["GRAPHWING_GROK_BIN"] = str(fixture)
        with mock.patch.object(server, "JOBS_DIR", jobs), \
             mock.patch.dict(os.environ, env, clear=False):
            with self._native_review_context(
                "grok", "xai", "grok-4.6", fixture
            ) as context:
                result = server.native_review_result(context)
        return result, json.loads(capture.read_text())

    def test_grok_acp_exact_argv_env_wire_and_successful_receipt(self):
        saved, capture, jdir = self._run_grok_fixture({"usage": {
            "totalTokens": 24, "inputTokens": 18, "outputTokens": 6,
            "thoughtTokens": 3, "cachedReadTokens": 0, "cachedWriteTokens": 0,
        }})
        self.assertEqual(capture["argv"], [
            "agent", "--always-approve", "--model", "grok-4.6",
            "--reasoning-effort", "high", "stdio",
        ])
        self.assertEqual(capture["cwd"], str(jdir.parent.parent.resolve()))
        self.assertEqual(capture["env"], {
            "GROK_DISABLE_AUTOUPDATER": "1", "PWD": str(jdir.parent.parent.resolve()),
            "TERMINAL_CWD": str(jdir.parent.parent.resolve()), "GRAPHWING_JOB_ID": "12" * 16,
            "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1",
        })
        requests = capture["requests"]
        self.assertEqual([r["jsonrpc"] for r in requests], ["2.0"] * 4)
        self.assertEqual([r["id"] for r in requests], [1, 2, 3, 4])
        self.assertEqual([r["method"] for r in requests], [
            "initialize", "authenticate", "session/new", "session/prompt",
        ])
        self.assertEqual(requests[0]["params"], {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
        })
        self.assertEqual(requests[1]["params"], {"methodId": "xai.api_key", "_meta": {"headless": True}})
        self.assertEqual(requests[2]["params"], {"cwd": str(jdir.parent.parent.resolve()), "mcpServers": []})
        self.assertEqual(requests[3]["params"], {
            "sessionId": "grok-123", "prompt": [{"type": "text", "text": "fixture prompt"}],
        })
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["session_identity"]["native_session_id"], "grok-123")
        self.assertEqual(saved["receipt"]["summary"], "ok")
        self.assertEqual(saved["receipt"]["failure_class"], "none")
        self.assertEqual(saved["receipt"]["failure_code"], "none")
        self.assertFalse(saved["receipt"]["failover_eligible"])
        self.assertEqual(saved["receipt"]["usage"], {
            "usage_version": "normalized-usage-v1",
            "fresh_input_tokens": 18,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 6,
            "reasoning_tokens": 3,
            "provider_cost_usd": None,
            "wall_seconds": saved["receipt"]["usage"]["wall_seconds"],
            "turns_observed": None,
        })
        self.assertIsInstance(saved["receipt"]["usage"]["wall_seconds"], float)
        self.assertGreaterEqual(saved["receipt"]["usage"]["wall_seconds"], 0)
        self.assertIsNone(saved["receipt"]["usage_diagnostic"])
        self.assertNotIn("totalTokens", json.dumps(saved["receipt"]))
        self.assertEqual((jdir / "last-message.txt").read_text(), '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}')

    def test_grok_acp_accepts_exact_vendor_notification_methods(self):
        methods = (
            "_x.ai/mcp/servers_updated",
            "_x.ai/models/update",
            "_x.ai/settings/update",
            "_x.ai/announcements/update",
            "_x.ai/mcp_initialized",
            "_x.ai/mcp/init_progress",
            "_x.ai/mcp/server_status",
            "_x.ai/queue/changed",
            "_x.ai/session/prompt_complete",
            "_x.ai/session_notification",
            "_x.ai/sessions/changed",
        )
        for method in methods:
            with self.subTest(method=method):
                saved, _capture, _jdir = self._run_grok_fixture({
                    "notification_after_method": "authenticate",
                    "notification_method": method,
                })
                self.assertEqual(saved["status"], "completed")
                self.assertEqual(saved["receipt"]["summary"], "ok")

    def test_grok_acp_session_new_update_establishes_provisional_identity(self):
        saved, capture, _ = self._run_grok_fixture({
            "session_new_update_ids": ["grok-123"],
        })
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["session_identity"]["native_session_id"], "grok-123")
        prompt = next(r for r in capture["requests"] if r["method"] == "session/prompt")
        self.assertEqual(prompt["params"]["sessionId"], "grok-123")

    def test_grok_acp_session_new_provisional_identity_must_not_change(self):
        cases = {
            "subsequent_update": {
                "session_new_update_ids": ["grok-123", "grok-other"],
            },
            "session_new_result": {
                "session_new_update_ids": ["grok-123"],
                "session_id": "grok-other",
            },
        }
        for name, cfg in cases.items():
            with self.subTest(name=name):
                saved, capture, _ = self._run_grok_fixture(cfg)
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["receipt"]["failure_code"], "session_identity_mismatch")
                self.assertNotIn("session/prompt", [r["method"] for r in capture["requests"]])

    def test_grok_acp_does_not_return_or_record_unconfirmed_provisional_identity(self):
        adapter_results = []
        saved, capture, _ = self._run_grok_fixture({
            "session_new_update_ids": ["grok-provisional"],
            "missing_session": True,
        }, adapter_results=adapter_results)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["receipt"]["failure_code"], "session_identity_missing")
        self.assertIsNone(adapter_results[0][2])
        self.assertIsNone(saved["session_identity"]["native_session_id"])
        self.assertIsNone(saved["receipt"]["session_identity"]["native_session_id"])
        self.assertNotIn("session/prompt", [r["method"] for r in capture["requests"]])

    def test_grok_acp_rejects_invalid_session_new_provisional_identity(self):
        cases = {
            "malformed_id": {"session_new_update_ids": ["not a native id"]},
            "null_id": {
                "session_update_before_method": "session/new",
                "early_update_session_id": None,
            },
            "absent_id": {
                "session_update_before_method": "session/new",
                "omit_early_update_session_id": True,
            },
            "response_shaped": {
                "session_new_update_ids": ["grok-123"],
                "session_new_update_has_id": True,
            },
        }
        for name, cfg in cases.items():
            with self.subTest(name=name):
                saved, capture, _ = self._run_grok_fixture(cfg)
                self.assertEqual(saved["status"], "failed")
                self.assertNotIn("session/prompt", [r["method"] for r in capture["requests"]])

    def test_grok_acp_does_not_establish_identity_before_other_responses(self):
        cases = {
            "valid_id": {},
            "null_id": {"early_update_session_id": None},
            "absent_id": {"omit_early_update_session_id": True},
        }
        for name, cfg in cases.items():
            with self.subTest(name=name):
                saved, capture, _ = self._run_grok_fixture({
                    "session_update_before_method": "authenticate",
                    **cfg,
                })
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["receipt"]["failure_code"], "session_identity_mismatch")
                self.assertNotIn("session/new", [r["method"] for r in capture["requests"]])

    def test_grok_acp_rejects_vendor_notifications_with_response_fields(self):
        methods = (
            "_x.ai/mcp/servers_updated",
            "_x.ai/models/update",
            "_x.ai/settings/update",
            "_x.ai/announcements/update",
            "_x.ai/mcp_initialized",
            "_x.ai/mcp/init_progress",
            "_x.ai/mcp/server_status",
            "_x.ai/queue/changed",
            "_x.ai/session/prompt_complete",
            "_x.ai/session_notification",
            "_x.ai/sessions/changed",
        )
        response_fields = (("id", 99), ("result", {}), ("error", {"code": -1}))
        for method in methods:
            for field, value in response_fields:
                with self.subTest(method=method, field=field):
                    saved, _capture, _jdir = self._run_grok_fixture({
                        "notification_after_method": "authenticate",
                        "notification_method": method,
                        "notification_fields": {field: value},
                    })
                    self.assertEqual(saved["status"], "failed")
                    self.assertEqual(saved["error"], saved["receipt"]["diagnostic"]["summary"])
                    self.assertNotIn("malformed Grok vendor notification", json.dumps(saved["receipt"]))

    def test_grok_acp_rejects_unknown_vendor_notification_method(self):
        methods = (
            "_x.ai/settings/unknown",
            "_x.ai/sessions/changed/extra",
            "_x.ai/queue/changed/extra",
            "_x.ai/session/prompt_complete/extra",
        )
        for method in methods:
            with self.subTest(method=method):
                saved, _capture, _jdir = self._run_grok_fixture({
                    "notification_after_method": "authenticate",
                    "notification_method": method,
                })
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["error"], saved["receipt"]["diagnostic"]["summary"])

    def test_grok_acp_command_uses_the_requested_immutable_model(self):
        saved, capture, _ = self._run_grok_fixture(model="grok-fixture-immutable")
        self.assertEqual(
            capture["argv"],
            [
                "agent", "--always-approve", "--model", "grok-fixture-immutable",
                "--reasoning-effort", "high", "stdio",
            ],
        )
        self.assertEqual(saved["session_identity"]["model"], "grok-fixture-immutable")

    def test_grok_acp_initializer_disables_unsupported_client_methods(self):
        _, capture, _ = self._run_grok_fixture()
        initialize = next(r for r in capture["requests"] if r["method"] == "initialize")
        self.assertEqual(initialize["params"]["clientCapabilities"], {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        })

    def test_grok_acp_accepts_structured_message_above_file_limit(self):
        saved, _, jdir = self._run_grok_fixture({
            "initialize_padding_bytes": server.FILE_MAX_BYTES,
        })
        first_line = (jdir / "stdout.log").read_bytes().splitlines()[0]
        self.assertGreater(len(first_line), server.FILE_MAX_BYTES)
        self.assertLess(len(first_line), server.CMD_MAX_BYTES)
        self.assertEqual(saved["status"], "completed")

    def test_grok_acp_accepts_valid_stream_above_cumulative_limit(self):
        thought_chunks = server.CMD_MAX_BYTES // 100
        saved, _, jdir = self._run_grok_fixture({
            "thought_chunks": thought_chunks,
        })
        logged = (jdir / "stdout.log").read_bytes()
        example = json.dumps({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "grok-123",
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "x"},
                },
            },
        }).encode() + b"\n"
        self.assertGreater(len(example) * thought_chunks, server.CMD_MAX_BYTES)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["receipt"]["summary"], "ok")
        self.assertTrue(logged.endswith(b"\n"))
        self.assertGreater(len(logged), server.CMD_MAX_BYTES - len(example))
        self.assertLessEqual(len(logged), server.CMD_MAX_BYTES)
        for line in logged.splitlines():
            self.assertIsInstance(json.loads(line), dict)

    def test_grok_acp_accepts_maximum_message_followed_by_response(self):
        saved, _, _ = self._run_grok_fixture({
            "wire_burst_record_bytes": server.CMD_MAX_BYTES,
        })
        self.assertEqual(saved["status"], "completed")

    def test_grok_acp_rejects_message_above_wire_limit(self):
        saved, _, _ = self._run_grok_fixture({
            "wire_burst_record_bytes": server.CMD_MAX_BYTES + 1,
        })
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error"], saved["receipt"]["diagnostic"]["summary"])

    def test_grok_acp_rejects_assistant_output_above_file_limit(self):
        saved, _, _ = self._run_grok_fixture({
            "chunks": ["x" * server.FILE_MAX_BYTES, "x"],
        })
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error"], saved["receipt"]["diagnostic"]["summary"])

    def test_grok_acp_resume_loads_only_after_capability_and_ignores_replay(self):
        saved, capture, _ = self._run_grok_fixture({
            "replay_chunks": ['{"status":"error","summary":"replayed"}'],
            "chunks": ['{"status":"ok","sha":null,"pr_url":null,"summary":"current"}'],
        }, resume=True)
        requests = capture["requests"]
        self.assertEqual([r["method"] for r in requests], [
            "initialize", "authenticate", "session/load", "session/prompt",
        ])
        self.assertEqual(requests[2]["params"], {
            "sessionId": "grok-123", "cwd": str(Path(saved["cwd"]).resolve()), "mcpServers": [],
        })
        self.assertEqual(saved["receipt"]["summary"], "ok")
        changed, _, _ = self._run_grok_fixture({
            "load_result": {"sessionId": "grok-other"},
        }, resume=True)
        self.assertEqual(changed["status"], "failed")
        self.assertEqual(changed["receipt"]["summary"], changed["receipt"]["diagnostic"]["summary"])
        self.assertNotIn("changed during load", json.dumps(changed["receipt"]))

    def test_grok_acp_uses_cached_auth_without_api_key(self):
        saved, capture, _ = self._run_grok_fixture(api_key=False)
        auth = next(r for r in capture["requests"] if r["method"] == "authenticate")
        self.assertEqual(auth["params"]["methodId"], "cached_token")
        self.assertEqual(saved["status"], "completed")

    def test_grok_acp_allows_omitted_auth_methods_without_authenticate(self):
        saved, capture, _ = self._run_grok_fixture({"omit_auth_methods": True})
        self.assertEqual(saved["status"], "completed")
        self.assertNotIn("authenticate", [r["method"] for r in capture["requests"]])

    def test_grok_acp_rejects_malformed_auth_methods(self):
        malformed = [
            False,
            {},
            "xai.api_key",
            [False],
            [{}],
            [{"id": ""}],
            [{"id": "   "}],
            [{"id": 7}],
            [{"id": "xai.api_key"}, {"id": "xai.api_key"}],
        ]
        for auth_methods in malformed:
            with self.subTest(auth_methods=auth_methods):
                saved, capture, _ = self._run_grok_fixture({"auth_methods": auth_methods})
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["error"], saved["receipt"]["diagnostic"]["summary"])
                self.assertNotIn("authenticate", [r["method"] for r in capture["requests"]])

    def test_grok_acp_rejects_selected_terminal_or_unknown_authentication(self):
        for method_type in ("terminal", "future-method"):
            with self.subTest(method_type=method_type):
                saved, capture, _ = self._run_grok_fixture({
                    "auth_methods": [{"id": "xai.api_key", "type": method_type}],
                })
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["error"], saved["receipt"]["diagnostic"]["summary"])
                self.assertNotIn("authenticate", [r["method"] for r in capture["requests"]])

    def test_grok_acp_rejects_boolean_response_id_for_integer_request(self):
        saved, _, _ = self._run_grok_fixture({
            "response_id_method": "initialize", "response_id": True,
        })
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error"], saved["receipt"]["diagnostic"]["summary"])

    def test_grok_acp_rejects_non_text_agent_message_chunk(self):
        saved, _, _ = self._run_grok_fixture({"chunk_content_type": "image"})
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error"], saved["receipt"]["diagnostic"]["summary"])

    def test_grok_acp_fail_closed_fixtures(self):
        cases = {
            "nonzero": ({"exit_method": "session/prompt"}, "process exited"),
            "jsonrpc_error": ({"error_method": "session/prompt"}, "session/prompt error"),
            "prose_only": ({"chunks": ["I finished the task"]}, "missing or invalid final receipt"),
            "status_only_receipt": ({"chunks": ['{"status":"ok"}']}, "missing or invalid final receipt"),
            "wrong_type_receipt": ({"chunks": ['{"status":"ok","sha":7,"pr_url":null,"summary":"done"}']}, "missing or invalid final receipt"),
            "non_string_pr_url_receipt": ({"chunks": ['{"status":"ok","sha":null,"pr_url":7,"summary":"done"}']}, "missing or invalid final receipt"),
            "non_string_summary_receipt": ({"chunks": ['{"status":"ok","sha":null,"pr_url":null,"summary":7}']}, "missing or invalid final receipt"),
            "blank_summary_receipt": ({"chunks": ['{"status":"ok","sha":null,"pr_url":null,"summary":"   "}']}, "missing or invalid final receipt"),
            "extra_field_receipt": ({"chunks": ['{"status":"ok","sha":null,"pr_url":null,"summary":"done","extra":true}']}, "missing or invalid final receipt"),
            "malformed_wire": ({"malformed_method": "initialize"}, "malformed Grok ACP wire"),
            "missing_result": ({"missing_result_method": "initialize"}, "invalid Grok ACP initialize response"),
            "missing_protocol_version": ({"omit_protocol_version": True}, "unsupported Grok ACP protocol version"),
            "unsupported_protocol_version": ({"protocol_version": 2}, "unsupported Grok ACP protocol version"),
            "boolean_protocol_version": ({"protocol_version": True}, "unsupported Grok ACP protocol version"),
            "missing_session": ({"missing_session": True}, "missing structured Grok session identity"),
            "changed_session": ({"update_session_id": "grok-other"}, "malformed or changed Grok session update"),
            "foreign_update": ({"foreign_update": True}, "malformed or changed Grok session update"),
            "malformed_update": ({"malformed_update": True}, "malformed or changed Grok session update"),
            "invalid_update_envelope": ({"invalid_update_envelope": True}, "malformed Grok session update"),
            "untyped_update": ({"untyped_update": True}, "malformed or changed Grok session update"),
            "non_end_turn": ({"stop_reason": "cancelled"}, "did not end_turn"),
            "unsupported_auth": ({"auth_methods": [{"id": "other"}]}, "unsupported Grok ACP authentication"),
        }
        for name, (fixture, summary) in cases.items():
            with self.subTest(name=name):
                saved, capture, _ = self._run_grok_fixture(fixture)
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["receipt"]["status"], "error")
                self.assertEqual(saved["error"], saved["receipt"]["diagnostic"]["summary"])
                self.assertNotIn(summary, json.dumps(saved["receipt"]))
                if name in {
                    "malformed_wire", "missing_result", "missing_protocol_version",
                    "unsupported_protocol_version", "boolean_protocol_version", "unsupported_auth",
                }:
                    self.assertNotIn("session/prompt", [r["method"] for r in capture["requests"]])
                if name in {
                    "status_only_receipt", "wrong_type_receipt", "non_string_pr_url_receipt",
                    "non_string_summary_receipt", "blank_summary_receipt", "extra_field_receipt",
                }:
                    self.assertIsNone(saved["session_identity"]["native_session_id"])

    def test_grok_failure_receipts_cover_non_failover_classes(self):
        fixtures = {
            "malformed_contract": ({"chunks": ["prose only"]}, "invalid_receipt"),
            "session_provenance": ({"load_result": {"sessionId": "grok-other"}}, "session_identity_mismatch"),
            "cancelled": ({"stop_reason": "cancelled"}, "cancelled"),
            "model_execution": ({"exit_method": "session/prompt"}, "process_exit"),
            "unknown": ({"error_method": "session/prompt"}, "unknown_failure"),
        }
        for expected, (fixture, code) in fixtures.items():
            with self.subTest(expected=expected):
                saved, _, _ = self._run_grok_fixture(
                    fixture, resume=expected == "session_provenance"
                )
                receipt = saved["receipt"]
                self.assertEqual((receipt["failure_class"], receipt["failure_code"]), (expected, code))
                self.assertFalse(receipt["failover_eligible"])

    def test_grok_acp_rejects_nonzero_exit_after_valid_prompt_result(self):
        saved, _, _ = self._run_grok_fixture({"exit_after_method": "session/prompt", "exit_code": 7})
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["returncode"], 7)
        self.assertEqual(saved["error"], saved["receipt"]["diagnostic"]["summary"])

    def test_grok_acp_protocol_error_preserves_summary_and_child_exit(self):
        saved, _, _ = self._run_grok_fixture({
            "error_method": "session/prompt",
            "exit_after_method": "session/prompt",
            "exit_code": 7,
        })
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["returncode"], 7)
        self.assertEqual(saved["error"], saved["receipt"]["diagnostic"]["summary"])

    def test_grok_structured_provider_error_is_failover_eligible(self):
        saved, _, _ = self._run_grok_fixture({
            "error_method": "session/prompt",
            "error_code": "rate_limit_exceeded",
        })
        self.assertEqual(saved["receipt"]["failure_class"], "provider_availability")
        self.assertEqual(saved["receipt"]["failure_code"], "provider_rate_limit")
        self.assertTrue(saved["receipt"]["failover_eligible"])
        self.assertEqual((saved["session_identity"]["native_session_id"], saved["receipt"]["session_identity"]["native_session_id"]), ("grok-123", "grok-123"))

    def test_grok_acp_bounds_stderr_log(self):
        saved, _, jdir = self._run_grok_fixture({"stderr_bytes": server.FILE_MAX_BYTES * 2})
        self.assertEqual(saved["status"], "completed")
        self.assertEqual((jdir / "stderr.log").stat().st_size, server.FILE_MAX_BYTES)

    def test_grok_acp_bounds_failed_stderr_and_preserves_protocol_summary(self):
        saved, _, jdir = self._run_grok_fixture({
            "stderr_bytes": server.FILE_MAX_BYTES * 2,
            "error_method": "session/prompt",
        })
        stderr = (jdir / "stderr.log").read_bytes()
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error"], saved["receipt"]["diagnostic"]["summary"])
        self.assertEqual(len(stderr), server.FILE_MAX_BYTES)
        self.assertTrue(stderr.endswith(b"Grok ACP session/prompt error\n"))

    def test_grok_acp_timeout_fails_closed_and_cleans_up(self):
        saved, capture, _ = self._run_grok_fixture({"budget": 1, "sleep": 1.2})
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["receipt"]["status"], "timeout")
        with self.assertRaises(ProcessLookupError):
            os.kill(capture["pid"], 0)

    def test_grok_acp_cleanup_timeout_preserves_valid_turn_and_kills_child(self):
        saved, capture, _ = self._run_grok_fixture({"hang_after_method": "session/prompt"})
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["receipt"]["status"], "ok")
        self.assertEqual(saved["receipt"]["summary"], "ok")
        with self.assertRaises(ProcessLookupError):
            os.kill(capture["pid"], 0)

    def test_grok_acp_deadline_bounds_blocked_prompt_write(self):
        saved, capture, _ = self._run_grok_fixture(
            {"budget": 1, "stop_reading_after": 3},
            prompt="x" * server.PROMPT_MAX_CHARS,
        )
        self.assertEqual(saved["receipt"]["status"], "timeout")
        with self.assertRaises(ProcessLookupError):
            os.kill(capture["pid"], 0)

    def test_grok_resume_requires_load_capability(self):
        saved, capture, _ = self._run_grok_fixture({"load_session": False}, resume=True)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual((saved["receipt"]["failure_class"], saved["receipt"]["failure_code"]),
                         ("session_provenance", "session_provenance_invalid"))
        self.assertNotIn("session/load", [r["method"] for r in capture["requests"]])

    def test_native_job_env_overrides_terminal_cwd(self):
        with mock.patch.dict(os.environ, {"TERMINAL_CWD": "/home/tim/rewst/riftwing", "PWD": "/home/tim", "PYTHONDONTWRITEBYTECODE": "inherited"}):
            env = server.native_job_env(
                {"job_id": "ab" * 16, "cwd": "/home/tim/work/gw-real-slice"}
            )
            codeoff_env = server.native_job_env({
                "job_id": "cd" * 16, "cwd": "/home/tim/work/gw-codeoff",
                "codeoff_workspace": {"experiment_id": "experiment-0001", "slot": "author-1"},
            })
        self.assertEqual(env["TERMINAL_CWD"], "/home/tim/work/gw-real-slice")
        self.assertEqual(env["PWD"], "/home/tim/work/gw-real-slice")
        self.assertNotEqual(env.get("TERMINAL_CWD"), "/home/tim/rewst/riftwing")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "inherited")
        self.assertEqual(codeoff_env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_run_agent_preserves_specific_spawn_error_in_saved_and_posted_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            codex = Path(td) / "codex"
            codex.write_text("fixture")
            job_id = "ab" * 16
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            job = {
                "job_id": job_id, "status": "queued", "repo": "scratch",
                "cwd": str(td), "prompt": "ping",
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "session_identity": {"launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", "repo": "scratch", "branch": "main", "starting_head": "0" * 40, "native_session_id": None},
                "response_webhook_url": "https://example.invalid/resume",
                "response_webhook_token": "tok_secret", "created_at": "t",
                "started_at": None, "finished_at": None, "max_turns": 1,
                "run_budget_seconds": 5, "receipt": None,
                "log_ref": str(jdir / "stdout.log"), "error": None, "webhook": None,
            }
            _stamp_fixture_execution_profile(job, codex)
            (jdir / "job.json").write_text(json.dumps(job))
            specific = "missing codex binary: /fixture/codex"
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                 mock.patch.object(server, "spawn_writer", return_value=(None, {"error": specific, "code": "missing_binary"})), \
                 mock.patch.object(server, "post_receipt", return_value={"ok": True, "status": 200}) as posted:
                server.run_agent_job(job_id)
            saved = json.loads((jdir / "job.json").read_text())
            self.assertEqual(saved["receipt"]["summary"], saved["receipt"]["diagnostic"]["summary"])
            self.assertEqual(saved["error"], specific)
            posted.assert_called_once()
            self.assertEqual(posted.call_args.args[1]["summary"], saved["receipt"]["diagnostic"]["summary"])
            self.assertEqual(posted.call_args.args[1], saved["receipt"])
            with mock.patch.object(server, "JOBS_DIR", jobs):
                public = server.public_job(server.read_job(job_id))
            self.assertEqual(public["receipt"], saved["receipt"])
            self.assertEqual(saved["receipt"]["failure_class"], "provider_availability")
            self.assertEqual(saved["receipt"]["failure_code"], "missing_binary")
            self.assertTrue(saved["receipt"]["failover_eligible"])

    def test_codex_job_completes_only_with_matching_session_and_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs"
            job_id = "cd" * 16
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            identity = {"launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", "repo": "scratch", "branch": "main", "starting_head": "0" * 40, "native_session_id": None}
            job = {"job_id": job_id, "status": "queued", "repo": "scratch", "cwd": td, "prompt": "x", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", "session_identity": identity, "created_at": "t", "started_at": None, "finished_at": None, "max_turns": 1, "run_budget_seconds": 30, "receipt": None, "log_ref": str(jdir / "stdout.log"), "error": None, "webhook": None, "response_webhook_url": None}
            (jdir / "job.json").write_text(json.dumps(job))
            (jdir / "prompt.txt").write_text("x")
            codex = root / "codex"
            codex.write_text("fixture")
            _stamp_fixture_execution_profile(job, codex)
            (jdir / "job.json").write_text(json.dumps(job))
            class FakeProc:
                pid = 42
                returncode = 0
                def wait(self, timeout=None):
                    (jdir / "stdout.log").write_text(
                        '{"type":"thread.started","thread_id":"codex-123"}\n'
                        + ("fixture-noise" * (server.CMD_MAX_BYTES // 10)) + "\n"
                        + json.dumps({"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}'}})
                    )
                    return 0
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                 mock.patch.object(server, "spawn_writer", return_value=(FakeProc(), None)):
                server.run_agent_job(job_id)
            saved = json.loads((jdir / "job.json").read_text())
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["receipt"]["summary"], "ok")
        self.assertEqual(saved["session_identity"]["native_session_id"], "codex-123")
        self.assertEqual(saved["receipt"]["session_identity"]["native_session_id"], "codex-123")

    def test_agent_worker_fails_closed_when_persisted_execution_identity_drifts(self):
        cases = {
            "coordinated_profile": lambda saved: (
                saved.update({"requested_effort": "high", "effort_source": "explicit"}),
                saved["session_identity"].update({"requested_effort": "high", "effort_source": "explicit"}),
            ),
            "single_profile_field": lambda saved: saved.__setitem__("requested_effort", "high"),
            "missing_kind": lambda saved: saved.pop("kind"),
            "alien_kind": lambda saved: saved.__setitem__("kind", "script"),
        }
        for index, (case, mutate) in enumerate(cases.items()):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                jobs = root / "jobs"
                job_id = f"{index + 1:02x}" * 16
                jdir = jobs / job_id
                jdir.mkdir(parents=True)
                binary = root / "codex"
                binary.write_text("fixture")
                identity = {
                    "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                    "repo": "scratch", "branch": "main", "starting_head": "0" * 40,
                    "native_session_id": None,
                }
                job = {
                    "job_id": job_id, "kind": "agent", "status": "queued", "repo": "scratch",
                    "cwd": td, "prompt": "x", "launcher": "codex", "provider": "openai",
                    "model": "gpt-5.6-sol", "session_identity": identity, "created_at": "t",
                    "started_at": None, "finished_at": None, "max_turns": 1,
                    "run_budget_seconds": 30, "receipt": None,
                    "log_ref": str(jdir / "stdout.log"), "error": None, "webhook": None,
                    "response_webhook_url": "https://example.invalid/callback",
                    "response_webhook_token": None,
                }
                _stamp_fixture_execution_profile(job, binary)
                (jdir / "job.json").write_text(json.dumps(job))

                class DriftProc:
                    pid = 42
                    returncode = 0
                    def wait(self, timeout=None):
                        saved = json.loads((jdir / "job.json").read_text())
                        mutate(saved)
                        (jdir / "job.json").write_text(json.dumps(saved))
                        (jdir / "stdout.log").write_text(
                            '{"type":"thread.started","thread_id":"codex-race"}\n'
                            + json.dumps({"type": "item.completed", "item": {
                                "type": "agent_message",
                                "text": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}',
                            }})
                        )
                        return 0

                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(binary)}), \
                     mock.patch.object(server, "spawn_writer", return_value=(DriftProc(), None)), \
                     mock.patch.object(server, "post_receipt", return_value={"ok": True}) as posted, \
                     mock.patch.object(server, "herdr_job_done"):
                    server.run_agent_job(job_id)
                    saved = server.read_job(job_id)
                if case in {"missing_kind", "alien_kind"}:
                    self.assertEqual(saved["status"], "running", saved)
                    self.assertIsNone(saved["receipt"], saved)
                    posted.assert_not_called()
                else:
                    self.assertEqual((saved["status"], saved["receipt"]["status"]), ("failed", "error"), saved)
                    self.assertEqual(saved["receipt"]["failure_code"], "session_provenance_invalid")
                    self.assertEqual(posted.call_args.args[1], saved["receipt"])

    def test_agent_worker_preserves_safe_operational_refresh_without_identity_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs"
            job_id = "9a" * 16
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            binary = root / "codex"
            binary.write_text("fixture")
            identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "repo": "scratch", "branch": "main", "starting_head": "0" * 40,
                "native_session_id": None,
            }
            job = {
                "job_id": job_id, "kind": "agent", "status": "queued", "repo": "scratch",
                "cwd": td, "prompt": "x", "launcher": "codex", "provider": "openai",
                "model": "gpt-5.6-sol", "session_identity": identity, "created_at": "t",
                "started_at": None, "finished_at": None, "max_turns": 1,
                "run_budget_seconds": 30, "receipt": None,
                "log_ref": str(jdir / "stdout.log"), "error": None, "webhook": None,
                "response_webhook_url": "https://example.invalid/callback",
                "response_webhook_token": None,
            }
            _stamp_fixture_execution_profile(job, binary)
            (jdir / "job.json").write_text(json.dumps(job))

            class OperationalProc:
                pid = 42
                returncode = 0
                def wait(self, timeout=None):
                    saved = json.loads((jdir / "job.json").read_text())
                    saved["webhook_attempts"] = 2
                    (jdir / "job.json").write_text(json.dumps(saved))
                    (jdir / "stdout.log").write_text(
                        '{"type":"thread.started","thread_id":"codex-safe"}\n'
                        + json.dumps({"type": "item.completed", "item": {
                            "type": "agent_message",
                            "text": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}',
                        }})
                    )
                    return 0

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(binary)}), \
                 mock.patch.object(server, "spawn_writer", return_value=(OperationalProc(), None)), \
                 mock.patch.object(server, "post_receipt", return_value={"ok": True}) as posted, \
                 mock.patch.object(server, "herdr_job_done"):
                server.run_agent_job(job_id)
                saved = server.read_job(job_id)
            self.assertEqual((saved["status"], saved["receipt"]["status"]), ("completed", "ok"), saved)
            self.assertEqual(saved["webhook_attempts"], 2)
            self.assertEqual(posted.call_args.args[1], saved["receipt"])

    def test_agent_worker_terminal_transition_rejects_replacement_after_postcheck(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs"
            job_id = "7b" * 16
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            binary = root / "codex"
            binary.write_text("fixture")
            identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "repo": "scratch", "branch": "main", "starting_head": "0" * 40,
                "native_session_id": None,
            }
            job = {
                "job_id": job_id, "kind": "agent", "status": "queued", "repo": "scratch",
                "cwd": td, "prompt": "x", "launcher": "codex", "provider": "openai",
                "model": "gpt-5.6-sol", "session_identity": identity, "created_at": "t",
                "started_at": None, "finished_at": None, "max_turns": 1,
                "run_budget_seconds": 30, "receipt": None,
                "log_ref": str(jdir / "stdout.log"), "error": None, "webhook": None,
                "response_webhook_url": "https://example.invalid/callback",
                "response_webhook_token": None,
            }
            _stamp_fixture_execution_profile(job, binary)
            (jdir / "job.json").write_text(json.dumps(job))

            process = mock.Mock(pid=42, returncode=0)
            process.wait.return_value = 0
            (jdir / "stdout.log").write_text(
                '{"type":"thread.started","thread_id":"codex-terminal-race"}\n'
                + json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message",
                    "text": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}',
                }})
            )
            original_match = server.agent_record_matches_execution_snapshot
            injected = False

            def replace_after_postcheck(current, snapshot):
                nonlocal injected
                matched = original_match(current, snapshot)
                if matched and current.get("status") == "running" and current.get("pid") == 42 and not injected:
                    injected = True
                    server.write_job({
                        "job_id": job_id, "kind": "script", "status": "failed",
                        "receipt": {"status": "error", "job_id": job_id},
                        "error": "trusted daemon replacement",
                    })
                return matched

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(binary)}), \
                 mock.patch.object(server, "spawn_writer", return_value=(process, None)), \
                 mock.patch.object(server, "agent_record_matches_execution_snapshot", side_effect=replace_after_postcheck), \
                 mock.patch.object(server, "post_receipt", return_value={"ok": True}) as posted, \
                 mock.patch.object(server, "herdr_job_done"):
                server.run_agent_job(job_id)
                saved = server.read_job(job_id)

            self.assertTrue(injected)
            self.assertEqual(saved["kind"], "script", saved)
            self.assertEqual(saved["status"], "failed", saved)
            self.assertEqual(saved["receipt"]["status"], "error", saved)
            posted.assert_not_called()

    def test_agent_terminal_transition_does_not_overwrite_reentrant_staging_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            job_id = "7c" * 16
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            binary = root / "codex"
            binary.write_text("fixture")
            branch = subprocess.run(
                ["git", "-C", str(repo), "branch", "--show-current"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "repo": "scratch", "branch": branch, "starting_head": head,
                "native_session_id": None,
            }
            job = {
                "job_id": job_id, "kind": "agent", "status": "queued", "repo": "scratch",
                "cwd": str(repo), "prompt": "x", "launcher": "codex", "provider": "openai",
                "model": "gpt-5.6-sol", "session_identity": identity, "created_at": "t",
                "started_at": None, "finished_at": None, "max_turns": 1,
                "run_budget_seconds": 30, "receipt": None,
                "log_ref": str(jdir / "stdout.log"), "error": None, "webhook": None,
                "response_webhook_url": "https://example.invalid/callback",
                "response_webhook_token": None,
                "git_baseline": server.capture_writer_baseline(repo)[0],
                "writer_parent_paths": [],
            }
            _stamp_fixture_execution_profile(job, binary)
            (jdir / "job.json").write_text(json.dumps(job))
            process = mock.Mock(pid=42, returncode=0)
            process.wait.return_value = 0
            (jdir / "stdout.log").write_text(
                '{"type":"thread.started","thread_id":"codex-stage-race"}\n'
                + json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message",
                    "text": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}',
                }})
            )
            alien = {
                "job_id": job_id, "kind": "script", "status": "failed",
                "receipt": {"status": "error", "job_id": job_id},
                "error": "trusted staging replacement",
            }
            stage_entered = False

            def replace_during_staging(_candidate):
                nonlocal stage_entered
                stage_entered = True
                self.assertTrue(server.write_job(alien))
                return None

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(binary)}), \
                 mock.patch.object(server, "spawn_writer", return_value=(process, None)), \
                 mock.patch.object(server, "stage_writer_changes", side_effect=replace_during_staging), \
                 mock.patch.object(server, "post_receipt", return_value={"ok": True}) as posted, \
                 mock.patch.object(server, "herdr_job_done"):
                server.run_agent_job(job_id)
                saved = server.read_job(job_id)

            self.assertTrue(stage_entered)
            self.assertEqual(saved, alien)
            posted.assert_not_called()

    def test_agent_success_is_immutable_before_callback_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs"
            job_id = "7d" * 16
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            binary = root / "codex"
            binary.write_text("fixture")
            identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "repo": "scratch", "branch": "main", "starting_head": "0" * 40,
                "native_session_id": None,
            }
            job = {
                "job_id": job_id, "kind": "agent", "status": "queued", "repo": "scratch",
                "cwd": td, "prompt": "x", "launcher": "codex", "provider": "openai",
                "model": "gpt-5.6-sol", "session_identity": identity, "created_at": "t",
                "started_at": None, "finished_at": None, "max_turns": 1,
                "run_budget_seconds": 30, "receipt": None,
                "log_ref": str(jdir / "stdout.log"), "error": None, "webhook": None,
                "response_webhook_url": "https://example.invalid/callback",
                "response_webhook_token": None,
            }
            _stamp_fixture_execution_profile(job, binary)
            (jdir / "job.json").write_text(json.dumps(job))
            process = mock.Mock(pid=42, returncode=0)
            process.wait.return_value = 0
            (jdir / "stdout.log").write_text(
                '{"type":"thread.started","thread_id":"codex-callback-race"}\n'
                + json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message",
                    "text": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}',
                }})
            )
            alien = {
                "job_id": job_id, "kind": "script", "status": "failed",
                "receipt": {"status": "error", "job_id": job_id},
                "error": "queued trusted replacement",
            }
            attempted = threading.Event()
            writer_done = threading.Event()
            writer_results = []
            callbacks = []
            writer_thread = None
            original_write = server._write_job_unlocked

            def queued_writer():
                attempted.set()
                writer_results.append(server.write_job(alien))
                writer_done.set()

            def queue_behind_terminal_commit(candidate):
                nonlocal writer_thread
                if candidate.get("kind") == "agent" and candidate.get("status") == "completed" and writer_thread is None:
                    writer_thread = threading.Thread(target=queued_writer, daemon=True)
                    writer_thread.start()
                    self.assertTrue(attempted.wait(0.5), "trusted writer did not reach the terminal lock")
                original_write(candidate)

            def deliver_after_queued_writer(_url, receipt, token=None):
                self.assertTrue(writer_done.wait(1.0), "queued writer did not finish before callback")
                callbacks.append(deepcopy(receipt))
                return {"ok": True, "status": 200}

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(binary)}), \
                 mock.patch.object(server, "spawn_writer", return_value=(process, None)), \
                 mock.patch.object(server, "_write_job_unlocked", side_effect=queue_behind_terminal_commit), \
                 mock.patch.object(server, "post_receipt", side_effect=deliver_after_queued_writer), \
                 mock.patch.object(server, "herdr_job_done"):
                server.run_agent_job(job_id)
                saved = server.read_job(job_id)

            self.assertIsNotNone(writer_thread)
            writer_thread.join(timeout=1.0)
            self.assertFalse(writer_thread.is_alive(), "trusted writer deadlocked behind terminal CAS")
            self.assertEqual(writer_results, [False])
            self.assertEqual(len(callbacks), 1)
            self.assertEqual(callbacks[0]["status"], "ok")
            self.assertEqual((saved["kind"], saved["status"]), ("agent", "completed"), saved)
            self.assertEqual(saved["receipt"], callbacks[0])
            self.assertEqual(saved["webhook"], {"ok": True, "status": 200})

    def test_native_writer_job_stages_server_attributed_paths_after_valid_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            codex = root / "codex"
            codex.write_text("fixture")

            class FakeProc:
                pid = 42
                returncode = 0

                def __init__(self, job):
                    self.job = job

                def wait(self, timeout=None):
                    (repo / "writer.txt").write_text("written\n")
                    jdir = jobs / self.job["job_id"]
                    (jdir / "stdout.log").write_text(
                        '{"type":"thread.started","thread_id":"codex-123"}\n'
                        + json.dumps({"type": "item.completed", "item": {
                            "id": "i", "type": "agent_message",
                            "text": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}',
                        }})
                    )
                    return 0

            body = json.dumps({
                "prompt": "write it", "cwd": "scratch", "launcher": "codex",
                "provider": "openai", "model": "gpt-5.6-sol",
            }).encode()
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                 mock.patch.object(server, "enqueue_agent", lambda job: server.run_agent_job(job["job_id"])), \
                 mock.patch.object(server, "spawn_writer", side_effect=lambda job, binary: (FakeProc(job), None)):
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
                saved = server.read_job(payload["job_id"])
            self.assertEqual(status, 202, payload)
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["writer_paths"], ["writer.txt"])
            self.assertEqual(subprocess.run(
                ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
                check=True, capture_output=True, text=True,
            ).stdout.strip(), "writer.txt")

    def test_claude_job_completes_only_with_structured_session_and_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs"
            job_id = "ef" * 16
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            identity = {
                "launcher": "claude", "provider": "anthropic", "model": "claude-opus-5",
                "repo": "scratch", "branch": "main", "starting_head": "0" * 40,
                "native_session_id": None,
            }
            job = {
                "job_id": job_id, "status": "queued", "repo": "scratch",
                "cwd": td, "prompt": "x", "launcher": "claude",
                "provider": "anthropic", "model": "claude-opus-5",
                "session_identity": identity, "created_at": "t",
                "started_at": None, "finished_at": None, "max_turns": 1,
                "run_budget_seconds": 30, "receipt": None,
                "log_ref": str(jdir / "stdout.log"), "error": None, "webhook": None,
                "response_webhook_url": None,
            }
            (jdir / "job.json").write_text(json.dumps(job))
            (jdir / "prompt.txt").write_text("x")
            claude = root / "claude"
            claude.write_text("fixture")
            _stamp_fixture_execution_profile(job, claude)
            (jdir / "job.json").write_text(json.dumps(job))

            class FakeProc:
                pid = 42
                returncode = 0

                def wait(self, timeout=None):
                    (jdir / "stdout.log").write_text(json.dumps({
                        "type": "result", "subtype": "success", "is_error": False,
                        "session_id": "claude-123",
                        "result": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}',
                    }))
                    return 0

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CLAUDE_BIN": str(claude)}), \
                 mock.patch.object(server, "spawn_writer", return_value=(FakeProc(), None)):
                server.run_agent_job(job_id)
            saved = json.loads((jdir / "job.json").read_text())
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["receipt"]["summary"], "ok")
        self.assertEqual(saved["session_identity"]["native_session_id"], "claude-123")
        self.assertEqual(saved["receipt"]["session_identity"]["native_session_id"], "claude-123")

    def test_agent_run_stores_webhook_hides_token(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            codex = Path(td) / "codex"
            codex.write_text("fixture")
            body = json.dumps(
                {
                    "prompt": "ping",
                    "launcher": "codex",
                    "provider": "openai",
                    "model": "gpt-5.6-sol",
                    "response_webhook_url": "https://app.rewst.ai/api/callbacks/11111111-1111-1111-1111-111111111111",
                    "response_webhook_token": "tok_secret",
                }
            ).encode()
            with mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), mock.patch.object(server, "JOBS_DIR", jobs):
                with mock.patch.object(server, "enqueue_agent", lambda job: None):
                    status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
            self.assertEqual(status, 202, payload)
            job_id = payload["job_id"]
            stored = json.loads((jobs / job_id / "job.json").read_text())
            self.assertEqual(
                stored["response_webhook_url"],
                "https://app.rewst.ai/api/callbacks/11111111-1111-1111-1111-111111111111",
            )
            self.assertEqual(stored["response_webhook_token"], "tok_secret")
            with mock.patch.object(server, "JOBS_DIR", jobs):
                _, gp, _ = server.dispatch("GET", f"/v1/agent/jobs/{job_id}", {}, True, b"")
            self.assertNotIn("response_webhook_token", gp)
            self.assertNotIn("response_webhook_url", gp)

    def test_post_receipt_sends_rewst_token(self):
        captured = {}

        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=15):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            return FakeResp()

        with mock.patch.object(server, "urlopen", fake_urlopen):
            out = server.post_receipt(
                "https://example.com/cb",
                {"status": "ok", "job_id": "ab" * 16},
                token="tok_secret",
            )
        self.assertTrue(out["ok"])
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(headers.get("x-rewst-token"), "tok_secret")

    def test_post_receipt_encoder_recursion_fails_closed_without_network(self):
        receipt = {
            "status": "ok", "job_id": "ab" * 16,
            "provider_output": "TOKEN_SECRET /private/provider/output",
        }
        with mock.patch.object(
            server.json, "dumps", side_effect=RecursionError("provider recursion")
        ), mock.patch.object(server, "urlopen") as posted:
            outcome = server.post_receipt("https://example.invalid/callback", receipt)
        self.assertEqual(
            outcome,
            {"ok": False, "error": "receipt serialization failed"},
        )
        posted.assert_not_called()
        self.assertNotIn("TOKEN_SECRET", str(outcome))
        self.assertNotIn("/private", str(outcome))

    def test_active_runtime_has_no_retired_model_runtime(self):
        root = Path(__file__).resolve().parent
        retired = "her" + "mes"
        active = [root / "server.py", root / "openapi.json", root / "install.py", root / "start.sh"]
        active.extend(sorted((root / "graphs").glob("*.json")))
        for path in active:
            self.assertNotIn(retired, path.read_text().lower(), str(path))

    def test_model_graph_nodes_pin_native_identity(self):
        root = Path(__file__).resolve().parent
        for graph_path in sorted((root / "graphs").glob("*.json")):
            graph = json.loads(graph_path.read_text())
            for node in graph["spec"]["nodes"]:
                if node["type"].endswith(("/v1/agent/run", "/v1/review/run")):
                    config = node.get("config", {})
                    for field in ("launcher", "provider", "model"):
                        self.assertIsInstance(config.get(field), str, (graph_path.name, node["id"], field))
                        self.assertTrue(config[field].strip(), (graph_path.name, node["id"], field))

    def test_openapi_file(self):
        status, payload, ctype = server.dispatch("GET", "/openapi.json", {}, False, b"")
        self.assertEqual(status, 200)
        spec = json.loads(payload)
        self.assertEqual(spec["info"]["title"], "graphwing")
        self.assertEqual(spec["info"]["version"], "0.5.5")
        self.assertEqual(spec["servers"][0]["url"], "http://127.0.0.1:8645")
        self.assertNotIn("tfour.net", spec["info"]["description"])
        self.assertNotIn("tim-graphwing", spec["info"]["description"])
        self.assertIn("/v1/stack/status", spec["paths"])
        self.assertIn("/v1/port/check", spec["paths"])
        self.assertIn("/v1/test/run", spec["paths"])
        self.assertIn("/v1/rr/run", spec["paths"])
        props = spec["paths"]["/v1/agent/run"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        if "$ref" in props:
            props = spec["components"]["schemas"]["AgentRunRequest"]["properties"]
        else:
            props = props["properties"]
        self.assertIn("response_webhook_url", props)
        self.assertIn("response_webhook_token", props)
        self.assertIn("session_identity", props)
        self.assertIn("resume_job_id", props)
        self.assertEqual(set(props["launcher"]["enum"]), {"claude", "codex", "grok"})
        commit_op = spec["paths"]["/v1/git/commit"]["post"]
        commit_request = spec["components"]["schemas"]["GitCommitRequest"]["properties"]
        self.assertEqual(commit_request["writer_job_id"]["pattern"], "^[0-9a-f]{32}$")
        self.assertIn("validated writer evidence", commit_op["summary"])
        self.assertIn("code-off", commit_op["summary"])
        self.assertIn("isolated index", commit_op["summary"])
        self.assertIn("code-off commits use the verified staged index", commit_request["add"]["description"])
        self.assertIn("isolated index", commit_request["add"]["description"])
        self.assertIn("409", commit_op["responses"])
        self.assertIn("409", spec["paths"]["/v1/agent/run"]["post"]["responses"])
        error_props = spec["components"]["schemas"]["Error"]["properties"]
        self.assertEqual(set(error_props["blocker_kind"]["enum"]), {"agent", "codeoff", "script", "test", "rr", "review"})
        self.assertEqual(error_props["blocker_job_id"]["pattern"], "^([0-9a-f]{32}|unknown)$")
        request_schema = spec["components"]["schemas"]["AgentRunRequest"]
        self.assertEqual(
            set(request_schema["required"]),
            {"launcher", "provider", "model"},
        )
        self.assertIn("codeoff_workspace", props)
        self.assertNotIn("default", props["cwd"])
        self.assertNotIn("oneOf", request_schema)
        self.assertNotIn("allOf", request_schema)
        self.assertFalse(request_schema["additionalProperties"])
        accepted_identity = spec["components"]["schemas"]["AgentRunAccepted"]["properties"]["session_identity"]
        self.assertEqual(accepted_identity["$ref"], "#/components/schemas/SessionIdentity")
        for schema_name in ("AgentJob", "AgentReceipt"):
            identity = spec["components"]["schemas"][schema_name]["properties"]["session_identity"]
            self.assertEqual(identity["oneOf"], [
                {"$ref": "#/components/schemas/SessionIdentity"}, {"type": "null"},
            ])
        receipt = spec["components"]["schemas"]["AgentReceipt"]["properties"]
        self.assertEqual(
            set(receipt["failure_class"]["enum"]),
            {"none", "provider_availability", "timeout_budget", "model_execution",
             "malformed_contract", "session_provenance", "repository_state",
             "cancelled", "unknown"},
        )
        for field in ("failure_class", "failure_code", "failover_eligible"):
            self.assertIn(field, receipt)
            self.assertIn(field, spec["components"]["schemas"]["Error"]["properties"])
        diagnostic = spec["components"]["schemas"]["Diagnostic"]
        self.assertEqual(
            set(diagnostic["required"]),
            {"diagnostic_version", "category", "component", "code", "evidence", "summary"},
        )
        self.assertEqual(diagnostic["properties"]["diagnostic_version"]["enum"], ["diagnostic-v1"])
        self.assertEqual(
            set(diagnostic["properties"]["category"]["enum"]),
            {metadata[0] for metadata in server.DIAGNOSTIC_METADATA.values()},
        )
        self.assertEqual(
            set(diagnostic["properties"]["component"]["enum"]),
            {metadata[1] for metadata in server.DIAGNOSTIC_METADATA.values()},
        )
        self.assertEqual(
            set(diagnostic["properties"]["code"]["enum"]),
            set(server.DIAGNOSTIC_METADATA),
        )
        for schema_name in ("AgentReceipt", "StackStatus", "PortCheck"):
            self.assertEqual(
                spec["components"]["schemas"][schema_name]["properties"]["diagnostic"]["$ref"],
                "#/components/schemas/Diagnostic",
            )
        self.assertNotIn("diagnostic", spec["components"]["schemas"]["Error"]["properties"])
        diagnostic_error = spec["components"]["schemas"]["DiagnosticError"]
        self.assertEqual(
            diagnostic_error["properties"]["diagnostic"]["$ref"],
            "#/components/schemas/Diagnostic",
        )
        for path in ("/v1/stack/status", "/v1/port/check"):
            responses = spec["paths"][path]["get"]["responses"]
            for status in ("400", "501"):
                self.assertEqual(
                    responses[status]["$ref"],
                    "#/components/responses/DiagnosticError",
                )
        self.assertEqual(
            spec["paths"]["/v1/agent/run"]["post"]["responses"]["501"]["$ref"],
            "#/components/responses/DiagnosticError",
        )
        self.assertEqual(set(receipt["role"]["enum"]), {"primary", "availability_fallback"})
        expected_codes = (set(server.FAILURE_CLASS_BY_CODE) - {"success"}) | {"none", "provider_http_5xx"} | {code for code, _ in server.PROVIDER_FAILURE_GROUPS}
        self.assertEqual(set(receipt["failure_code"]["enum"]), expected_codes)
        self.assertNotIn("profile", props)
        self.assertNotIn("/v1/agent/profiles", spec["paths"])
        self.assertEqual(set(props["provider"]["enum"]), {"openai", "anthropic", "xai"})
        identity = spec["components"]["schemas"]["SessionIdentity"]["properties"]
        self.assertEqual(set(identity["launcher"]["enum"]), {"codex", "claude", "grok"})
        self.assertEqual(set(identity["provider"]["enum"]), {"openai", "anthropic", "xai"})
        self.assertEqual(
            set(identity["model"]["enum"]),
            {"gpt-5.6-sol", "gpt-5.6-terra", "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "grok-4.6"},
        )
        self.assertIn("/v1/git/status", spec["paths"])
        self.assertIn("/v1/git/checkout", spec["paths"])
        self.assertIn("/v1/git/restore", spec["paths"])
        self.assertIn("/v1/git/commit", spec["paths"])
        self.assertIn("/v1/git/push", spec["paths"])
        self.assertIn("/v1/script/run", spec["paths"])
        self.assertIn("/v1/gh/pr/list", spec["paths"])
        self.assertIn("/v1/file/head", spec["paths"])
        self.assertIn("/v1/slice/frontier", spec["paths"])
        self.assertIn("/v1/slice/complete", spec["paths"])
        self.assertIn("/v1/slice/continue", spec["paths"])
        self.assertEqual(spec["paths"]["/v1/slice/frontier"]["get"]["operationId"], "sliceFrontier")
        self.assertEqual(spec["paths"]["/v1/slice/complete"]["post"]["operationId"], "sliceComplete")
        self.assertEqual(spec["paths"]["/v1/slice/continue"]["post"]["operationId"], "sliceContinue")
        self.assertEqual(spec["paths"]["/v1/slice/route"]["post"]["operationId"], "sliceRoute")
        self.assertEqual(spec["paths"]["/v1/slice/route/fallback"]["post"]["operationId"], "sliceRouteFallback")
        self.assertEqual(spec["paths"]["/v1/review/run"]["post"]["operationId"], "reviewRun")
        self.assertEqual(spec["paths"]["/v1/slice/e2e"]["post"]["operationId"], "sliceE2e")
        self.assertIn("/v1/agent/run", spec["paths"])
        self.assertIn("/v1/agent/jobs/{job_id}", spec["paths"])
        self.assertIn("/v1/watch", spec["paths"])
        self.assertEqual(spec["paths"]["/v1/watch"]["get"]["operationId"], "watch")
        doorbell = spec["paths"]["/v1/doorbell/pr-drive"]["post"]
        self.assertEqual(doorbell["operationId"], "doorbellPrDrive")
        self.assertEqual(doorbell["security"], [])
        self.assertEqual(spec["paths"]["/v1/git/checkout"]["post"]["operationId"], "gitCheckout")
        self.assertEqual(spec["paths"]["/v1/git/restore"]["post"]["operationId"], "gitRestore")
        self.assertEqual(spec["paths"]["/v1/git/commit"]["post"]["operationId"], "gitCommit")
        self.assertEqual(spec["paths"]["/v1/git/push"]["post"]["operationId"], "gitPush")
        self.assertEqual(spec["paths"]["/v1/script/run"]["post"]["operationId"], "scriptRun")
        self.assertIn("202", spec["paths"]["/v1/agent/run"]["post"]["responses"])
        self.assertIn("202", spec["paths"]["/v1/script/run"]["post"]["responses"])
        self.assertIn("response_webhook_url", spec["components"]["schemas"]["ScriptRunRequest"]["properties"])
        self.assertEqual(ctype, "application/json")

    def test_openapi_request_roots_are_rewst_importable(self):
        spec = json.loads(server.openapi_bytes())
        # Root combinators caused canary b7c619e4 to import agentRun without body fields.
        unsupported_root_keywords = {"oneOf", "allOf", "anyOf", "not", "if"}
        for path, operations in spec["paths"].items():
            for method, operation in operations.items():
                request_body = operation.get("requestBody") if isinstance(operation, dict) else None
                if not request_body:
                    continue
                content = request_body["content"]
                self.assertIn("application/json", content, (method, path))
                self.assertIn("schema", content["application/json"], (method, path))
                schema = content["application/json"]["schema"]
                if "$ref" in schema:
                    schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
                self.assertTrue(unsupported_root_keywords.isdisjoint(schema), (method, path))
        agent_properties = spec["components"]["schemas"]["AgentRunRequest"]["properties"]
        self.assertTrue(
            {"prompt", "launcher", "provider", "model", "cwd", "codeoff_workspace"}
            <= set(agent_properties)
        )



    def test_rev_parse(self):
        status, payload, _ = server.dispatch("GET", "/v1/git/rev-parse", {}, True, b"")
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(payload["sha"]), 40)

    def test_worktrees(self):
        status, payload, _ = server.dispatch("GET", "/v1/git/worktrees", {}, True, b"")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["worktrees"])

    def test_file_head(self):
        status, payload, _ = server.dispatch(
            "GET", "/v1/file/head", {"path": ["README"]}, True, b""
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["text"])

    def _write_slice_index(self, repo: Path, tickets: list) -> str:
        rel = "slices/demo/index.json"
        dest = repo / "slices" / "demo"
        dest.mkdir(parents=True)
        (dest / "index.json").write_text(json.dumps({"tickets": tickets}, indent=2) + "\n")
        return rel

    def test_slice_frontier_serial_and_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            rel = self._write_slice_index(
                repo,
                [
                    {
                        "id": "01-login",
                        "path": "slices/demo/01-login.md",
                        "blocked_by": [],
                        "kind": "build",
                        "status": "open",
                    },
                    {
                        "id": "02-checkout",
                        "path": "slices/demo/02-checkout.md",
                        "blocked_by": ["01-login"],
                        "kind": "build",
                        "status": "open",
                    },
                ],
            )
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "GET",
                    "/v1/slice/frontier",
                    {"repo": ["scratch"], "index": [rel]},
                    True,
                    b"",
                )
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["kind"], "build")
                self.assertEqual(payload["id"], "01-login")
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/slice/complete",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "index": rel, "id": "01-login"}).encode(),
                )
                self.assertEqual(status, 200, payload)
                status, payload, _ = server.dispatch(
                    "GET",
                    "/v1/slice/frontier",
                    {"repo": ["scratch"], "index": [rel]},
                    True,
                    b"",
                )
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["id"], "02-checkout")

    def test_slice_frontier_decision_stops(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            rel = self._write_slice_index(
                repo,
                [
                    {
                        "id": "01-ask",
                        "path": "slices/demo/01-ask.md",
                        "blocked_by": [],
                        "kind": "decision",
                        "status": "open",
                    },
                    {
                        "id": "02-build",
                        "path": "slices/demo/02-build.md",
                        "blocked_by": [],
                        "kind": "build",
                        "status": "open",
                    },
                ],
            )
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "GET",
                    "/v1/slice/frontier",
                    {"repo": ["scratch"], "index": [rel]},
                    True,
                    b"",
                )
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["kind"], "decision")
            self.assertEqual(payload["id"], "01-ask")

    def test_slice_continue_kicks_https_webhook(self):
        captured = {}

        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=15):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return FakeResp()

        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            rel = self._write_slice_index(
                repo,
                [
                    {
                        "id": "01-login",
                        "path": "slices/demo/01-login.md",
                        "blocked_by": [],
                        "kind": "build",
                        "status": "done",
                    },
                    {
                        "id": "02-checkout",
                        "path": "slices/demo/02-checkout.md",
                        "blocked_by": ["01-login"],
                        "kind": "build",
                        "status": "open",
                    },
                ],
            )
            body = json.dumps(
                {
                    "repo": "scratch",
                    "index": rel,
                    "branch": "feature/x",
                    "test": "graphwing-unit",
                    "commit_message": "slice",
                    "work_kind": "typescript_coding",
                    "kick_url": "https://example.com/hook",
                    "kick_token": "tok",
                }
            ).encode()
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                with mock.patch.object(server, "urlopen", fake_urlopen):
                    status, payload, _ = server.dispatch(
                        "POST", "/v1/slice/continue", {}, True, body
                    )
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["kicked"])
            self.assertEqual(payload["id"], "02-checkout")
            self.assertEqual(captured["url"], "https://example.com/hook")
            self.assertEqual(captured["body"]["ticket"], "slices/demo/02-checkout.md")
            self.assertEqual(captured["body"]["kick_url"], "https://example.com/hook")
            self.assertEqual(captured["body"]["work_kind"], "typescript_coding")

    def test_slice_continue_accepts_exact_rendered_happy_path_without_recovery_evidence(self):
        captured = []

        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=15):
            captured.append(json.loads(req.data.decode()))
            return FakeResp()

        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            rel = self._write_slice_index(
                repo,
                [
                    {"id": "01", "path": "slices/01.md", "blocked_by": [], "kind": "build", "status": "done"},
                    {"id": "02", "path": "slices/02.md", "blocked_by": ["01"], "kind": "build", "status": "open"},
                ],
            )
            rendered = {
                "repo": "scratch", "index": rel, "branch": "feature/x",
                "test": "graphwing-unit", "commit_message": "slice",
                "iters_left": 2, "class": "mechanical", "work_kind": "go_coding",
                "size": "M", "ac_count": 3, "seams": 1,
                "recovery_version": "", "prior_primary_route": "",
                "prior_primary_receipt": "", "prior_fallback_route": "",
                "prior_fallback_receipt": "", "fresh_primary_receipt": "",
                "kick_url": "https://example.com/hook", "kick_token": "",
            }
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "urlopen", fake_urlopen):
                # Normal-primary and recovered-primary continuations render the
                # same recovery-free request body after a successful primary.
                for origin in ("normal_primary", "recovered_primary"):
                    with self.subTest(origin=origin):
                        status, payload, _ = server.dispatch(
                            "POST", "/v1/slice/continue", {}, True,
                            json.dumps(rendered).encode(),
                        )
                        self.assertEqual(status, 200, payload)
                        self.assertTrue(payload["kicked"], payload)
        self.assertEqual(len(captured), 2)
        recovery_fields = {
            "recovery_version", "prior_primary_route", "prior_primary_receipt",
            "prior_fallback_route", "prior_fallback_receipt", "fresh_primary_receipt",
        }
        for forwarded in captured:
            self.assertTrue(recovery_fields.isdisjoint(forwarded), forwarded)

    def test_slice_continue_forwards_only_complete_versioned_recovery_evidence(self):
        captured = []

        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=15):
            captured.append(json.loads(req.data.decode()))
            return FakeResp()

        normal_fallback_evidence = {
            "recovery_version": "provider-recovery-v1",
            "prior_primary_route": {"route_version": "normal-v1"},
            "prior_primary_receipt": {"job_id": "1" * 32, "status": "error"},
            "prior_fallback_route": {"route_version": "availability-fallback-v1"},
            "prior_fallback_receipt": {"job_id": "2" * 32, "status": "ok"},
        }
        retained_fallback_evidence = {
            **normal_fallback_evidence,
            "prior_primary_receipt": {"job_id": "3" * 32, "status": "error"},
            "prior_fallback_receipt": {"job_id": "4" * 32, "status": "ok"},
        }
        evidence_with_fresh_primary = {
            **retained_fallback_evidence,
            "fresh_primary_receipt": {"job_id": "5" * 32, "status": "ok"},
        }
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            rel = self._write_slice_index(
                repo,
                [
                    {"id": "01", "path": "slices/01.md", "blocked_by": [], "kind": "build", "status": "done"},
                    {"id": "02", "path": "slices/02.md", "blocked_by": ["01"], "kind": "build", "status": "open"},
                ],
            )
            base = {
                "repo": "scratch",
                "index": rel,
                "branch": "feature/x",
                "test": "graphwing-unit",
                "commit_message": "slice",
                "work_kind": "go_coding",
                "kick_url": "https://example.com/hook",
            }
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "urlopen", fake_urlopen):
                for label, evidence in (
                    ("normal fallback", normal_fallback_evidence),
                    ("direct recovery-retained fallback", retained_fallback_evidence),
                    ("optional fresh primary", evidence_with_fresh_primary),
                ):
                    with self.subTest(label=label):
                        status, payload, _ = server.dispatch(
                            "POST", "/v1/slice/continue", {}, True,
                            json.dumps({**base, **evidence}).encode(),
                        )
                        self.assertEqual(status, 200, payload)
                status, payload, _ = server.dispatch(
                    "POST", "/v1/slice/continue", {}, True, json.dumps(base).encode()
                )
                self.assertEqual(status, 200, payload)
                rejected = (
                    ({**base, **evidence_with_fresh_primary, "recovery_version": None}, "missing version"),
                    ({**base, **evidence_with_fresh_primary, "recovery_version": "provider-recovery-v999"}, "unknown version"),
                    ({**base, **evidence_with_fresh_primary, "prior_fallback_receipt": None}, "incomplete core evidence"),
                    ({**base, **evidence_with_fresh_primary, "fresh_primary_receipt": ["not", "an", "object"]}, "malformed fresh receipt"),
                )
                for candidate, label in rejected:
                    with self.subTest(label=label):
                        before = len(captured)
                        status, payload, _ = server.dispatch(
                            "POST", "/v1/slice/continue", {}, True,
                            json.dumps(candidate).encode(),
                        )
                        self.assertEqual(status, 400, payload)
                        self.assertEqual(payload["code"], "malformed_recovery_evidence")
                        self.assertEqual(
                            len(captured), before,
                            "invalid recovery evidence must fail before the webhook",
                        )
                no_kick = {**base, **evidence_with_fresh_primary, "recovery_version": None}
                no_kick.pop("kick_url")
                status, payload, _ = server.dispatch(
                    "POST", "/v1/slice/continue", {}, True,
                    json.dumps(no_kick).encode(),
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "malformed_recovery_evidence")
        for position, evidence in enumerate(
            (
                normal_fallback_evidence,
                retained_fallback_evidence,
                evidence_with_fresh_primary,
            )
        ):
            for key, value in evidence.items():
                self.assertEqual(captured[position][key], value)
        for key in evidence_with_fresh_primary:
            self.assertNotIn(key, captured[3])
        self.assertEqual(len(captured), 4)

    def test_slice_continue_rejects_http_kick_url(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            rel = self._write_slice_index(
                repo,
                [
                    {
                        "id": "01-login",
                        "path": "slices/demo/01-login.md",
                        "blocked_by": [],
                        "kind": "build",
                        "status": "open",
                    }
                ],
            )
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/slice/continue",
                    {},
                    True,
                    json.dumps(
                        {
                            "repo": "scratch",
                            "index": rel,
                            "kick_url": "http://127.0.0.1/x",
                        }
                    ).encode(),
                )
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], "bad_kick_url")

    def test_slice_e2e_skip_without_test_name(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            rel = self._write_slice_index(
                repo,
                [
                    {
                        "id": "01-login",
                        "path": "slices/demo/01-login.md",
                        "blocked_by": [],
                        "kind": "build",
                        "status": "done",
                    }
                ],
            )
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/slice/e2e",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "index": rel}).encode(),
                )
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["kind"], "skip")

    def test_slice_e2e_auto_ticket_from_fail_lines(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            rel = self._write_slice_index(
                repo,
                [
                    {
                        "id": "01-login",
                        "path": "slices/demo/01-login.md",
                        "blocked_by": [],
                        "kind": "build",
                        "status": "done",
                    }
                ],
            )
            fake = {
                "ok": False,
                "returncode": 1,
                "stdout": "FAIL: test_checkout_valid_card\nAssertionError: no\n",
                "stderr": "",
            }
            orig = server.run_cmd

            def run_cmd(args, **kwargs):
                if args and args[0] == "git":
                    return orig(args, **kwargs)
                return fake

            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                with mock.patch.object(server, "run_cmd", side_effect=run_cmd):
                    status, payload, _ = server.dispatch(
                        "POST",
                        "/v1/slice/e2e",
                        {},
                        True,
                        json.dumps({"repo": "scratch", "index": rel, "test": "always-fail"}).encode(),
                    )
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["kind"], "auto")
            self.assertTrue((repo / payload["path"]).is_file())
            idx = json.loads((repo / rel).read_text())
            self.assertEqual(idx["e2e_reds"], 1)
            self.assertEqual(idx["tickets"][-1]["id"], payload["id"])

    def test_slice_e2e_parks_on_third_red(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            rel = self._write_slice_index(
                repo,
                [
                    {
                        "id": "01-login",
                        "path": "slices/demo/01-login.md",
                        "blocked_by": [],
                        "kind": "build",
                        "status": "done",
                    }
                ],
            )
            (repo / rel).write_text(
                json.dumps(
                    {
                        "e2e_reds": 2,
                        "tickets": [
                            {
                                "id": "01-login",
                                "path": "slices/demo/01-login.md",
                                "blocked_by": [],
                                "kind": "build",
                                "status": "done",
                            }
                        ],
                    },
                    indent=2,
                )
                + "\n"
            )
            fake = {"ok": False, "returncode": 1, "stdout": "boom", "stderr": ""}
            orig = server.run_cmd

            def run_cmd(args, **kwargs):
                if args and args[0] == "git":
                    return orig(args, **kwargs)
                return fake

            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                with mock.patch.object(server, "run_cmd", side_effect=run_cmd):
                    status, payload, _ = server.dispatch(
                        "POST",
                        "/v1/slice/e2e",
                        {},
                        True,
                        json.dumps({"repo": "scratch", "index": rel, "test": "always-fail"}).encode(),
                    )
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["kind"], "park")
            self.assertEqual(payload["e2e_reds"], 3)

    def test_git_commit_add_accepts_string_path(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            (repo / "extra.txt").write_text("x\n")
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/commit",
                    {},
                    True,
                    json.dumps(
                        {"repo": "scratch", "message": "add extra", "add": "extra.txt"}
                    ).encode(),
                )
            self.assertEqual((status, payload["code"]), (400, "writer_job_required"), payload)
            self.assertTrue((repo / "extra.txt").is_file())

    def test_file_head_nested_relative_path(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            dest = repo / "slices" / "sc-1"
            dest.mkdir(parents=True)
            (dest / "02-checkout.md").write_text("AC: pay with a valid card\n")
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "GET",
                    "/v1/file/head",
                    {"repo": ["scratch"], "path": ["slices/sc-1/02-checkout.md"]},
                    True,
                    b"",
                )
            self.assertEqual(status, 200, payload)
            self.assertIn("valid card", payload["text"])
            self.assertFalse(payload["truncated"])

    def test_file_head_traversal(self):
        status, payload, _ = server.dispatch(
            "GET", "/v1/file/head", {"path": ["../.graphwing/api.key"]}, True, b""
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "bad_path")

    def test_units_status(self):
        status, payload, _ = server.dispatch("GET", "/v1/units/status", {}, True, b"")
        self.assertEqual(status, 200, payload)
        self.assertIn("graphwing-api", payload["units"])

    def test_job_title_strips_markdown_and_prefers_ticket(self):
        self.assertEqual(
            server.job_title(
                {
                    "kind": "agent",
                    "prompt": "# 01-pre-commit-guardrail-tests: Test script for the pre-commit\nmore",
                }
            ),
            "01-pre-commit-guardrail-tests",
        )
        self.assertEqual(
            server.job_title({"kind": "agent", "ticket": "slices/demo/02-checkout.md", "prompt": "# ignore me"}),
            "02-checkout",
        )
        self.assertEqual(server.job_title({"kind": "agent", "pr": 3523, "prompt": "fix ci"}), "PR 3523")
        self.assertEqual(
            server.job_title({"kind": "agent", "prompt": "please edit slices/demo/03-login.md today"}),
            "03-login",
        )
        self.assertEqual(server.job_title({"kind": "review", "reviewer": "sonnet"}), "review sonnet")
        self.assertEqual(server.job_title({"kind": "test", "script": "riftwing-local-gates"}), "riftwing-local-gates")

    def test_watch_requires_key(self):
        status, payload, _ = server.dispatch("GET", "/v1/watch", {}, False, b"")
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "unauthorized")

    def _write_watch_job(self, root: Path, job: dict) -> None:
        jdir = root / job["job_id"]
        jdir.mkdir(parents=True)
        (jdir / "job.json").write_text(json.dumps(job) + "\n")

    def test_watch_snapshot_strips_secrets_and_ranks_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td)
            running_id = "aa" * 16
            queued_id = "bb" * 16
            failed_id = "cc" * 16
            done_id = "dd" * 16
            self._write_watch_job(
                jobs,
                {
                    "job_id": running_id,
                    "kind": "agent",
                    "status": "running",
                    "repo": "riftwing",
                    "prompt": "implement the login tracer\nsecret second line",
                    "response_webhook_token": "do-not-leak",
                    "created_at": "2026-08-24T12:00:00Z",
                    "started_at": "2026-08-24T12:00:01Z",
                    "finished_at": None,
                    "error": None,
                },
            )
            self._write_watch_job(
                jobs,
                {
                    "job_id": queued_id,
                    "kind": "review",
                    "status": "queued",
                    "reviewer": "sonnet",
                    "repo": "riftwing",
                    "created_at": "2026-08-24T12:01:00Z",
                },
            )
            self._write_watch_job(
                jobs,
                {
                    "job_id": failed_id,
                    "kind": "test",
                    "status": "failed",
                    "script": "riftwing-local-gates",
                    "created_at": "2026-08-24T11:00:00Z",
                    "finished_at": "2026-08-24T11:05:00Z",
                    "error": "suite red\ntraceback",
                    "response_webhook_url": "https://example.invalid/resume",
                },
            )
            self._write_watch_job(
                jobs,
                {
                    "job_id": done_id,
                    "kind": "script",
                    "status": "completed",
                    "script": "publish-graphs",
                    "created_at": "2026-08-24T10:00:00Z",
                    "finished_at": "2026-08-24T10:01:00Z",
                    "receipt": {"status": "ok", "summary": "published implement-slice", "token": "nope"},
                },
            )
            long_prompt = "x" * 200
            self._write_watch_job(
                jobs,
                {
                    "job_id": "ee" * 16,
                    "kind": "agent",
                    "status": "completed",
                    "prompt": long_prompt,
                    "created_at": "2026-08-24T09:00:00Z",
                    "finished_at": "2026-08-24T09:02:00Z",
                },
            )
            with mock.patch.object(server, "JOBS_DIR", jobs), mock.patch.object(
                server, "units_status", return_value={
                    "ok": True,
                    "healthy": False,
                    "units": {
                        "graphwing-api": {"active": True, "state": "active"},
                        "graphwing-tunnel": {"active": True, "state": "active"},
                        "graphwing-herdr": {"active": False, "state": "inactive"},
                    },
                }
            ):
                status, payload, _ = server.dispatch("GET", "/v1/watch", {}, True, b"")
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["api_active"])
            self.assertTrue(payload["units_healthy"])
            self.assertEqual(payload["counts"]["running"], 1)
            self.assertEqual(payload["counts"]["queued"], 1)
            self.assertEqual(payload["counts"]["active"], 2)
            self.assertEqual(payload["counts"]["failed_recent"], 1)
            active_ids = [j["job_id"] for j in payload["active"]]
            self.assertEqual(active_ids[0], queued_id)
            self.assertIn(running_id, active_ids)
            titles = {j["job_id"]: j["title"] for j in payload["active"] + payload["recent"]}
            self.assertEqual(titles[queued_id], "review sonnet")
            self.assertEqual(titles[running_id], "implement the login tracer")
            self.assertEqual(titles[failed_id], "riftwing-local-gates")
            running = next(j for j in payload["active"] if j["job_id"] == running_id)
            self.assertEqual(running["tab"], "gw-a-" + running_id[:8])
            failed = next(j for j in payload["recent"] if j["job_id"] == failed_id)
            self.assertEqual(failed["tab"], "gw-t-" + failed_id[:8])
            dumped = json.dumps(payload)
            self.assertNotIn("do-not-leak", dumped)
            self.assertNotIn("https://example.invalid/resume", dumped)
            self.assertNotIn("secret second line", dumped)
            long_row = next(j for j in payload["recent"] if j["job_id"] == "ee" * 16)
            self.assertLessEqual(len(long_row["title"]), server.WATCH_TITLE_CHARS)
            self.assertTrue(long_row["title"].endswith("…"))
            failed = next(j for j in payload["recent"] if j["job_id"] == failed_id)
            self.assertEqual(failed["error"], "suite red")
            self.assertEqual(failed["status"], "failed")
            self.assertNotIn("webhook", failed)
            self.assertNotIn("prompt", failed)
            done = next(j for j in payload["recent"] if j["job_id"] == done_id)
            self.assertEqual(done["summary"], "published implement-slice")
            self.assertNotIn("token", json.dumps(done))
            self.assertNotIn("nope", json.dumps(payload))

    def test_watch_empty_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(server, "JOBS_DIR", Path(td)), mock.patch.object(
                server, "units_status", return_value={
                    "ok": True,
                    "healthy": True,
                    "units": {"graphwing-api": {"active": True, "state": "active"}},
                }
            ):
                status, payload, _ = server.dispatch("GET", "/v1/watch", {}, True, b"")
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["counts"]["active"], 0)
            self.assertEqual(payload["active"], [])
            self.assertEqual(payload["recent"], [])
            self.assertEqual(payload["workflows"]["recent"], [])

    def test_watch_recent_workflows_come_from_fires(self):
        self.runs_path.write_text(
            json.dumps(
                {
                    "workflow": "implement-slice",
                    "status": "fired",
                    "source": "fire",
                    "input": {
                        "repo": "riftwing",
                        "ticket": "slices/demo/01-login.md",
                        "kick_url": "https://app.rewst.ai/api/hooks/secret",
                    },
                    "created_at": "2026-08-24T12:00:00Z",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "workflow": "pr-drive",
                    "status": "fired",
                    "source": "doorbell",
                    "input": {"repo": "riftwing", "pr": 3523},
                    "created_at": "2026-08-24T13:00:00Z",
                }
            )
            + "\n"
        )
        with mock.patch.object(server, "JOBS_DIR", Path(tempfile.mkdtemp())), mock.patch.object(
            server,
            "units_status",
            return_value={"ok": True, "healthy": True, "units": {"graphwing-api": {"active": True, "state": "active"}}},
        ):
            status, payload, _ = server.dispatch("GET", "/v1/watch", {}, True, b"")
        self.assertEqual(status, 200, payload)
        recent = payload["workflows"]["recent"]
        self.assertEqual(recent[0]["kind"], "pr-drive")
        self.assertEqual(recent[0]["title"], "pr-drive PR 3523")
        self.assertEqual(recent[1]["title"], "implement-slice 01-login")
        self.assertEqual(recent[1]["tab"], "graph")
        dumped = json.dumps(payload)
        self.assertNotIn("kick_url", dumped)
        self.assertNotIn("hooks/secret", dumped)

    def test_herdr_agents(self):
        fixture = {"ok": True, "stdout": json.dumps({"result": {"agents": []}})}
        with mock.patch.object(server, "run_cmd", return_value=fixture) as run:
            status, payload, _ = server.dispatch("GET", "/v1/herdr/agents", {}, True, b"")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["session"], "graphwing")
        self.assertEqual(run.call_args.args[0][:2], ["herdr", "--session"])

    def test_gh_pr_view_requires_number(self):
        status, payload, _ = server.dispatch("GET", "/v1/gh/pr/view", {}, True, b"")
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "missing_number")

    def test_gh_pr_list(self):
        with mock.patch.object(server, "gh_json", return_value={"ok": True, "data": []}):
            status, payload, _ = server.dispatch(
                "GET", "/v1/gh/pr/list", {"n": ["1"]}, True, b""
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertIsInstance(payload["data"], list)

    def test_annotate_pr_view_approved(self):
        out = server.annotate_pr_view({"ok": True, "data": {"reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN"}})
        self.assertEqual(out["review_decision"], "APPROVED")
        self.assertEqual(out["merge_state"], "CLEAN")
        self.assertFalse(out["reviews_blocking"])
        self.assertTrue(out["reviews_ok"])

    def test_annotate_pr_view_changes_requested(self):
        out = server.annotate_pr_view({"ok": True, "data": {"reviewDecision": "CHANGES_REQUESTED"}})
        self.assertEqual(out["review_decision"], "CHANGES_REQUESTED")
        self.assertTrue(out["reviews_blocking"])
        self.assertFalse(out["reviews_ok"])

    def test_annotate_pr_view_review_required(self):
        out = server.annotate_pr_view({"ok": True, "data": {"reviewDecision": "REVIEW_REQUIRED"}})
        self.assertEqual(out["review_decision"], "REVIEW_REQUIRED")
        self.assertTrue(out["reviews_blocking"])
        self.assertFalse(out["reviews_ok"])

    def test_annotate_pr_view_empty(self):
        out = server.annotate_pr_view({"ok": True, "data": {}})
        self.assertEqual(out["review_decision"], "")
        self.assertEqual(out["merge_state"], "")
        self.assertFalse(out["reviews_blocking"])
        self.assertTrue(out["reviews_ok"])

    def test_annotate_pr_checks_all_green(self):
        out = server.annotate_pr_checks(
            {"ok": True, "data": [{"name": "ci", "bucket": "pass"}, {"name": "lint", "bucket": "skipping"}]}
        )
        self.assertTrue(out["all_green"])
        self.assertFalse(out["any_red"])
        self.assertEqual(out["failing"], [])
        self.assertEqual(out["pending"], [])

    def test_annotate_pr_checks_red(self):
        out = server.annotate_pr_checks(
            {"ok": True, "data": [{"name": "ci", "bucket": "fail"}, {"name": "lint", "bucket": "pass"}]}
        )
        self.assertFalse(out["all_green"])
        self.assertTrue(out["any_red"])
        self.assertEqual(out["failing"], ["ci"])

    def test_annotate_pr_checks_pending(self):
        out = server.annotate_pr_checks(
            {"ok": True, "data": [{"name": "ci", "bucket": "pending"}]}
        )
        self.assertFalse(out["all_green"])
        self.assertFalse(out["any_red"])
        self.assertEqual(out["pending"], ["ci"])

    def test_gh_pr_checks_requires_number(self):
        status, payload, _ = server.dispatch("GET", "/v1/gh/pr/checks", {}, True, b"")
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "missing_number")

    def _scratch_git(self, root: Path) -> Path:
        repo = root / "repo"
        remote = root / "remote.git"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "gw@test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "graphwing-test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True, capture_output=True)
        (repo / "README").write_text("hi\n")
        subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True, capture_output=True)
        return repo

    def test_git_checkout_create_and_switch(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/checkout",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "branch": "feat-x", "create": True}).encode(),
                )
                self.assertEqual(status, 200, payload)
                self.assertTrue(payload["ok"])
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/checkout",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "branch": "main"}).encode(),
                )
                self.assertEqual(status, 200, payload)
                cur = subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(cur.stdout.strip(), "main")

    def test_git_checkout_create_is_idempotent(self):
        # Structure commits the slice map on the story branch before the run
        # fires, so create:true routinely lands on a branch that already
        # exists. `checkout -b` dies there with returncode 128 and the walk
        # stops at its first writing step (seen on the SC-110290 run).
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                body = json.dumps({"repo": "scratch", "branch": "feat-x", "create": True}).encode()
                status, payload, _ = server.dispatch("POST", "/v1/git/checkout", {}, True, body)
                self.assertEqual(status, 200, payload)
                subprocess.run(
                    ["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True
                )
                # Same call again: switch to the existing branch, do not fail.
                status, payload, _ = server.dispatch("POST", "/v1/git/checkout", {}, True, body)
                self.assertEqual(status, 200, payload)
                self.assertTrue(payload["ok"])
                cur = subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(cur.stdout.strip(), "feat-x")

    def test_file_head_uses_rel_not_path(self):
        # Rewst's connector treats a query param named `path` as the request
        # URL path, so fileHead's param had to be renamed. It sent
        # GET /slices/01-ticket.md instead of GET /v1/file/head?... and the
        # walk 404'd at its first fileHead (the SC-110290 run). `path` stays
        # accepted so integration v9 keeps working until v10 ships.
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            (repo / "note.md").write_text("hello\n")
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                for param in ("rel", "path"):
                    status, payload, _ = server.dispatch(
                        "GET", "/v1/file/head", {"repo": ["scratch"], param: ["note.md"]}, True, b""
                    )
                    self.assertEqual(status, 200, payload)
                    self.assertEqual(payload["text"], "hello\n")
                status, payload, _ = server.dispatch(
                    "GET", "/v1/file/head", {"repo": ["scratch"]}, True, b""
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "missing_rel")

    def test_openapi_has_no_path_query_param(self):
        # Guard the whole class of bug, not just fileHead: any request query
        # param named `path` gets eaten by the connector.
        spec = json.loads(server.openapi_bytes())
        offenders = [
            (method.upper(), route, op.get("operationId"))
            for route, item in spec["paths"].items()
            for method, op in item.items()
            if isinstance(op, dict)
            for prm in (op.get("parameters") or [])
            if prm.get("name") == "path" and prm.get("in") == "query"
        ]
        self.assertEqual(offenders, [])

    def test_rewst_fire_requires_recorded_url(self):
        # The point of the proxy: the caller sends workflow + input and never
        # holds the webhook key. With no URL recorded it must say so plainly
        # instead of posting nowhere.
        with mock.patch.object(server, "load_rewst_install", return_value={"hook_secret": "s"}):
            status, payload, _ = server.dispatch(
                "POST", "/v1/rewst/fire", {}, True,
                json.dumps({"workflow": "implement-slice", "input": {"repo": "riftwing"}}).encode(),
            )
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["code"], "no_hook_url")

    def test_rewst_fire_rejects_unknown_workflow(self):
        status, payload, _ = server.dispatch(
            "POST", "/v1/rewst/fire", {}, True,
            json.dumps({"workflow": "rm-rf", "input": {}}).encode(),
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "unknown_workflow")

    def test_rewst_fire_sends_secret_header_and_wraps_input(self):
        seen = {}

        class FakeResp:
            status = 200
            def read(self): return b'{"ok":true}'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["secret"] = req.get_header("X-rewst-secret")
            seen["body"] = json.loads(req.data.decode())
            return FakeResp()

        install = {"hook_secret": "shh", "implement_slice_hook_url": "https://app.rewst.ai/api/hooks/o/trigger/t"}
        with mock.patch.object(server, "load_rewst_install", return_value=install), \
             mock.patch.object(server, "urlopen", fake_urlopen):
            status, payload, _ = server.dispatch(
                "POST", "/v1/rewst/fire", {}, True,
                json.dumps({"workflow": "implement-slice", "input": {"repo": "riftwing"}}).encode(),
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(seen["url"], "https://app.rewst.ai/api/hooks/o/trigger/t")
        self.assertEqual(seen["secret"], "shh")
        self.assertEqual(seen["body"], {"input": {"repo": "riftwing"}})
        recorded = [json.loads(line) for line in self.runs_path.read_text().splitlines() if line.strip()]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["workflow"], "implement-slice")
        self.assertEqual(recorded[0]["input"], {"repo": "riftwing"})
        self.assertNotIn("kick_url", json.dumps(recorded[0]))
        self.assertNotIn("shh", json.dumps(recorded[0]))

    def test_pr_merge_run_id_uses_a_variable_that_exists(self):
        # CTX.RUN.id does not exist. The run identity lives at WORKFLOW.runId,
        # so the guard refused every merge with no_run_id while every other
        # condition passed: all_green, MERGEABLE, no holds, auto_merge true.
        graph = json.loads((Path(server.__file__).parent / "graphs" / "pr-drive.json").read_text())
        cfg = {n["id"]: n for n in graph["spec"]["nodes"]}["merge"]["config"]
        self.assertIn("WORKFLOW.runId", cfg["run_id"])
        self.assertNotIn("CTX.RUN", cfg["run_id"])

    def test_pr_merge_field_is_not_called_method(self):
        # The connector treats a config field named `method` as the HTTP verb,
        # the same way it treats `path` as the URL path. The graph sent
        # method: "squash" and Rewst issued `SQUASH /v1/gh/pr/merge`, which the
        # server answered 501. Third reserved name found this way.
        spec = json.loads((Path(server.__file__).parent / "openapi.json").read_text())
        props = spec["components"]["schemas"]["GhPrMergeRequest"]["properties"]
        self.assertNotIn("method", props, "`method` is reserved by the connector")
        self.assertIn("merge_method", props)

        graph = json.loads((Path(server.__file__).parent / "graphs" / "pr-drive.json").read_text())
        merge_cfg = {n["id"]: n for n in graph["spec"]["nodes"]}["merge"]["config"]
        self.assertNotIn("method", merge_cfg)
        self.assertEqual(merge_cfg.get("merge_method"), "squash")

    def test_pr_merge_reads_the_real_gh_shapes(self):
        # The first version of this endpoint read all_green out of
        # checks["data"], where annotate_pr_checks does not put it, so every
        # merge attempt crashed with AttributeError. The unit tests above
        # passed a hand-built state dict and sailed straight past it. Drive
        # the real function with the real gh payload shapes instead.
        head = "1" * 40
        with tempfile.TemporaryDirectory() as td:
            repo, jobs = Path(td) / "repo", Path(td) / "jobs"
            repo.mkdir()
            self._write_merge_job(jobs, repo, head)
            status, payload, merge, calls = self._merge_with_fakes(jobs, repo, head)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["merged"], payload)
        self.assertTrue(payload["state"]["all_green"], payload["state"])
        self.assertEqual(merge.call_args.args[1][:3], ["pr", "merge", "7"])
        self.assertTrue(any(call[1] == "checks" for call in calls))

    def test_pr_merge_does_not_call_gh_merge_when_it_refuses(self):
        # A refusal that still shelled out to `gh pr merge` would be the worst
        # possible version of this bug.
        head = "1" * 40
        with tempfile.TemporaryDirectory() as td:
            repo, jobs = Path(td) / "repo", Path(td) / "jobs"
            repo.mkdir()
            self._write_merge_job(jobs, repo, head)
            status, payload, merge, _ = self._merge_with_fakes(
                jobs, repo, head, view=self._strict_pr_view(head, labels=[{"name": "hold:pm-review"}])
            )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "held")
        merge.assert_not_called()

    def test_pr_merge_reports_success_for_a_plain_text_merge(self):
        # `gh pr merge` prints plain text, but the endpoint sent it through
        # gh_json, so a merge that succeeded came back merged=False with
        # code=gh_json. A caller retrying on that error would re-attempt an
        # action that had already happened. This is not hypothetical: it is
        # exactly what riftwing#3523 reported after it had already merged.
        head = "1" * 40
        with tempfile.TemporaryDirectory() as td:
            repo, jobs = Path(td) / "repo", Path(td) / "jobs"
            repo.mkdir()
            self._write_merge_job(jobs, repo, head)
            status, payload, _, _ = self._merge_with_fakes(
                jobs, repo, head, merge={"ok": True, "stdout": "Squashed and merged pull request #7\n"}
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["merged"], payload)
        self.assertNotEqual(payload.get("code"), "gh_json")

    def test_pr_merge_requires_a_run_id(self):
        # auto_merge defaults false so a graph cannot merge by accident, but
        # any caller holding the API key could still pass the flag by hand.
        # That is how riftwing#3523 got merged: a curl from a shell, not a run.
        # Merge is reachable only from inside a run.
        ok, err = server.pr_merge_allowed(
            {"all_green": True, "mergeable": "MERGEABLE", "is_draft": False, "holds": []},
            auto_merge=True, run_id="",
        )
        self.assertFalse(ok)
        self.assertEqual(err["code"], "no_run_id")

    def test_pr_continue_stops_at_the_attempt_ceiling(self):
        # pr-drive took one swing and stopped. Looping is the point, but an
        # unbounded loop on a PR the writer cannot fix would grind forever.
        posted = []
        with mock.patch.object(server, "post_receipt",
                               lambda url, payload, token=None: posted.append(payload) or {"ok": True}):
            status, out = server.pr_continue(json.dumps({
                "repo": "r", "pr": 1, "attempt": 3, "max_attempts": 3,
                "kick_url": "https://app.rewst.ai/api/hooks/x",
            }).encode(), {"r": str(Path(server.__file__).parent)})
        self.assertEqual(status, 200, out)
        self.assertFalse(out["kicked"])
        self.assertEqual(out["code"], "attempts_exhausted")
        self.assertEqual(posted, [])

    def test_pr_continue_kicks_the_next_attempt(self):
        posted = []

        def fake_post(url, payload, token=None):
            posted.append((url, payload))
            return {"ok": True}

        with mock.patch.object(server, "post_receipt", fake_post):
            status, out = server.pr_continue(json.dumps({
                "repo": "r", "pr": 1, "attempt": 1, "max_attempts": 3,
                "test": "riftwing-local-gates", "auto_merge": True,
                "kick_url": "https://app.rewst.ai/api/hooks/x",
            }).encode(), {"r": str(Path(server.__file__).parent)})
        self.assertEqual(status, 200, out)
        self.assertTrue(out["kicked"])
        self.assertEqual(len(posted), 1)
        body = posted[0][1]
        # The next run reads CTX.INPUT.pr_number; a plain `pr` key never arrives.
        self.assertIn("pr_number", body)
        # The next run must carry the incremented attempt, or the ceiling
        # never arrives and the loop is unbounded after all.
        self.assertEqual(body["attempt"], 2)
        # pr rides as a string, matching how gh_pr_merge and the graph treat it.
        self.assertEqual(body["pr_number"], "1")
        self.assertTrue(body["auto_merge"])

    def test_pr_continue_refuses_a_non_https_kick(self):
        status, out = server.pr_continue(json.dumps({
            "repo": "r", "pr": 1, "attempt": 1, "kick_url": "http://evil.example/x",
        }).encode(), {"r": str(Path(server.__file__).parent)})
        self.assertEqual(status, 400)
        self.assertEqual(out["code"], "bad_kick_url")

    def test_pr_merge_refuses_without_an_explicit_opt_in(self):
        # Auto-merge is per-run and off by default. The operator lock says the
        # engineer merges; this endpoint exists only for the runs where they
        # said otherwise, so absence of the flag must be a refusal.
        ok, err = server.pr_merge_allowed({"all_green": True, "mergeable": "MERGEABLE",
                                           "is_draft": False}, auto_merge=False, run_id="r1")
        self.assertFalse(ok)
        self.assertEqual(err["code"], "auto_merge_not_requested")

    def test_pr_merge_refuses_when_not_actually_green(self):
        # The graph deciding "green" is what let a queue receipt pass as a test
        # pass. Re-check here rather than trusting the caller's word.
        for state, code in (
            ({"all_green": False, "mergeable": "MERGEABLE", "is_draft": False}, "not_green"),
            ({"all_green": True, "mergeable": "CONFLICTING", "is_draft": False}, "not_mergeable"),
            ({"all_green": True, "mergeable": "MERGEABLE", "is_draft": True}, "is_draft"),
            ({"all_green": True, "mergeable": "MERGEABLE", "is_draft": False,
              "holds": ["hold:pm-review"]}, "held"),
        ):
            ok, err = server.pr_merge_allowed(state, auto_merge=True, run_id="r1")
            self.assertFalse(ok, state)
            self.assertEqual(err["code"], code, state)

    def test_pr_merge_allows_a_green_unheld_pr_when_asked(self):
        ok, err = server.pr_merge_allowed(
            {"all_green": True, "mergeable": "MERGEABLE", "is_draft": False, "holds": []},
            auto_merge=True, run_id="r1",
        )
        self.assertTrue(ok, err)
        self.assertIsNone(err)

    @staticmethod
    def _strict_pr_view(head, **overrides):
        data = {
            "number": 7,
            "title": "fixture",
            "state": "OPEN",
            "url": "https://example.invalid/pull/7",
            "headRefName": "feature",
            "headRefOid": head,
            "baseRefName": "main",
            "mergeable": "MERGEABLE",
            "isDraft": False,
            "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN",
            "labels": [],
        }
        data.update(overrides)
        return {"ok": True, "data": data}

    @staticmethod
    def _strict_checks(bucket="pass"):
        return {"ok": True, "data": [{"name": "ci", "bucket": bucket}]}

    def _write_merge_job(self, jobs, repo, head, **overrides):
        job_id = overrides.pop("job_id", "a" * 32)
        evidence = {
            "version": "pr-merge-evidence-v1",
            "repo": "r",
            "cwd": str(repo.resolve()),
            "pr": "7",
            "run_id": "run-7",
            "recipe": "catalog-compile",
            "expected_head": head,
            "live_head": head,
            "head_ref": "feature",
            "start": {"branch": "feature", "sha": head, "clean": True},
            "final": {"branch": "feature", "sha": head, "clean": True},
        }
        evidence.update(overrides.pop("evidence", {}))
        evidence.setdefault(
            "writer_evidence_mode", "writer" if "writer_job_id" in evidence else "initial_green"
        )
        job = {
            "job_id": job_id,
            "kind": "test",
            "status": "completed",
            "script": "catalog-compile",
            "repo": "r",
            "cwd": str(repo.resolve()),
            "returncode": 0,
            "receipt": {"status": "ok", "job_id": job_id},
            "merge_evidence": evidence,
        }
        job.update(overrides)
        path = jobs / job_id / "job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(job) + "\n")
        return job_id, job

    @staticmethod
    def _merge_body(evidence_job_id="a" * 32, **overrides):
        body = {
            "repo": "r",
            "number": "7",
            "auto_merge": True,
            "run_id": "run-7",
            "test": "catalog-compile",
            "evidence_job_id": evidence_job_id,
            "merge_method": "squash",
        }
        body.update(overrides)
        return json.dumps(body).encode()

    def _merge_with_fakes(self, jobs, repo, head, *, view=None, checks=None, merge=None, body=None):
        calls = []

        def fake_json(_repo, argv):
            calls.append(list(argv))
            return deepcopy(view or self._strict_pr_view(head)) if argv[1] == "view" else deepcopy(checks or self._strict_checks())

        merge = merge or {"ok": True, "stdout": "merged"}
        with mock.patch.object(server, "JOBS_DIR", jobs), \
             mock.patch.object(server, "gh_json", fake_json), \
             mock.patch.object(server, "gh_text", return_value=merge) as gh_merge, \
             mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")), \
             mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("provider CLI forbidden")):
            status, payload = server.gh_pr_merge(body or self._merge_body(), {"r": str(repo)})
        return status, payload, gh_merge, calls

    def test_gh_checks_nonzero_verdict_json_is_not_a_transport_failure(self):
        fixtures = (
            (1, [{"name": "ci", "bucket": "fail"}], "red"),
            (8, [{"name": "ci", "bucket": "pending"}], "pending"),
        )
        for returncode, rows, state in fixtures:
            with self.subTest(returncode=returncode), mock.patch.object(
                server,
                "run_cmd",
                return_value={"ok": False, "returncode": returncode, "stdout": json.dumps(rows), "stderr": "", "truncated": False, "status": 400},
            ):
                out = server.annotate_pr_checks(server.gh_json(Path("/tmp"), ["pr", "checks", "7", "--json", "name,state,bucket,link"]))
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["checks_state"], state, out)
        with mock.patch.object(server, "run_cmd", return_value={"ok": False, "returncode": 1, "stdout": "not-json", "stderr": "boom", "truncated": False, "status": 400}):
            self.assertFalse(server.gh_json(Path("/tmp"), ["pr", "checks", "7"])["ok"])

    def test_gh_operations_bypass_path_shims_with_resolved_binary(self):
        result = {"ok": True, "returncode": 0, "stdout": "[]", "stderr": "", "truncated": False}
        native = Path("/resolved/bin/gh")
        with mock.patch.object(server, "GH_BIN", native), mock.patch.object(
            server, "run_cmd", return_value=result
        ) as run:
            self.assertTrue(server.gh_json(Path("/tmp"), ["pr", "list"])["ok"])
            self.assertTrue(server.gh_text(Path("/tmp"), ["pr", "merge", "7"])["ok"])
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["/resolved/bin/gh", "pr", "list"], ["/resolved/bin/gh", "pr", "merge", "7"]],
        )

    def test_check_classification_is_nonempty_strict_and_fail_closed(self):
        cases = (
            ({"ok": True, "data": []}, "no_checks"),
            ({"ok": True, "data": [{"name": "ci", "bucket": "mystery"}]}, "unknown"),
            ({"ok": True, "data": {}}, "malformed"),
            ({"ok": True, "data": [None]}, "malformed"),
            ({"ok": True, "data": [{"name": "", "bucket": "pass"}]}, "malformed"),
            ({"ok": True, "data": [{"name": "ci", "bucket": "pass", "state": "FAILURE"}]}, "inconsistent"),
        )
        for raw, expected in cases:
            with self.subTest(expected=expected):
                out = server.annotate_pr_checks(deepcopy(raw))
                self.assertFalse(out.get("all_green"), out)
                self.assertEqual(out.get("checks_state"), expected, out)

    def test_pr_view_classifies_only_truthful_remote_ready_state(self):
        head = "1" * 40
        cases = (
            ({}, "ready", True),
            ({"state": "CLOSED"}, "closed", False),
            ({"isDraft": True}, "draft", False),
            ({"labels": [{"name": "hold:pm-review"}]}, "held", False),
            ({"reviewDecision": "CHANGES_REQUESTED"}, "blocking_review", False),
            ({"reviewDecision": "REVIEW_REQUIRED"}, "blocking_review", False),
            ({"reviewDecision": "SURPRISE"}, "unknown", False),
            ({"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}, "conflicting", False),
            ({"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}, "unknown", False),
            ({"mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED"}, "inconsistent_merge_state", False),
            ({"headRefOid": None}, "malformed", False),
            ({"labels": ["hold:bad"]}, "malformed", False),
        )
        for changes, expected, ready in cases:
            with self.subTest(changes=changes):
                out = server.annotate_pr_view(self._strict_pr_view(head, **changes))
                self.assertEqual(out.get("remote_state"), expected, out)
                self.assertIs(out.get("remote_ready"), ready, out)
        # No effective review decision is not invented as an approval; CLEAN
        # remains the source of truth when the repository requires no review.
        out = server.annotate_pr_view(self._strict_pr_view(head, reviewDecision=None))
        self.assertEqual(out["review_decision"], "")
        self.assertTrue(out["remote_ready"])

    def test_merge_evidence_creation_stamps_server_side_pr_and_git_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature"], check=True, capture_output=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            jobs = root / "jobs"
            spec = {"catalog-compile": {"name": "catalog-compile", "argv": [sys.executable, "-c", "pass"], "cwd": repo.resolve(), "timeout_seconds": 120, "async": True}}
            body = json.dumps({
                "name": "catalog-compile", "evidence_mode": "pr_merge", "repo": "r",
                "pr": "7", "run_id": "run-7", "expected_head": head,
                "response_webhook_url": "https://example.invalid/resume",
            }).encode()
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_tests", return_value=spec), \
                 mock.patch.object(server, "gh_json", return_value=self._strict_pr_view(head)), \
                 mock.patch.object(server, "enqueue_script") as enqueue, \
                 mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")):
                status, payload = server.test_run(body, {"r": str(repo)})
            self.assertEqual(status, 202, payload)
            job = json.loads((jobs / payload["job_id"] / "job.json").read_text())
            evidence = job["merge_evidence"]
            self.assertEqual(evidence["writer_evidence_mode"], "initial_green")
            self.assertNotIn("writer_job_id", evidence)
            self.assertEqual(evidence["repo"], "r")
            self.assertEqual(job["repo"], "r")
            self.assertEqual(evidence["cwd"], str(repo.resolve()))
            self.assertEqual(evidence["pr"], "7")
            self.assertEqual(evidence["run_id"], "run-7")
            self.assertEqual(evidence["recipe"], job["script"])
            self.assertEqual((evidence["expected_head"], evidence["live_head"], evidence["head_ref"]), (head, head, "feature"))
            self.assertEqual(evidence["start"], {"branch": "feature", "sha": head, "clean": True})
            self.assertIsNone(evidence["final"])
            enqueue.assert_called_once()

    def test_merge_evidence_creation_requires_valid_writer_before_stamping_writer_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature"], check=True, capture_output=True)
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            jobs = root / "jobs"
            writer_id = "c" * 32
            spec = {"catalog-compile": {
                "name": "catalog-compile", "argv": [sys.executable, "-c", "pass"],
                "cwd": repo.resolve(), "timeout_seconds": 120, "async": True,
            }}
            request = {
                "name": "catalog-compile", "evidence_mode": "pr_merge", "repo": "r",
                "pr": "7", "run_id": "run-7", "expected_head": head,
                "writer_job_id": writer_id,
            }
            patches = (
                mock.patch.object(server, "JOBS_DIR", jobs),
                mock.patch.object(server, "load_tests", return_value=spec),
                mock.patch.object(server, "gh_json", return_value=self._strict_pr_view(head)),
                mock.patch.object(server, "enqueue_script"),
            )
            with patches[0], patches[1], patches[2], patches[3] as enqueue:
                status, payload = server.test_run(json.dumps(request).encode(), {"r": str(repo)})
            self.assertEqual(status, 409, payload)
            enqueue.assert_not_called()

            writer = _stamp_complete_writer_evidence({
                "job_id": writer_id, "status": "completed", "repo": "r", "cwd": str(repo.resolve()),
                "session_identity": {"branch": "feature", "starting_head": head}, "receipt": {"status": "ok"},
            })
            writer_path = jobs / writer_id / "job.json"
            writer_path.parent.mkdir(parents=True)
            writer_path.write_text(json.dumps(writer) + "\n")
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_tests", return_value=spec), \
                 mock.patch.object(server, "gh_json", return_value=self._strict_pr_view(head)), \
                 mock.patch.object(server, "enqueue_script") as enqueue:
                status, payload = server.test_run(json.dumps(request).encode(), {"r": str(repo)})
            self.assertEqual(status, 202, payload)
            created = json.loads((jobs / payload["job_id"] / "job.json").read_text())
            self.assertEqual(created["merge_evidence"]["writer_evidence_mode"], "writer")
            self.assertEqual(created["merge_evidence"]["writer_job_id"], writer_id)
            enqueue.assert_called_once()

    def test_merge_evidence_creation_rejects_stale_branch_dirty_and_wrong_recipe_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature"], check=True, capture_output=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            base_spec = {"name": "catalog-compile", "argv": [sys.executable, "-c", "pass"], "cwd": repo.resolve(), "timeout_seconds": 120, "async": True}
            base = {"name": "catalog-compile", "evidence_mode": "pr_merge", "repo": "r", "pr": "7", "run_id": "run-7", "expected_head": head}
            cases = (
                ("stale", {**base, "expected_head": "2" * 40}, self._strict_pr_view(head), base_spec),
                ("branch", base, self._strict_pr_view(head, headRefName="elsewhere"), base_spec),
                ("cwd", base, self._strict_pr_view(head), {**base_spec, "cwd": root.resolve()}),
            )
            for label, request, view, spec in cases:
                with self.subTest(label=label), mock.patch.object(server, "JOBS_DIR", root / f"jobs-{label}"), \
                     mock.patch.object(server, "load_tests", return_value={"catalog-compile": spec}), \
                     mock.patch.object(server, "gh_json", return_value=view), \
                     mock.patch.object(server, "enqueue_script") as enqueue:
                    status, payload = server.test_run(json.dumps(request).encode(), {"r": str(repo)})
                self.assertEqual(status, 409, payload)
                enqueue.assert_not_called()
            (repo / "dirty").write_text("x")
            with mock.patch.object(server, "JOBS_DIR", root / "jobs-dirty"), \
                 mock.patch.object(server, "load_tests", return_value={"catalog-compile": base_spec}), \
                 mock.patch.object(server, "gh_json", return_value=self._strict_pr_view(head)), \
                 mock.patch.object(server, "enqueue_script") as enqueue:
                status, payload = server.test_run(json.dumps(base).encode(), {"r": str(repo)})
            self.assertEqual(status, 409, payload)
            self.assertEqual(payload["code"], "dirty_evidence_start")
            enqueue.assert_not_called()

    def test_merge_evidence_job_fails_when_test_changes_branch_sha_or_cleanliness(self):
        commands = (
            [sys.executable, "-c", "open('dirty-after-test','w').write('x')"],
            ["git", "checkout", "-b", "other"],
            ["git", "commit", "--allow-empty", "-m", "move head"],
        )
        for argv in commands:
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                repo = self._scratch_git(root)
                subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature"], check=True, capture_output=True)
                head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
                jobs = root / "jobs"
                job_id = "b" * 32
                job = {
                    "job_id": job_id, "kind": "test", "status": "queued", "script": "catalog-compile",
                    "argv": argv, "cwd": str(repo), "timeout_seconds": 30, "log_ref": str(jobs / job_id / "stdout.log"),
                    "response_webhook_url": None, "response_webhook_token": None,
                    "merge_evidence": {
                        "version": "pr-merge-evidence-v1", "repo": "r", "cwd": str(repo.resolve()), "pr": "7",
                        "run_id": "run-7", "recipe": "catalog-compile", "expected_head": head, "live_head": head,
                        "head_ref": "feature", "start": {"branch": "feature", "sha": head, "clean": True}, "final": None,
                    },
                }
                (jobs / job_id).mkdir(parents=True)
                (jobs / job_id / "job.json").write_text(json.dumps(job) + "\n")
                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.object(server, "deliver_webhook", return_value=None), \
                     mock.patch.object(server, "herdr_job_done", return_value=None):
                    server.run_script_job(job_id)
                stored = json.loads((jobs / job_id / "job.json").read_text())
                self.assertEqual(stored["status"], "failed", stored)
                self.assertEqual(stored["receipt"]["status"], "error", stored)
                self.assertIsNotNone(stored["merge_evidence"]["final"])
                self.assertNotEqual(stored["merge_evidence"]["final"], stored["merge_evidence"]["start"])

    def test_merge_rejects_every_unbound_or_nonterminal_test_evidence_fixture(self):
        head = "1" * 40
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, jobs = root / "repo", root / "jobs"
            repo.mkdir()
            cases = []
            job_id, base = self._write_merge_job(jobs, repo, head)
            cases.extend([
                ("legacy", {k: v for k, v in base.items() if k != "merge_evidence"}),
                ("wrong_kind", {**base, "kind": "script"}),
                ("running", {**base, "status": "running", "receipt": None}),
                ("red", {**base, "status": "failed", "returncode": 1, "receipt": {"status": "error", "job_id": job_id}}),
                ("wrong_repo", {**base, "merge_evidence": {**base["merge_evidence"], "repo": "other"}}),
                ("wrong_test", {**base, "script": "other"}),
                ("wrong_pr", {**base, "merge_evidence": {**base["merge_evidence"], "pr": "8"}}),
                ("wrong_run", {**base, "merge_evidence": {**base["merge_evidence"], "run_id": "other"}}),
                ("wrong_head", {**base, "merge_evidence": {**base["merge_evidence"], "live_head": "2" * 40}}),
                ("branch_mismatch", {**base, "merge_evidence": {**base["merge_evidence"], "final": {"branch": "other", "sha": head, "clean": True}}}),
                ("dirty_start", {**base, "merge_evidence": {**base["merge_evidence"], "start": {"branch": "feature", "sha": head, "clean": False}}}),
                ("dirty_end", {**base, "merge_evidence": {**base["merge_evidence"], "final": {"branch": "feature", "sha": head, "clean": False}}}),
                ("missing_writer_mode", {**base, "merge_evidence": {key: value for key, value in base["merge_evidence"].items() if key != "writer_evidence_mode"}}),
                ("unknown_writer_mode", {**base, "merge_evidence": {**base["merge_evidence"], "writer_evidence_mode": "legacy"}}),
                ("writer_mode_without_id", {**base, "merge_evidence": {**base["merge_evidence"], "writer_evidence_mode": "writer"}}),
                ("initial_mode_with_id", {**base, "merge_evidence": {**base["merge_evidence"], "writer_evidence_mode": "initial_green", "writer_job_id": "c" * 32}}),
            ])
            for label, job in cases:
                with self.subTest(label=label):
                    (jobs / job_id / "job.json").write_text(json.dumps(job) + "\n")
                    status, payload, merge, _ = self._merge_with_fakes(jobs, repo, head)
                    self.assertEqual(status, 409, payload)
                    self.assertFalse(payload.get("merged"), payload)
                    merge.assert_not_called()
            (jobs / job_id / "job.json").write_text("not-json")
            status, payload, merge, _ = self._merge_with_fakes(jobs, repo, head)
            self.assertEqual((status, payload["code"]), (409, "corrupt_test_evidence"))
            merge.assert_not_called()
            (jobs / job_id / "job.json").unlink()
            status, payload, merge, _ = self._merge_with_fakes(jobs, repo, head)
            self.assertEqual((status, payload["code"]), (409, "missing_test_evidence"))
            merge.assert_not_called()

    def test_merge_fresh_state_matrix_refuses_without_invoking_merge(self):
        head = "1" * 40
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, jobs = root / "repo", root / "jobs"
            repo.mkdir()
            self._write_merge_job(jobs, repo, head)
            fixtures = (
                ("empty_checks", self._strict_pr_view(head), {"ok": True, "data": []}),
                ("red_checks", self._strict_pr_view(head), self._strict_checks("fail")),
                ("pending_checks", self._strict_pr_view(head), self._strict_checks("pending")),
                ("unknown_checks", self._strict_pr_view(head), self._strict_checks("mystery")),
                ("malformed_checks", self._strict_pr_view(head), {"ok": True, "data": {}}),
                ("closed", self._strict_pr_view(head, state="CLOSED"), self._strict_checks()),
                ("draft", self._strict_pr_view(head, isDraft=True), self._strict_checks()),
                ("held", self._strict_pr_view(head, labels=[{"name": "hold:pm-review"}]), self._strict_checks()),
                ("blocking_review", self._strict_pr_view(head, reviewDecision="CHANGES_REQUESTED"), self._strict_checks()),
                ("unknown_review", self._strict_pr_view(head, reviewDecision="SURPRISE"), self._strict_checks()),
                ("conflicting", self._strict_pr_view(head, mergeable="CONFLICTING", mergeStateStatus="DIRTY"), self._strict_checks()),
                ("unknown_merge", self._strict_pr_view(head, mergeable="UNKNOWN", mergeStateStatus="UNKNOWN"), self._strict_checks()),
                ("inconsistent", self._strict_pr_view(head, mergeStateStatus="BLOCKED"), self._strict_checks()),
            )
            for label, view, checks in fixtures:
                with self.subTest(label=label):
                    status, payload, merge, _ = self._merge_with_fakes(jobs, repo, head, view=view, checks=checks)
                    self.assertEqual(status, 409, payload)
                    merge.assert_not_called()

    def test_merge_binds_valid_evidence_to_match_head_and_never_retries_moved_head(self):
        head = "1" * 40
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, jobs = root / "repo", root / "jobs"
            repo.mkdir()
            self._write_merge_job(jobs, repo, head)
            status, payload, merge, _ = self._merge_with_fakes(jobs, repo, head)
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["merged"])
            argv = merge.call_args.args[1]
            self.assertIn("--match-head-commit", argv)
            self.assertEqual(argv[argv.index("--match-head-commit") + 1], head)

            moved = {"ok": False, "status": 400, "code": "cmd_failed", "error": "head commit does not match", "stderr": "head commit does not match"}
            status, payload, merge, _ = self._merge_with_fakes(jobs, repo, head, merge=moved)
            self.assertEqual((status, payload["code"]), (409, "head_moved"), payload)
            self.assertFalse(payload["merged"])
            merge.assert_called_once()

            status, payload, merge, _ = self._merge_with_fakes(jobs, repo, "2" * 40, view=self._strict_pr_view("2" * 40))
            self.assertEqual((status, payload["code"]), (409, "head_moved"), payload)
            merge.assert_not_called()

    def test_merge_refuses_head_change_between_fresh_and_confirmation_views(self):
        first_head, confirmed_head = "1" * 40, "2" * 40
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, jobs = root / "repo", root / "jobs"
            repo.mkdir()
            self._write_merge_job(jobs, repo, first_head)
            views = iter((self._strict_pr_view(first_head), self._strict_pr_view(confirmed_head)))

            def fake_json(_repo, argv):
                return next(views) if argv[1] == "view" else self._strict_checks()

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "gh_json", fake_json), \
                 mock.patch.object(server, "gh_text") as merge, \
                 mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")), \
                 mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("provider CLI forbidden")):
                status, payload = server.gh_pr_merge(self._merge_body(), {"r": str(repo)})
            self.assertEqual((status, payload["code"]), (409, "head_moved"), payload)
            merge.assert_not_called()

    def test_merge_refuses_confirmation_view_for_a_different_pr(self):
        head = "1" * 40
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, jobs = root / "repo", root / "jobs"
            repo.mkdir()
            self._write_merge_job(jobs, repo, head)
            views = iter((self._strict_pr_view(head), self._strict_pr_view(head, number=8)))

            def fake_json(_repo, argv):
                return next(views) if argv[1] == "view" else self._strict_checks()

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "gh_json", fake_json), \
                 mock.patch.object(server, "gh_text") as merge, \
                 mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")), \
                 mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("provider CLI forbidden")):
                status, payload = server.gh_pr_merge(self._merge_body(), {"r": str(repo)})
            self.assertEqual((status, payload["code"]), (409, "pr_state_mismatch"), payload)
            merge.assert_not_called()

    def test_optional_writer_session_evidence_is_strict_but_initial_green_needs_none(self):
        head = "1" * 40
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, jobs = root / "repo", root / "jobs"
            repo.mkdir()
            # Initial-green evidence intentionally has no writer reference.
            self._write_merge_job(jobs, repo, head)
            status, payload, _, _ = self._merge_with_fakes(jobs, repo, head)
            self.assertEqual(status, 200, payload)

            writer_id = "c" * 32
            profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
            identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                **_fixture_identity_profile(profile),
                "repo": "r", "branch": "feature", "starting_head": "0" * 40,
                "native_session_id": "session-7",
            }
            writer = {
                "job_id": writer_id, "kind": "agent", "status": "completed", "repo": "r", "cwd": str(repo.resolve()),
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", **profile,
                "session_identity": identity,
                "receipt": {"status": "ok", "job_id": writer_id, "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", "session_identity": identity, **profile, "failure_class": "none", "failure_code": "none", "failover_eligible": False},
            }
            wp = jobs / writer_id / "job.json"
            wp.parent.mkdir(parents=True)
            wp.write_text(json.dumps(writer) + "\n")
            self._write_merge_job(jobs, repo, head, evidence={"writer_job_id": writer_id})
            status, payload, _, _ = self._merge_with_fakes(jobs, repo, head)
            self.assertEqual(status, 200, payload)
            for label, broken in (
                ("running", {**writer, "status": "running"}),
                ("receipt", {**writer, "receipt": {**writer["receipt"], "status": "error"}}),
                ("repo", {**writer, "repo": "other"}),
                ("cwd", {**writer, "cwd": str(root / "other")}),
                ("session", {**writer, "session_identity": {**identity, "native_session_id": None}}),
                ("route", {**writer, "provider": "anthropic"}),
            ):
                with self.subTest(label=label):
                    wp.write_text(json.dumps(broken) + "\n")
                    status, payload, merge, _ = self._merge_with_fakes(jobs, repo, head)
                    self.assertEqual(status, 409, payload)
                    merge.assert_not_called()

    def test_precommit_test_and_codeoff_receipts_never_waive_final_evidence(self):
        head = "1" * 40
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, jobs = root / "repo", root / "jobs"
            repo.mkdir()
            precommit_id = "d" * 32
            precommit = {"job_id": precommit_id, "kind": "test", "status": "completed", "script": "catalog-compile", "cwd": str(repo), "returncode": 0, "receipt": {"status": "ok", "job_id": precommit_id}}
            pp = jobs / precommit_id / "job.json"
            pp.parent.mkdir(parents=True)
            pp.write_text(json.dumps(precommit) + "\n")
            status, payload, merge, _ = self._merge_with_fakes(jobs, repo, head, body=self._merge_body(precommit_id))
            self.assertEqual(status, 409, payload)
            merge.assert_not_called()
            for extra in (
                {"codeoff_experiment_id": "experiment-0001"},
                {"final_verification_hash": "e" * 64},
            ):
                status, payload, merge, _ = self._merge_with_fakes(jobs, repo, head, body=self._merge_body("e" * 32, **extra))
                self.assertEqual((status, payload["code"]), (400, "codeoff_gate_incomplete"), payload)
                merge.assert_not_called()
            complete = {"codeoff_experiment_id": "experiment-0001", "final_verification_hash": "e" * 64}
            with mock.patch.object(server, "_codeoff_git_gate", return_value=None) as issue67_gate:
                status, payload, merge, _ = self._merge_with_fakes(jobs, repo, head, body=self._merge_body("e" * 32, **complete))
            self.assertEqual((status, payload["code"]), (409, "missing_test_evidence"), payload)
            issue67_gate.assert_called_once_with(mock.ANY, "r", repo, "push")
            merge.assert_not_called()

    def test_keyed_repo_lock_serializes_evidence_tests_git_writes_and_merge(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature"], check=True, capture_output=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True)
            self.assertIs(server.repo_lock(repo), server.repo_lock(repo.resolve()))
            self.assertIsNot(server.repo_lock(repo), server.repo_lock(root / "other"))

            def blocked_while_held(target):
                started, done = threading.Event(), threading.Event()
                def run():
                    started.set()
                    target()
                    done.set()
                lock = server.repo_lock(repo)
                with lock:
                    thread = threading.Thread(target=run)
                    thread.start()
                    self.assertTrue(started.wait(1))
                    self.assertFalse(done.wait(.05), "same-repo operation escaped the keyed lock")
                thread.join(2)
                self.assertTrue(done.is_set())

            blocked_while_held(lambda: server.git_checkout(
                json.dumps({"repo": "r", "branch": "feature"}).encode(), {"r": str(repo)}
            ))

            jobs = root / "jobs"
            test_id = "f" * 32
            evidence = {"version": "pr-merge-evidence-v1", "repo": "r", "cwd": str(repo.resolve()), "pr": "7", "run_id": "run-7", "recipe": "catalog-compile", "expected_head": head, "live_head": head, "head_ref": "feature", "start": {"branch": "feature", "sha": head, "clean": True}, "final": None}
            test_job = {"job_id": test_id, "kind": "test", "status": "queued", "script": "catalog-compile", "argv": [sys.executable, "-c", "pass"], "cwd": str(repo), "timeout_seconds": 30, "log_ref": str(jobs / test_id / "stdout.log"), "response_webhook_url": None, "response_webhook_token": None, "merge_evidence": evidence}
            (jobs / test_id).mkdir(parents=True)
            (jobs / test_id / "job.json").write_text(json.dumps(test_job) + "\n")
            with mock.patch.object(server, "JOBS_DIR", jobs), mock.patch.object(server, "deliver_webhook", return_value=None), mock.patch.object(server, "herdr_job_done", return_value=None):
                blocked_while_held(lambda: server.run_script_job(test_id))
            self.assertEqual(json.loads((jobs / test_id / "job.json").read_text())["status"], "completed")

            # The merge wrapper uses the identical key, so it cannot overtake
            # a Graphwing checkout/test operation for this repo.
            self._write_merge_job(jobs, repo, head)
            with mock.patch.object(server, "JOBS_DIR", jobs), mock.patch.object(server, "gh_json", side_effect=lambda _p, a: self._strict_pr_view(head) if a[1] == "view" else self._strict_checks()), mock.patch.object(server, "gh_text", return_value={"ok": True, "stdout": "merged"}):
                blocked_while_held(lambda: server.gh_pr_merge(self._merge_body(), {"r": str(repo)}))

    def test_pr_drive_has_one_final_evidence_leg_and_no_merge_bypass(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "pr-drive.json").read_text())
        description = graph["description"].lower()
        self.assertIn("remote-ready", description)
        self.assertIn("final head-bound named test", description)
        self.assertIn("evidence gate", description)
        self.assertIn("optional merge", description)
        self.assertNotIn("green + reviews_ok is mergeable", description)
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        triples = {(e["source"], e.get("sourceHandle"), e["target"]) for e in edges}
        self.assertIn(("switch_review", "case-2", "join_final"), triples)
        self.assertIn(("switch_review", "default", "herdr_remote_not_ready"), triples)
        self.assertIn(("if_green2", "pass", "join_final"), triples)
        self.assertIn(("join_final", "out", "final_view"), triples)
        self.assertIn(("final_view", "success", "final_checkout"), triples)
        self.assertIn(("final_checkout", "success", "final_wait"), triples)
        self.assertIn(("final_wait", "pending", "final_test"), triples)
        self.assertIn(("final_wait", "out", "if_final_test"), triples)
        self.assertEqual(nodes["final_test"]["config"]["evidence_mode"], "pr_merge")
        self.assertEqual(nodes["final_test"]["config"]["response_webhook_url"], "{{ TASKS.final_wait.pending.resumeUrl }}")
        self.assertNotIn("evidence_mode", nodes["fix_test"]["config"])
        merge_cfg = nodes["merge"]["config"]
        self.assertEqual(merge_cfg["evidence_job_id"], "{{ TASKS.final_wait.request.body.job_id }}")
        self.assertEqual(merge_cfg["test"], "{{ CTX.INPUT.test }}")
        self.assertIn("WORKFLOW.runId", merge_cfg["run_id"])

        outgoing = {}
        for edge in edges:
            outgoing.setdefault(edge["source"], []).append(edge["target"])
        def reaches(start, goal, seen=None):
            seen = set() if seen is None else seen
            if start == goal:
                return True
            if start in seen:
                return False
            seen.add(start)
            return any(reaches(nxt, goal, seen.copy()) for nxt in outgoing.get(start, []))
        for edge in edges:
            if edge["target"] == "merge":
                self.assertEqual((edge["source"], edge.get("sourceHandle")), ("switch_merge", "case-0"))
        def merge_paths(node, path=()):
            current = (*path, node)
            if node == "merge":
                return [current]
            return [found for nxt in outgoing.get(node, []) if nxt not in current for found in merge_paths(nxt, current)]
        paths = merge_paths("join_start")
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue({"join_final", "final_view", "final_checkout", "final_wait", "if_final_test"} <= set(path), path)
        for source, handle in (("final_wait", "timeout"), ("final_wait", "failure"), ("if_final_test", "fail"), ("final_test", "failure")):
            targets = [e["target"] for e in edges if e["source"] == source and e.get("sourceHandle") == handle]
            self.assertTrue(targets, (source, handle))
            self.assertTrue(all(not reaches(target, "merge") for target in targets), (source, handle, targets))

    def test_pr_status_reports_remote_only_states_without_side_effects(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "pr-status.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        types = {n["type"].lower() for n in nodes.values()}
        for banned in ("/v1/test/run", "/v1/agent/run", "/v1/git/", "/v1/gh/pr/merge"):
            self.assertFalse(any(banned in node_type for node_type in types), banned)
        labels = " ".join(str(n.get("label", "")).lower() for n in nodes.values())
        for state in ("no checks", "red", "pending", "blocking review", "malformed", "unknown", "inconsistent"):
            self.assertIn(state, labels)
        ready = nodes["done_remote_ready"]
        dumped = json.dumps(ready).lower()
        self.assertIn("remote_ready", dumped)
        self.assertIn("named_test_required", dumped)
        self.assertNotIn('"mergeable"', dumped)
        for node_id in ("view", "checks"):
            self.assertEqual(nodes[node_id]["config"]["number"], "{{ CTX.INPUT.pr }}")
        self.assertFalse(nodes["hook"]["config"]["enabled"])
        ready_mappings = {m["output"]: m["expression"] for m in ready["config"]["mappings"]}
        self.assertEqual(ready_mappings["pr"], {"kind": "getField", "path": "CTX.INPUT.pr"})

    def test_pr_graph_action_configs_match_openapi_request_shapes(self):
        spec = json.loads((Path(server.__file__).parent / "openapi.json").read_text())
        ignored = {"integrationInstanceId", "timeout", "_comment"}
        for filename in ("pr-drive.json", "pr-status.json"):
            graph = json.loads((Path(server.__file__).parent / "graphs" / filename).read_text())
            for node in graph["spec"]["nodes"]:
                match = re.fullmatch(r"action\.graphwing\.(GET|POST):(.+)", node["type"])
                if not match:
                    continue
                method, route = match.groups()
                op = spec["paths"][route][method.lower()]
                parameters = []
                for parameter in op.get("parameters", []):
                    if "$ref" in parameter:
                        parameter = spec["components"]["parameters"][parameter["$ref"].split("/")[-1]]
                    parameters.append(parameter)
                allowed = {p.get("name") for p in parameters if isinstance(p, dict) and p.get("name")}
                schema = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
                if "$ref" in schema:
                    schema = spec["components"]["schemas"][schema["$ref"].split("/")[-1]]
                allowed |= set(schema.get("properties", {}))
                configured = set(node.get("config", {})) - ignored
                self.assertLessEqual(configured, allowed, (filename, node["id"], configured - allowed))

    def test_openapi_documents_narrow_merge_evidence_contract_exactly(self):
        spec = json.loads((Path(server.__file__).parent / "openapi.json").read_text())
        test_props = spec["components"]["schemas"]["TestRunRequest"]["properties"]
        self.assertEqual(
            {"evidence_mode", "repo", "pr", "run_id", "expected_head", "writer_job_id"},
            set(test_props) - {"name", "response_webhook_url", "response_webhook_token", "resume_url"},
        )
        merge = spec["components"]["schemas"]["GhPrMergeRequest"]
        self.assertEqual(set(merge["required"]), {"repo", "number", "auto_merge", "run_id", "test", "evidence_job_id"})
        self.assertIn("match-head", spec["paths"]["/v1/gh/pr/merge"]["post"]["summary"].lower())

    def test_git_commit_names_an_empty_commit_for_what_it_is(self):
        # A run handed the writer findings that were already fixed. It
        # correctly changed nothing, so nothing was staged, and git_commit
        # failed like any other error. In the trace that is indistinguishable
        # from a broken commit, and it cost two rounds of misdiagnosis. The
        # lock treats an empty commit as a failed slice, which stays true; it
        # just has to say which failure it is.
        def fake_run_git(path, argv):
            if argv[0] == "commit":
                return {"ok": False, "stdout": "nothing to commit, working tree clean",
                        "stderr": "", "returncode": 1}
            return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}

        with mock.patch.object(server, "run_git", fake_run_git):
            status, payload = server.git_commit(
                json.dumps({"repo": "r", "message": "fix: nothing"}).encode(),
                {"r": str(Path(server.__file__).parent)},
            )
        self.assertFalse(payload.get("ok"))
        self.assertEqual(payload.get("code"), "writer_job_required", payload)

    def test_pr_findings_extracts_the_machine_verdict_not_the_prose(self):
        # pr-drive took its fix instructions from CTX.INPUT.prompt, so a human
        # had to read the review and write them. The reviewers already publish
        # a machine-readable block; parse that instead.
        body = (
            "## Claude's Review\n> **Grade: B-** | Action: REQUEST_CHANGES\n"
            "prose a model should not have to parse\n"
            '<!-- engineering-findings-json\n'
            '{"findings":[{"category":"tests","severity":"major",'
            '"fingerprint":"shim-path","location":{"path":"a.sh","line":88},'
            '"remedy":"prepend rather than replace"}]}\n-->\n'
        )
        second = (
            '<!-- engineering-findings-json\n'
            '{"findings":[{"category":"correctness","severity":"major",'
            '"fingerprint":"mktemp-root","location":{"path":"a.sh","line":21},'
            '"remedy":"abort if mktemp fails"},'
            '{"category":"tests","severity":"major",'
            '"fingerprint":"shim-path","location":{"path":"a.sh","line":88},'
            '"remedy":"duplicate of the first reviewer"}]}\n-->\n'
        )
        out = server.pr_findings_from(
            labels=["grade-B-", "hold:pm-review"],
            comment_bodies=[body, "unrelated chatter", second],
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["grade"], "B-")
        self.assertEqual(out["holds"], ["hold:pm-review"])
        self.assertTrue(out["blocking"])
        # Two reviewers raised shim-path; it is one thing to fix, not two.
        prints = [f["fingerprint"] for f in out["findings"]]
        self.assertEqual(sorted(prints), ["mktemp-root", "shim-path"])
        self.assertEqual(out["major"], 2)
        # The brief is what the writer sees, so every finding must reach it.
        for fp in ("shim-path", "mktemp-root"):
            self.assertIn(fp, out["brief"])
        self.assertNotIn("prose a model should not have to parse", out["brief"])

    def test_pr_findings_blocking_means_would_block_a_merge(self):
        # "Any finding at all" never converges: at grade A the reviewer still
        # lists nits, so the loop would keep spending writer sessions forever.
        # The repo's own bar is review-hold.yml, which clears hold:pm-review at
        # A/A-. Match that: holds block, and so does a grade below the bar.
        # Findings on an A-graded PR are advisory and stay in the payload.
        graded_a = server.pr_findings_from(
            labels=["grade-A"],
            comment_bodies=['<!-- engineering-findings-json\n'
                            '{"findings":[{"severity":"minor","fingerprint":"nit",'
                            '"remedy":"tidy"}]}\n-->'],
        )
        self.assertFalse(graded_a["blocking"], graded_a)
        self.assertEqual(len(graded_a["findings"]), 1, "advisory findings still reported")

        for label in ("grade-A-", "grade-A+"):
            self.assertFalse(server.pr_findings_from(labels=[label], comment_bodies=[])["blocking"])
        for label in ("grade-B+", "grade-B", "grade-C"):
            self.assertTrue(server.pr_findings_from(labels=[label], comment_bodies=[])["blocking"], label)

        # A hold blocks regardless of grade.
        held = server.pr_findings_from(labels=["grade-A", "hold:pm-review"], comment_bodies=[])
        self.assertTrue(held["blocking"])
        # No grade yet means the audit has not spoken; do not call that clear.
        self.assertTrue(server.pr_findings_from(labels=[], comment_bodies=[])["blocking"])

    def test_pr_findings_says_clear_when_nothing_blocks(self):
        # A green PR must not look like a PR whose findings failed to parse:
        # both would otherwise be an empty list.
        out = server.pr_findings_from(labels=["grade-A"], comment_bodies=["no markers here"])
        self.assertTrue(out["ok"])
        self.assertFalse(out["blocking"])
        self.assertEqual(out["findings"], [])
        self.assertEqual(out["grade"], "A")

    def test_pr_findings_survives_a_corrupt_marker(self):
        # A truncated or malformed block must not read as "no findings", which
        # would let the walker call a blocked PR clean.
        out = server.pr_findings_from(
            labels=["hold:codequality"],
            comment_bodies=["<!-- engineering-findings-json\n{not json at all\n-->"],
        )
        self.assertFalse(out["ok"])
        self.assertTrue(out["blocking"])
        self.assertEqual(out["code"], "unparsable_findings")

    def test_review_no_verdict_is_flagged_not_an_opinion(self):
        # "Reached max turns (1)" parsed as NACK, so a reviewer that never ran
        # counted as a reviewer that said no. Both SC-110290 review passes died
        # that way. Verdict stays NACK for the graph, but no_verdict makes a
        # provider blip distinguishable from a real rejection.
        self.assertTrue(server.review_said_nothing("Error: Reached max turns (1)"))
        self.assertTrue(server.review_said_nothing(""))
        self.assertFalse(server.review_said_nothing("VERDICT: PASS"))
        self.assertFalse(server.review_said_nothing("VERDICT: NACK\nmissing a case"))

    def test_review_turn_budget_is_not_one(self):
        # A one-turn review cannot read the ticket and diff.
        self.assertGreaterEqual(server.REVIEW_MAX_TURNS, 8)

    def test_direct_reviewer_routes_are_always_vendor_independent(self):
        vendor = {
            "openai": "openai", "anthropic": "anthropic", "xai": "xai",
        }
        reviewing = set()
        for work_kind in ("go_coding", "typescript_coding", "research_ops"):
            for cls in ("mechanical", "visual", "sensitive"):
                for size in ("S", "M", "L"):
                    route = server.slice_route_lookup(cls, size, work_kind=work_kind)
                    writer = vendor[route["provider"]]
                    for slot in ("reviewer1", "reviewer2"):
                        launcher = route[f"{slot}_launcher"]
                        if launcher == "none":
                            continue
                        reviewer = vendor[route[f"{slot}_provider"]]
                        reviewing.add(reviewer)
                        self.assertNotEqual(
                            reviewer, writer,
                            f"{work_kind}/{cls}/{size}: {slot} shares vendor {writer}",
                        )
                        self.assertIn(launcher, server.NATIVE_LAUNCHERS)
                        self.assertIn(
                            route[f"{slot}_model"], server.NATIVE_LAUNCHERS[launcher]["models"]
                        )
                    if route["reviewer2_launcher"] != "none":
                        self.assertNotEqual(
                            route["reviewer1_provider"], route["reviewer2_provider"],
                            f"{work_kind}/{cls}/{size}: reviewers share a provider",
                        )
        self.assertEqual(reviewing, {"anthropic", "openai", "xai"})

    def test_normal_route_contract_is_frozen_and_versioned(self):
        expected = {
            "go_coding": {
                "writer": ("codex", "openai", "gpt-5.6-sol", "high", "work_kind=go_coding"),
                "reviewers": (
                    ("claude", "anthropic", "claude-sonnet-5", "default", "different_provider_from_writer"),
                    ("grok", "xai", "grok-4.6", "default", "different_provider_from_writer_and_reviewer1"),
                ),
            },
            "typescript_coding": {
                "writer": ("claude", "anthropic", "claude-opus-5", "default", "work_kind=typescript_coding"),
                "reviewers": (
                    ("codex", "openai", "gpt-5.6-sol", "high", "different_provider_from_writer"),
                    ("grok", "xai", "grok-4.6", "default", "different_provider_from_writer_and_reviewer1"),
                ),
            },
            "research_ops": {
                "writer": ("grok", "xai", "grok-4.6", "default", "work_kind=research_ops"),
                "reviewers": (
                    ("codex", "openai", "gpt-5.6-sol", "high", "different_provider_from_writer"),
                    ("claude", "anthropic", "claude-opus-5", "default", "different_provider_from_writer_and_reviewer1"),
                ),
            },
        }
        for work_kind, contract in expected.items():
            route = server.slice_route_lookup("sensitive", "M", work_kind=work_kind)
            self.assertEqual(route["route_version"], "normal-v1")
            self.assertEqual(
                tuple(route[key] for key in ("launcher", "provider", "model", "effort", "reason")),
                contract["writer"],
            )
            for index, reviewer in enumerate(contract["reviewers"], 1):
                self.assertEqual(
                    tuple(route[f"reviewer{index}_{key}"] for key in ("launcher", "provider", "model", "effort", "reason")),
                    reviewer,
                )
        review_counts = {
            ("mechanical", "S"): 0,
            ("mechanical", "M"): 1,
            ("mechanical", "L"): 1,
            ("visual", "S"): 1,
            ("visual", "M"): 1,
            ("visual", "L"): 1,
            ("sensitive", "S"): 2,
            ("sensitive", "M"): 2,
            ("sensitive", "L"): 2,
        }
        for (class_name, size), count in review_counts.items():
            route = server.slice_route_lookup(class_name, size, seams=0, work_kind="go_coding")
            actual = sum(route[f"reviewer{i}_launcher"] != "none" for i in (1, 2))
            self.assertEqual(actual, count, f"{class_name}/{size}")

    def test_normal_v1_route_matrix_is_byte_for_byte_unchanged(self):
        routes = [
            server.slice_route_lookup(class_name, size, work_kind=work_kind)
            for work_kind in ("go_coding", "typescript_coding", "research_ops")
            for class_name in ("mechanical", "visual", "sensitive")
            for size in ("S", "M", "L")
        ]
        encoded = json.dumps(routes, separators=(",", ":"), ensure_ascii=True).encode()
        self.assertEqual(
            hashlib.sha256(encoded).hexdigest(),
            "f173c6543685d38a1cf67c2e1c7ffe01ccb54f98b5eba00e9e8a8d2ae5e234ed",
        )

    def test_slice_route_current_matrix_is_frozen(self):
        writers = {
            "go_coding": ("codex", "openai", "gpt-5.6-sol", "high"),
            "typescript_coding": ("claude", "anthropic", "claude-opus-5", "default"),
            "research_ops": ("grok", "xai", "grok-4.6", "default"),
        }
        reviewers = {
            "openai": (
                ("claude", "anthropic", "claude-sonnet-5", "default"),
                ("grok", "xai", "grok-4.6", "default"),
            ),
            "anthropic": (
                ("codex", "openai", "gpt-5.6-sol", "high"),
                ("grok", "xai", "grok-4.6", "default"),
            ),
            "xai": (
                ("codex", "openai", "gpt-5.6-sol", "high"),
                ("claude", "anthropic", "claude-opus-5", "default"),
            ),
        }
        budgets = {
            ("mechanical", "S"): (10, 120),
            ("mechanical", "M"): (30, 300),
            ("mechanical", "L"): (50, 600),
            ("visual", "S"): (10, 180),
            ("visual", "M"): (30, 600),
            ("visual", "L"): (50, 900),
            ("sensitive", "S"): (10, 180),
            ("sensitive", "M"): (30, 600),
            ("sensitive", "L"): (50, 900),
        }
        omitted = object()

        def dispatch_route(class_name, size_floor, work_kind, ac_count=omitted, seams=omitted):
            request = {"class": class_name, "size": size_floor, "work_kind": work_kind}
            if ac_count is not omitted:
                request["ac_count"] = ac_count
            if seams is not omitted:
                request["seams"] = seams
            status, payload, _ = server.dispatch(
                "POST", "/v1/slice/route", {}, True, json.dumps(request).encode()
            )
            self.assertEqual(status, 200, payload)
            return payload

        def expected_route(class_name, size_floor, effective_size, work_kind):
            writer = writers[work_kind]
            if class_name == "sensitive":
                review_count = 2
            elif class_name == "mechanical" and effective_size == "S":
                review_count = 0
            else:
                review_count = 1
            selected = list(reviewers[writer[1]][:review_count])
            selected.extend([("none", "none", "none", "none")] * (2 - len(selected)))
            reviewer1, reviewer2 = selected
            turns, wall = budgets[(class_name, effective_size)]
            expected = {
                "ok": True,
                "route_version": "normal-v1",
                "class": class_name,
                "work_kind": work_kind,
                "size_floor": size_floor,
                "size": effective_size,
                "launcher": writer[0],
                "provider": writer[1],
                "model": writer[2],
                "effort": writer[3],
                "reason": f"work_kind={work_kind}",
                "max_turns": turns,
                "run_budget_seconds": wall,
                "reviewer1": reviewer1[2],
                "reviewer1_launcher": reviewer1[0],
                "reviewer1_provider": reviewer1[1],
                "reviewer1_model": reviewer1[2],
                "reviewer1_effort": reviewer1[3],
                "reviewer1_reason": (
                    "different_provider_from_writer"
                    if reviewer1[0] != "none"
                    else "not_required_by_review_policy"
                ),
                "reviewer2": reviewer2[2],
                "reviewer2_launcher": reviewer2[0],
                "reviewer2_provider": reviewer2[1],
                "reviewer2_model": reviewer2[2],
                "reviewer2_effort": reviewer2[3],
                "reviewer2_reason": (
                    "different_provider_from_writer_and_reviewer1"
                    if reviewer2[0] != "none"
                    else "not_required_by_review_policy"
                ),
                "review": (
                    "none"
                    if reviewer1[0] == "none"
                    else (
                        f"{reviewer1[2]}_{reviewer2[2]}"
                        if reviewer2[0] != "none"
                        else reviewer1[2]
                    )
                ),
            }
            for role, profile in (
                ("writer", writer),
                ("reviewer1", reviewer1),
                ("reviewer2", reviewer2),
            ):
                if profile[0] == "none":
                    continue
                expected[f"{role}_execution_profile"] = {
                    "version": "route-execution-profile-v1",
                    "route_version": "normal-v1",
                    "role": role,
                    "work_kind": work_kind,
                    "class": class_name,
                    "size": effective_size,
                    "launcher": profile[0],
                    "provider": profile[1],
                    "model": profile[2],
                    "effort": profile[3],
                }
            return expected

        # With seams omitted, normal-v1 treats every visual floor below L as a
        # one-step bump. All 27 cases cross the public parsing/dispatch boundary.
        matrix = {
            ("mechanical", "S"): "S",
            ("mechanical", "M"): "M",
            ("mechanical", "L"): "L",
            ("visual", "S"): "M",
            ("visual", "M"): "L",
            ("visual", "L"): "L",
            ("sensitive", "S"): "S",
            ("sensitive", "M"): "M",
            ("sensitive", "L"): "L",
        }
        for work_kind in writers:
            for (class_name, size_floor), effective_size in matrix.items():
                with self.subTest(work_kind=work_kind, class_name=class_name, size=size_floor):
                    self.assertEqual(
                        dispatch_route(class_name, size_floor, work_kind),
                        expected_route(class_name, size_floor, effective_size, work_kind),
                    )

        bump_cases = (
            ("mechanical", "S", omitted, omitted, "S"),
            ("mechanical", "S", 5, omitted, "S"),
            ("mechanical", "S", 6, omitted, "M"),
            ("mechanical", "M", 6, omitted, "L"),
            ("mechanical", "L", 6, omitted, "L"),
            ("visual", "S", omitted, omitted, "M"),
            ("visual", "S", omitted, 1, "M"),
            ("visual", "S", omitted, 0, "S"),
            ("visual", "S", omitted, 2, "S"),
            ("visual", "S", 6, 0, "M"),
            # Both bump conditions are true, but normal-v1 applies one step only.
            ("visual", "S", 6, omitted, "M"),
            ("visual", "S", 6, 1, "M"),
        )
        for class_name, floor, ac_count, seams, effective_size in bump_cases:
            with self.subTest(
                class_name=class_name,
                floor=floor,
                ac_count="omitted" if ac_count is omitted else ac_count,
                seams="omitted" if seams is omitted else seams,
            ):
                self.assertEqual(
                    dispatch_route(class_name, floor, "go_coding", ac_count, seams),
                    expected_route(class_name, floor, effective_size, "go_coding"),
                )

    def test_agent_effort_schema_is_closed_and_receipt_is_sanitized(self):
        schemas = json.loads(server.openapi_bytes())["components"]["schemas"]
        values = ["default", "low", "medium", "high", "max"]
        self.assertEqual(schemas["AgentRunRequest"]["properties"]["effort"]["enum"], values)
        self.assertEqual(schemas["SessionIdentity"]["properties"]["effective_effort"]["enum"], values)
        receipt = schemas["AgentReceipt"]
        self.assertFalse(receipt["additionalProperties"])
        for field in ("requested_effort", "effective_effort", "effort_source", "launcher_version"):
            self.assertIn(field, receipt["required"])
            self.assertIn(field, receipt["properties"])
        self.assertEqual(
            receipt["properties"]["effort_source"]["enum"],
            ["route", "explicit", "launcher_default", None],
        )
        for forbidden in ("prompt", "log", "log_ref", "path", "cwd", "token", "provider_output"):
            self.assertNotIn(forbidden, receipt["properties"])

    def test_review_openapi_contract_is_closed_typed_and_complete(self):
        spec = json.loads(server.openapi_bytes())
        schemas = spec["components"]["schemas"]
        request = schemas["ReviewRunRequest"]
        self.assertFalse(request["additionalProperties"])
        self.assertEqual(
            set(request["required"]), {"repo", "launcher", "provider", "model", "prompt"}
        )
        self.assertEqual(set(request["properties"]), {
            "repo", "launcher", "provider", "model", "prompt", "effort", "async",
            "run_budget_seconds", "max_turns", "response_webhook_url",
            "response_webhook_token", "route_execution_profile",
        })
        identity = schemas["ReviewExecutionIdentity"]
        self.assertFalse(identity["additionalProperties"])
        self.assertEqual(
            set(identity["properties"]),
            set(identity["required"]) | {"route_execution_profile"},
        )
        self.assertEqual(
            identity["properties"]["route_execution_profile"]["$ref"],
            "#/components/schemas/RouteExecutionProfile",
        )
        self.assertEqual(identity["properties"]["version"]["const"], "review-execution-v1")
        self.assertEqual(identity["properties"]["kind"]["const"], "review")
        receipt = schemas["ReviewReceipt"]
        job = schemas["ReviewJob"]
        review_error = schemas["ReviewError"]
        for schema in (receipt, job, review_error):
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(set(review_error["properties"]), {"error", "code"})
        for field in ("created_at", "started_at", "finished_at"):
            self.assertEqual(
                job["properties"][field]["pattern"],
                "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$",
            )
        self.assertEqual(
            receipt["properties"]["execution_identity"],
            {"$ref": "#/components/schemas/ReviewExecutionIdentity"},
        )
        self.assertEqual(
            job["properties"]["execution_identity"],
            {"$ref": "#/components/schemas/ReviewExecutionIdentity"},
        )
        run_responses = spec["paths"]["/v1/review/run"]["post"]["responses"]
        self.assertEqual(
            set(run_responses), {"200", "202", "400", "401", "409", "501", "504"}
        )
        self.assertEqual(
            run_responses["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ReviewReceipt",
        )
        self.assertEqual(
            run_responses["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ReviewJob",
        )
        poll = spec["paths"]["/v1/review/jobs/{job_id}"]["get"]
        self.assertEqual(set(poll["responses"]), {"200", "400", "401", "404", "409"})
        wait = spec["paths"]["/v1/review/jobs/wait"]["post"]
        self.assertEqual(set(wait["responses"]), {"200", "400", "401", "404", "408", "409"})

    def test_review_401_runtime_bodies_match_exact_draft_2020_12_schemas(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:  # catalog CI intentionally installs no third-party packages
            Draft202012Validator = None

        spec = json.loads(server.openapi_bytes())

        def dereference(value):
            if isinstance(value, dict):
                if set(value) == {"$ref"} and value["$ref"].startswith("#/"):
                    target = spec
                    for part in value["$ref"][2:].split("/"):
                        target = target[part.replace("~1", "/").replace("~0", "~")]
                    return dereference(target)
                return {key: dereference(item) for key, item in value.items()}
            if isinstance(value, list):
                return [dereference(item) for item in value]
            return value

        cases = (
            ("POST", "/v1/review/run", "/v1/review/run", "post", b"{}"),
            (
                "GET",
                "/v1/review/jobs/" + "a" * 32,
                "/v1/review/jobs/{job_id}",
                "get",
                b"",
            ),
            ("POST", "/v1/review/jobs/wait", "/v1/review/jobs/wait", "post", b"{}"),
        )
        expected = {
            "error": "invalid or missing X-Graphwing-Key",
            "code": "unauthorized",
        }
        for method, runtime_path, spec_path, operation, body in cases:
            with self.subTest(path=spec_path):
                status, payload, _ = server.dispatch(
                    method, runtime_path, {}, False, body
                )
                self.assertEqual((status, payload), (401, expected))
                response = dereference(
                    spec["paths"][spec_path][operation]["responses"]["401"]
                )
                schema = dereference(response["content"]["application/json"]["schema"])
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["required"]), {"error", "code"})
                self.assertEqual(set(schema["properties"]), {"error", "code"})

                def valid(instance):
                    if Draft202012Validator is not None:
                        return not list(Draft202012Validator(schema).iter_errors(instance))
                    if set(instance) != set(schema["required"]):
                        return False
                    return all(
                        isinstance(instance[field], str)
                        and len(instance[field]) >= rules.get("minLength", 0)
                        and len(instance[field]) <= rules.get("maxLength", len(instance[field]))
                        and (
                            "pattern" not in rules
                            or re.search(rules["pattern"], instance[field]) is not None
                        )
                        for field, rules in schema["properties"].items()
                    )

                self.assertTrue(valid(expected))
                for missing in ("error", "code"):
                    invalid = dict(expected)
                    invalid.pop(missing)
                    self.assertFalse(valid(invalid), missing)
                self.assertFalse(valid({**expected, "detail": "must stay closed"}))

    def test_agent_review_and_routed_graph_effort_are_real(self):
        spec = json.loads(server.openapi_bytes())
        schemas = spec["components"]["schemas"]
        agent_request = schemas["AgentRunRequest"]
        self.assertFalse(agent_request["additionalProperties"])
        self.assertEqual(
            agent_request["properties"]["effort"]["enum"],
            ["default", "low", "medium", "high", "max"],
        )

        review_request = schemas["ReviewRunRequest"]
        self.assertFalse(review_request["additionalProperties"])
        self.assertEqual(
            review_request["properties"]["effort"]["enum"],
            ["default", "low", "medium", "high", "max"],
        )
        raw_result = {
            "ok": True, "verdict": "PASS", "no_verdict": False,
            "summary": "provider prose must not escape", "returncode": 0,
        }
        body = json.dumps({
            "repo": "scratch",
            "launcher": "claude",
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "prompt": "bounded fixture",
            "effort": "medium",
        }).encode()
        with mock.patch.object(server, "native_review_result", return_value=raw_result) as review, \
             mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(
                 server, "enqueue_review", side_effect=AssertionError("review process forbidden")
             ):
            status, payload, _ = server.dispatch(
                "POST", "/v1/review/run", {}, True, body
            )
        self.assertEqual(status, 200, payload)
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["summary"], "review passed")
        self.assertEqual(payload["execution_identity"]["requested_effort"], "medium")
        self.assertEqual(payload["execution_identity"]["effective_effort"], "medium")
        self.assertEqual(payload["execution_identity"]["effort_source"], "explicit")
        self.assertNotIn("provider prose", json.dumps(payload))
        review.assert_called_once()
        args, kwargs = review.call_args
        self.assertEqual(kwargs, {})
        self.assertEqual(len(args), 1)
        context = args[0]
        self.assertIsInstance(context, server.NativeReviewExecutionContext)
        self.assertEqual(context.execution_identity, payload["execution_identity"])
        self.assertEqual(context.authority_identity, payload["execution_identity"])
        self.assertEqual((context.launcher, context.provider, context.model), (
            "claude", "anthropic", "claude-sonnet-5",
        ))
        self.assertEqual(context.repo, "scratch")
        self.assertEqual(context.resolved, self.scratch.resolve())
        self.assertEqual(context.prompt, "bounded fixture")
        self.assertEqual(context.run_budget_seconds, server.REVIEW_DEFAULT_BUDGET_SECONDS)
        self.assertEqual(context.max_turns, server.REVIEW_MAX_TURNS)
        self.assertEqual(context.requested_effort, "medium")
        self.assertEqual(context.effective_effort, "medium")
        self.assertEqual(context.effort_source, "explicit")
        self.assertEqual(context.permission_profile, "claude-plan")
        self.assertEqual(context.pinned_launcher.path.parent, Path("/proc/self/fd"))
        self.assertEqual(
            context.pinned_launcher.fingerprint,
            "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        )

        root = Path(server.__file__).resolve().parent
        expected_counts = {"implement-slice.json": 10, "pr-drive.json": 1}
        for graph_name, expected_count in expected_counts.items():
            graph = json.loads((root / "graphs" / graph_name).read_text())
            model_nodes = [
                node for node in graph["spec"]["nodes"]
                if node["type"].endswith(("/v1/agent/run", "/v1/review/run"))
            ]
            self.assertEqual(len(model_nodes), expected_count, graph_name)
            for node in model_nodes:
                self.assertIn("effort", node.get("config", {}), (graph_name, node["id"]))

    @staticmethod
    def _route_execution_profile(route, role):
        prefix = "" if role == "writer" else f"{role}_"
        return {
            "version": "route-execution-profile-v1",
            "route_version": route["route_version"],
            "role": role,
            "work_kind": route["work_kind"],
            "class": route["class"],
            "size": route["size"],
            "launcher": route[f"{prefix}launcher"],
            "provider": route[f"{prefix}provider"],
            "model": route[f"{prefix}model"],
            "effort": route[f"{prefix}effort"],
        }

    def test_routed_agent_acceptance_missing_binary_fallback_and_resume_are_closed(self):
        contracts = {
            "go_coding": ("codex", "openai", "gpt-5.6-sol"),
            "typescript_coding": ("claude", "anthropic", "claude-opus-5"),
            "research_ops": ("grok", "xai", "grok-4.6"),
        }
        for work_kind, primary in contracts.items():
            with self.subTest(work_kind=work_kind), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                jobs = root / "jobs"
                missing = root / f"missing-{primary[0]}"
                route = server.slice_route_lookup("sensitive", "M", work_kind=work_kind)
                evidence = self._route_execution_profile(route, "writer")
                self.assertEqual(route["writer_execution_profile"], evidence)
                request = {
                    "prompt": "provider-free routed fixture", "cwd": "scratch",
                    "launcher": primary[0], "provider": primary[1], "model": primary[2],
                    "effort": route["effort"], "route_execution_profile": evidence,
                    "response_webhook_url": "https://callback.invalid/fixture",
                }
                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.object(server, "resolve_launcher_binary_now", return_value=missing), \
                     mock.patch.object(server, "enqueue_agent", lambda job: None), \
                     mock.patch.object(server, "deliver_webhook", return_value=None):
                    status, accepted, _ = server.dispatch(
                        "POST", "/v1/agent/run", {}, True, json.dumps(request).encode()
                    )
                    self.assertEqual(status, 202, accepted)
                    self.assertEqual(accepted["effort_source"], "route")
                    self.assertEqual(accepted["session_identity"]["route_execution_profile"], evidence)
                    server.run_agent_job(accepted["job_id"])
                    failed_job = server.read_job(accepted["job_id"])
                    self.assertEqual(failed_job["receipt"]["failure_code"], "missing_binary")
                    primary_receipt = {**failed_job["receipt"], "role": "primary"}
                    fallback_status, fallback_route = server.slice_route_fallback(json.dumps({
                        "class": "sensitive", "size": "M", "work_kind": work_kind,
                        "primary_route": route, "primary_receipt": primary_receipt,
                    }).encode())
                    self.assertEqual(fallback_status, 200, fallback_route)
                    fallback_evidence = self._route_execution_profile(fallback_route, "writer")
                    self.assertEqual(fallback_route["writer_execution_profile"], fallback_evidence)

                    fallback_binary = root / f"fixture-{fallback_route['launcher']}"
                    fallback_binary.write_text("provider-free fixture")
                    fallback_request = {
                        "prompt": "fallback fixture", "cwd": "scratch",
                        "launcher": fallback_route["launcher"],
                        "provider": fallback_route["provider"],
                        "model": fallback_route["model"],
                        "effort": fallback_route["effort"],
                        "route_execution_profile": fallback_evidence,
                    }
                    with mock.patch.object(
                        server, "resolve_launcher_binary_now", return_value=fallback_binary
                    ):
                        fallback_accept_status, fallback_accept, _ = server.dispatch(
                            "POST", "/v1/agent/run", {}, True,
                            json.dumps(fallback_request).encode(),
                        )
                    self.assertEqual(fallback_accept_status, 202, fallback_accept)
                    self.assertEqual(fallback_accept["effort_source"], "route")
                    self.assertEqual(
                        fallback_accept["session_identity"]["route_execution_profile"],
                        fallback_evidence,
                    )

                    fallback_job = server.read_job(fallback_accept["job_id"])
                    self.assertIsInstance(fallback_job, dict)
                    assert fallback_job is not None
                    identity = deepcopy(fallback_job["session_identity"])
                    identity["native_session_id"] = f"fixture-{work_kind}"
                    fallback_job.update({
                        "status": "completed", "session_identity": identity,
                        "started_at": fallback_job["created_at"],
                        "finished_at": fallback_job["created_at"],
                    })
                    fallback_job["receipt"] = {
                        "status": "ok", "job_id": fallback_job["job_id"],
                        "launcher": fallback_job["launcher"],
                        "provider": fallback_job["provider"],
                        "model": fallback_job["model"],
                        **{key: fallback_job[key] for key in server.AGENT_PROFILE_FIELDS},
                        "session_identity": identity, "failure_class": "none",
                        "failure_code": "none", "failover_eligible": False,
                    }
                    _replace_persisted_job_fixture(fallback_job["job_id"], fallback_job)
                    recovery_status, recovery = server.derive_slice_recovery_route({
                        "class": "sensitive", "size": "M", "work_kind": work_kind,
                        "primary_route": route, "primary_receipt": primary_receipt,
                        "fallback_route": fallback_route,
                        "fallback_receipt": {
                            **fallback_job["receipt"], "role": "availability_fallback",
                        },
                    })
                    self.assertEqual(recovery_status, 200, recovery)
                    self.assertEqual(recovery["decision"], "fallback_retained")
                    self.assertEqual(
                        recovery["selected_route"]["writer_execution_profile"],
                        fallback_evidence,
                    )
                    correction = {
                        **fallback_request, "prompt": "same-session correction",
                        "session_identity": identity,
                        "resume_job_id": fallback_job["job_id"],
                    }
                    with mock.patch.object(
                        server, "resolve_launcher_binary_now", return_value=fallback_binary
                    ):
                        resume_status, resumed, _ = server.dispatch(
                            "POST", "/v1/agent/run", {}, True, json.dumps(correction).encode()
                        )
                    self.assertEqual(resume_status, 202, resumed)
                    self.assertEqual(resumed["effort_source"], "route")
                    self.assertEqual(resumed["session_identity"], identity)

                    for mutation in (
                        {"effort": "low" if fallback_route["effort"] != "low" else "medium"},
                        {"route_execution_profile": {**fallback_evidence, "route_version": "normal-v1"}},
                        {"route_execution_profile": {**fallback_evidence, "role": "reviewer1"}},
                    ):
                        with mock.patch.object(server, "enqueue_agent") as spawn:
                            rejected_status, rejected, _ = server.dispatch(
                                "POST", "/v1/agent/run", {}, True,
                                json.dumps({**correction, **mutation}).encode(),
                            )
                        self.assertEqual(rejected_status, 400, rejected)
                        spawn.assert_not_called()

    def test_routed_reviewer1_sync_reaches_real_native_effort_boundary_once(self):
        route = server.slice_route_lookup("sensitive", "M", work_kind="research_ops")
        evidence = self._route_execution_profile(route, "reviewer1")
        request = {
            "repo": "scratch", "launcher": evidence["launcher"],
            "provider": evidence["provider"], "model": evidence["model"],
            "effort": evidence["effort"], "route_execution_profile": evidence,
            "prompt": "real routed reviewer1 boundary fixture",
        }
        commands = []
        real_run = subprocess.run

        def fake_run(command, **kwargs):
            if "--output-last-message" not in command:
                return real_run(command, **kwargs)
            commands.append((list(command), kwargs))
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text(json.dumps({
                "status": "ok", "sha": None, "pr_url": None,
                "summary": "VERDICT: PASS",
            }))
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs"
            launcher = root / "codex-review-wrapper"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            launcher.chmod(0o755)
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=launcher), \
                 mock.patch.object(server, "git_diff", return_value={"diff": "fixture"}), \
                 mock.patch.object(server.subprocess, "run", side_effect=fake_run):
                status, payload, _ = server.dispatch(
                    "POST", "/v1/review/run", {}, True, json.dumps(request).encode()
                )

        self.assertEqual((status, payload["status"]), (200, "ok"), payload)
        self.assertEqual(payload["execution_identity"]["effort_source"], "route")
        self.assertEqual(
            payload["execution_identity"]["route_execution_profile"], evidence
        )
        self.assertEqual(len(commands), 1)
        command, kwargs = commands[0]
        self.assertEqual(command[command.index("--model") + 1], evidence["model"])
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(
            [value for value in command if value.startswith("model_reasoning_effort=")],
            [f"model_reasoning_effort={evidence['effort']}"],
        )
        self.assertIn("input", kwargs)

    def test_routed_reviewer2_async_reaches_real_native_effort_boundary_once(self):
        route = server.slice_route_lookup("sensitive", "M", work_kind="typescript_coding")
        evidence = self._route_execution_profile(route, "reviewer2")
        request = {
            "repo": "scratch", "launcher": evidence["launcher"],
            "provider": evidence["provider"], "model": evidence["model"],
            "effort": evidence["effort"], "route_execution_profile": evidence,
            "prompt": "real routed reviewer2 boundary fixture", "async": True,
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            launcher = self._write_grok_acp_fixture(root)
            capture = root / "capture.json"
            launches = []
            real_popen = subprocess.Popen

            def counted_popen(command, **kwargs):
                if len(command) > 1 and command[1] == "agent":
                    launches.append((list(command), kwargs))
                return real_popen(command, **kwargs)

            env = {
                "ACP_TEST_FIXTURE": json.dumps({
                    "chunks": [json.dumps({
                        "status": "ok", "sha": None, "pr_url": None,
                        "summary": "VERDICT: PASS",
                    })],
                }),
                "ACP_TEST_CAPTURE": str(capture),
                "XAI_API_KEY": "fixture-key",
            }
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "active_job_count", return_value=0), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=launcher), \
                 mock.patch.object(server, "git_diff", return_value={"diff": "fixture"}), \
                 mock.patch.object(server, "enqueue_review") as enqueue, \
                 mock.patch.object(server.subprocess, "Popen", side_effect=counted_popen), \
                 mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(server, "herdr_job_done"):
                status, accepted, _ = server.dispatch(
                    "POST", "/v1/review/run", {}, True, json.dumps(request).encode()
                )
                self.assertEqual(status, 202, accepted)
                self.assertIsInstance(accepted, dict)
                assert isinstance(accepted, dict)
                enqueue.assert_called_once()
                authority = server.review_authority(accepted["job_id"])
                self.assertIsNotNone(authority)
                assert authority is not None
                sealed = authority.snapshot()
                self.assertIsNotNone(sealed)
                assert sealed is not None
                self.assertEqual(
                    sealed["execution_identity"]["route_execution_profile"], evidence
                )
                server.run_review_job(accepted["job_id"])
                self.assertIsNone(server.review_authority(accepted["job_id"]))
                poll_status, polled, _ = server.dispatch(
                    "GET", accepted["poll"], {}, True, b""
                )

            self.assertEqual((poll_status, polled["status"]), (200, "completed"), polled)
            self.assertEqual(polled["receipt"]["status"], "ok")
            self.assertEqual(polled["execution_identity"]["effort_source"], "route")
            self.assertEqual(
                polled["execution_identity"]["route_execution_profile"], evidence
            )
            self.assertEqual(len(launches), 1)
            native = json.loads(capture.read_text())
            self.assertEqual(native["argv"], [
                "agent", "--model", evidence["model"],
                "--reasoning-effort", "high", "stdio",
            ])

    def test_native_routed_review_rejects_unsealed_or_drifted_evidence_before_spawn(self):
        route = server.slice_route_lookup("sensitive", "M", work_kind="go_coding")
        evidence = self._route_execution_profile(route, "reviewer1")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, _jobs, repo, launcher, _enqueue = self._queue_closed_review(
                root,
                effort=evidence["effort"],
                extra={
                    "launcher": evidence["launcher"],
                    "provider": evidence["provider"],
                    "model": evidence["model"],
                    "route_execution_profile": evidence,
                },
            )
            self.assertEqual(status, 202, accepted)
            self.assertIsInstance(accepted, dict)
            assert isinstance(accepted, dict)
            canonical = accepted["execution_identity"]
            authority = server.review_authority(accepted["job_id"])
            self.assertIsNotNone(authority)
            assert authority is not None
            snapshot = authority.snapshot()
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            reviewer2 = self._route_execution_profile(route, "reviewer2")
            cases = {
                "unsealed direct route claim": None,
                "missing evidence": {
                    key: value for key, value in canonical.items()
                    if key != "route_execution_profile"
                },
                "wrong role": {
                    **canonical,
                    "route_execution_profile": self._route_execution_profile(route, "writer"),
                },
                "wrong version": {
                    **canonical,
                    "route_execution_profile": {**evidence, "version": "route-execution-profile-v2"},
                },
                "profile mismatch": {
                    **canonical,
                    "route_execution_profile": {**evidence, "model": "claude-opus-5"},
                },
                "native permission profile drift": {
                    **canonical,
                    "permission_profile": "codex-read-only",
                },
                "coordinated identity and evidence drift": {
                    **canonical,
                    "launcher": reviewer2["launcher"],
                    "provider": reviewer2["provider"],
                    "model": reviewer2["model"],
                    "requested_effort": reviewer2["effort"],
                    "effective_effort": "high",
                    "route_execution_profile": reviewer2,
                },
            }
            for name, identity in cases.items():
                with self.subTest(name=name), \
                     mock.patch.object(
                         server, "current_branch_head",
                         return_value=(canonical["branch"], canonical["starting_head"], None),
                     ), \
                     mock.patch.object(
                         server.subprocess, "run",
                         side_effect=AssertionError("invalid routed review must not spawn"),
                     ) as spawn:
                    result = server.native_review_result(server.NativeReviewExecutionContext(
                        authority_identity=canonical,
                        execution_identity=identity or {},
                        repo=snapshot["repo"], resolved=repo.resolve(),
                        prompt=snapshot["prompt"], diff_text="",
                        launcher=snapshot["launcher"], provider=snapshot["provider"],
                        model=snapshot["model"],
                        requested_effort=snapshot["requested_effort"],
                        effective_effort=snapshot["effective_effort"],
                        effort_source=snapshot["effort_source"],
                        route_execution_profile=canonical["route_execution_profile"],
                        run_budget_seconds=snapshot["run_budget_seconds"],
                        max_turns=snapshot["max_turns"],
                        permission_profile=snapshot["permission_profile"],
                        pinned_launcher=authority.launcher,
                    ))
                self.assertFalse(result["ok"], result)
                self.assertIn(
                    result["code"],
                    {
                        "bad_effort_source",
                        "bad_route_execution_profile",
                        "route_execution_profile_mismatch",
                        "review_execution_identity_mismatch",
                    },
                )
                spawn.assert_not_called()

    def test_native_routed_review_binds_complete_identity_to_invocation_authority(self):
        route = server.slice_route_lookup("sensitive", "M", work_kind="go_coding")
        evidence = self._route_execution_profile(route, "reviewer1")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, _jobs, repo, launcher, _enqueue = self._queue_closed_review(
                root,
                effort=evidence["effort"],
                extra={
                    "launcher": evidence["launcher"],
                    "provider": evidence["provider"],
                    "model": evidence["model"],
                    "route_execution_profile": evidence,
                },
            )
            self.assertEqual(status, 202, accepted)
            self.assertIsInstance(accepted, dict)
            assert isinstance(accepted, dict)
            canonical = accepted["execution_identity"]
            authority = server.review_authority(accepted["job_id"])
            self.assertIsNotNone(authority)
            assert authority is not None
            snapshot = authority.snapshot()
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            actual_budget = canonical["run_budget_seconds"]
            actual_turns = canonical["max_turns"]
            actual_launcher_version = "sha256:" + hashlib.sha256(launcher.read_bytes()).hexdigest()
            self.assertEqual(canonical["repo"], "scratch")
            self.assertEqual(canonical["launcher_version"], actual_launcher_version)
            self.assertEqual(actual_budget, server.REVIEW_DEFAULT_BUDGET_SECONDS)
            self.assertEqual(actual_turns, server.REVIEW_MAX_TURNS)

            other_budget = (
                actual_budget + 1
                if actual_budget < server.REVIEW_MAX_BUDGET_SECONDS
                else actual_budget - 1
            )
            other_turns = (
                actual_turns + 1
                if actual_turns < server.REVIEW_MAX_REQUEST_TURNS
                else actual_turns - 1
            )
            different_launcher_version = "sha256:" + (
                "0" if actual_launcher_version[-1] != "0" else "1"
            ) * 64
            cases = {
                "repo": {**canonical, "repo": "other-repo"},
                "launcher_version": {
                    **canonical, "launcher_version": different_launcher_version,
                },
                "run_budget_seconds": {
                    **canonical, "run_budget_seconds": other_budget,
                },
                "max_turns": {**canonical, "max_turns": other_turns},
                "coordinated": {
                    **canonical,
                    "repo": "other-repo",
                    "launcher_version": different_launcher_version,
                    "run_budget_seconds": other_budget,
                    "max_turns": other_turns,
                },
            }
            for name, identity in cases.items():
                with self.subTest(name=name), \
                     mock.patch.object(
                         server, "current_branch_head",
                         return_value=(canonical["branch"], canonical["starting_head"], None),
                     ), \
                     mock.patch.object(
                         server.subprocess, "run",
                         side_effect=AssertionError("drifted review must not spawn"),
                     ) as spawn:
                    result = server.native_review_result(server.NativeReviewExecutionContext(
                        authority_identity=canonical,
                        execution_identity=identity,
                        repo=snapshot["repo"], resolved=repo.resolve(),
                        prompt=snapshot["prompt"], diff_text="",
                        launcher=snapshot["launcher"], provider=snapshot["provider"],
                        model=snapshot["model"],
                        requested_effort=snapshot["requested_effort"],
                        effective_effort=snapshot["effective_effort"],
                        effort_source=snapshot["effort_source"],
                        route_execution_profile=canonical["route_execution_profile"],
                        run_budget_seconds=actual_budget, max_turns=actual_turns,
                        permission_profile=snapshot["permission_profile"],
                        pinned_launcher=authority.launcher,
                    ))
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["code"], "review_execution_identity_mismatch")
                spawn.assert_not_called()

    def test_routed_review_requires_exact_closed_route_evidence_before_boundary(self):
        route = server.slice_route_lookup("sensitive", "M", work_kind="go_coding")
        evidence = self._route_execution_profile(route, "reviewer1")
        self.assertEqual(route["reviewer1_execution_profile"], evidence)
        request = {
            "repo": "scratch", "launcher": evidence["launcher"],
            "provider": evidence["provider"], "model": evidence["model"],
            "effort": evidence["effort"], "route_execution_profile": evidence,
            "prompt": "closed routed review fixture",
        }
        result = {"ok": True, "verdict": "PASS", "no_verdict": False, "returncode": 0}
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "native_review_result", return_value=result) as boundary:
            status, payload, _ = server.dispatch(
                "POST", "/v1/review/run", {}, True, json.dumps(request).encode()
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["execution_identity"]["effort_source"], "route")
        self.assertEqual(payload["execution_identity"]["route_execution_profile"], evidence)
        boundary.assert_called_once()

        with tempfile.TemporaryDirectory() as td:
            async_status, accepted, _jobs, _repo, _launcher, _enqueue = (
                self._queue_closed_review(
                    Path(td),
                    effort=evidence["effort"],
                    extra={
                        "launcher": evidence["launcher"],
                        "provider": evidence["provider"],
                        "model": evidence["model"],
                        "route_execution_profile": evidence,
                    },
                )
            )
            self.assertEqual(async_status, 202, accepted)
            self.assertIsInstance(accepted, dict)
            assert isinstance(accepted, dict)
            poll_status, polled, _ = server.dispatch(
                "GET", accepted["poll"], {}, True, b""
            )
            self.assertIsInstance(polled, dict)
            assert isinstance(polled, dict)
            self.assertEqual((poll_status, polled["status"]), (200, "queued"))
            self.assertEqual(polled["execution_identity"]["effort_source"], "route")
            self.assertEqual(
                polled["execution_identity"]["route_execution_profile"], evidence
            )

        mutations = (
            {**evidence, "role": "writer"},
            {**evidence, "route_version": "availability-fallback-v1"},
            {**evidence, "model": "claude-opus-5"},
            {**evidence, "version": "route-execution-profile-v2"},
        )
        for changed in mutations:
            with self.subTest(changed=changed), \
                 mock.patch.object(server, "native_review_result") as boundary, \
                 mock.patch.object(server, "enqueue_review") as spawn:
                rejected_status, rejected, _ = server.dispatch(
                    "POST", "/v1/review/run", {}, True,
                    json.dumps({**request, "route_execution_profile": changed}).encode(),
                )
            self.assertEqual(rejected_status, 400, rejected)
            boundary.assert_not_called()
            spawn.assert_not_called()

        with mock.patch.object(server, "native_review_result") as boundary:
            claim_status, claim, _ = server.dispatch(
                "POST", "/v1/review/run", {}, True,
                json.dumps({**request, "route_execution_profile": {"effort_source": "route"}}).encode(),
            )
        self.assertEqual(claim_status, 400, claim)
        boundary.assert_not_called()

    def test_fallback_receipt_binds_execution_profile(self):
        route = server.slice_route_lookup("mechanical", "M", work_kind="go_coding")
        base_identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            "requested_effort": "high", "effective_effort": "high", "effort_source": "route",
            "launcher_version": "sha256:" + "a" * 64,
            "repo": "scratch", "branch": "main", "starting_head": "a" * 40,
            "native_session_id": None,
        }
        cases = (
            ({"effective_effort": "default"}, {}, "sha256:" + "a" * 64),
            ({}, {}, "missing"),
            ({}, {"requested_effort": "default", "effort_source": "launcher_default"}, "sha256:" + "a" * 64),
            ({}, {"provider": "anthropic"}, "sha256:" + "a" * 64),
        )
        for identity_change, receipt_change, receipt_version in cases:
            identity = {**base_identity, **identity_change}
            receipt = {
                "status": "error", "job_id": "a" * 32, "role": "primary",
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "requested_effort": "high", "effective_effort": identity["effective_effort"],
                "effort_source": "route", "launcher_version": receipt_version,
                "failure_class": "provider_availability", "failure_code": "provider_rate_limit",
                "failover_eligible": True, "session_identity": identity, **receipt_change,
            }
            stored_receipt = {key: value for key, value in receipt.items() if key != "role"}
            stored_job = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "requested_effort": receipt["requested_effort"],
                "effective_effort": identity["effective_effort"],
                "effort_source": receipt["effort_source"],
                "launcher_version": base_identity["launcher_version"], "status": "failed",
                "session_identity": identity, "receipt": stored_receipt,
            }
            body = json.dumps({
                "class": "mechanical", "size": "M", "work_kind": "go_coding",
                "primary_route": route, "primary_receipt": receipt,
            }).encode()
            with self.subTest(identity_change=identity_change, receipt_version=receipt_version), mock.patch.object(server, "read_job", return_value=stored_job):
                status, payload, _ = server.dispatch(
                    "POST", "/v1/slice/route/fallback", {}, True, body
                )
            self.assertEqual(status, 400, payload)
            self.assertEqual(payload["code"], "primary_execution_profile_mismatch")
            self.assertEqual(payload["diagnostic"]["code"], "primary_execution_profile_mismatch")

    def test_fallback_rejects_route_profile_drift_even_when_all_evidence_agrees(self):
        route = server.slice_route_lookup("mechanical", "M", work_kind="go_coding")
        route_profile = _fixture_execution_profile(
            "codex", "openai", "gpt-5.6-sol", requested=route["effort"], source="route"
        )
        base_identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(route_profile), "repo": "scratch", "branch": "main",
            "starting_head": "a" * 40, "native_session_id": None,
        }
        mutations = (
            {"requested_effort": "default"},
            {"effective_effort": "default"},
            {"effort_source": "explicit"},
            {"requested_effort": "default", "effective_effort": "high", "effort_source": "explicit"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                identity = {**base_identity, **mutation}
                profile = {**route_profile, **mutation}
                receipt = {
                    "status": "error", "job_id": "a" * 32, "role": "primary",
                    "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                    **profile, "failure_class": "provider_availability",
                    "failure_code": "provider_rate_limit", "failover_eligible": True,
                    "session_identity": identity,
                }
                stored_job = {
                    "job_id": receipt["job_id"], "status": "failed", "launcher": "codex",
                    "provider": "openai", "model": "gpt-5.6-sol", **profile,
                    "session_identity": identity,
                    "receipt": {key: value for key, value in receipt.items() if key != "role"},
                }
                body = json.dumps({
                    "class": "mechanical", "size": "M", "work_kind": "go_coding",
                    "primary_route": route, "primary_receipt": receipt,
                }).encode()
                with mock.patch.object(server, "read_job", return_value=stored_job):
                    status, payload = server.slice_route_fallback(body)
                self.assertEqual(
                    (status, payload.get("code")),
                    (400, "primary_execution_profile_mismatch"),
                    payload,
                )

    def test_fallback_rejects_cross_route_profile_substitution(self):
        primary = server.slice_route_lookup("mechanical", "M", work_kind="go_coding")
        expected = primary["writer_execution_profile"]
        substitutions = {
            "class": server.slice_route_lookup(
                "sensitive", "M", work_kind="go_coding"
            )["writer_execution_profile"],
            "size": server.slice_route_lookup(
                "mechanical", "L", work_kind="go_coding"
            )["writer_execution_profile"],
            "work_kind": {**expected, "work_kind": "typescript_coding"},
            "role": {**expected, "role": "reviewer1"},
            "profile_version": {
                **expected, "version": "route-execution-profile-v2"
            },
            "route_version": {
                **expected, "route_version": "availability-fallback-v1"
            },
        }
        for dimension, substituted in substitutions.items():
            with self.subTest(dimension=dimension), tempfile.TemporaryDirectory() as td:
                jobs = Path(td) / "jobs"
                body = self._provider_recovery_fixture(jobs, "provider_rate_limit")
                self.assertNotEqual(substituted, expected)
                if dimension in {"class", "size"}:
                    self.assertEqual(
                        tuple(
                            substituted[key]
                            for key in ("launcher", "provider", "model", "effort")
                        ),
                        tuple(
                            expected[key]
                            for key in ("launcher", "provider", "model", "effort")
                        ),
                    )

                receipt = body["primary_receipt"]
                receipt["session_identity"]["route_execution_profile"] = substituted
                stored_path = jobs / receipt["job_id"] / "job.json"
                stored = json.loads(stored_path.read_text())
                stored["session_identity"]["route_execution_profile"] = substituted
                stored["receipt"]["session_identity"]["route_execution_profile"] = substituted
                stored_path.write_text(json.dumps(stored) + "\n")

                with mock.patch.object(server, "JOBS_DIR", jobs):
                    status, payload = server.derive_slice_fallback_route({
                        key: body[key]
                        for key in (
                            "class", "size", "work_kind", "primary_route",
                            "primary_receipt",
                        )
                    })

                self.assertEqual(
                    (status, payload.get("code")),
                    (400, "primary_execution_profile_mismatch"),
                    payload,
                )

    def test_fallback_rejects_unvalidated_session_provenance(self):
        route = server.slice_route_lookup("mechanical", "M", work_kind="go_coding")
        profile = _fixture_execution_profile(
            "codex", "openai", "gpt-5.6-sol", requested=route["effort"], source="route"
        )
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "TOKEN_SECRET", "native_session_id": None,
        }
        receipt = {
            "status": "error", "job_id": "a" * 32, "role": "primary",
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **profile, "failure_class": "provider_availability",
            "failure_code": "provider_rate_limit", "failover_eligible": True,
            "session_identity": identity,
        }
        stored_job = {
            "job_id": receipt["job_id"], "status": "failed", "launcher": "codex",
            "provider": "openai", "model": "gpt-5.6-sol", **profile,
            "session_identity": identity,
            "receipt": {key: value for key, value in receipt.items() if key != "role"},
        }
        with mock.patch.object(server, "read_job", return_value=stored_job):
            status, payload = server.slice_route_fallback(json.dumps({
                "class": "mechanical", "size": "M", "work_kind": "go_coding",
                "primary_route": route, "primary_receipt": receipt,
            }).encode())
        self.assertEqual((status, payload.get("code")), (400, "primary_execution_profile_mismatch"), payload)
        self.assertNotIn("TOKEN_SECRET", json.dumps(payload))

    def test_availability_fallback_route_locked_matrix_and_reviewers(self):
        expected = {
            "go_coding": (("codex", "openai", "gpt-5.6-sol"), ("claude", "anthropic", "claude-opus-5", "default")),
            "typescript_coding": (("claude", "anthropic", "claude-opus-5"), ("codex", "openai", "gpt-5.6-sol", "high")),
            "research_ops": (("grok", "xai", "grok-4.6"), ("claude", "anthropic", "claude-sonnet-5", "default")),
        }
        for work_kind, (primary, fallback) in expected.items():
            primary_route = server.slice_route_lookup("sensitive", "M", work_kind=work_kind)
            self.assertEqual(tuple(primary_route[k] for k in ("launcher", "provider", "model")), primary)
            profile = _fixture_execution_profile(*primary, requested=primary_route["effort"], source="route")
            identity = {
                "launcher": primary[0], "provider": primary[1], "model": primary[2],
                **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
                "starting_head": "a" * 40, "native_session_id": None,
                "route_execution_profile": primary_route["writer_execution_profile"],
            }
            receipt = {"status": "error", "job_id": "a" * 32, "role": "primary", "launcher": primary[0], "provider": primary[1], "model": primary[2], **profile, "failure_class": "provider_availability", "failure_code": "provider_rate_limit", "failover_eligible": True, "session_identity": identity}
            stored_job = {
                "job_id": receipt["job_id"], "kind": "agent", "repo": "scratch",
                "launcher": primary[0], "provider": primary[1], "model": primary[2],
                **profile,
                "status": "failed", "session_identity": receipt["session_identity"],
                "receipt": {key: value for key, value in receipt.items() if key != "role"},
            }
            with mock.patch.object(server, "read_job", return_value=stored_job):
                status, payload, _ = server.dispatch(
                    "POST", "/v1/slice/route/fallback", {}, True,
                    json.dumps({"class": "sensitive", "size": "M", "work_kind": work_kind, "primary_route": primary_route, "primary_receipt": receipt}).encode(),
                )
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["route_version"], "availability-fallback-v1")
            self.assertEqual(tuple(payload[k] for k in ("launcher", "provider", "model", "effort")), fallback)
            self.assertEqual((payload["role"], payload["primary_route_version"], payload["primary_reason"]), ("availability_fallback", "normal-v1", f"work_kind={work_kind}"))
            self.assertEqual(payload["fallback_code"], "provider_rate_limit")
            self.assertEqual(payload["selected_alternate_route"]["provider"], fallback[1])
            for slot in ("reviewer1", "reviewer2"):
                self.assertNotEqual(payload[f"{slot}_launcher"], "none")
                self.assertNotEqual(payload[f"{slot}_provider"], payload["provider"])
            self.assertNotEqual(payload["reviewer1_provider"], primary[1])
            self.assertNotEqual(payload["reviewer1_provider"], payload["reviewer2_provider"])

    def test_availability_fallback_route_fails_closed(self):
        primary_route = server.slice_route_lookup("mechanical", "M", work_kind="go_coding")
        profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol", requested="high", source="route")
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "a" * 40, "native_session_id": None,
            "route_execution_profile": primary_route["writer_execution_profile"],
        }
        receipt = {"status": "error", "job_id": "a" * 32, "role": "primary", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", **profile, "failure_class": "provider_availability", "failure_code": "missing_binary", "failover_eligible": True, "session_identity": identity}
        base = {"class": "mechanical", "size": "M", "work_kind": "go_coding", "primary_route": primary_route, "primary_receipt": receipt}
        for patch, code in (
            ({"primary_receipt": {**receipt, "status": "timeout"}}, "not_fallback_eligible"),
            ({"primary_receipt": {k: v for k, v in receipt.items() if k != "role"}}, "not_fallback_eligible"),
            ({"primary_receipt": {**receipt, "role": "availability_fallback"}}, "not_fallback_eligible"),
            ({"primary_receipt": {**receipt, "failure_class": "timeout_budget"}}, "not_fallback_eligible"),
            ({"primary_receipt": {**receipt, "failure_code": "unknown_failure"}}, "not_fallback_eligible"),
            ({"primary_receipt": {**receipt, "failover_eligible": False}}, "not_fallback_eligible"),
            ({"primary_receipt": {**receipt, "failover_eligible": "true"}}, "bad_failover_eligible"),
            ({"primary_receipt": {**receipt, "job_id": "../bad"}}, "bad_job_id"),
            ({"primary_receipt": {**receipt, "provider": "openai", "session_identity": {**identity, "launcher": "claude"}}}, "primary_provider_mismatch"),
            ({"primary_receipt": {**receipt, "provider": "openai", "session_identity": {**identity, "provider": "anthropic"}}}, "primary_provider_mismatch"),
            ({"primary_receipt": {**receipt, "session_identity": {**identity, "model": "claude-opus-5"}}}, "primary_provider_mismatch"),
            ({"primary_route": {**primary_route, "provider": "anthropic"}}, "primary_provider_mismatch"),
            ({"primary_route": {**primary_route, "reason": "forged"}}, "primary_provider_mismatch"),
        ):
            body = {**base, **patch}
            status, payload, _ = server.dispatch(
                "POST", "/v1/slice/route/fallback", {}, True, json.dumps(body).encode()
            )
            self.assertEqual(status, 400, (patch, payload))
            self.assertEqual(payload["code"], code)

    def test_availability_fallback_route_requires_stored_primary_receipt(self):
        primary_route = server.slice_route_lookup("mechanical", "M", work_kind="go_coding")
        profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol", requested="high", source="route")
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "a" * 40, "native_session_id": None,
            "route_execution_profile": primary_route["writer_execution_profile"],
        }
        receipt = {"status": "error", "job_id": "a" * 32, "role": "primary", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", **profile, "failure_class": "provider_availability", "failure_code": "missing_binary", "failover_eligible": True, "session_identity": identity}
        body = json.dumps({"class": "mechanical", "size": "M", "work_kind": "go_coding", "primary_route": primary_route, "primary_receipt": receipt}).encode()
        stored_receipt = {key: value for key, value in receipt.items() if key != "role"}
        valid_job = {"job_id": receipt["job_id"], "kind": "agent", "repo": "scratch", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", **profile, "status": "failed", "session_identity": identity, "receipt": stored_receipt}
        bad_jobs = (
            None,
            {**valid_job, "status": "queued"},
            {**valid_job, "session_identity": {**identity, "provider": "anthropic"}},
            {**valid_job, "receipt": {**stored_receipt, "failure_code": "provider_rate_limit"}},
        )
        for stored_job in bad_jobs:
            with self.subTest(stored_job=stored_job), mock.patch.object(server, "read_job", return_value=stored_job):
                status, payload, _ = server.dispatch("POST", "/v1/slice/route/fallback", {}, True, body)
            self.assertEqual(status, 400, payload)
            self.assertEqual(payload["code"], "primary_receipt_mismatch")

    def _provider_recovery_fixture(
        self,
        jobs: Path,
        failure_code: str = "missing_binary",
        *,
        primary_job_id: str = "1" * 32,
        primary_starting_head: str | None = None,
        primary_created_at: str = "2026-08-26T12:00:00Z",
        primary_finished_at: str = "2026-08-26T12:00:30Z",
        fallback_status: str = "completed",
        fallback_job_id: str = "2" * 32,
        fallback_starting_head: str | None = None,
        fallback_created_at: str = "2026-08-26T12:00:30Z",
        fallback_finished_at: str = "2026-08-26T12:01:00Z",
        fresh_primary: bool = False,
        fresh_created_at: str = "2026-08-26T12:02:00Z",
    ) -> dict:
        primary_route = server.slice_route_lookup(
            "mechanical", "M", work_kind="go_coding"
        )
        fallback_route = server.build_slice_route(
            "mechanical",
            "M",
            None,
            None,
            "go_coding",
            server.AVAILABILITY_FALLBACK_WRITER_ROUTES["go_coding"],
            server.FALLBACK_ROUTE_VERSION,
            f"availability_fallback:{failure_code}",
            "openai",
        )
        selected = {
            key: fallback_route[key]
            for key in ("route_version", "launcher", "provider", "model", "effort", "reason")
        }
        fallback_route.update(
            {
                "role": "availability_fallback",
                "primary_route_version": "normal-v1",
                "primary_reason": "work_kind=go_coding",
                "fallback_reason": f"availability_fallback:{failure_code}",
                "fallback_code": failure_code,
                "selected_alternate_route": selected,
            }
        )

        initial_head = "a" * 40

        def execution_profile(route):
            return _fixture_execution_profile(
                route["launcher"], route["provider"], route["model"],
                requested=route["effort"], source="route",
            )

        def identity(route, native_session_id, starting_head=initial_head):
            profile = execution_profile(route)
            return {
                "launcher": route["launcher"],
                "provider": route["provider"],
                "model": route["model"],
                **_fixture_identity_profile(profile),
                "repo": "scratch",
                "branch": "main",
                "starting_head": starting_head,
                "native_session_id": native_session_id,
                "route_execution_profile": route["writer_execution_profile"],
            }

        primary_identity = identity(
            primary_route, None, primary_starting_head or initial_head
        )
        fallback_identity = identity(
            fallback_route,
            f"fallback-session-{fallback_job_id[:8]}",
            fallback_starting_head or initial_head,
        )
        primary_profile = execution_profile(primary_route)
        fallback_profile = execution_profile(fallback_route)
        primary_receipt = {
            "status": "error",
            "job_id": primary_job_id,
            "role": "primary",
            "launcher": primary_route["launcher"],
            "provider": primary_route["provider"],
            "model": primary_route["model"],
            "session_identity": primary_identity,
            **primary_profile,
            "failure_class": "provider_availability",
            "failure_code": failure_code,
            "failover_eligible": True,
            "diagnostic": server.compact_diagnostic(failure_code),
        }
        fallback_receipt = {
            "status": "ok",
            "job_id": fallback_job_id,
            "role": "availability_fallback",
            "launcher": fallback_route["launcher"],
            "provider": fallback_route["provider"],
            "model": fallback_route["model"],
            "session_identity": fallback_identity,
            **fallback_profile,
            "failure_class": "none",
            "failure_code": "none",
            "failover_eligible": False,
            "diagnostic": server.compact_diagnostic("none"),
        }

        def write(job):
            path = jobs / job["job_id"] / "job.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(job, indent=2) + "\n")

        write(
            {
                "job_id": primary_receipt["job_id"],
                "kind": "agent",
                "status": "failed",
                "repo": "scratch",
                "created_at": primary_created_at,
                "finished_at": primary_finished_at,
                "launcher": primary_route["launcher"],
                "provider": primary_route["provider"],
                "model": primary_route["model"],
                **primary_profile,
                "session_identity": primary_identity,
                "receipt": {key: value for key, value in primary_receipt.items() if key != "role"},
            }
        )
        write(
            {
                "job_id": fallback_receipt["job_id"],
                "kind": "agent",
                "status": fallback_status,
                "repo": "scratch",
                "created_at": fallback_created_at,
                "finished_at": fallback_finished_at if fallback_status == "completed" else None,
                "launcher": fallback_route["launcher"],
                "provider": fallback_route["provider"],
                "model": fallback_route["model"],
                **fallback_profile,
                "session_identity": fallback_identity,
                "receipt": {key: value for key, value in fallback_receipt.items() if key != "role"},
            }
        )
        body = {
            "class": "mechanical",
            "size": "M",
            "work_kind": "go_coding",
            "primary_route": primary_route,
            "primary_receipt": primary_receipt,
            "fallback_route": fallback_route,
            "fallback_receipt": fallback_receipt,
        }
        if fresh_primary:
            fresh_identity = identity(primary_route, "fresh-primary-session")
            fresh_receipt = {
                "status": "ok",
                "job_id": "3" * 32,
                "role": "primary",
                "launcher": primary_route["launcher"],
                "provider": primary_route["provider"],
                "model": primary_route["model"],
                "session_identity": fresh_identity,
                **primary_profile,
                "failure_class": "none",
                "failure_code": "none",
                "failover_eligible": False,
                "diagnostic": server.compact_diagnostic("none"),
            }
            write(
                {
                    "job_id": fresh_receipt["job_id"],
                    "kind": "agent",
                    "status": "completed",
                    "repo": "scratch",
                    "created_at": fresh_created_at,
                    "finished_at": "2026-08-26T12:02:30Z",
                    "launcher": primary_route["launcher"],
                    "provider": primary_route["provider"],
                    "model": primary_route["model"],
                    **primary_profile,
                    "session_identity": fresh_identity,
                    "receipt": {key: value for key, value in fresh_receipt.items() if key != "role"},
                }
            )
            body["fresh_primary_receipt"] = fresh_receipt
        return body

    def test_provider_recovery_remains_valid_after_fallback_slice_head_progression(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            first = self._provider_recovery_fixture(jobs, "provider_network")
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "resolve_launcher_binary_now", side_effect=AssertionError("remote recovery cannot inspect binaries")), \
                 mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")), \
                 mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("provider CLI forbidden")):
                status, retained, _ = server.dispatch(
                    "POST", "/v1/slice/route/recovery", {}, True,
                    json.dumps(first).encode(),
                )
            self.assertEqual(status, 200, retained)
            self.assertEqual(
                (retained["decision"], retained["role"]),
                ("fallback_retained", "availability_fallback"),
            )

            # The retained fallback runs the next slice after the H0 commit, so
            # its new durable job starts at H1.  The following boundary carries
            # the original primary failure and this latest fallback success.
            h0, h1 = "a" * 40, "b" * 40
            following = self._provider_recovery_fixture(
                jobs,
                "provider_network",
                fallback_job_id="4" * 32,
                fallback_starting_head=h1,
                fallback_created_at="2026-08-26T12:02:00Z",
                fallback_finished_at="2026-08-26T12:02:30Z",
            )
            self.assertEqual(following["primary_receipt"]["session_identity"]["starting_head"], h0)
            self.assertEqual(following["fallback_receipt"]["session_identity"]["starting_head"], h1)
            self.assertTrue((jobs / ("2" * 32) / "job.json").is_file())

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "resolve_launcher_binary_now", side_effect=AssertionError("remote recovery cannot inspect binaries")), \
                 mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")), \
                 mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("provider CLI forbidden")):
                status, retained, _ = server.dispatch(
                    "POST", "/v1/slice/route/recovery", {}, True,
                    json.dumps(following).encode(),
                )
            self.assertEqual(status, 200, retained)
            self.assertEqual(
                (retained["decision"], retained["role"]),
                ("fallback_retained", "availability_fallback"),
            )
            self.assertEqual(retained["evidence"]["primary"]["job_id"], "1" * 32)
            self.assertEqual(retained["evidence"]["fallback"]["job_id"], "4" * 32)

    def test_provider_recovery_repo_and_branch_mismatches_fail_closed(self):
        for field, value in (("repo", "other"), ("branch", "other")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                jobs = Path(td) / "jobs"
                body = self._provider_recovery_fixture(jobs, "provider_network")
                fallback_identity = {
                    **body["fallback_receipt"]["session_identity"],
                    field: value,
                }
                body["fallback_receipt"]["session_identity"] = fallback_identity
                stored_path = jobs / body["fallback_receipt"]["job_id"] / "job.json"
                stored = json.loads(stored_path.read_text())
                stored["session_identity"] = fallback_identity
                stored["receipt"]["session_identity"] = fallback_identity
                stored_path.write_text(json.dumps(stored) + "\n")

                # The fallback receipt still matches its own durable job.  It
                # must nevertheless be rejected against the primary evidence.
                self.assertEqual(stored["session_identity"], fallback_identity)
                self.assertEqual(stored["receipt"]["session_identity"], fallback_identity)
                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.object(server, "resolve_launcher_binary_now", side_effect=AssertionError("remote recovery cannot inspect binaries")):
                    status, payload = server.derive_slice_recovery_route(body)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "recovery_evidence_mismatch")

    def test_provider_recovery_rejects_coordinated_job_receipt_profile_drift(self):
        mutations = (
            {"requested_effort": "high"},
            {"effective_effort": "high"},
            {"effort_source": "explicit"},
            {"requested_effort": "high", "effective_effort": "high", "effort_source": "explicit"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                jobs = Path(td) / "jobs"
                body = self._provider_recovery_fixture(jobs, "provider_network")
                receipt = body["fallback_receipt"]
                receipt.update(mutation)
                receipt["session_identity"].update(mutation)
                stored_path = jobs / receipt["job_id"] / "job.json"
                stored = json.loads(stored_path.read_text())
                stored.update(mutation)
                stored["session_identity"].update(mutation)
                stored["receipt"].update(mutation)
                stored["receipt"]["session_identity"].update(mutation)
                if mutation.get("effort_source") == "explicit":
                    receipt["session_identity"].pop("route_execution_profile", None)
                    stored["session_identity"].pop("route_execution_profile", None)
                    stored["receipt"]["session_identity"].pop(
                        "route_execution_profile", None
                    )
                stored_path.write_text(json.dumps(stored) + "\n")
                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.object(server, "resolve_launcher_binary_now", side_effect=AssertionError("remote recovery cannot inspect binaries")):
                    status, payload = server.derive_slice_recovery_route(body)
                self.assertEqual(status, 400, payload)
                expected_code = (
                    "recovery_evidence_mismatch"
                    if mutation in (
                        {"effort_source": "explicit"},
                        {"requested_effort": "high", "effective_effort": "high", "effort_source": "explicit"},
                    )
                    else "malformed_recovery_evidence"
                )
                self.assertEqual(payload["code"], expected_code, payload)

    def test_provider_recovery_rejects_cross_route_profile_substitution(self):
        for target in ("fallback_receipt", "fresh_primary_receipt"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as td:
                jobs = Path(td) / "jobs"
                body = self._provider_recovery_fixture(
                    jobs, "provider_network", fresh_primary=True
                )
                receipt = body[target]
                expected_route = (
                    body["fallback_route"]
                    if target == "fallback_receipt"
                    else body["primary_route"]
                )
                expected = expected_route["writer_execution_profile"]
                substituted_route = server.build_slice_route(
                    "sensitive",
                    "M",
                    None,
                    None,
                    "go_coding",
                    (
                        server.AVAILABILITY_FALLBACK_WRITER_ROUTES["go_coding"]
                        if target == "fallback_receipt"
                        else server.NORMAL_WRITER_ROUTES["go_coding"]
                    ),
                    expected_route["route_version"],
                    (
                        "availability_fallback:validated"
                        if target == "fallback_receipt"
                        else "work_kind=go_coding"
                    ),
                    "openai" if target == "fallback_receipt" else None,
                )
                substituted = substituted_route["writer_execution_profile"]
                self.assertNotEqual(substituted, expected)
                self.assertEqual(
                    tuple(
                        substituted[key]
                        for key in ("launcher", "provider", "model", "effort")
                    ),
                    tuple(
                        expected[key]
                        for key in ("launcher", "provider", "model", "effort")
                    ),
                )

                receipt["session_identity"]["route_execution_profile"] = substituted
                stored_path = jobs / receipt["job_id"] / "job.json"
                stored = json.loads(stored_path.read_text())
                stored["session_identity"]["route_execution_profile"] = substituted
                stored["receipt"]["session_identity"]["route_execution_profile"] = substituted
                stored_path.write_text(json.dumps(stored) + "\n")

                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.object(
                         server,
                         "resolve_launcher_binary_now",
                         side_effect=AssertionError("remote recovery cannot inspect binaries"),
                     ):
                    status, payload = server.derive_slice_recovery_route(body)

                self.assertEqual(
                    (status, payload.get("code")),
                    (400, "recovery_evidence_mismatch"),
                    payload,
                )

    def test_provider_recovery_rejects_own_job_head_mismatch_and_out_of_order_fallback(self):
        cases = ("own_job_head_mismatch", "out_of_order")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                jobs = Path(td) / "jobs"
                body = self._provider_recovery_fixture(
                    jobs,
                    "provider_network",
                    fallback_created_at=(
                        "2026-08-26T11:59:00Z"
                        if case == "out_of_order"
                        else "2026-08-26T12:00:30Z"
                    ),
                    fallback_finished_at=(
                        "2026-08-26T12:00:00Z"
                        if case == "out_of_order"
                        else "2026-08-26T12:01:00Z"
                    ),
                )
                if case == "own_job_head_mismatch":
                    body["fallback_receipt"]["session_identity"] = {
                        **body["fallback_receipt"]["session_identity"],
                        "starting_head": "b" * 40,
                    }
                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.object(server, "resolve_launcher_binary_now", side_effect=AssertionError("remote recovery cannot inspect binaries")):
                    status, payload = server.derive_slice_recovery_route(body)
                self.assertEqual(status, 400, payload)
                self.assertEqual(
                    payload["code"],
                    (
                        "recovery_evidence_mismatch"
                        if case == "own_job_head_mismatch"
                        else "stale_recovery_evidence"
                    ),
                    payload,
                )

    def test_provider_recovery_re_resolves_missing_binary_at_later_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs"
            body = self._provider_recovery_fixture(jobs)
            configured = root / "bin" / "codex"
            configured.parent.mkdir()
            configured.write_text("#!/bin/sh\nexit 99\n")
            configured.chmod(0o755)
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(configured)}), \
                 mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")), \
                 mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("provider CLI forbidden")):
                status, payload, _ = server.dispatch(
                    "POST", "/v1/slice/route/recovery", {}, True, json.dumps(body).encode()
                )
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["recovery_version"], "provider-recovery-v1")
            self.assertEqual((payload["decision"], payload["role"]), ("primary_recovered", "primary"))
            self.assertEqual(payload["selected_route"], body["primary_route"])
            evidence = payload["evidence"]
            self.assertEqual(evidence["recovery_version"], "provider-recovery-v1")
            self.assertEqual(evidence["basis"], "configured_binary_available")
            self.assertEqual(
                (evidence["primary"]["provider"], evidence["fallback"]["provider"]),
                ("openai", "anthropic"),
            )
            dumped = json.dumps(evidence)
            self.assertNotIn(str(root), dumped)
            for unsafe in ("callback", "token", "stdout", "stderr", "provider_output"):
                self.assertNotIn(unsafe, dumped.lower())

            configured.unlink()
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(configured)}), \
                 mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")), \
                 mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("provider CLI forbidden")):
                status, retained, _ = server.dispatch(
                    "POST", "/v1/slice/route/recovery", {}, True, json.dumps(body).encode()
                )
            self.assertEqual(status, 200, retained)
            self.assertEqual((retained["decision"], retained["role"]), ("fallback_retained", "availability_fallback"))
            self.assertEqual(retained["evidence"]["basis"], "configured_binary_unavailable")

    def test_provider_recovery_remote_failure_needs_later_fresh_primary_success(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            body = self._provider_recovery_fixture(jobs, "provider_rate_limit")
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "resolve_launcher_binary_now", side_effect=AssertionError("remote recovery cannot inspect binaries")), \
                 mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")), \
                 mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("provider CLI forbidden")):
                status, retained, _ = server.dispatch(
                    "POST", "/v1/slice/route/recovery", {}, True, json.dumps(body).encode()
                )
            self.assertEqual(status, 200, retained)
            self.assertEqual((retained["decision"], retained["role"]), ("fallback_retained", "availability_fallback"))
            self.assertEqual(retained["selected_route"], body["fallback_route"])
            self.assertEqual(retained["evidence"]["basis"], "fresh_primary_success_required")

            recovered = self._provider_recovery_fixture(
                jobs, "provider_rate_limit", fresh_primary=True
            )
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "resolve_launcher_binary_now", side_effect=AssertionError("remote recovery cannot inspect binaries")), \
                 mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")), \
                 mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("provider CLI forbidden")):
                status, payload, _ = server.dispatch(
                    "POST", "/v1/slice/route/recovery", {}, True, json.dumps(recovered).encode()
                )
            self.assertEqual(status, 200, payload)
            self.assertEqual((payload["decision"], payload["role"]), ("primary_recovered", "primary"))
            self.assertEqual(payload["evidence"]["basis"], "fresh_primary_success")
            self.assertEqual(payload["evidence"]["fresh_primary"]["job_id"], "3" * 32)

    def test_recovered_primary_refailure_drops_consumed_fresh_success_before_next_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            initial = self._provider_recovery_fixture(
                jobs, "provider_network", fresh_primary=True
            )
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(
                     server,
                     "resolve_launcher_binary_now",
                     side_effect=AssertionError("remote recovery cannot inspect binaries"),
                 ):
                status, recovered = server.derive_slice_recovery_route(initial)
            self.assertEqual(status, 200, recovered)
            self.assertEqual(
                (recovered["decision"], recovered["role"]),
                ("primary_recovered", "primary"),
            )

            # The recovered primary is the current writer. It fails, then its
            # newly derived fallback succeeds at the same slice head.
            continuation = self._provider_recovery_fixture(
                jobs,
                "provider_network",
                primary_job_id="4" * 32,
                primary_starting_head="b" * 40,
                primary_created_at="2026-08-26T12:03:00Z",
                primary_finished_at="2026-08-26T12:03:30Z",
                fallback_job_id="5" * 32,
                fallback_starting_head="b" * 40,
                fallback_created_at="2026-08-26T12:03:30Z",
                fallback_finished_at="2026-08-26T12:04:00Z",
            )

            with mock.patch.object(server, "JOBS_DIR", jobs):
                stale_status, stale = server.derive_slice_recovery_route({
                    **continuation,
                    "fresh_primary_receipt": initial["fresh_primary_receipt"],
                })
            self.assertEqual(stale_status, 400, stale)
            self.assertEqual(stale["code"], "stale_recovery_evidence")

            graph = json.loads(
                (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
            )
            nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
            for walk_id, alias in (("walk", "walk_recovery_input"), ("walk_e2e", "walk_e2e_recovery_input")):
                with self.subTest(walk_id=walk_id):
                    config = nodes[walk_id]["config"]
                    self.assertEqual(config["fresh_primary_receipt"], f"{{{{ CTX.{alias}.fresh_primary_receipt }}}}")
                    self.assertIn("'fresh_primary_receipt': ''", nodes[alias]["config"]["code"]["code"])
                    with mock.patch.object(server, "JOBS_DIR", jobs):
                        next_status, retained = server.derive_slice_recovery_route(
                            continuation
                        )
                    self.assertEqual(next_status, 200, retained)
                    self.assertEqual(
                        (retained["decision"], retained["role"]),
                        ("fallback_retained", "availability_fallback"),
                    )
                    self.assertEqual(
                        retained["evidence"]["basis"],
                        "fresh_primary_success_required",
                    )

    def test_fallback_handler_and_recovery_share_dict_policy_without_code_collapse(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            body = self._provider_recovery_fixture(jobs)
            fallback_request = {
                key: body[key]
                for key in (
                    "class", "size", "work_kind", "primary_route",
                    "primary_receipt",
                )
            }
            with mock.patch.object(server, "JOBS_DIR", jobs):
                helper_status, helper_payload = server.derive_slice_fallback_route(
                    fallback_request
                )
                handler_status, handler_payload, _ = server.dispatch(
                    "POST", "/v1/slice/route/fallback", {}, True,
                    json.dumps(fallback_request).encode(),
                )
            self.assertEqual(
                (handler_status, handler_payload),
                (helper_status, helper_payload),
            )

            malformed = self._provider_recovery_fixture(jobs)
            malformed["primary_receipt"]["failover_eligible"] = "true"
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "slice_route_fallback", wraps=server.slice_route_fallback) as http_handler, \
                 mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")), \
                 mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("provider CLI forbidden")):
                status, payload, _ = server.dispatch(
                    "POST", "/v1/slice/route/recovery", {}, True,
                    json.dumps(malformed).encode(),
                )
            self.assertEqual(status, 400, payload)
            self.assertEqual(payload["code"], "bad_failover_eligible")
            http_handler.assert_not_called()

    def test_recovery_handler_is_a_thin_wrapper_over_dict_policy(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            body = self._provider_recovery_fixture(jobs, "provider_rate_limit")
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")), \
                 mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("provider CLI forbidden")):
                direct = server.derive_slice_recovery_route(body)
                handled_status, handled_payload, _ = server.dispatch(
                    "POST", "/v1/slice/route/recovery", {}, True,
                    json.dumps(body).encode(),
                )
            self.assertEqual((handled_status, handled_payload), direct)

    def test_provider_recovery_evidence_fails_closed(self):
        status, payload, _ = server.dispatch(
            "POST", "/v1/slice/route/recovery", {}, True,
            b'{"work_kind":"go_coding"}',
        )
        self.assertEqual((status, payload["code"]), (400, "missing_recovery_evidence"))

        cases = (
            ("malformed", "malformed_recovery_evidence"),
            ("mismatched", "recovery_evidence_mismatch"),
            ("durable_mismatch", "recovery_evidence_mismatch"),
            ("nonterminal", "nonterminal_recovery_evidence"),
            ("stale", "stale_recovery_evidence"),
        )
        for case, expected_code in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                jobs = Path(td) / "jobs"
                body = self._provider_recovery_fixture(
                    jobs,
                    "provider_network" if case == "stale" else "missing_binary",
                    fallback_status="running" if case == "nonterminal" else "completed",
                    fresh_primary=case == "stale",
                    fresh_created_at="2026-08-26T11:59:00Z",
                )
                if case == "malformed":
                    body["fallback_receipt"] = {**body["fallback_receipt"], "job_id": "../bad"}
                elif case == "mismatched":
                    body["fallback_route"] = {**body["fallback_route"], "provider": "xai"}
                elif case == "durable_mismatch":
                    stored_path = jobs / ("2" * 32) / "job.json"
                    stored = json.loads(stored_path.read_text())
                    stored["receipt"]["failure_code"] = "provider_network"
                    stored_path.write_text(json.dumps(stored) + "\n")
                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")), \
                     mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("provider CLI forbidden")):
                    status, payload, _ = server.dispatch(
                        "POST", "/v1/slice/route/recovery", {}, True, json.dumps(body).encode()
                    )
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], expected_code, payload)

    def test_route_never_returns_a_profile_named_reviewer(self):
        banned = {"terra", "sol", "sonnet", "opus", "fable"}
        for work_kind in ("go_coding", "typescript_coding", "research_ops"):
            for cls in ("mechanical", "visual", "sensitive"):
                route = server.slice_route_lookup(cls, "M", work_kind=work_kind)
                self.assertTrue(banned.isdisjoint({route["reviewer1"], route["reviewer2"]}))

    def test_implement_slice_wires_writer_and_reviewer_effort(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        expected_effort = {
            "agent": "{{ CTX.selected_route.effort }}",
            "agent_fallback": "{{ CTX.fallback_route_choice.effort }}",
            "agent2": "{{ CTX.fallback_receipt.session_identity.requested_effort | default(CTX.receipt.session_identity.requested_effort) }}",
            "agent3": "{{ CTX.receipt2.session_identity.requested_effort }}",
            "agent_rn1": "{{ CTX.receipt3.session_identity.requested_effort | default(CTX.receipt2.session_identity.requested_effort) | default(CTX.fallback_receipt.session_identity.requested_effort) | default(CTX.receipt.session_identity.requested_effort) }}",
            "agent_rn2": "{{ CTX.receipt_rn1.session_identity.requested_effort | default(CTX.receipt3.session_identity.requested_effort) | default(CTX.receipt2.session_identity.requested_effort) | default(CTX.fallback_receipt.session_identity.requested_effort) | default(CTX.receipt.session_identity.requested_effort) }}",
            "review1": "{{ CTX.active_route.reviewer1_effort }}",
            "review1b": "{{ CTX.active_route.reviewer1_effort }}",
            "review2": "{{ CTX.active_route.reviewer2_effort }}",
            "review2b": "{{ CTX.active_route.reviewer2_effort }}",
        }
        expected_profile = {
            "agent": "{{ CTX.selected_route.writer_execution_profile }}",
            "agent_fallback": "{{ CTX.fallback_route_choice.writer_execution_profile }}",
            "agent2": "{{ CTX.fallback_receipt.session_identity.route_execution_profile | default(CTX.receipt.session_identity.route_execution_profile) }}",
            "agent3": "{{ CTX.receipt2.session_identity.route_execution_profile }}",
            "agent_rn1": "{{ CTX.receipt3.session_identity.route_execution_profile | default(CTX.receipt2.session_identity.route_execution_profile) | default(CTX.fallback_receipt.session_identity.route_execution_profile) | default(CTX.receipt.session_identity.route_execution_profile) }}",
            "agent_rn2": "{{ CTX.receipt_rn1.session_identity.route_execution_profile | default(CTX.receipt3.session_identity.route_execution_profile) | default(CTX.receipt2.session_identity.route_execution_profile) | default(CTX.fallback_receipt.session_identity.route_execution_profile) | default(CTX.receipt.session_identity.route_execution_profile) }}",
            "review1": "{{ CTX.active_route.reviewer1_execution_profile }}",
            "review1b": "{{ CTX.active_route.reviewer1_execution_profile }}",
            "review2": "{{ CTX.active_route.reviewer2_execution_profile }}",
            "review2b": "{{ CTX.active_route.reviewer2_execution_profile }}",
        }
        actual_model_nodes = {
            node_id for node_id, node in nodes.items()
            if node["type"].endswith(("/v1/agent/run", "/v1/review/run"))
        }
        self.assertEqual(actual_model_nodes, set(expected_effort))
        for node_id, expression in expected_effort.items():
            self.assertEqual(nodes[node_id]["config"].get("effort"), expression, node_id)
            self.assertEqual(
                nodes[node_id]["config"].get("route_execution_profile"),
                expected_profile[node_id], node_id,
            )

        # Initial, fallback, and recovered launches consume the selected route;
        # active-session corrections consume the immutable receipt identity.
        self.assertEqual(
            nodes["normal_primary_candidate"]["config"]["mappings"][0]["expression"]["path"],
            "TASKS.route.data",
        )
        self.assertEqual(
            nodes["selected_route"]["config"]["code"]["code"],
            "{{ CTX.recovery_selection.route | default(CTX.normal_primary_candidate.route) | tojson }}",
        )
        self.assertEqual(
            nodes["normal_fallback_candidate"]["config"]["mappings"][0]["expression"]["path"],
            "TASKS.fallback_route.data",
        )
        self.assertEqual(
            nodes["fallback_route_choice"]["config"]["code"]["code"],
            "{{ CTX.normal_fallback_candidate.route | default(CTX.recovery_selection.route) | tojson }}",
        )
        for node_id in ("agent2", "agent3", "agent_rn1", "agent_rn2"):
            self.assertNotIn("TASKS.route", nodes[node_id]["config"]["effort"], node_id)
            self.assertIn("session_identity.requested_effort", nodes[node_id]["config"]["effort"], node_id)

        # reviewer2 remains optional: its effort expression is evaluated only
        # behind the existing reviewer2-present switch default path.
        triples = {
            (edge["source"], edge.get("sourceHandle"), edge["target"])
            for edge in graph["edges"]
        }
        self.assertIn(("switch_rev2", "case-0", "join_commit"), triples)
        self.assertIn(("switch_rev2", "default", "wait_rev2"), triples)
        self.assertEqual(
            {edge["source"] for edge in graph["edges"] if edge["target"] == "review2"},
            {"wait_rev2"},
        )
        self.assertEqual(
            {edge["source"] for edge in graph["edges"] if edge["target"] == "review2b"},
            {"wait_r2b"},
        )

    def test_pr_drive_wires_and_pins_effort(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "pr-drive.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        model_nodes = [
            node for node in graph["nodes"]
            if node["type"].endswith(("/v1/agent/run", "/v1/review/run"))
        ]
        self.assertEqual([node["id"] for node in model_nodes], ["agent"])
        self.assertEqual(nodes["agent"]["config"].get("effort"), "{{ TASKS.route.data.effort }}")
        self.assertEqual(
            nodes["agent"]["config"].get("route_execution_profile"),
            "{{ TASKS.route.data.writer_execution_profile }}",
        )
        self.assertEqual(
            [node["id"] for node in graph["nodes"] if "/v1/slice/route" in node["type"]],
            ["route"],
        )
        self.assertNotIn("session_identity", nodes["agent"]["config"])
        self.assertNotIn("resume_job_id", nodes["agent"]["config"])

        # One API invocation is one bounded attempt: there is no graph cycle,
        # same-session correction, or second route/writer that can change effort.
        outgoing = {}
        for edge in graph["edges"]:
            outgoing.setdefault(edge["source"], []).append(edge["target"])
        seen, pending = set(), ["continue"]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(outgoing.get(current, ()))
        self.assertTrue({"route", "agent"}.isdisjoint(seen))
        self.assertEqual(
            {edge["source"] for edge in graph["edges"] if edge["target"] == "route"},
            {"findings"},
        )

    def test_implement_slice_preserves_only_normalized_usage_for_workflow_outcomes(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        callback_sources = {
            "record": "wait",
            "record_fallback": "wait_fallback",
            "record2": "wait2",
            "record3": "wait3",
            "record_rn1": "wait_rn1",
            "record_rn2": "wait_rn2",
        }
        for node_id, wait_id in callback_sources.items():
            mappings = {
                mapping["output"]: mapping["expression"]
                for mapping in nodes[node_id]["config"]["mappings"]
            }
            self.assertEqual(
                mappings["usage"],
                {"kind": "getField", "path": f"TASKS.{wait_id}.request.body.usage"},
                node_id,
            )
            self.assertEqual(
                mappings["usage_diagnostic"],
                {"kind": "getField", "path": f"TASKS.{wait_id}.request.body.usage_diagnostic"},
                node_id,
            )
            dumped = json.dumps(mappings)
            for forbidden in (
                "raw_event", "provider_payload", "prompt", "thought", "response_text",
                "log_ref", "command_line", "/home/",
            ):
                self.assertNotIn(forbidden, dumped, node_id)

    def test_implement_slice_durable_outcome_aggregates_every_unique_accepted_job(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        candidates = json.dumps(nodes["durable_outcome_candidates"]["config"])
        writer_pairs = {
            "agent": "wait", "agent_fallback": "wait_fallback",
            "agent2": "wait2", "agent3": "wait3",
            "agent_rn1": "wait_rn1", "agent_rn2": "wait_rn2",
        }
        reviewer_pairs = {
            "review1": "wait_rev1", "review1b": "wait_r1b",
            "review2": "wait_rev2", "review2b": "wait_r2b",
        }
        for action_id, wait_id in {**writer_pairs, **reviewer_pairs}.items():
            with self.subTest(action_id=action_id):
                accepted = f"TASKS.{action_id}.data.job_id"
                callback = f"TASKS.{wait_id}.request.body.job_id"
                self.assertIn(accepted, candidates)
                self.assertIn(callback, candidates)

        self.assertEqual(nodes["durable_accepted_attempts"]["type"], "transforms.transformArray")
        self.assertEqual(nodes["durable_accepted_attempts"]["config"]["operation"], "filter")
        unique = nodes["durable_unique_attempts"]
        self.assertEqual(unique["type"], "transforms.transformArray")
        self.assertEqual(unique["config"]["operation"], "unique")
        self.assertEqual(unique["config"]["uniqueBy"], "item.job_id")
        for node_id in ("durable_writer_rollup", "durable_reviewer_rollup", "durable_slice_rollup"):
            rollup = nodes[node_id]
            self.assertEqual(rollup["type"], "transforms.groupByAggregate")
            self.assertEqual(rollup["config"]["groupBy"], "group")

        # transforms.aggregate stores its native output envelope at CTX; the
        # selected value is always under .result rather than at the alias root.
        outcome_dump = json.dumps(nodes["durable_outcome"]["config"])
        for alias in ("durable_selected_agent", "durable_selected_named_test"):
            selected_paths = re.findall(rf'CTX\.{alias}[^" ]*', outcome_dump)
            self.assertTrue(selected_paths, alias)
            self.assertTrue(
                all(path.startswith(f"CTX.{alias}.result") for path in selected_paths),
                selected_paths,
            )

        # Durable projection is a deterministic native-node pipeline. In
        # particular, it cannot regress to unsupported namespace/dotted-set
        # Nunjucks or an opaque codeExpression implementation.
        durable_nodes = [node for node in graph["nodes"] if node["id"].startswith("durable_")]
        self.assertNotIn("transforms.codeExpression", {node["type"] for node in durable_nodes})
        durable_dump = json.dumps(durable_nodes)
        self.assertNotIn("namespace(", durable_dump)
        self.assertNotRegex(durable_dump, r"{%-?\s*set\s+[A-Za-z_]\w*[.\[]")

    def test_implement_slice_durable_usage_fails_closed_and_never_sums_binary_costs(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        candidates = json.dumps(nodes["durable_outcome_candidates"]["config"])
        # Every accepted action is represented independently of callback
        # success, so a timeout remains an attempt with null usage.
        for action_id in (
            "agent", "agent_fallback", "agent2", "agent3", "agent_rn1", "agent_rn2",
            "review1", "review1b", "review2", "review2b",
        ):
            self.assertIn(f"TASKS.{action_id}.data.job_id", candidates)
        outcome = json.dumps(nodes["durable_outcome"]["config"])
        self.assertIn("usage_not_reported", outcome)
        self.assertIn("usage_not_spent", outcome)
        self.assertIn("provider_cost_aggregation_unavailable", outcome)
        for node_id in ("durable_writer_rollup", "durable_reviewer_rollup", "durable_slice_rollup"):
            aggregations = nodes[node_id]["config"]["aggregations"]
            self.assertNotIn("provider_cost_usd", {item["attribute"] for item in aggregations})
            self.assertIn({"op": "count", "attribute": "usage", "as": "usage_count"}, aggregations)
        # Exact decimal cost aggregation is deliberately unavailable rather
        # than exposing JavaScript's false 0.30000000000000004 precision.
        self.assertNotIn("provider_cost_usd", candidates)
        self.assertNotIn("0.30000000000000004", outcome)

        # Exercise the saved native AST with a clean-stdlib evaluator. This
        # proves the production topology's essential data semantics without
        # treating Python Jinja2 as a Rewst runtime.
        def by_path(context: Any, path: str) -> Any:
            value = context
            for part in path.split("."):
                if isinstance(value, dict):
                    value = value.get(part)
                elif isinstance(value, list) and part.isdigit():
                    index = int(part)
                    value = value[index] if index < len(value) else None
                else:
                    return None
            return value

        def evaluate(expression: dict[str, Any], context: dict[str, Any]) -> Any:
            kind = expression["kind"]
            if kind == "literal":
                return expression.get("value")
            if kind == "getField":
                return by_path(context, expression["path"])
            if kind == "array":
                return [evaluate(item, context) for item in expression["elements"]]
            if kind == "object":
                return {
                    key: evaluate(value, context)
                    for key, value in expression["properties"].items()
                }
            if kind == "conditional":
                branch = "then" if evaluate(expression["condition"], context) else "else"
                return evaluate(expression[branch], context)
            if kind == "coalesce":
                primary = evaluate(expression["primary"], context)
                return primary if primary is not None else evaluate(expression["fallback"], context)
            if kind == "binary":
                left = evaluate(expression["left"], context)
                operator = expression["operator"]
                if operator == "and":
                    return left and evaluate(expression["right"], context)
                if operator == "or":
                    return left or evaluate(expression["right"], context)
                right = evaluate(expression["right"], context)
                return {
                    "==": lambda: left == right,
                    "!=": lambda: left != right,
                    ">": lambda: left > right,
                    "+": lambda: left + right,
                }[operator]()
            self.fail(f"unsupported native AST kind {kind}")

        def usage(seed: int, cost: float) -> dict[str, Any]:
            return {
                "usage_version": "normalized-usage-v1",
                "fresh_input_tokens": seed,
                "cached_input_tokens": seed,
                "cache_write_tokens": seed,
                "output_tokens": seed,
                "reasoning_tokens": seed,
                "provider_cost_usd": cost,
                "wall_seconds": seed,
                "turns_observed": 1,
            }

        tasks: dict[str, Any] = {
            "agent": {"data": {"job_id": "writer-1"}},
            "wait": {"request": {"body": {
                "job_id": "writer-1", "usage": usage(1, 0.1), "status": "ok",
            }}},
            "agent2": {"data": {"job_id": "writer-2"}},
            "wait2": {"request": {"body": {
                "job_id": "writer-2", "usage": usage(10, 0.2), "status": "ok",
            }}},
            # A later alias for writer-1 must not double count it.
            "agent3": {"data": {"job_id": "writer-1"}},
            "wait3": {"request": {"body": {
                "job_id": "writer-1", "usage": usage(1, 0.1), "status": "ok",
            }}},
            # Accepted review with no callback still counts as an attempt.
            "review1": {"data": {"job_id": "review-1"}},
            # A forged named-test callback cannot become evidence.
            "test": {"data": {"job_id": "named-1"}},
            "wait_test": {"request": {"body": {
                "job_id": "forged", "status": "ok", "name": "forged-name",
            }}},
            "test2": {"data": {"job_id": "named-2"}},
            "wait_test2": {"request": {"body": {
                "job_id": "named-2", "status": "ok", "name": "callback-name-is-ignored",
            }}},
        }
        context: dict[str, Any] = {
            "TASKS": tasks, "CTX": {
                "INPUT": {"test": "configured-name"},
                "receipt": {"status": "ok"},
                "receipt2": {"status": "ok"},
                "receipt3": {"status": "ok"},
            }
        }
        candidate_mappings = nodes["durable_outcome_candidates"]["config"]["mappings"]
        built = {
            mapping["output"]: evaluate(mapping["expression"], context)
            for mapping in candidate_mappings
        }
        accepted = [item for item in built["attempts"] if item["job_id"] is not None]
        unique: list[dict[str, Any]] = []
        seen = set()
        for item in accepted:
            if item["job_id"] not in seen:
                seen.add(item["job_id"])
                unique.append(item)

        def rollup(items: list[dict[str, Any]]) -> dict[str, Any]:
            fields = {
                "fresh": "fresh_input_tokens", "cached": "cached_input_tokens",
                "cache_write": "cache_write_tokens", "output": "output_tokens",
                "reasoning": "reasoning_tokens", "wall": "wall_seconds",
                "turns": "turns_observed",
            }
            reported = [item["usage"] for item in items if item["usage"] is not None]
            row = {alias: sum(value[field] for value in reported) for alias, field in fields.items()}
            row.update({"usage_count": len(reported), "turns_count": len(reported)})
            return {"itemCount": len(items), "rows": [row] if items else []}

        writers = [item for item in unique if item["kind"] == "writer"]
        reviewers = [item for item in unique if item["kind"] == "reviewer"]
        bound_named = [
            item for item in built["named_tests"]
            if item["accepted_job_id"] is not None and item["callback_matches"] is True
        ]
        context["CTX"].update({
            "durable_outcome_candidates": built,
            "durable_writer_rollup": rollup(writers),
            "durable_reviewer_rollup": rollup(reviewers),
            "durable_slice_rollup": rollup(unique),
            "durable_selected_agent": {
                "result": writers[-1] if writers else None,
                "operation": "last", "field": None, "itemCount": len(writers),
            },
            "durable_selected_named_test": {
                "result": bound_named[-1] if bound_named else None,
                "operation": "last", "field": None, "itemCount": len(bound_named),
            },
        })
        projected = {
            mapping["output"]: evaluate(mapping["expression"], context)
            for mapping in nodes["durable_outcome"]["config"]["mappings"]
        }
        self.assertEqual(projected["writer_usage"]["attempts"], 2)
        self.assertEqual(projected["writer_usage"]["usage"]["fresh_input_tokens"], 11)
        self.assertIsNone(projected["writer_usage"]["usage"]["provider_cost_usd"])
        self.assertEqual(
            projected["writer_usage"]["usage_diagnostics"],
            ["provider_cost_aggregation_unavailable"],
        )
        self.assertEqual(projected["reviewer_usage"]["attempts"], 1)
        self.assertIsNone(projected["reviewer_usage"]["usage"])
        self.assertIn("usage_not_reported", projected["reviewer_usage"]["usage_diagnostics"])
        self.assertEqual(projected["slice_usage"]["attempts"], 3)
        self.assertIsNone(projected["slice_usage"]["usage"])
        self.assertEqual(projected["agent_job_id"], "writer-2")
        self.assertEqual(projected["agent_status"], "ok")
        self.assertEqual(projected["named_test_status"], "passed")
        self.assertEqual(projected["named_test_evidence"], {
            "evidence_version": "graphwing-named-test-evidence-v1",
            "job_id": "named-2", "name": "configured-name", "status": "ok",
        })
        self.assertEqual(projected["named_test_name"], "configured-name")

    def test_implement_slice_actual_riftwing_workflow_lint_when_available(self):
        """Use Riftwing's authoritative Go checker; keep CI hermetic and stdlib-only."""
        graph_path = Path(server.__file__).parent / "graphs" / "implement-slice.json"
        riftwing = os.environ.get("RIFTWING_CHECKOUT")
        if not riftwing:
            graph = json.loads(graph_path.read_text())["spec"]
            durable_nodes = [node for node in graph["nodes"] if node["id"].startswith("durable_")]
            self.assertNotIn("transforms.codeExpression", {node["type"] for node in durable_nodes})
            dumped = json.dumps(durable_nodes)
            self.assertNotIn("namespace(", dumped)
            self.assertNotRegex(dumped, r"{%-?\s*set\s+[A-Za-z_]\w*[.\[]")
            return

        go_source = r'''package main
import (
    "encoding/json"
    "fmt"
    "os"
    "github.com/rewstapp/riftwing/rewst-go/services/api/domain/workflows/linter"
)
func main() {
    raw, err := os.ReadFile(os.Args[1]); if err != nil { panic(err) }
    var envelope map[string]any
    if err := json.Unmarshal(raw, &envelope); err != nil { panic(err) }
    spec, ok := envelope["spec"].(map[string]any); if !ok { panic("missing object spec") }
    result := linter.Lint(spec)
    for _, issue := range result.Issues {
        encoded, _ := json.Marshal(issue)
        fmt.Println(string(encoded))
    }
    fmt.Printf("issues=%d\n", len(result.Issues))
    if len(result.Issues) != 0 { os.Exit(1) }
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            checker = Path(tmp) / "graphwing_workflow_lint.go"
            checker.write_text(go_source)
            result = subprocess.run(
                ["go", "run", str(checker), str(graph_path)],
                cwd=Path(riftwing) / "rewst-go", text=True,
                capture_output=True, timeout=120, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("issues=0", result.stdout)

    def test_implement_slice_named_tests_all_emit_bound_async_job_evidence(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        triples = {
            (edge["source"], edge.get("sourceHandle"), edge["target"])
            for edge in graph["edges"]
        }
        cases = {
            "test": ("wait_test", "join_test_accept", "if_test_ok", "join_test_http_fail"),
            "test2": ("wait_test2", "join_test2_accept", "if_test_ok2", "join_test2_http_fail"),
            "test3": ("wait_test3", "join_test3_accept", "if_test_ok3", "join_test2_http_fail"),
            "test_rn1": ("wait_test_rn1", "join_test_rn1_accept", "if_test_rn1", "join_test_http_fail"),
            "test_rn2": ("wait_test_rn2", "join_test_rn2_accept", "if_test_rn2", "join_test_http_fail"),
        }
        def callback_gate_passes(rules, body, accepted_job_id):
            actual = {
                "request.body.status": body.get("status"),
                "request.body.job_id": body.get("job_id"),
            }
            for rule in rules:
                expected = rule["value"]
                if isinstance(expected, str) and expected.startswith("{{ TASKS."):
                    expected = accepted_job_id
                if rule["op"] != "equals" or actual[rule["path"]] != expected:
                    return False
            return True

        for test_id, (wait_id, barrier_id, gate_id, failure_join) in cases.items():
            wait = nodes[wait_id]
            barrier = nodes[barrier_id]
            self.assertEqual(wait["type"], "action.wait.webhook")
            self.assertEqual(barrier["type"], "logic.join.all")
            self.assertEqual(barrier["config"], {})
            self.assertEqual(wait["config"], {
                "alias": wait_id,
                "allowedMethods": ["POST"], "authMode": "token",
                "responseStatus": 200, "timeoutSeconds": 960,
            })
            self.assertEqual(
                nodes[test_id]["config"]["response_webhook_url"],
                f"{{{{ TASKS.{wait_id}.pending.resumeUrl }}}}",
            )
            self.assertEqual(
                nodes[test_id]["config"]["response_webhook_token"],
                f"{{{{ TASKS.{wait_id}.pending.resumeToken }}}}",
            )
            self.assertIn((wait_id, "pending", test_id), triples)
            self.assertIn((wait_id, "out", barrier_id), triples)
            self.assertIn((test_id, "success", barrier_id), triples)
            self.assertIn((barrier_id, "out", gate_id), triples)
            self.assertNotIn((wait_id, "out", gate_id), triples)
            self.assertIn((wait_id, "timeout", failure_join), triples)
            self.assertIn((wait_id, "failure", failure_join), triples)
            self.assertIn((test_id, "failure", failure_join), triples)
            self.assertNotIn((test_id, "success", gate_id), triples)
            rules = nodes[gate_id]["config"]["rules"]
            self.assertEqual(rules, [
                {"path": "request.body.status", "op": "equals", "value": "ok"},
                {
                    "path": "request.body.job_id", "op": "equals",
                    "value": f"{{{{ TASKS.{test_id}.data.job_id }}}}",
                },
            ])
            accepted = f"accepted-{test_id}"
            self.assertTrue(callback_gate_passes(
                rules, {"status": "ok", "job_id": accepted}, accepted
            ))
            self.assertFalse(callback_gate_passes(
                rules, {"status": "ok", "job_id": f"forged-{test_id}"}, accepted
            ))

        mappings = {
            mapping["output"]: mapping["expression"]
            for mapping in nodes["durable_outcome"]["config"]["mappings"]
        }
        evidence = mappings["named_test_evidence"]
        self.assertEqual(evidence["kind"], "conditional")
        self.assertEqual(
            set(evidence["then"]["properties"]),
            {"evidence_version", "job_id", "name", "status"},
        )

    def test_implement_slice_named_test_evidence_uses_acceptance_identity_not_callback_text(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        candidates = json.dumps(nodes["durable_outcome_candidates"]["config"])
        for test_id, wait_id in (
            ("test", "wait_test"), ("test2", "wait_test2"),
            ("test3", "wait_test3"), ("test_rn1", "wait_test_rn1"),
            ("test_rn2", "wait_test_rn2"),
        ):
            accepted = f"TASKS.{test_id}.data.job_id"
            callback = f"TASKS.{wait_id}.request.body.job_id"
            self.assertIn(accepted, candidates)
            self.assertIn(callback, candidates)
        selected = json.dumps(nodes["durable_outcome"]["config"])
        self.assertIn("CTX.INPUT.test", selected)
        self.assertIn("CTX.durable_selected_named_test.result.accepted_job_id", selected)
        self.assertNotRegex(candidates + selected, r"request\.body\.(?:name|recipe)")

    def test_implement_slice_verification_classification_uses_terminal_gates_not_join_presence(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        stage = json.dumps(nodes["durable_outcome_candidates"]["config"]["mappings"][-1])
        outcome = json.dumps(nodes["durable_outcome"]["config"])
        self.assertIn("CTX.slices_complete_terminal.terminal_stage", stage)
        self.assertIn("CTX.done", stage)
        self.assertIn("all_slices_complete", stage)
        self.assertIn("slice_complete", stage)
        self.assertIn("CTX.push_fail_terminal.terminal_stage", stage)
        self.assertIn("verified", outcome)
        self.assertNotIn("TASKS.join_commit", stage + outcome)

    def test_implement_slice_durable_outcome_uses_exact_datastore_records_contract(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        upsert = nodes["durable_outcome_upsert"]
        readback = nodes["durable_outcome_readback"]
        record_key = "{{ CTX.durable_outcome_record_key.value }}"

        self.assertEqual(upsert["type"], "action.datastore.records.upsert")
        self.assertEqual(upsert["config"], {
            "alias": "durable_outcome_upsert",
            "scope": "tenant",
            "collection": "graphwing_verified_outcomes_v1",
            "recordKey": record_key,
            "indexedFields": [
                "verified_outcome", "work_kind", "work_class", "effective_size",
                "route_version", "role", "named_test_status",
            ],
            "data": "{{ CTX.durable_outcome }}",
        })
        self.assertNotIn("ttlSeconds", upsert["config"])
        self.assertEqual(readback["type"], "action.datastore.records.get")
        self.assertEqual(readback["config"], {
            "alias": "durable_outcome_readback",
            "scope": "tenant",
            "collection": "graphwing_verified_outcomes_v1",
            "recordKey": record_key,
        })
        key_builder = nodes["durable_outcome_record_key"]
        self.assertEqual(key_builder["type"], "transforms.objectBuilder")
        key_expression = json.dumps(key_builder["config"]["mappings"][0]["expression"])
        self.assertIn("graphwing-outcome-v1:", key_expression)
        self.assertIn("WORKFLOW.runId", key_expression)
        self.assertIn(":implement-slice-terminal", key_expression)

    def test_implement_slice_readback_matches_exact_data_and_upsert_version(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        edges = {
            (edge["source"], edge["sourceHandle"], edge["target"])
            for edge in graph["edges"]
        }
        check = nodes["durable_outcome_readback_check"]
        self.assertEqual(check["type"], "transforms.objectBuilder")
        checks = {
            mapping["output"]: mapping["expression"]
            for mapping in check["config"]["mappings"]
        }
        self.assertEqual(
            set(checks), {"found_matches", "key_matches", "data_matches", "version_matches"}
        )
        expected_hash = nodes["durable_outcome_expected_hash"]
        returned_hash = nodes["durable_outcome_readback_hash"]
        for hash_node in (expected_hash, returned_hash):
            self.assertEqual(hash_node["type"], "transforms.hash")
            self.assertEqual(hash_node["config"]["algorithm"], "sha256")
            self.assertEqual(hash_node["config"]["inputKind"], "json_stringify")
        self.assertEqual(expected_hash["config"]["input"], "{{ CTX.durable_outcome }}")
        self.assertEqual(
            returned_hash["config"]["input"],
            "{{ TASKS.durable_outcome_readback.data }}",
        )
        check_dump = json.dumps(checks)
        for field in ("found", "recordKey", "version"):
            self.assertIn(f"TASKS.durable_outcome_readback.{field}", check_dump)
            self.assertNotIn(f"TASKS.durable_outcome_readback.data.{field}", check_dump)
        self.assertIn("TASKS.durable_outcome_upsert.version", check_dump)
        self.assertNotIn("TASKS.durable_outcome_upsert.data.version", check_dump)
        self.assertIn("CTX.durable_outcome_expected_hash.value", check_dump)
        self.assertIn("CTX.durable_outcome_readback_hash.value", check_dump)
        gate = nodes["if_durable_outcome_readback"]
        self.assertEqual(gate["config"]["rules"], [
            {"path": "found_matches", "op": "equals", "value": True},
            {"path": "key_matches", "op": "equals", "value": True},
            {"path": "data_matches", "op": "equals", "value": True},
            {"path": "version_matches", "op": "equals", "value": True},
        ])
        self.assertIn(("durable_outcome_readback", "success", "durable_outcome_readback_hash"), edges)
        self.assertIn(("durable_outcome_readback_hash", "out", "durable_outcome_readback_check"), edges)
        self.assertIn(("durable_outcome_readback_check", "out", "if_durable_outcome_readback"), edges)
        self.assertIn(("if_durable_outcome_readback", "fail", "durable_outcome_readback_mismatch"), edges)
        def gate_passes(values):
            return all(values[rule["path"]] == rule["value"] for rule in gate["config"]["rules"])

        self.assertTrue(gate_passes({
            "found_matches": True, "key_matches": True,
            "data_matches": True, "version_matches": True,
        }))
        # Same key is insufficient: stale/wrong payload or version takes the
        # hard-failing mismatch leg.
        self.assertFalse(gate_passes({
            "found_matches": True, "key_matches": True,
            "data_matches": False, "version_matches": True,
        }))
        self.assertFalse(gate_passes({
            "found_matches": True, "key_matches": True,
            "data_matches": True, "version_matches": False,
        }))

    def test_implement_slice_persistence_failures_hard_fail_with_proven_rewst_regex_contract(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        outgoing = {edge["source"] for edge in graph["edges"]}
        failure_nodes = {
            "durable_outcome_write_failed": "graphwing_durable_outcome_write_failed",
            "durable_outcome_readback_failed": "graphwing_durable_outcome_readback_failed",
            "durable_outcome_readback_mismatch": "graphwing_durable_outcome_readback_mismatch",
        }
        # Local Rewst's transforms.regexReplace contract explicitly hard-fails invalid
        # RE2 patterns. Lookahead is intentionally unsupported by RE2.
        for node_id, failure_code in failure_nodes.items():
            node = nodes[node_id]
            self.assertEqual(node["type"], "transforms.regexReplace")
            self.assertEqual(node["config"], {
                "input": failure_code,
                "pattern": "(?=graphwing)",
                "replacement": "",
            })
            self.assertNotIn(node_id, outgoing)

    def test_implement_slice_durable_outcome_has_closed_sanitized_record_allowlist(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        builder = nodes["durable_outcome"]
        expected_fields = {
            "schema_version", "workflow_slug", "workflow_id", "workflow_version",
            "workflow_run_id", "workflow_stage", "route_version",
            "route_profile_version", "role", "work_kind", "work_class",
            "effective_size", "agent_job_id", "agent_status",
            "agent_failure_class", "agent_failure_code", "launcher", "provider",
            "model", "requested_effort", "effective_effort", "effort_source",
            "named_test_name", "named_test_status", "named_test_evidence",
            "commit_eligible", "verified_outcome", "writer_usage", "reviewer_usage",
            "slice_usage", "diagnostic",
        }
        self.assertEqual(builder["type"], "transforms.objectBuilder")
        mappings = {
            mapping["output"]: mapping["expression"]
            for mapping in builder["config"]["mappings"]
        }
        self.assertEqual(set(mappings), expected_fields)
        for summary_field in ("writer_usage", "reviewer_usage", "slice_usage"):
            summary = mappings[summary_field]
            self.assertEqual(summary["kind"], "object")
            self.assertEqual(
                set(summary["properties"]),
                {"attempts", "usage", "usage_diagnostics"},
            )
            usage_dump = json.dumps(summary["properties"]["usage"])
            for field in (
                "usage_version", "fresh_input_tokens", "cached_input_tokens",
                "cache_write_tokens", "output_tokens", "reasoning_tokens",
                "provider_cost_usd", "wall_seconds", "turns_observed",
            ):
                self.assertIn(field, usage_dump)
        self.assertEqual(
            set(mappings["diagnostic"]["properties"]),
            {"diagnostic_version", "stage", "status", "code", "summary"},
        )
        projection = json.dumps(builder["config"])
        for identity in ("WORKFLOW.id", "WORKFLOW.version", "WORKFLOW.runId"):
            self.assertIn(identity, projection)
        for forbidden in (
            "prompt", "log", "raw", "output_text", "provider_payload",
            "response_webhook_token", "resumetoken", "credential", "secret",
            "webhook", "resumeurl", "/home/",
        ):
            self.assertNotIn(forbidden, projection.lower())

    def test_implement_slice_every_terminal_branch_converges_on_exactly_one_durable_write(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        terminal_sources = {
            "recovery_route_fail", "fallback_route_fail", "fallback_wait_timeout",
            "fallback_wait_fail", "fallback_action_fail", "fallback_receipt_fail",
            "human_ack", "commit_fail", "push_fail", "done",
            "primary_action_fail", "timeout", "fail", "receipt_fail",
            "checkout_fail", "ticket_fail", "frontier_fail", "complete_fail",
            "walk_fail", "slices_complete", "route_fail", "review_fail",
            "e2e_fail", "test_http_fail", "correction_action_fail",
            "wait2_timeout", "wait2_fail", "test2_http_fail", "nack_timeout",
        }
        into_join = [
            edge for edge in graph["edges"]
            if edge["target"] == "join_durable_outcome"
        ]
        self.assertEqual({edge["source"] for edge in into_join}, terminal_sources)
        self.assertEqual(len(into_join), len(terminal_sources))
        for source in terminal_sources:
            self.assertEqual(
                sum(edge["source"] == source for edge in into_join), 1, source
            )

        outgoing = {}
        for edge in graph["edges"]:
            outgoing.setdefault(edge["source"], []).append(edge["target"])
        self.assertEqual(outgoing["join_durable_outcome"], ["durable_outcome_candidates"])
        self.assertEqual(outgoing["durable_outcome"], ["durable_outcome_record_key"])
        self.assertEqual(
            [node["id"] for node in graph["nodes"]
             if node["type"] == "action.datastore.records.upsert"],
            ["durable_outcome_upsert"],
        )
        # The pending dashboard notification is not terminal; the same wait can
        # later acknowledge or time out and only that terminal leg is persisted.
        self.assertNotIn("herdr_waiting", {edge["source"] for edge in into_join})
        self.assertEqual(
            {
                node["id"] for node in graph["nodes"]
                if node["id"] not in outgoing
            },
            {
                "herdr_waiting", "durable_outcome_written",
                "durable_outcome_write_failed", "durable_outcome_readback_failed",
                "durable_outcome_readback_mismatch",
            },
        )

    def test_implement_slice_all_terminal_sources_classify_with_runtime_handles(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        terminal_edges = {
            edge["source"]: edge
            for edge in graph["edges"]
            if edge["target"] == "join_durable_outcome"
        }
        # Keep this ledger hard-coded. It is deliberately independent of the graph,
        # so neither a changed node type/handle nor a changed stage can silently make
        # the fixture agree with the same runtime defect.
        expected = {
            "recovery_route_fail": ("provider_recovery", "recovery_route_diagnostic"),
            "fallback_route_fail": ("fallback_route", "fallback_route_diagnostic"),
            "fallback_wait_timeout": ("fallback_wait_timeout", "fallback_wait_timeout_diagnostic"),
            "fallback_wait_fail": ("fallback_wait_failure", "fallback_wait_failure_diagnostic"),
            "fallback_action_fail": ("fallback_action", "fallback_action_diagnostic"),
            "fallback_receipt_fail": ("fallback_receipt", "fallback_receipt_diagnostic"),
            "human_ack": ("human_ack", "human_ack"),
            "commit_fail": ("commit_failed", "commit_fail_terminal"),
            "push_fail": ("push_failed", "push_fail_terminal"),
            "done": ("slice_complete", "done"),
            "primary_action_fail": ("primary_action", "primary_action_diagnostic"),
            "timeout": ("primary_wait_timeout", "primary_wait_timeout"),
            "fail": ("primary_wait_failure", "primary_wait_failure"),
            "receipt_fail": ("writer_receipt", "writer_receipt_diagnostic"),
            "checkout_fail": ("checkout_failed", "checkout_fail_terminal"),
            "ticket_fail": ("ticket_failed", "ticket_fail_terminal"),
            "frontier_fail": ("frontier_failed", "frontier_fail_terminal"),
            "complete_fail": ("completion_failed", "complete_fail_terminal"),
            "walk_fail": ("continuation_failed", "walk_fail_terminal"),
            "slices_complete": ("all_slices_complete", "slices_complete_terminal"),
            "route_fail": ("route_failed", "route_fail_terminal"),
            "review_fail": ("review_failed", "review_fail_terminal"),
            "e2e_fail": ("e2e_failed", "e2e_fail_terminal"),
            "test_http_fail": ("named_test_transport_failed", "test_http_fail_terminal"),
            "correction_action_fail": ("correction_action", "correction_writer_action_failure"),
            "wait2_timeout": ("correction_wait_timeout", "correction_writer_wait_timeout"),
            "wait2_fail": ("correction_wait_failure", "correction_writer_wait_failure"),
            "test2_http_fail": ("retry_test_transport_failed", "test2_http_fail_terminal"),
            "nack_timeout": ("human_timeout", "nack_timeout_terminal"),
        }
        terminal_marker_sources = {
            "commit_fail", "push_fail", "checkout_fail", "ticket_fail", "frontier_fail",
            "complete_fail", "walk_fail", "slices_complete", "route_fail", "review_fail",
            "e2e_fail", "test_http_fail", "test2_http_fail", "nack_timeout",
        }

        def resolve(path, values):
            value = values
            for part in path.split("."):
                if not isinstance(value, dict) or part not in value:
                    return None
                value = value[part]
            return value

        def evaluate(expression, values):
            kind = expression["kind"]
            if kind == "literal":
                return expression["value"]
            if kind == "getField":
                return resolve(expression["path"], values)
            if kind == "binary":
                left = evaluate(expression["left"], values)
                right = evaluate(expression["right"], values)
                if expression["operator"] == "!=":
                    return left != right
                self.fail(f"unsupported stage operator {expression['operator']}")
            if kind == "conditional":
                branch = "then" if evaluate(expression["condition"], values) else "else"
                return evaluate(expression[branch], values)
            self.fail(f"unsupported stage expression {kind}")

        stage_expression = next(
            mapping["expression"]
            for mapping in nodes["durable_outcome_candidates"]["config"]["mappings"]
            if mapping["output"] == "stage"
        )
        self.assertEqual(set(terminal_edges), set(expected))
        self.assertEqual(len(expected), 29)
        for source, (expected_stage, alias) in expected.items():
            with self.subTest(source=source):
                node = nodes[source]
                self.assertEqual(node["type"], "transforms.objectBuilder")
                self.assertEqual(node["config"]["alias"], alias)
                self.assertEqual(terminal_edges[source]["sourceHandle"], "out")
                self.assertEqual(terminal_edges[source]["targetHandle"], "in")
                marker = {"terminal_stage": expected_stage} if source in terminal_marker_sources else {"emitted": True}
                values = {"CTX": {alias: marker}, "TASKS": {source: None}}
                self.assertEqual(evaluate(stage_expression, values), expected_stage)

        for source in terminal_marker_sources:
            with self.subTest(terminal_marker=source):
                expected_stage, alias = expected[source]
                self.assertEqual(nodes[source]["config"], {
                    "alias": alias,
                    "version": 1,
                    "mappings": [{
                        "id": "m1",
                        "output": "terminal_stage",
                        "expression": {"kind": "literal", "value": expected_stage},
                    }],
                })

        # Exact live regression: action.noop emitted null TASKS.frontier_fail in run
        # 80277f03-4198-46bb-9259-474c9ab49099. The native marker must classify it.
        self.assertEqual(evaluate(stage_expression, {
            "CTX": {"frontier_fail_terminal": {"terminal_stage": "frontier_failed"}},
            "TASKS": {"frontier_fail": None},
        }), "frontier_failed")

    def test_implement_slice_every_terminal_emission_can_route_to_durable_join(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        emitted_handle = {
            "action.noop": "success",
            "transforms.objectBuilder": "out",
        }
        terminal_edges = [
            edge for edge in graph["edges"]
            if edge["target"] == "join_durable_outcome"
        ]
        self.assertEqual(len(terminal_edges), 29)
        for edge in terminal_edges:
            with self.subTest(source=edge["source"]):
                node_type = nodes[edge["source"]]["type"]
                self.assertIn(node_type, emitted_handle)
                routed_targets = [
                    candidate["target"] for candidate in graph["edges"]
                    if candidate["source"] == edge["source"]
                    and candidate.get("sourceHandle") == emitted_handle[node_type]
                ]
                self.assertEqual(routed_targets, ["join_durable_outcome"])

    def test_implement_slice_durable_outcome_key_is_replay_idempotent_and_run_scoped(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        key = nodes["durable_outcome_upsert"]["config"]["recordKey"]
        self.assertEqual(key, "{{ CTX.durable_outcome_record_key.value }}")
        self.assertEqual(key, nodes["durable_outcome_readback"]["config"]["recordKey"])
        key_expression = json.dumps(
            nodes["durable_outcome_record_key"]["config"]["mappings"][0]["expression"]
        )
        self.assertEqual(key_expression.count("WORKFLOW.runId"), 1)
        self.assertNotIn("now", key_expression.lower())
        self.assertNotIn("uuid", key_expression.lower())

        edges = graph["edges"]
        self.assertIn({
            "id": "e_durable_write_readback", "source": "durable_outcome_upsert",
            "sourceHandle": "success", "target": "durable_outcome_readback",
            "targetHandle": "in",
        }, edges)
        self.assertIn({
            "id": "e_durable_readback_hash", "source": "durable_outcome_readback",
            "sourceHandle": "success", "target": "durable_outcome_readback_hash",
            "targetHandle": "in",
        }, edges)
        self.assertIn({
            "id": "e_durable_readback_compare", "source": "durable_outcome_readback_hash",
            "sourceHandle": "out", "target": "durable_outcome_readback_check",
            "targetHandle": "in",
        }, edges)
        self.assertIn({
            "id": "e_durable_readback_validate", "source": "durable_outcome_readback_check",
            "sourceHandle": "out", "target": "if_durable_outcome_readback",
            "targetHandle": "in",
        }, edges)
        readback_rules = nodes["if_durable_outcome_readback"]["config"]["rules"]
        self.assertEqual(readback_rules, [
            {"path": "found_matches", "op": "equals", "value": True},
            {"path": "key_matches", "op": "equals", "value": True},
            {"path": "data_matches", "op": "equals", "value": True},
            {"path": "version_matches", "op": "equals", "value": True},
        ])

    @staticmethod
    def _claude_21250_usage_result(cost: object = 0.0125) -> dict:
        """Sanitized shape observed from Claude Code 2.1.250 JSON output."""
        return {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "api_error_status": None,
            "duration_ms": 10,
            "duration_api_ms": 8,
            "num_turns": 1,
            "result": "sanitized fixture result",
            "stop_reason": "end_turn",
            "session_id": "claude-usage-fixture",
            "total_cost_usd": cost,
            "usage": {
                "input_tokens": 40,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 7,
                "output_tokens": 11,
                "server_tool_use": {
                    "web_search_requests": 0,
                    "web_fetch_requests": 0,
                },
                "service_tier": "standard",
                "cache_creation": {
                    "ephemeral_1h_input_tokens": 0,
                    "ephemeral_5m_input_tokens": 3,
                },
                "inference_geo": "",
                "iterations": [{
                    "input_tokens": 40,
                    "output_tokens": 11,
                    "cache_read_input_tokens": 7,
                    "cache_creation_input_tokens": 3,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": 0,
                        "ephemeral_5m_input_tokens": 3,
                    },
                    "type": "message",
                }],
                "speed": "standard",
            },
            "modelUsage": {
                "claude-opus-5": {
                    "inputTokens": 40,
                    "outputTokens": 11,
                    "cacheReadInputTokens": 7,
                    "cacheCreationInputTokens": 3,
                    "webSearchRequests": 0,
                    "costUSD": cost,
                    "contextWindow": 200000,
                    "maxOutputTokens": 32000,
                },
            },
            "permission_denials": [],
            "terminal_reason": "completed",
            "fast_mode_state": "off",
            "uuid": "claude-usage-fixture",
        }

    def test_claude_21250_result_schema_snapshot_matches_parser_sets(self):
        """Sanitized field inventory derived from the installed 2.1.250 schema."""
        snapshot = {
            "result_required": {
                "type", "subtype", "duration_ms", "duration_api_ms", "is_error",
                "num_turns", "result", "stop_reason", "total_cost_usd", "usage",
                "modelUsage", "permission_denials", "uuid", "session_id",
            },
            "result_optional": {
                "ttft_ms", "ttft_stream_ms", "time_to_request_ms",
                "user_message_uuid", "request_sent_wall_ms",
                "time_to_request_from_spawn_ms", "warm_spare_claimed",
                "time_origin_ms", "api_error_status", "subagent_stats",
                "queued_turn_count", "structured_output", "deferred_tool_use",
                "terminal_reason", "fast_mode_state", "fast_mode_disabled_reason",
                "origin",
            },
            "model_usage_required": {
                "inputTokens", "outputTokens", "cacheReadInputTokens",
                "cacheCreationInputTokens", "webSearchRequests", "costUSD",
                "contextWindow", "maxOutputTokens",
            },
            "model_usage_optional": {"canonicalModel", "provider", "costBasis"},
            "terminal_reasons": {
                "blocking_limit", "rapid_refill_breaker", "prompt_too_long",
                "image_error", "model_error", "api_error",
                "malformed_tool_use_exhausted", "aborted_streaming",
                "aborted_tools", "stop_hook_prevented", "hook_stopped",
                "tool_deferred", "max_turns", "background_requested", "completed",
                "budget_exhausted", "structured_output_retry_exhausted",
                "tool_deferred_unavailable", "turn_setup_failed",
            },
            "fast_mode_states": {"off", "cooldown", "on"},
            "fast_mode_disabled_reasons": {
                "free", "preference", "extra_usage_disabled", "network_error",
                "unknown", "not_first_party", "disabled_by_env",
                "model_not_allowed", "sdk_opt_in_required", "pending",
            },
            "origin_kinds": {
                "human", "channel", "peer", "task-notification", "coordinator",
                "unclassified", "observer", "auto-continuation",
                "observer-activity",
            },
            "subagent_fields": {
                "spawned", "requested", "started_in_background", "by_type",
                "max_depth", "spawned_by_subagents", "completed", "failed",
                "killed", "refused",
            },
        }
        self.assertEqual(server._CLAUDE_RESULT_REQUIRED_FIELDS, snapshot["result_required"])
        self.assertEqual(server._CLAUDE_RESULT_OPTIONAL_FIELDS, snapshot["result_optional"])
        self.assertEqual(
            server._CLAUDE_MODEL_USAGE_REQUIRED_FIELDS,
            snapshot["model_usage_required"],
        )
        self.assertEqual(
            server._CLAUDE_MODEL_USAGE_OPTIONAL_FIELDS,
            snapshot["model_usage_optional"],
        )
        self.assertEqual(server._CLAUDE_TERMINAL_REASONS, snapshot["terminal_reasons"])
        self.assertEqual(server._CLAUDE_FAST_MODE_STATES, snapshot["fast_mode_states"])
        self.assertEqual(
            server._CLAUDE_FAST_MODE_DISABLED_REASONS,
            snapshot["fast_mode_disabled_reasons"],
        )
        self.assertEqual(server._CLAUDE_ORIGIN_KINDS, snapshot["origin_kinds"])
        self.assertEqual(server._CLAUDE_SUBAGENT_FIELDS, snapshot["subagent_fields"])
        self.assertEqual(
            server._CLAUDE_RESULT_REQUIRED_FIELDS
            & server._CLAUDE_RESULT_OPTIONAL_FIELDS,
            set(),
        )

    def test_claude_21250_required_field_nullability_matches_static_schema(self):
        nullable_required = {"stop_reason"}
        self.assertEqual(
            nullable_required,
            server._CLAUDE_RESULT_NULLABLE_REQUIRED_FIELDS,
        )
        baseline = self._claude_21250_usage_result()
        for field in sorted(server._CLAUDE_RESULT_REQUIRED_FIELDS):
            event = deepcopy(baseline)
            event[field] = None
            with self.subTest(field=field):
                usage, diagnostic = server.normalize_native_usage(
                    "claude", json.dumps(event), 1,
                )
                if field in nullable_required:
                    self.assertIsNotNone(usage)
                    self.assertIsNone(diagnostic)
                else:
                    self.assertIsNone(usage)
                    self.assertEqual(diagnostic, "usage_malformed")

    def test_claude_21250_contract_optional_variants_are_validation_only(self):
        base = self._claude_21250_usage_result()
        for field in ("api_error_status", "terminal_reason", "fast_mode_state"):
            del base[field]

        expected, diagnostic = server.normalize_native_usage(
            "claude", json.dumps(base), 1,
        )
        self.assertIsNone(diagnostic)
        self.assertIsNotNone(expected)

        variants = {}
        variants["omitted_optionals"] = deepcopy(base)

        null_stop_reason = deepcopy(base)
        null_stop_reason["stop_reason"] = None
        variants["null_stop_reason"] = null_stop_reason

        ttft = deepcopy(base)
        ttft["ttft_ms"] = 3
        variants["ttft_ms"] = ttft

        canonical_model = deepcopy(base)
        canonical_model["modelUsage"]["claude-opus-5"]["canonicalModel"] = (
            "claude-opus-5"
        )
        variants["canonicalModel"] = canonical_model

        provider = deepcopy(base)
        provider["modelUsage"]["claude-opus-5"]["provider"] = "firstParty"
        variants["provider"] = provider

        cost_basis = deepcopy(base)
        cost_basis["modelUsage"]["claude-opus-5"]["costBasis"] = "managed"
        variants["costBasis"] = cost_basis

        optional_values = {
            "ttft_stream_ms": 4,
            "time_to_request_ms": 5,
            "user_message_uuid": "user-message-fixture",
            "request_sent_wall_ms": 1_787_000_000_000.5,
            "time_to_request_from_spawn_ms": 6,
            "warm_spare_claimed": True,
            "time_origin_ms": 1_787_000_000_000,
            "api_error_status": 429,
            "subagent_stats": {
                "spawned": 1,
                "requested": {"background": 1, "foreground": 0, "unset": 0},
                "started_in_background": 1,
                "by_type": {"Explore": 1},
                "max_depth": 1,
                "spawned_by_subagents": 0,
                "completed": 1,
                "failed": 0,
                "killed": {"parent": 0, "user": 0, "system": 0},
                "refused": {"depth_limit": 0, "concurrency_limit": 0, "budget": 0},
            },
            "queued_turn_count": 2,
            "structured_output": {"ok": True, "items": [1, None, "value"]},
            "deferred_tool_use": {
                "id": "tool-use-fixture",
                "name": "ExampleTool",
                "input": {"safe": True},
            },
            "fast_mode_disabled_reason": "preference",
            "origin": {"kind": "channel", "server": "fixture-channel"},
        }
        for field, value in optional_values.items():
            event = deepcopy(base)
            event[field] = value
            variants[field] = event

        combined = self._claude_21250_usage_result()
        combined["stop_reason"] = None
        combined["ttft_ms"] = 3
        for field, value in optional_values.items():
            combined[field] = deepcopy(value)
        combined["modelUsage"]["claude-opus-5"].update({
            "canonicalModel": "claude-opus-5",
            "provider": "firstParty",
            "costBasis": "list",
        })
        combined["modelUsage"]["claude-sonnet-5"] = {
            **combined["modelUsage"]["claude-opus-5"],
            "canonicalModel": "claude-sonnet-5",
            "provider": "bedrock",
            "costBasis": "unknown",
        }
        variants["combined_multiple_models"] = combined

        self.assertEqual(len(variants), 21)
        for name, event in variants.items():
            with self.subTest(case=name):
                self.assertEqual(
                    server.normalize_native_usage(
                        "claude", json.dumps(event), 1,
                    ),
                    (expected, None),
                )

    def test_claude_21250_contract_optionals_remain_closed_and_typed(self):
        top_level_wrong = {
            "api_error_status": "none",
            "terminal_reason": 1,
            "fast_mode_state": [],
            "ttft_ms": 1.5,
        }
        for field, value in top_level_wrong.items():
            with self.subTest(location="result", field=field, case="wrong_value"):
                event = self._claude_21250_usage_result()
                event[field] = value
                self.assertEqual(
                    server.normalize_native_usage(
                        "claude", json.dumps(event), 1,
                    ),
                    (None, "usage_malformed"),
                )
            with self.subTest(location="result", field=field, case="unknown_mirror"):
                event = self._claude_21250_usage_result()
                event.pop(field, None)
                event[f"{field}_unexpected"] = value
                self.assertEqual(
                    server.normalize_native_usage(
                        "claude", json.dumps(event), 1,
                    ),
                    (None, "usage_malformed"),
                )

        model_wrong = {
            "canonicalModel": 1,
            "provider": [],
            "costBasis": {},
        }
        for field, value in model_wrong.items():
            with self.subTest(location="modelUsage", field=field, case="wrong_value"):
                event = self._claude_21250_usage_result()
                event["modelUsage"]["claude-opus-5"][field] = value
                self.assertEqual(
                    server.normalize_native_usage(
                        "claude", json.dumps(event), 1,
                    ),
                    (None, "usage_malformed"),
                )
            with self.subTest(location="modelUsage", field=field, case="unknown_mirror"):
                event = self._claude_21250_usage_result()
                event["modelUsage"]["claude-opus-5"][f"{field}Unexpected"] = value
                self.assertEqual(
                    server.normalize_native_usage(
                        "claude", json.dumps(event), 1,
                    ),
                    (None, "usage_malformed"),
                )

        for field, value in {
            "terminal_reason": "not-a-terminal-reason",
            "fast_mode_state": "turbo",
        }.items():
            with self.subTest(location="result", field=field, case="unknown_value"):
                event = self._claude_21250_usage_result()
                event[field] = value
                self.assertEqual(
                    server.normalize_native_usage(
                        "claude", json.dumps(event), 1,
                    ),
                    (None, "usage_malformed"),
                )

        event = self._claude_21250_usage_result()
        event["modelUsage"]["claude-opus-5"]["costBasis"] = "retail"
        self.assertEqual(
            server.normalize_native_usage("claude", json.dumps(event), 1),
            (None, "usage_malformed"),
        )

        combined = self._claude_21250_usage_result()
        combined["ttft_ms"] = 3
        combined["modelUsage"]["claude-opus-5"].update({
            "canonicalModel": "claude-opus-5",
            "provider": "firstParty",
            "costBasis": "list",
        })
        combined["modelUsage"]["claude-sonnet-5"] = deepcopy(
            combined["modelUsage"]["claude-opus-5"]
        )
        combined["modelUsage"]["claude-sonnet-5"]["unexpected"] = 1
        self.assertEqual(
            server.normalize_native_usage("claude", json.dumps(combined), 1),
            (None, "usage_malformed"),
        )

        duplicate_optional = json.dumps(event).replace(
            '"costBasis": "retail"',
            '"costBasis": "list", "costBasis": "managed"',
        )
        self.assertEqual(duplicate_optional.count('"costBasis"'), 2)
        self.assertEqual(
            server.normalize_native_usage("claude", duplicate_optional, 1),
            (None, "usage_malformed"),
        )

        for value in (True, -1, "3"):
            with self.subTest(location="result", field="ttft_ms", value=value):
                event = self._claude_21250_usage_result()
                event["ttft_ms"] = value
                self.assertEqual(
                    server.normalize_native_usage(
                        "claude", json.dumps(event), 1,
                    ),
                    (None, "usage_malformed"),
                )

    def test_claude_21250_optional_telemetry_is_bounded_and_exactly_typed(self):
        integer_fields = (
            "ttft_ms", "ttft_stream_ms", "time_to_request_ms",
            "time_to_request_from_spawn_ms", "queued_turn_count",
        )
        for field in integer_fields:
            for value in (True, 1.5, -1, 10 ** 100):
                with self.subTest(field=field, value=repr(value)):
                    event = self._claude_21250_usage_result()
                    event[field] = value
                    self.assertEqual(
                        server.normalize_native_usage("claude", json.dumps(event), 1),
                        (None, "usage_malformed"),
                    )
            event = self._claude_21250_usage_result()
            event[f"{field}_unexpected"] = 1
            self.assertEqual(
                server.normalize_native_usage("claude", json.dumps(event), 1),
                (None, "usage_malformed"),
            )

        for field in ("request_sent_wall_ms", "time_origin_ms"):
            for value in (True, "1", -1, 10 ** 100, 1e308):
                with self.subTest(field=field, value=repr(value)):
                    event = self._claude_21250_usage_result()
                    event[field] = value
                    self.assertEqual(
                        server.normalize_native_usage("claude", json.dumps(event), 1),
                        (None, "usage_malformed"),
                    )

        baseline = server.normalize_native_usage(
            "claude", json.dumps(self._claude_21250_usage_result()), 1,
        )
        for value in (False, True):
            event = self._claude_21250_usage_result()
            event["warm_spare_claimed"] = value
            self.assertEqual(
                server.normalize_native_usage("claude", json.dumps(event), 1),
                baseline,
            )
        for value in (1, "true", None):
            event = self._claude_21250_usage_result()
            event["warm_spare_claimed"] = value
            self.assertEqual(
                server.normalize_native_usage("claude", json.dumps(event), 1),
                (None, "usage_malformed"),
            )

        for value in (-1, 10 ** 100, True, 1.5, "429"):
            event = self._claude_21250_usage_result()
            event["api_error_status"] = value
            self.assertEqual(
                server.normalize_native_usage("claude", json.dumps(event), 1),
                (None, "usage_malformed"),
            )

        valid_stats = {
            "spawned": 1,
            "requested": {"background": 1, "foreground": 0, "unset": 0},
            "started_in_background": 1,
            "by_type": {"Explore": 1},
            "max_depth": 1,
            "spawned_by_subagents": 0,
            "completed": 1,
            "failed": 0,
            "killed": {"parent": 0, "user": 0, "system": 0},
            "refused": {"depth_limit": 0, "concurrency_limit": 0, "budget": 0},
        }
        for location in ("requested", "killed", "refused"):
            event = self._claude_21250_usage_result()
            event["subagent_stats"] = deepcopy(valid_stats)
            event["subagent_stats"][location]["unexpected"] = 1
            with self.subTest(field="subagent_stats", location=location):
                self.assertEqual(
                    server.normalize_native_usage("claude", json.dumps(event), 1),
                    (None, "usage_malformed"),
                )
        event = self._claude_21250_usage_result()
        event["subagent_stats"] = deepcopy(valid_stats)
        event["subagent_stats"]["by_type"]["bad\nagent"] = 1
        self.assertEqual(
            server.normalize_native_usage("claude", json.dumps(event), 1),
            (None, "usage_malformed"),
        )
        for value in (-1, True, 10 ** 100):
            event = self._claude_21250_usage_result()
            event["subagent_stats"] = deepcopy(valid_stats)
            event["subagent_stats"]["spawned"] = value
            with self.subTest(field="subagent_stats.spawned", value=repr(value)):
                self.assertEqual(
                    server.normalize_native_usage("claude", json.dumps(event), 1),
                    (None, "usage_malformed"),
                )

        event = self._claude_21250_usage_result()
        event.update({
            "ttft_stream_ms": 4,
            "time_to_request_ms": 5,
            "request_sent_wall_ms": 1_787_000_000_000,
            "time_to_request_from_spawn_ms": 6,
            "time_origin_ms": 1_787_000_000_000,
        })
        duplicate = json.dumps(event).replace(
            '"ttft_stream_ms": 4',
            '"ttft_stream_ms": 4, "ttft_stream_ms": 5',
        )
        self.assertEqual(
            server.normalize_native_usage("claude", duplicate, 1),
            (None, "usage_malformed"),
        )
        nonfinite = json.dumps(event).replace(
            '"time_origin_ms": 1787000000000', '"time_origin_ms": Infinity',
        )
        self.assertEqual(
            server.normalize_native_usage("claude", nonfinite, 1),
            (None, "usage_malformed"),
        )

    def test_claude_21250_optional_metadata_is_bounded_control_free_and_closed(self):
        for field in (
            "ttft_ms", "ttft_stream_ms", "time_to_request_ms",
            "user_message_uuid", "request_sent_wall_ms",
            "time_to_request_from_spawn_ms", "warm_spare_claimed",
            "time_origin_ms", "api_error_status", "subagent_stats",
            "queued_turn_count", "structured_output", "deferred_tool_use",
            "terminal_reason", "fast_mode_state", "fast_mode_disabled_reason",
            "origin",
        ):
            event = self._claude_21250_usage_result()
            event[f"{field}_unexpected"] = None
            with self.subTest(field=field, case="unknown_mirror"):
                self.assertEqual(
                    server.normalize_native_usage("claude", json.dumps(event), 1),
                    (None, "usage_malformed"),
                )

        for value in ("", "bad\nvalue", "x" * 1025, 7, None):
            event = self._claude_21250_usage_result()
            event["user_message_uuid"] = value
            with self.subTest(field="user_message_uuid", value_type=type(value).__name__):
                self.assertEqual(
                    server.normalize_native_usage("claude", json.dumps(event), 1),
                    (None, "usage_malformed"),
                )

        for field, value in {
            "deferred_tool_use": {"id": "id", "name": "name", "input": {}, "extra": 1},
            "origin": {"kind": "channel", "server": "fixture", "extra": 1},
            "subagent_stats": {},
        }.items():
            event = self._claude_21250_usage_result()
            event[field] = value
            with self.subTest(field=field, case="closed"):
                self.assertEqual(
                    server.normalize_native_usage("claude", json.dumps(event), 1),
                    (None, "usage_malformed"),
                )

        for field, values in {
            "fast_mode_disabled_reason": ("free", "pending", "invalid"),
            "terminal_reason": ("completed", "api_error", "invalid"),
        }.items():
            for value in values:
                event = self._claude_21250_usage_result()
                event[field] = value
                usage, diagnostic = server.normalize_native_usage(
                    "claude", json.dumps(event), 1,
                )
                with self.subTest(field=field, value=value):
                    if value == "invalid":
                        self.assertIsNone(usage)
                        self.assertEqual(diagnostic, "usage_malformed")
                    else:
                        self.assertIsNotNone(usage)
                        self.assertIsNone(diagnostic)

    @staticmethod
    def _claude_result_with_raw_optional(field, raw_value):
        event = DispatchTests._claude_21250_usage_result()
        encoded = json.dumps(event, separators=(",", ":"))
        if field == "permission_denials":
            return encoded.replace('"permission_denials":[]', f'"permission_denials":{raw_value}')
        return encoded[:-1] + f',"{field}":{raw_value}' + "}"

    def test_optional_json_telemetry_depth_is_explicitly_bounded(self):
        self.assertEqual(server._PROVIDER_JSON_MAX_DEPTH, 32)
        expected = server.normalize_native_usage(
            "claude", json.dumps(self._claude_21250_usage_result()), 1,
        )
        family_offsets = {
            "structured_output": (1, lambda raw: raw),
            "deferred_tool_use": (
                2,
                lambda raw: '{"id":"id","name":"name","input":' + raw + "}",
            ),
            "permission_denials": (
                3,
                lambda raw: (
                    '[{"tool_name":"tool","tool_use_id":"id","tool_input":'
                    + raw + "}]"
                ),
            ),
        }
        for field, (offset, wrap) in family_offsets.items():
            for delta, accepted in ((-1, True), (0, True), (1, False)):
                depth = server._PROVIDER_JSON_MAX_DEPTH + delta
                raw = "{}"
                for _ in range(depth - offset):
                    raw = '{"nested":' + raw + "}"
                native = self._claude_result_with_raw_optional(field, wrap(raw))
                with self.subTest(field=field, depth=depth):
                    result = server.normalize_native_usage("claude", native, 1)
                    self.assertEqual(
                        result,
                        expected if accepted else (None, "usage_malformed"),
                    )
            raw = "{}"
            for _ in range(1500 - offset):
                raw = '{"nested":' + raw + "}"
            native = self._claude_result_with_raw_optional(field, wrap(raw))
            with self.subTest(field=field, depth=1500):
                self.assertEqual(
                    server.normalize_native_usage("claude", native, 1),
                    (None, "usage_malformed"),
                )

    def test_optional_json_telemetry_width_and_strings_are_bounded(self):
        self.assertEqual(server._PROVIDER_JSON_MAX_ARRAY_ITEMS, 1024)
        self.assertEqual(server._PROVIDER_JSON_MAX_OBJECT_ITEMS, 1024)
        self.assertEqual(server._PROVIDER_JSON_MAX_KEY_CHARS, 1024)
        self.assertEqual(server._PROVIDER_JSON_MAX_STRING_CHARS, 256 * 1024)
        expected = server.normalize_native_usage(
            "claude", json.dumps(self._claude_21250_usage_result()), 1,
        )

        for count, accepted in ((1024, True), (1025, False)):
            raw = "[" + ",".join("0" for _ in range(count)) + "]"
            native = self._claude_result_with_raw_optional("structured_output", raw)
            with self.subTest(family="structured_output", shape="array", count=count):
                self.assertEqual(
                    server.normalize_native_usage("claude", native, 1),
                    expected if accepted else (None, "usage_malformed"),
                )

        for count, accepted in ((1024, True), (1025, False)):
            raw = "{" + ",".join(f'"k{i}":0' for i in range(count)) + "}"
            deferred = '{"id":"id","name":"name","input":' + raw + "}"
            native = self._claude_result_with_raw_optional("deferred_tool_use", deferred)
            with self.subTest(family="deferred_tool_use", shape="object", count=count):
                self.assertEqual(
                    server.normalize_native_usage("claude", native, 1),
                    expected if accepted else (None, "usage_malformed"),
                )

        for length, accepted in ((1024, True), (1025, False)):
            raw = json.dumps({"k" * length: 0}, separators=(",", ":"))
            native = self._claude_result_with_raw_optional("structured_output", raw)
            with self.subTest(family="structured_output", shape="key", length=length):
                self.assertEqual(
                    server.normalize_native_usage("claude", native, 1),
                    expected if accepted else (None, "usage_malformed"),
                )

        for length, accepted in ((256 * 1024, True), (256 * 1024 + 1, False)):
            raw = json.dumps({"value": "x" * length}, separators=(",", ":"))
            deferred = '{"id":"id","name":"name","input":' + raw + "}"
            native = self._claude_result_with_raw_optional("deferred_tool_use", deferred)
            with self.subTest(family="deferred_tool_use", shape="string", length=length):
                self.assertEqual(
                    server.normalize_native_usage("claude", native, 1),
                    expected if accepted else (None, "usage_malformed"),
                )

        denial = '{"tool_name":"tool","tool_use_id":"id","tool_input":{}}'
        for count, accepted in ((512, True), (1025, False)):
            raw = "[" + ",".join(denial for _ in range(count)) + "]"
            native = self._claude_result_with_raw_optional("permission_denials", raw)
            with self.subTest(family="permission_denials", shape="array", count=count):
                self.assertEqual(
                    server.normalize_native_usage("claude", native, 1),
                    expected if accepted else (None, "usage_malformed"),
                )

    def test_provider_json_aggregate_budgets_reject_distributed_excess(self):
        self.assertEqual(server._PROVIDER_JSON_MAX_NODES, 8192)
        self.assertEqual(server._PROVIDER_JSON_MAX_CONTAINERS, 2048)
        self.assertEqual(server._PROVIDER_JSON_MAX_TOTAL_STRING_CHARS, 288 * 1024)
        self.assertEqual(server._PROVIDER_JSON_MAX_TOTAL_ARRAY_ITEMS, 4096)
        self.assertEqual(server._PROVIDER_JSON_MAX_TOTAL_OBJECT_ITEMS, 4096)

        cases = (
            [[0, 0, 0, 0] for _ in range(1024)],
            [{"a": {}, "b": {}} for _ in range(683)],
            {
                f"k{i}": {f"v{j}": 0 for j in range(4)}
                for i in range(1024)
            },
            {"a": "x" * (144 * 1024), "b": "y" * (144 * 1024 + 1)},
        )
        for index, value in enumerate(cases):
            with self.subTest(case=index):
                self.assertFalse(server._provider_json_is_bounded(value))

    def test_json_decoder_recursion_is_mapped_to_stable_malformed_diagnostic(self):
        raw = json.dumps(self._claude_21250_usage_result())
        with mock.patch.object(server.json, "loads", side_effect=RecursionError("deep")):
            with self.assertRaisesRegex(ValueError, "malformed JSON object"):
                server.strict_json_object(raw)
            self.assertEqual(
                server.normalize_native_usage("claude", raw, 1),
                (None, "usage_malformed"),
            )

        provider_object = {}
        for _ in range(1500):
            provider_object = {"nested": provider_object}
        self.assertEqual(
            server.normalize_native_usage("grok", provider_object, 1),
            (None, "usage_malformed"),
        )
        with mock.patch.object(
            server, "_normalize_native_usage", side_effect=RecursionError("validator"),
        ):
            self.assertEqual(
                server.normalize_native_usage("grok", {}, 1),
                (None, "usage_malformed"),
            )

    def test_provider_output_consumers_fail_closed_on_deep_objects(self):
        raw_value = "{}"
        for _ in range(1500):
            raw_value = '{"nested":' + raw_value + "}"
        native = self._claude_result_with_raw_optional("structured_output", raw_value)
        self.assertIsNone(server.parse_native_session_id(native, "claude"))

        receipt_native = native.replace(
            '"result":"sanitized fixture result"',
            '"result":"{\\"status\\":\\"ok\\"}"',
        )
        self.assertIsNone(server.parse_receipt_text(receipt_native))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider.jsonl"
            path.write_text(native + "\n")
            self.assertIsNone(server._codeoff_jsonl(path))

        error = '{"code":"rate_limit"}'
        for _ in range(1500):
            error = '{"data":' + error + "}"
        self.assertIsNone(
            server.structured_provider_failure('{"type":"error","error":' + error + "}")
        )

    def test_claude_21250_origin_union_variants_are_validation_only(self):
        origins = (
            {"kind": "human"},
            {"kind": "channel", "server": "fixture"},
            {
                "kind": "peer", "from": "peer-fixture", "fromMode": "prompting",
                "name": "Peer", "fromSession": "session-fixture",
                "inbound_origin": "socket", "senderTaskId": "task-fixture",
                "body": "line one\nline two", "verifiedPeerPid": 1234,
            },
            {"kind": "task-notification", "subkind": "scheduled-trigger"},
            {"kind": "coordinator"},
            {"kind": "unclassified"},
            {"kind": "observer", "from": "observer-fixture", "senderTaskId": "task"},
            {"kind": "auto-continuation"},
            {"kind": "observer-activity"},
        )
        expected = server.normalize_native_usage(
            "claude", json.dumps(self._claude_21250_usage_result()), 1,
        )
        for origin in origins:
            event = self._claude_21250_usage_result()
            event["origin"] = origin
            with self.subTest(kind=origin["kind"]):
                self.assertEqual(
                    server.normalize_native_usage("claude", json.dumps(event), 1),
                    expected,
                )

    def test_claude_usage_normalization(self):
        event = self._claude_21250_usage_result()
        event["result"] = "hostile prompt TOKEN_SECRET /home/private response"
        native = json.dumps(event) + "\n"
        usage, diagnostic = server.normalize_native_usage("claude", native, 1.25)
        self.assertIsNone(diagnostic)
        self.assertEqual(usage, {
            "usage_version": "normalized-usage-v1",
            "fresh_input_tokens": 40, "cached_input_tokens": 7,
            "cache_write_tokens": 3, "output_tokens": 11,
            "reasoning_tokens": 0, "provider_cost_usd": 0.0125,
            "wall_seconds": 1.25, "turns_observed": None,
        })

    def test_claude_empty_model_usage_is_valid_support_metadata(self):
        nonzero = self._claude_21250_usage_result()
        nonzero["modelUsage"] = {}
        usage, diagnostic = server.normalize_native_usage(
            "claude", json.dumps(nonzero), 1,
        )
        self.assertIsNone(diagnostic)
        self.assertEqual(usage, {
            "usage_version": "normalized-usage-v1",
            "fresh_input_tokens": 40, "cached_input_tokens": 7,
            "cache_write_tokens": 3, "output_tokens": 11,
            "reasoning_tokens": 0, "provider_cost_usd": 0.0125,
            "wall_seconds": 1.0, "turns_observed": None,
        })

        zero = self._claude_21250_usage_result(0)
        zero["modelUsage"] = {}
        for field in (
            "input_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens", "output_tokens",
        ):
            zero["usage"][field] = 0
            zero["usage"]["iterations"][0][field] = 0
        zero["usage"]["cache_creation"] = {
            "ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 0,
        }
        zero["usage"]["iterations"][0]["cache_creation"] = {
            "ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 0,
        }
        usage, diagnostic = server.normalize_native_usage(
            "claude", json.dumps(zero), 1,
        )
        self.assertIsNone(diagnostic)
        self.assertEqual(usage, {
            "usage_version": "normalized-usage-v1",
            "fresh_input_tokens": 0, "cached_input_tokens": 0,
            "cache_write_tokens": 0, "output_tokens": 0,
            "reasoning_tokens": 0, "provider_cost_usd": 0,
            "wall_seconds": 1.0, "turns_observed": None,
        })

        missing = self._claude_21250_usage_result()
        del missing["modelUsage"]
        self.assertEqual(
            server.normalize_native_usage("claude", json.dumps(missing), 1),
            (None, "usage_malformed"),
        )

    def test_claude_21250_success_envelope_is_closed_and_typed(self):
        valid = self._claude_21250_usage_result()
        usage, diagnostic = server.normalize_native_usage(
            "claude", json.dumps(valid), 1,
        )
        self.assertIsNone(diagnostic)
        self.assertIsNotNone(usage)

        missing_fields = (
            "type", "subtype", "is_error", "duration_ms",
            "duration_api_ms", "num_turns", "result", "stop_reason",
            "session_id", "total_cost_usd", "usage", "modelUsage",
            "permission_denials", "uuid",
        )
        for field in missing_fields:
            with self.subTest(case="missing", field=field):
                event = deepcopy(valid)
                del event[field]
                self.assertEqual(
                    server.normalize_native_usage("claude", json.dumps(event), 1),
                    (None, "usage_malformed"),
                )

        mutations = (
            ("bogus_subtype", "subtype", "bogus"),
            ("error_result", "is_error", True),
            ("numeric_type", "type", 1),
            ("string_is_error", "is_error", "false"),
            ("numeric_session", "session_id", 7),
            ("float_duration", "duration_ms", 10.0),
            ("bool_api_duration", "duration_api_ms", True),
            ("string_turns", "num_turns", "1"),
            ("numeric_result", "result", 1),
            ("bad_api_status", "api_error_status", "200"),
            ("bad_stop_reason", "stop_reason", 1),
            ("bad_terminal_reason", "terminal_reason", "failed"),
            ("bad_fast_mode", "fast_mode_state", 0),
            ("numeric_uuid", "uuid", 9),
            ("object_denials", "permission_denials", {}),
            ("list_model_usage", "modelUsage", []),
        )
        for name, field, value in mutations:
            with self.subTest(case=name):
                event = deepcopy(valid)
                event[field] = value
                self.assertEqual(
                    server.normalize_native_usage("claude", json.dumps(event), 1),
                    (None, "usage_malformed"),
                )

        extra = deepcopy(valid)
        extra["unexpected"] = "must reject"
        self.assertEqual(
            server.normalize_native_usage("claude", json.dumps(extra), 1),
            (None, "usage_malformed"),
        )

    def test_non_json_constants_are_rejected_at_every_depth(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            envelope = self._claude_21250_usage_result()
            envelope["permission_denials"] = ["__CONSTANT__"]
            nested = self._claude_21250_usage_result()
            nested["usage"]["iterations"][0]["type"] = "__CONSTANT__"
            cases = {
                "top_level": constant,
                "ignored_envelope": json.dumps(envelope).replace(
                    '"__CONSTANT__"', constant,
                ),
                "nested_usage": json.dumps(nested).replace(
                    '"__CONSTANT__"', constant,
                ),
            }
            for placement, raw in cases.items():
                with self.subTest(constant=constant, placement=placement):
                    with self.assertRaises(ValueError):
                        server.strict_json_object(raw)
                    self.assertEqual(
                        server.normalize_native_usage("claude", raw, 1),
                        (None, "usage_malformed"),
                    )

    def test_claude_cost_literals_preserve_zero_and_reject_unsafe_conversion(self):
        template = json.dumps(self._claude_21250_usage_result("__COST__"))
        accepted = {
            "0": 0,
            "0.0": 0.0,
            "0.0125": 0.0125,
            "0.123456789012345678901234567890": float(
                "0.123456789012345678901234567890"
            ),
            "100000": 100000,
            "100000.0": 100000.0,
        }
        for literal, expected in accepted.items():
            with self.subTest(literal=literal):
                usage, diagnostic = server.normalize_native_usage(
                    "claude", template.replace('"__COST__"', literal), 1,
                )
                self.assertIsNone(diagnostic)
                assert isinstance(usage, dict)
                self.assertEqual(usage["provider_cost_usd"], expected)
                roundtrip = json.loads(json.dumps(usage, allow_nan=False))
                self.assertEqual(roundtrip, usage)

        for literal in (
            "1e-400", "-1e-400", "-1e-3",
            "1e999999999999999999999999", "1e-999999999999999999999999",
            "100000.0000000000000000001",
        ):
            with self.subTest(literal=literal):
                self.assertEqual(
                    server.normalize_native_usage(
                        "claude", template.replace('"__COST__"', literal), 1,
                    ),
                    (None, "usage_malformed"),
                )

    def test_codex_usage_normalization(self):
        native = "\n".join(json.dumps(event) for event in (
            {"type": "thread.started", "thread_id": "codex-usage-1"},
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {
                "input_tokens": 120, "cached_input_tokens": 20,
                "cache_write_input_tokens": 5, "output_tokens": 35,
                "reasoning_output_tokens": 7,
            }},
        )) + "\n"
        usage, diagnostic = server.normalize_native_usage("codex", native, 2)
        self.assertIsNone(diagnostic)
        self.assertEqual(usage, {
            "usage_version": "normalized-usage-v1",
            "fresh_input_tokens": 95, "cached_input_tokens": 20,
            "cache_write_tokens": 5, "output_tokens": 35,
            "reasoning_tokens": 7, "provider_cost_usd": None,
            "wall_seconds": 2.0, "turns_observed": None,
        })

    def test_grok_usage_normalization(self):
        native = {
            "stopReason": "end_turn",
            "usage": {
                "totalTokens": 70, "inputTokens": 50, "outputTokens": 20,
                "thoughtTokens": 10, "cachedReadTokens": 15,
                "cachedWriteTokens": 5,
            },
            "cost": {"amount": 99, "currency": "hostile-not-api-dollars"},
        }
        usage, diagnostic = server.normalize_native_usage("grok", native, 0.5)
        self.assertIsNone(usage)
        self.assertEqual(diagnostic, "usage_malformed")

    def test_native_usage_absence_and_wrong_type_provider_matrix(self):
        for launcher in ("claude", "codex", "grok"):
            with self.subTest(launcher=launcher, case="absent"):
                self.assertEqual(
                    server.normalize_native_usage(launcher, None, 1),
                    (None, "usage_not_reported"),
                )
        for launcher in ("claude", "codex"):
            for native in (b"{}", {}, [], 0, False):
                with self.subTest(
                    launcher=launcher, case="present_wrong_type",
                    native_type=type(native).__name__,
                ):
                    self.assertEqual(
                        server.normalize_native_usage(launcher, native, 1),
                        (None, "usage_malformed"),
                    )
        for native in (b"{}", [], 0, False):
            with self.subTest(
                launcher="grok", case="present_wrong_type",
                native_type=type(native).__name__,
            ):
                self.assertEqual(
                    server.normalize_native_usage("grok", native, 1),
                    (None, "usage_malformed"),
                )

    def test_usage_numeric_bounds_are_exception_safe_and_closed(self):
        from decimal import Decimal

        huge_integer = 10 ** 4000
        self.assertIsNone(server._usage_number(huge_integer))
        self.assertIsNone(server._usage_number(1e308))
        self.assertIsNone(server._usage_number(10 ** 20))
        self.assertIsNone(server._usage_number(Decimal("1")))

        event = self._claude_21250_usage_result(0.01)
        event["usage"]["input_tokens"] = huge_integer
        usage, diagnostic = server.normalize_native_usage(
            "claude", json.dumps(event), 1,
        )
        self.assertIsNone(usage)
        self.assertEqual(diagnostic, "usage_malformed")

        event["usage"]["input_tokens"] = 1
        for field, value in (("total_cost_usd", 1e308), ("total_cost_usd", 10 ** 20)):
            with self.subTest(field=field, value_type=type(value).__name__):
                event[field] = value
                self.assertEqual(
                    server.normalize_native_usage("claude", json.dumps(event), 1),
                    (None, "usage_malformed"),
                )

    def test_codex_01501_complete_usage_event_maps_every_reported_counter(self):
        native = "\n".join(json.dumps(event) for event in (
            {"type": "thread.started", "thread_id": "codex-supported-shape"},
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {
                "input_tokens": 120, "cached_input_tokens": 20,
                "cache_write_input_tokens": 10, "output_tokens": 35,
                "reasoning_output_tokens": 7,
            }},
        )) + "\n"
        usage, diagnostic = server.normalize_native_usage("codex", native, 2)
        self.assertIsNone(diagnostic)
        self.assertEqual(usage, {
            "usage_version": "normalized-usage-v1",
            "fresh_input_tokens": 90, "cached_input_tokens": 20,
            "cache_write_tokens": 10, "output_tokens": 35,
            "reasoning_tokens": 7, "provider_cost_usd": None,
            "wall_seconds": 2.0, "turns_observed": None,
        })

        incomplete = native.replace(', "reasoning_output_tokens": 7', "")
        self.assertEqual(
            server.normalize_native_usage("codex", incomplete, 2),
            (None, "usage_incomplete"),
        )

        contradictory = native.replace(
            '"cached_input_tokens": 20', '"cached_input_tokens": 111',
        )
        self.assertEqual(
            server.normalize_native_usage("codex", contradictory, 2),
            (None, "usage_malformed"),
        )

    def test_grok_105_terminal_contract_accepts_every_stop_reason_and_optional_usage(self):
        # Sanitized from the installed 1.0.5 static ACP PromptResponse/StopReason
        # contract; no launcher or provider is invoked by this fixture.
        valid_stop_reasons = (
            "end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled",
        )
        native_usage = {
            "totalTokens": 70, "inputTokens": 50, "outputTokens": 20,
            "thoughtTokens": 10, "cachedReadTokens": 0,
            "cachedWriteTokens": 0,
        }
        expected = {
            "usage_version": "normalized-usage-v1",
            "fresh_input_tokens": 50, "cached_input_tokens": 0,
            "cache_write_tokens": 0, "output_tokens": 20,
            "reasoning_tokens": 10, "provider_cost_usd": None,
            "wall_seconds": 0.5, "turns_observed": None,
        }
        for stop_reason in valid_stop_reasons:
            for usage_state in ("present", "absent", "null"):
                envelope: dict[str, object] = {"stopReason": stop_reason}
                if usage_state == "present":
                    envelope["usage"] = deepcopy(native_usage)
                elif usage_state == "null":
                    envelope["usage"] = None
                with self.subTest(stop_reason=stop_reason, usage_state=usage_state):
                    if usage_state == "present":
                        self.assertEqual(
                            server.normalize_native_usage("grok", envelope, 0.5),
                            (expected, None),
                        )
                    else:
                        self.assertEqual(
                            server.normalize_native_usage("grok", envelope, 0.5),
                            (None, "usage_not_reported"),
                        )

    def test_grok_105_terminal_contract_accepts_typed_optional_response_metadata(self):
        native_usage = {
            "totalTokens": 2, "inputTokens": 1, "outputTokens": 1,
            "thoughtTokens": 0, "cachedReadTokens": 0,
            "cachedWriteTokens": 0,
        }
        valid_optional_fields = (
            {"userMessageId": "fixture-message-id"},
            {"userMessageId": None},
            {"_meta": {"fixture-extension": True}},
            {"_meta": None},
            {"userMessageId": "fixture-message-id", "_meta": {}},
        )
        for optional_fields in valid_optional_fields:
            envelope = {
                "stopReason": "end_turn", "usage": deepcopy(native_usage),
                **optional_fields,
            }
            with self.subTest(optional_fields=optional_fields):
                usage, diagnostic = server.normalize_native_usage("grok", envelope, 1)
                self.assertIsNone(diagnostic)
                self.assertIsNotNone(usage)

        for optional_fields in (
            {"userMessageId": 0}, {"userMessageId": False},
            {"userMessageId": {}}, {"_meta": "metadata"}, {"_meta": []},
        ):
            envelope = {
                "stopReason": "end_turn", "usage": deepcopy(native_usage),
                **optional_fields,
            }
            with self.subTest(optional_fields=optional_fields):
                self.assertEqual(
                    server.normalize_native_usage("grok", envelope, 1),
                    (None, "usage_malformed"),
                )

    def test_grok_105_terminal_contract_validates_stop_reason_and_closed_envelope(self):
        native_usage = {
            "totalTokens": 2, "inputTokens": 1, "outputTokens": 1,
            "thoughtTokens": 0, "cachedReadTokens": 0,
            "cachedWriteTokens": 0,
        }
        for stop_reason in (None, 0, False, {}, [], "", "unknown_stop"):
            with self.subTest(stop_reason=repr(stop_reason)):
                self.assertEqual(
                    server.normalize_native_usage(
                        "grok",
                        {"stopReason": stop_reason, "usage": deepcopy(native_usage)},
                        1,
                    ),
                    (None, "usage_malformed"),
                )

        for envelope in (
            {"usage": deepcopy(native_usage)},
            {"stopReason": "end_turn", "usage": deepcopy(native_usage), "unknown": 0},
        ):
            with self.subTest(keys=sorted(envelope)):
                self.assertEqual(
                    server.normalize_native_usage("grok", envelope, 1),
                    (None, "usage_malformed"),
                )

    def test_grok_105_terminal_contract_distinguishes_absent_incomplete_and_malformed_usage(self):
        complete = {
            "totalTokens": 2, "inputTokens": 1, "outputTokens": 1,
            "thoughtTokens": 0, "cachedReadTokens": 0,
            "cachedWriteTokens": 0,
        }
        incomplete = deepcopy(complete)
        del incomplete["thoughtTokens"]
        malformed = deepcopy(complete)
        malformed["unexpected"] = 0
        cases = (
            ({"stopReason": "end_turn"}, "usage_not_reported"),
            ({"stopReason": "end_turn", "usage": None}, "usage_not_reported"),
            ({"stopReason": "end_turn", "usage": incomplete}, "usage_incomplete"),
            ({"stopReason": "end_turn", "usage": malformed}, "usage_malformed"),
            ({"stopReason": "end_turn", "usage": []}, "usage_malformed"),
        )
        for envelope, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic, envelope=envelope):
                self.assertEqual(
                    server.normalize_native_usage("grok", envelope, 1),
                    (None, diagnostic),
                )

    def test_grok_105_usage_does_not_double_count_overlapping_thought_tokens(self):
        native = {
            "stopReason": "end_turn",
            "usage": {
                "totalTokens": 70, "inputTokens": 50, "outputTokens": 20,
                "thoughtTokens": 10, "cachedReadTokens": 0,
                "cachedWriteTokens": 0,
            },
        }
        usage, diagnostic = server.normalize_native_usage("grok", native, 0.5)
        self.assertIsNone(diagnostic)
        self.assertEqual(usage, {
            "usage_version": "normalized-usage-v1",
            "fresh_input_tokens": 50, "cached_input_tokens": 0,
            "cache_write_tokens": 0, "output_tokens": 20,
            "reasoning_tokens": 10, "provider_cost_usd": None,
            "wall_seconds": 0.5, "turns_observed": None,
        })

        native["usage"]["cachedReadTokens"] = 5
        self.assertEqual(
            server.normalize_native_usage("grok", native, 0.5),
            (None, "usage_incomplete"),
        )

    def test_turns_observed_never_trusts_provider_authored_turn_counts(self):
        claude_event = self._claude_21250_usage_result(0.01)
        claude_event["num_turns"] = 999999999
        claude = json.dumps(claude_event)
        claude_usage, diagnostic = server.normalize_native_usage("claude", claude, 1)
        self.assertIsNone(diagnostic)
        self.assertIsNone(claude_usage["turns_observed"])

        codex = "\n".join((
            '{"type":"thread.started","thread_id":"observed-boundaries"}',
            '{"type":"turn.started"}',
            '{"type":"turn.completed","usage":{"input_tokens":4,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":2,"reasoning_output_tokens":1}}',
        ))
        codex_usage, diagnostic = server.normalize_native_usage("codex", codex, 1)
        self.assertIsNone(diagnostic)
        self.assertIsNone(codex_usage["turns_observed"])

    def test_usage_parsers_reject_duplicate_keys_and_unknown_extras_for_all_providers(self):
        claude_valid = self._claude_21250_usage_result()
        claude_raw = json.dumps(claude_valid)
        claude_usage_extra = deepcopy(claude_valid)
        claude_usage_extra["usage"]["unexpected"] = 0
        claude_server_tool_extra = deepcopy(claude_valid)
        claude_server_tool_extra["usage"]["server_tool_use"]["unexpected"] = 0
        claude_iteration_extra = deepcopy(claude_valid)
        claude_iteration_extra["usage"]["iterations"][0]["unexpected"] = 0
        claude_model_extra = deepcopy(claude_valid)
        claude_model_extra["modelUsage"]["claude-opus-5"]["unexpected"] = 0
        claude_cases = (
            claude_raw.replace(
                '"type": "result"',
                '"type": "result", "type": "result"',
                1,
            ),
            claude_raw.replace(
                '"input_tokens": 40',
                '"input_tokens": 40, "input_tokens": 40',
                1,
            ),
            json.dumps(claude_usage_extra),
            json.dumps(claude_server_tool_extra),
            json.dumps(claude_iteration_extra),
            json.dumps(claude_model_extra),
        )
        codex_cases = (
            '{"type":"turn.completed","type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}}',
            '{"type":"turn.completed","usage":{"input_tokens":1,"input_tokens":1,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}}',
            '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0,"unexpected":0}}',
        )
        for launcher, cases in (("claude", claude_cases), ("codex", codex_cases)):
            for index, raw in enumerate(cases):
                with self.subTest(launcher=launcher, case=index):
                    self.assertEqual(
                        server.normalize_native_usage(launcher, raw, 1),
                        (None, "usage_malformed"),
                    )

        grok_extra = {
            "stopReason": "end_turn", "unexpected": 0,
            "usage": {
                "totalTokens": 2, "inputTokens": 1, "outputTokens": 1,
                "thoughtTokens": 0, "cachedReadTokens": 0,
                "cachedWriteTokens": 0,
            },
        }
        self.assertEqual(
            server.normalize_native_usage("grok", grok_extra, 1),
            (None, "usage_malformed"),
        )
        for raw in (
            '{"stopReason":"end_turn","stopReason":"end_turn","usage":{"totalTokens":2,"inputTokens":1,"outputTokens":1,"thoughtTokens":0,"cachedReadTokens":0,"cachedWriteTokens":0}}',
            '{"stopReason":"end_turn","usage":{"totalTokens":2,"inputTokens":1,"inputTokens":1,"outputTokens":1,"thoughtTokens":0,"cachedReadTokens":0,"cachedWriteTokens":0}}',
        ):
            with self.assertRaises(ValueError):
                server.strict_json_object(raw)

    def test_openapi_usage_pair_is_an_exact_exclusive_invariant(self):
        spec = json.loads(server.openapi_bytes())
        schemas = spec["components"]["schemas"]
        supported_keywords = {
            "$ref", "additionalProperties", "const", "enum", "maximum", "minimum",
            "properties", "required", "type",
        }

        def matches_test_schema(schema, value):
            self.assertFalse(set(schema) - supported_keywords, schema)
            if "$ref" in schema:
                prefix = "#/components/schemas/"
                self.assertEqual(set(schema), {"$ref"}, schema)
                self.assertTrue(schema["$ref"].startswith(prefix), schema)
                return matches_test_schema(schemas[schema["$ref"][len(prefix):]], value)

            expected_types = schema.get("type")
            if expected_types is not None:
                if isinstance(expected_types, str):
                    expected_types = [expected_types]
                type_matches = {
                    "null": value is None,
                    "object": isinstance(value, dict),
                    "string": isinstance(value, str),
                    "integer": isinstance(value, int) and not isinstance(value, bool),
                    "number": isinstance(value, (int, float)) and not isinstance(value, bool),
                }
                self.assertFalse(set(expected_types) - set(type_matches), schema)
                if not any(type_matches[item] for item in expected_types):
                    return False
            if "const" in schema and value != schema["const"]:
                return False
            if "enum" in schema and value not in schema["enum"]:
                return False
            is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
            if "minimum" in schema and is_number and value < schema["minimum"]:
                return False
            if "maximum" in schema and is_number and value > schema["maximum"]:
                return False
            if isinstance(value, dict):
                required = set(schema.get("required", ()))
                if not required <= set(value):
                    return False
                properties = schema.get("properties", {})
                if schema.get("additionalProperties") is False and set(value) - set(properties):
                    return False
                if any(
                    key in value and not matches_test_schema(property_schema, value[key])
                    for key, property_schema in properties.items()
                ):
                    return False
            return True

        diagnostic_values = set(schemas["UsageDiagnostic"]["enum"]) - {None}
        for schema_name in ("AgentReceipt", "ReviewReceipt"):
            schema = schemas[schema_name]
            branches = schema.get("oneOf")
            self.assertEqual(len(branches or []), 2, schema_name)
            valid_usage, diagnostic = server._closed_usage(
                1, 0, 0, 1, 0, None, 1, None,
            )
            self.assertIsNone(diagnostic)
            pair_match_counts = (
                ({"usage": valid_usage, "usage_diagnostic": None}, 1),
                ({"usage": None, "usage_diagnostic": "usage_malformed"}, 1),
                ({"usage": None, "usage_diagnostic": None}, 0),
                ({"usage": valid_usage, "usage_diagnostic": "usage_malformed"}, 0),
            )
            for pair, expected_match_count in pair_match_counts:
                self.assertEqual(
                    sum(matches_test_schema(branch, pair) for branch in branches),
                    expected_match_count,
                    (schema_name, pair),
                )
        self.assertEqual(diagnostic_values, server.USAGE_DIAGNOSTICS)

    def test_usage_receipt_is_numeric_and_sanitized(self):
        hostile = "TOKEN_SECRET /home/private prompt thought response --dangerous"
        event = self._claude_21250_usage_result()
        event["result"] = hostile
        native = json.dumps(event)
        usage, diagnostic = server.normalize_native_usage("claude", native, 0)
        self.assertIsNone(diagnostic)
        assert isinstance(usage, dict)
        self.assertEqual(set(usage), {
            "usage_version", "fresh_input_tokens", "cached_input_tokens",
            "cache_write_tokens", "output_tokens", "reasoning_tokens",
            "provider_cost_usd", "wall_seconds", "turns_observed",
        })
        compact = json.dumps(usage, sort_keys=True)
        self.assertNotIn(hostile, compact)
        self.assertNotIn("/home/", compact)
        for key in (
            "fresh_input_tokens", "cached_input_tokens", "cache_write_tokens",
            "output_tokens", "reasoning_tokens", "wall_seconds",
        ):
            self.assertNotIsInstance(usage[key], bool)
            self.assertIsInstance(usage[key], (int, float))
        self.assertEqual(usage["provider_cost_usd"], 0.0125)

    def test_usage_normalization_rejects_partial_negative_overflow_bool_nan_and_infinity(self):
        valid = self._claude_21250_usage_result()
        missing, missing_diagnostic = server.normalize_native_usage("claude", "", 1)
        self.assertIsNone(missing)
        self.assertEqual(missing_diagnostic, "usage_not_reported")
        partial = deepcopy(valid)
        del partial["usage"]["output_tokens"]
        self.assertEqual(
            server.normalize_native_usage("claude", json.dumps(partial), 1),
            (None, "usage_incomplete"),
        )
        mutations = (
            ("negative", lambda event: event["usage"].update(input_tokens=-1), 1),
            ("bool", lambda event: event["usage"].update(input_tokens=True), 1),
            ("fraction", lambda event: event["usage"].update(output_tokens=1.5), 1),
            ("overflow", lambda event: event["usage"].update(output_tokens=2**63), 1),
            ("nan_cost", lambda event: event.update(total_cost_usd=float("nan")), 1),
            ("infinite_cost", lambda event: event.update(total_cost_usd=float("inf")), 1),
            ("nan_wall", lambda event: None, float("nan")),
            ("infinite_wall", lambda event: None, float("inf")),
            ("bool_wall", lambda event: None, True),
        )
        for name, mutate, wall in mutations:
            with self.subTest(name=name):
                event = deepcopy(valid)
                mutate(event)
                usage, diagnostic = server.normalize_native_usage(
                    "claude", json.dumps(event), wall
                )
                self.assertIsNone(usage)
                self.assertEqual(diagnostic, "usage_malformed")

    def test_usage_receipt_authority_rejects_drift_and_restart(self):
        usage = {
            "usage_version": "normalized-usage-v1",
            "fresh_input_tokens": 10, "cached_input_tokens": 2,
            "cache_write_tokens": 1, "output_tokens": 3,
            "reasoning_tokens": 4, "provider_cost_usd": None,
            "wall_seconds": 1.5, "turns_observed": 1,
        }
        for kind in ("agent", "review"):
            with self.subTest(kind=kind):
                receipt = {
                    "job_id": ("a1" if kind == "agent" else "b2") * 16,
                    "kind": kind, "usage": usage, "usage_diagnostic": None,
                    "status": "ok",
                }
                server.seal_terminal_receipt_authority(kind, receipt)
                self.assertEqual(
                    server.authorized_receipt_usage(kind, receipt),
                    (usage, None),
                )
                forged = deepcopy(receipt)
                forged["usage"]["output_tokens"] = 999
                self.assertEqual(
                    server.authorized_receipt_usage(kind, forged),
                    (None, "usage_authority_mismatch"),
                )
                forged_status = deepcopy(receipt)
                forged_status["status"] = "error"
                self.assertEqual(
                    server.authorized_receipt_usage(kind, forged_status),
                    (None, "usage_authority_mismatch"),
                )
                nested = {}
                for _ in range(1500):
                    nested = {"provider_extra": nested}
                forged_deep = dict(receipt)
                forged_deep["provider_extra"] = nested
                self.assertEqual(
                    server.authorized_receipt_usage(kind, forged_deep),
                    (None, "usage_authority_mismatch"),
                )
                server.clear_terminal_receipt_authority(kind, receipt["job_id"])
                self.assertEqual(
                    server.authorized_receipt_usage(kind, receipt),
                    (None, "usage_authority_unavailable"),
                )

    def test_receipts_fail_closed_when_native_usage_is_missing(self):
        profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "b" * 40, "native_session_id": "codex-usage-missing",
        }
        job = {
            "job_id": "c3" * 16, "kind": "agent", "repo": "scratch",
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **profile, "session_identity": identity,
        }
        receipt = server.normalize_receipt(
            job, {"status": "ok"}, 0, False,
            usage=None, usage_diagnostic="usage_not_reported",
        )
        self.assertIsNone(receipt["usage"])
        self.assertEqual(receipt["usage_diagnostic"], "usage_not_reported")
        self.assertNotIn("_native_usage", json.dumps(receipt))
        self.assertNotIn("provider_payload", json.dumps(receipt))

    def test_catalog_has_no_dead_effort_fields(self):
        root = Path(server.__file__).parent
        # This is an independent, hard-coded catalog contract. Never derive
        # expected consumers from whichever fields the graph happens to have.
        expected = {
            "implement-slice.json": {
                "route_nodes": {"route", "fallback_route", "recovery_route"},
                "consumers": {
                    "agent": "{{ CTX.selected_route.effort }}",
                    "agent_fallback": "{{ CTX.fallback_route_choice.effort }}",
                    "agent2": "{{ CTX.fallback_receipt.session_identity.requested_effort | default(CTX.receipt.session_identity.requested_effort) }}",
                    "agent3": "{{ CTX.receipt2.session_identity.requested_effort }}",
                    "agent_rn1": "{{ CTX.receipt3.session_identity.requested_effort | default(CTX.receipt2.session_identity.requested_effort) | default(CTX.fallback_receipt.session_identity.requested_effort) | default(CTX.receipt.session_identity.requested_effort) }}",
                    "agent_rn2": "{{ CTX.receipt_rn1.session_identity.requested_effort | default(CTX.receipt3.session_identity.requested_effort) | default(CTX.receipt2.session_identity.requested_effort) | default(CTX.fallback_receipt.session_identity.requested_effort) | default(CTX.receipt.session_identity.requested_effort) }}",
                    "review1": "{{ CTX.active_route.reviewer1_effort }}",
                    "review1b": "{{ CTX.active_route.reviewer1_effort }}",
                    "review2": "{{ CTX.active_route.reviewer2_effort }}",
                    "review2b": "{{ CTX.active_route.reviewer2_effort }}",
                },
                "profiles": {
                    "agent": "{{ CTX.selected_route.writer_execution_profile }}",
                    "agent_fallback": "{{ CTX.fallback_route_choice.writer_execution_profile }}",
                    "agent2": "{{ CTX.fallback_receipt.session_identity.route_execution_profile | default(CTX.receipt.session_identity.route_execution_profile) }}",
                    "agent3": "{{ CTX.receipt2.session_identity.route_execution_profile }}",
                    "agent_rn1": "{{ CTX.receipt3.session_identity.route_execution_profile | default(CTX.receipt2.session_identity.route_execution_profile) | default(CTX.fallback_receipt.session_identity.route_execution_profile) | default(CTX.receipt.session_identity.route_execution_profile) }}",
                    "agent_rn2": "{{ CTX.receipt_rn1.session_identity.route_execution_profile | default(CTX.receipt3.session_identity.route_execution_profile) | default(CTX.receipt2.session_identity.route_execution_profile) | default(CTX.fallback_receipt.session_identity.route_execution_profile) | default(CTX.receipt.session_identity.route_execution_profile) }}",
                    "review1": "{{ CTX.active_route.reviewer1_execution_profile }}",
                    "review1b": "{{ CTX.active_route.reviewer1_execution_profile }}",
                    "review2": "{{ CTX.active_route.reviewer2_execution_profile }}",
                    "review2b": "{{ CTX.active_route.reviewer2_execution_profile }}",
                },
            },
            "pr-drive.json": {
                "route_nodes": {"route"},
                "consumers": {"agent": "{{ TASKS.route.data.effort }}"},
                "profiles": {"agent": "{{ TASKS.route.data.writer_execution_profile }}"},
            },
        }
        graphs = {
            path.name: json.loads(path.read_text())["spec"]
            for path in sorted((root / "graphs").glob("*.json"))
        }
        actual_routed_graphs = {
            name for name, graph in graphs.items()
            if any("/v1/slice/route" in node["type"] for node in graph["nodes"])
        }
        self.assertEqual(actual_routed_graphs, set(expected))
        for graph_name, contract in expected.items():
            nodes = {node["id"]: node for node in graphs[graph_name]["nodes"]}
            self.assertEqual(
                {node_id for node_id, node in nodes.items() if "/v1/slice/route" in node["type"]},
                contract["route_nodes"],
                graph_name,
            )
            actual_consumers = {
                node_id for node_id, node in nodes.items()
                if node["type"].endswith(("/v1/agent/run", "/v1/review/run"))
            }
            self.assertEqual(actual_consumers, set(contract["consumers"]), graph_name)
            for node_id, expression in contract["consumers"].items():
                self.assertEqual(nodes[node_id]["config"].get("effort"), expression, (graph_name, node_id))
                self.assertRegex(expression, r"^\{\{ (?:TASKS|CTX)\.")
                self.assertEqual(
                    nodes[node_id]["config"].get("route_execution_profile"),
                    contract["profiles"][node_id], (graph_name, node_id),
                )
                self.assertRegex(contract["profiles"][node_id], r"^\{\{ (?:TASKS|CTX)\.")

        spec = json.loads((root / "openapi.json").read_text())
        closed_effort = ["default", "low", "medium", "high", "max"]
        for schema_name in ("AgentRunRequest", "ReviewRunRequest"):
            properties = spec["components"]["schemas"][schema_name]["properties"]
            self.assertEqual(properties["effort"]["enum"], closed_effort)
            self.assertEqual(
                properties["route_execution_profile"]["$ref"],
                "#/components/schemas/RouteExecutionProfile",
            )
            self.assertNotIn("reasoning_effort", properties)
        evidence_schema = spec["components"]["schemas"]["RouteExecutionProfile"]
        self.assertFalse(evidence_schema["additionalProperties"])
        self.assertEqual(set(evidence_schema["required"]), server.ROUTE_EXECUTION_PROFILE_FIELDS)
        self.assertEqual(
            evidence_schema["properties"]["role"]["enum"],
            ["writer", "reviewer1", "reviewer2"],
        )
        route_properties = spec["components"]["schemas"]["SliceRoute"]["properties"]
        self.assertEqual(route_properties["effort"]["enum"], ["high", "default"])
        self.assertEqual(route_properties["reviewer1_effort"]["enum"], ["none", "high", "default"])
        self.assertEqual(route_properties["reviewer2_effort"]["enum"], ["none", "high", "default"])

    def test_pr_drive_gates_wait_for_results_too(self):
        # implement-slice got wait nodes for every async op; pr-drive did not.
        # riftwing-local-gates is async (120s > the 25s sync cap), so
        # if_fix_test read a queue receipt and pr-drive would commit and push
        # a "fix" the instant the test job was accepted.
        graph = json.loads((Path(server.__file__).parent / "graphs" / "pr-drive.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]

        self.assertIn("wait_fix_test", nodes)
        self.assertEqual(nodes["wait_fix_test"]["type"], "action.wait.webhook")
        # The action must be reached from the wait node's pending handle, and
        # the filter from its out handle. Anything else is the old shape.
        self.assertTrue(any(e["source"] == "wait_fix_test" and e["sourceHandle"] == "pending"
                            and e["target"] == "fix_test" for e in edges))
        self.assertTrue(any(e["source"] == "wait_fix_test" and e["sourceHandle"] == "out"
                            and e["target"] == "if_fix_test" for e in edges))
        self.assertFalse(any(e["source"] == "fix_test" and e["target"] == "if_fix_test"
                             for e in edges), "fix_test still feeds the gate directly")
        cfg = nodes["fix_test"]["config"]
        self.assertIn("wait_fix_test", cfg.get("response_webhook_url", ""))
        rules = nodes["if_fix_test"]["config"]["rules"]
        self.assertEqual(rules[0]["path"], "request.body.status")

    def test_pr_drive_reads_findings_instead_of_a_human_prompt(self):
        # The agent prompt came from CTX.INPUT.prompt, so driving a PR green
        # started with a person reading the review and writing the brief.
        graph = json.loads((Path(server.__file__).parent / "graphs" / "pr-drive.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        self.assertIn("findings", nodes)
        self.assertTrue(nodes["findings"]["type"].endswith("/v1/gh/pr/findings"))
        self.assertTrue(any(e["source"] == "checks" and e["target"] == "findings" for e in edges))
        self.assertIn("findings", nodes["agent"]["config"]["prompt"])

    def test_pr_drive_agent_budget_fits_inside_its_wait(self):
        # The agent node passed no budget, so the writer took
        # AGENT_RUN_BUDGET=300 and was SIGINTed at 300+60 while still working.
        # rc 130 at 363s, and the graph read the dead receipt as a nack.
        # An explicit budget must leave the wait node room to hear the answer.
        graph = json.loads((Path(server.__file__).parent / "graphs" / "pr-drive.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        cfg = nodes["agent"]["config"]
        budget = int(cfg["run_budget_seconds"])
        self.assertGreater(budget, server.AGENT_RUN_BUDGET,
                           "an unset budget is what killed three writers")
        self.assertGreater(nodes["wait"]["config"]["timeoutSeconds"], budget + 60,
                           "the wait must outlast the budget plus its grace period")

    def test_pr_drive_waits_outlast_the_work_they_wait_on(self):
        # A writer took 335s against a 300s wait and the run was discarded at
        # herdr_wait_timeout, 35 seconds short, with rc 0 and the work staged.
        # The lock says "Graph wait >= budget", but equal leaves no room for
        # process start, the webhook round trip, or a slow provider. The
        # mechanical L budget is 600s, so a wait must clear that with margin.
        graph = json.loads((Path(server.__file__).parent / "graphs" / "pr-drive.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        longest_budget = max(w for _, w in server.SLICE_BUDGET.values())
        for n_id, n in nodes.items():
            if n["type"] == "action.wait.webhook":
                self.assertGreater(n["config"]["timeoutSeconds"], longest_budget,
                                   f"{n_id} can time out on work that is still succeeding")

    def test_pr_drive_pins_its_writer_instead_of_inheriting_the_seat(self):
        # implement-slice pins model and launcher from sliceRoute. pr-drive
        # pinned neither, so its writer inherited ambient launcher state and
        # job.json recorded model: null. Local model changes silently changed
        # which model fixes PRs, and the closed class table did not apply.
        graph = json.loads((Path(server.__file__).parent / "graphs" / "pr-drive.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        self.assertIn("route", nodes, "pr-drive must consult sliceRoute")
        self.assertTrue(nodes["route"]["type"].endswith("/v1/slice/route"))
        cfg = nodes["agent"]["config"]
        self.assertIn("TASKS.route.data.model", cfg.get("model", ""))
        self.assertIn("TASKS.route.data.launcher", cfg.get("launcher", ""))
        # route has to run before the writer, not merely exist.
        order = {e["source"]: e["target"] for e in edges}
        self.assertTrue(any(e["target"] == "route" for e in edges), "route is unreachable")

    def test_pr_drive_gates_never_read_an_artifact_stub(self):
        # A node returning more than ~20KB has its output replaced by an
        # artifact stub, and a filter fed that stub evaluates nothing while
        # still reporting pass. if_green2 decided the loop was finished that
        # way, off a 21KB checks payload it could not see. gh pr checks is the
        # known-large one; gates must read the small findings payload instead.
        graph = json.loads((Path(server.__file__).parent / "graphs" / "pr-drive.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        big = {n_id for n_id, n in nodes.items() if n["type"].endswith("/v1/gh/pr/checks")}
        for e in edges:
            target = nodes.get(e["target"], {})
            if target.get("type") in ("logic.filter", "logic.switch"):
                self.assertNotIn(e["source"], big,
                                 f"{e['target']} gates on {e['source']}, whose output is "
                                 "artifact-stubbed and unreadable")

    def test_pr_drive_auto_merge_rule_matches_the_value_it_receives(self):
        # Form inputs arrive as strings, so auto_merge is "true"/"false". The
        # rule compared against boolean true, so the merge branch could never
        # be taken even when a run explicitly asked for it.
        graph = json.loads((Path(server.__file__).parent / "graphs" / "pr-drive.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        rule = nodes["switch_merge"]["config"]["cases"][0]["rules"][0]
        self.assertIsInstance(rule["value"], str, "auto_merge arrives as a string")
        self.assertEqual(rule["value"], "true")

    def test_pr_drive_switch_can_actually_match_its_input(self):
        # Inserting findings in front of switch_checks left its rules reading
        # data.all_green, which lives on the checks payload and not the findings
        # one, so every case missed and every PR fell through to pending. The
        # first version of the test above asserted only that the edge existed,
        # which the broken graph satisfied. Assert the switch can match instead.
        graph = json.loads((Path(server.__file__).parent / "graphs" / "pr-drive.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]

        feeders = [e["source"] for e in edges if e["target"] == "switch_checks"]
        self.assertEqual(len(feeders), 1, feeders)
        feeder = nodes[feeders[0]]
        self.assertEqual(feeder["type"], "transforms.objectBuilder",
                         "a switch reads its immediate input; snapshot the tasks first")
        produced = {m["output"] for m in feeder["config"]["mappings"]}
        for case in nodes["switch_checks"]["config"]["cases"]:
            for rule in case["rules"]:
                self.assertIn(rule["path"], produced,
                              f"case {case['label']} reads {rule['path']}, which "
                              f"{feeders[0]} never produces")
        # A green PR that is still graded down must reach the fix path, so the
        # blocking case has to be tested before the green one.
        labels = [c["label"] for c in nodes["switch_checks"]["config"]["cases"]]
        self.assertLess(labels.index("blocked_by_findings"), labels.index("green"))

    def test_implement_slice_rewst_templates_are_path_valid_and_jinja_lite_safe(self):
        graph = json.loads(
            (Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text()
        )["spec"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        predecessors = {node_id: set() for node_id in nodes}
        for edge in graph["edges"]:
            predecessors[edge["target"]].add(edge["source"])

        roots = {node_id for node_id, incoming in predecessors.items() if not incoming}
        all_nodes = set(nodes)
        dominators = {
            node_id: ({node_id} if node_id in roots else set(all_nodes))
            for node_id in nodes
        }
        while True:
            updated = {}
            for node_id, incoming in predecessors.items():
                common = set.intersection(*(dominators[pred] for pred in incoming)) if incoming else set()
                updated[node_id] = {node_id} | common
            if updated == dominators:
                break
            dominators = updated

        task_reference = re.compile(r"TASKS\.([A-Za-z0-9_]+)")
        path_errors = []
        jinja_lite_errors = []
        forbidden_jinja_lite = re.compile(r"(?:\{%-?|==|!=|\bif\b|\belse\b|\band\b|\bor\b)")
        for node_id, node in nodes.items():
            dumped = json.dumps(node.get("config", {}))
            for alias in sorted(set(task_reference.findall(dumped))):
                guarded_projection_ref = (
                    node_id == "durable_outcome_candidates"
                    # This post-terminal native objectBuilder deliberately
                    # samples every possible branch. Its AST conditionals
                    # coalesce missing task fields to null; the actual
                    # Riftwing checker validates these expressions.
                )
                bound_async_gate_ref = {
                    "if_test_ok": "test",
                    "if_test_ok2": "test2",
                    "if_test_ok3": "test3",
                    "if_test_rn1": "test_rn1",
                    "if_test_rn2": "test_rn2",
                }.get(node_id) == alias
                if alias not in dominators[node_id] and not (
                    guarded_projection_ref or bound_async_gate_ref
                ):
                    path_errors.append(f"{node_id} references non-dominating TASKS.{alias}")
            if node["type"].startswith("action."):
                def scan(value, path):
                    if isinstance(value, str):
                        if "{%" in value:
                            jinja_lite_errors.append(f"{node_id}.{path}")
                        for expression in re.findall(r"{{(.*?)}}", value, re.DOTALL):
                            if forbidden_jinja_lite.search(expression):
                                jinja_lite_errors.append(f"{node_id}.{path}")
                    elif isinstance(value, dict):
                        for key, child in value.items():
                            scan(child, f"{path}.{key}" if path else key)
                    elif isinstance(value, list):
                        for index, child in enumerate(value):
                            scan(child, f"{path}[{index}]")
                scan(node.get("config", {}), "config")

        self.assertEqual(path_errors, [])
        self.assertEqual(jinja_lite_errors, [])

        indegree = {node_id: 0 for node_id in nodes}
        outgoing = {node_id: [] for node_id in nodes}
        for edge in graph["edges"]:
            indegree[edge["target"]] += 1
            outgoing[edge["source"]].append(edge["target"])
        self.assertTrue(all(
            indegree[node_id] < 2 or nodes[node_id]["type"].startswith("logic.join.")
            for node_id in nodes
        ))
        pending = [node_id for node_id, degree in indegree.items() if degree == 0]
        visited = []
        while pending:
            node_id = pending.pop()
            visited.append(node_id)
            for target in outgoing[node_id]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    pending.append(target)
        self.assertEqual(set(visited), set(nodes), "implement-slice graph must remain acyclic")

    def test_async_gates_wait_for_the_result_not_the_ack(self):
        # testRun and reviewRun return a queue receipt ({"ok": true,
        # "status": "queued"}). The graph used to feed that straight into a
        # filter checking data.ok, so both gates passed the moment the job was
        # accepted, never learning whether tests passed or the reviewer agreed.
        # Every long op must post its result back to a wait node, the way
        # agentRun already did.
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        for target, wait_id, filt, barrier in (
            ("test", "wait_test", "if_test_ok", "join_test_accept"),
            ("review1", "wait_rev1", "if_review1", None),
            ("review1b", "wait_r1b", "if_review1b", None),
            ("review2", "wait_rev2", "if_review2", None),
            ("review2b", "wait_r2b", "if_review2b", None),
        ):
            self.assertEqual(nodes[wait_id]["type"], "action.wait.webhook", wait_id)
            cfg = nodes[target]["config"]
            self.assertEqual(cfg.get("response_webhook_url"), f"{{{{ TASKS.{wait_id}.pending.resumeUrl }}}}")
            self.assertEqual(cfg.get("response_webhook_token"), f"{{{{ TASKS.{wait_id}.pending.resumeToken }}}}")
            # the wait drives the async node, and its delivered body drives the filter
            self.assertIn({"source": wait_id, "handle": "pending", "target": target},
                          [{"source": e["source"], "handle": e.get("sourceHandle"), "target": e["target"]} for e in edges])
            outs = [e["target"] for e in edges if e["source"] == wait_id and e.get("sourceHandle") == "out"]
            self.assertEqual(outs, [barrier or filt], wait_id)
            if barrier:
                self.assertEqual(nodes[barrier]["type"], "logic.join.all")
                self.assertIn(
                    {"source": target, "handle": "success", "target": barrier},
                    [{"source": e["source"], "handle": e.get("sourceHandle"), "target": e["target"]} for e in edges],
                )
                self.assertIn(
                    {"source": barrier, "handle": "out", "target": filt},
                    [{"source": e["source"], "handle": e.get("sourceHandle"), "target": e["target"]} for e in edges],
                )
            # nothing may reach the async node except its wait
            feeders = {e["source"] for e in edges if e["target"] == target}
            self.assertEqual(feeders, {wait_id}, target)
            expected_rules = [
                {"path": "request.body.status", "op": "equals", "value": "ok"}
            ]
            if target == "test":
                expected_rules.append({
                    "path": "request.body.job_id", "op": "equals",
                    "value": "{{ TASKS.test.data.job_id }}",
                })
            self.assertEqual(nodes[filt]["config"]["rules"], expected_rules, filt)

    def test_every_writer_receipt_gates_followup_tests(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        cases = {"record": ("if_receipt_ok", "join_writer_success"), "record2": ("if_receipt_ok2", "wait_test2"), "record3": ("if_receipt_ok3", "wait_test3"), "record_rn1": ("if_receipt_ok_rn1", "wait_test_rn1"), "record_rn2": ("if_receipt_ok_rn2", "wait_test_rn2")}
        for source, (gate, target) in cases.items():
            self.assertEqual(nodes[gate]["type"], "logic.filter")
            self.assertTrue(any(e["source"] == source and e["target"] == gate for e in edges), source)
            self.assertTrue(any(e["source"] == gate and e.get("sourceHandle") == "pass" and e["target"] == target for e in edges), gate)
            if gate != "if_receipt_ok":
                self.assertTrue(any(e["source"] == gate and e.get("sourceHandle") == "fail" and e["target"] == "join_receipt_fail" for e in edges), gate)
        self.assertEqual(nodes["join_receipt_fail"]["type"], "logic.join.any")
        self.assertTrue(any(e["source"] == "join_receipt_fail" and e["target"] == "receipt_fail" for e in edges))
        self.assertEqual(nodes["join_writer_success"]["type"], "logic.join.any")
        self.assertTrue(any(e["source"] == "join_writer_success" and e["target"] == "active_route" for e in edges))

    def test_native_retry_writers_use_recorded_receipts_only(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        expected = {
            "agent2": ("fallback_receipt", "receipt"),
            "agent3": ("receipt2",),
            "agent_rn1": ("receipt3", "receipt2", "fallback_receipt", "receipt"),
            "agent_rn2": ("receipt_rn1", "receipt3", "receipt2", "fallback_receipt", "receipt"),
        }
        for node_id, receipts in expected.items():
            config = nodes[node_id]["config"]
            for receipt in receipts:
                self.assertIn(f"CTX.{receipt}.session_identity", config["session_identity"])
                self.assertIn(f"CTX.{receipt}.resume_job_id", config["resume_job_id"])
                self.assertIn(f"CTX.{receipt}.session_identity.provider", config["provider"])
        for receipt in ("record", "record2", "record3", "record_rn1", "record_rn2"):
            outputs = {m["output"] for m in nodes[receipt]["config"]["mappings"]}
            self.assertIn("session_identity", outputs)
            self.assertIn("resume_job_id", outputs)

    def test_implement_slice_initial_availability_fallback_topology(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        triples = {(e["source"], e.get("sourceHandle"), e["target"]) for e in graph["spec"]["edges"]}
        self.assertIn(("if_initial_fallback_eligible", "pass", "fallback_route"), triples)
        self.assertIn(("if_receipt_ok", "fail", "if_initial_fallback_eligible"), triples)
        self.assertIn(("if_initial_fallback_eligible", "fail", "join_receipt_fail"), triples)
        edges = graph["spec"]["edges"]
        self.assertEqual([(e["source"], e.get("sourceHandle")) for e in edges if e["target"] == "fallback_route"], [("if_initial_fallback_eligible", "pass")])
        self.assertEqual({e["source"] for e in edges if e["target"] == "if_initial_fallback_eligible"}, {"if_receipt_ok"})
        def reaches(start, target):
            seen, pending = set(), [start]
            while pending:
                current = pending.pop()
                if current == target:
                    return True
                if current not in seen:
                    seen.add(current)
                    pending.extend(e["target"] for e in edges if e["source"] == current)
            return False
        for source in ("if_receipt_ok2", "if_receipt_ok3", "if_receipt_ok_rn1", "if_receipt_ok_rn2", "if_review1", "if_review1b", "if_review2", "if_review2b", "if_test_ok", "if_test_ok2", "if_test_rn1", "if_test_rn2"):
            self.assertFalse(reaches(source, "fallback_route"), source)
            self.assertFalse(reaches(source, "agent_fallback"), source)
        self.assertEqual(nodes["if_initial_fallback_eligible"]["config"], {
            "group": "AND",
            "rules": [
                {"path": "status", "op": "equals", "value": "error"},
                {"path": "role", "op": "equals", "value": "primary"},
                {"path": "failure_class", "op": "equals", "value": "provider_availability"},
                {"path": "failover_eligible", "op": "equals", "value": True},
            ],
        })
        self.assertIn(("fallback_route", "success", "normal_fallback_candidate"), triples)
        self.assertIn(("normal_fallback_candidate", "out", "join_fallback_start"), triples)
        self.assertIn(("join_fallback_start", "out", "fallback_route_choice"), triples)
        self.assertIn(("fallback_route_choice", "out", "wait_fallback"), triples)
        self.assertIn(("if_fallback_receipt_ok", "pass", "join_writer_success"), triples)
        self.assertNotIn("session_identity", nodes["agent_fallback"]["config"])
        for field in ("launcher", "provider", "model", "max_turns", "run_budget_seconds"):
            self.assertEqual(nodes["agent_fallback"]["config"][field], f"{{{{ CTX.fallback_route_choice.{field} }}}}")

    def test_implement_slice_fallback_templates_resolve_the_actual_later_route(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        self.assertEqual(
            nodes["normal_fallback_candidate"]["config"]["mappings"][0]["expression"]["path"],
            "TASKS.fallback_route.data",
        )
        self.assertEqual(nodes["selected_route"]["config"]["code"]["code"], "{{ CTX.recovery_selection.route | default(CTX.normal_primary_candidate.route) | tojson }}")
        self.assertEqual(nodes["active_route"]["config"]["code"]["code"], "{{ CTX.fallback_receipt.route | default(CTX.receipt.route) | tojson }}")
        choice = nodes["fallback_route_choice"]
        self.assertEqual(choice["type"], "transforms.codeExpression")
        self.assertEqual(
            choice["config"]["code"]["code"],
            "{{ CTX.normal_fallback_candidate.route | default(CTX.recovery_selection.route) | tojson }}",
        )
        for field in ("launcher", "provider", "model", "max_turns", "run_budget_seconds"):
            self.assertEqual(nodes["agent_fallback"]["config"][field], f"{{{{ CTX.fallback_route_choice.{field} }}}}")

    def test_implement_slice_recovery_is_only_a_later_initial_boundary(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        triples = {(e["source"], e.get("sourceHandle"), e["target"]) for e in edges}
        recovery_nodes = [
            n["id"] for n in graph["spec"]["nodes"]
            if n["type"].endswith("/v1/slice/route/recovery")
        ]
        self.assertEqual(recovery_nodes, ["recovery_route"])
        self.assertEqual(nodes["recovery_marker"]["type"], "transforms.objectBuilder")
        marker = {
            m["output"]: m["expression"]
            for m in nodes["recovery_marker"]["config"]["mappings"]
        }
        self.assertEqual(
            marker,
            {"recovery_version": {"kind": "getField", "path": "CTX.INPUT.recovery_version"}},
        )
        self.assertEqual(
            nodes["switch_recovery"]["config"]["cases"][0]["rules"],
            [{
                "path": "CTX.recovery_marker.recovery_version",
                "op": "equals",
                "value": "provider-recovery-v1",
            }],
        )
        self.assertIn(("ticket_head", "success", "recovery_marker"), triples)
        self.assertIn(("switch_recovery", "case-0", "recovery_route"), triples)
        self.assertIn(("switch_recovery", "default", "join_primary_start"), triples)
        self.assertIn(("recovery_route", "success", "recovery_selection"), triples)
        self.assertIn(("recovery_selection", "out", "switch_recovery_route"), triples)
        self.assertEqual(nodes["switch_recovery_route"]["config"]["cases"][0]["rules"], [{
            "path": "CTX.recovery_selection.role", "op": "equals", "value": "availability_fallback",
        }])
        recovery_outputs = {m["output"]: m["expression"]["path"] for m in nodes["recovery_selection"]["config"]["mappings"]}
        self.assertEqual(recovery_outputs, {
            "route": "TASKS.recovery_route.data.selected_route",
            "evidence": "TASKS.recovery_route.data.evidence",
            "role": "TASKS.recovery_route.data.role",
        })
        self.assertIn(("switch_recovery_route", "case-0", "join_fallback_start"), triples)
        self.assertIn(("switch_recovery_route", "default", "join_primary_start"), triples)
        self.assertIn(("join_primary_start", "out", "selected_route"), triples)
        self.assertIn(("selected_route", "out", "wait"), triples)
        self.assertIn(("join_fallback_start", "out", "fallback_route_choice"), triples)
        self.assertIn(("fallback_route_choice", "out", "wait_fallback"), triples)
        self.assertIn(("recovery_route", "failure", "recovery_route_fail"), triples)
        self.assertEqual(
            [
                (e.get("sourceHandle"), e["target"])
                for e in edges
                if e["source"] == "recovery_route_fail"
            ],
            [("out", "join_durable_outcome")],
        )
        terminal_mappings = nodes["recovery_route_fail"]["config"]["mappings"]
        self.assertEqual([m["id"] for m in terminal_mappings], [f"m{i}" for i in range(1, 6)])
        self.assertEqual(
            {m["output"] for m in terminal_mappings},
            {"diagnostic_version", "status", "workflow", "stage", "summary"},
        )
        dumped_terminal = json.dumps(terminal_mappings).lower()
        for unsafe in ("getfield", "tasks.", "data.error", "callback", "token", "stdout", "stderr", "trace"):
            self.assertNotIn(unsafe, dumped_terminal)

        recovery_config = nodes["recovery_route"]["config"]
        for field in (
            "primary_route", "primary_receipt", "fallback_route", "fallback_receipt",
            "fresh_primary_receipt",
        ):
            self.assertIn(field, recovery_config)
            if field != "primary_route":
                self.assertIn("CTX.INPUT.", recovery_config[field])

        route_decisions = {
            node_id for node_id, node in nodes.items()
            if "/v1/slice/route" in node["type"]
        }
        downstream_starts = {
            "fallback_route", "wait_fallback", "agent_fallback", "record_fallback",
            "if_fallback_receipt_ok", "wait2", "agent2", "record2", "if_receipt_ok2",
            "wait3", "agent3", "if_receipt_ok3", "wait_rn1", "agent_rn1",
            "if_receipt_ok_rn1", "wait_rn2", "agent_rn2", "if_receipt_ok_rn2",
            "wait_rev1", "review1", "if_review1", "wait_r1b", "review1b",
            "if_review1b", "wait_rev2", "review2", "if_review2", "wait_r2b",
            "review2b", "if_review2b",
        }
        for start in downstream_starts:
            seen, pending = set(), [start]
            while pending:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                pending.extend(e["target"] for e in edges if e["source"] == current)
            self.assertNotIn("recovery_route", seen - {start}, start)
            self.assertTrue((seen - {start}).isdisjoint(route_decisions), (start, seen & route_decisions))

        writer_nodes = {
            node_id for node_id, node in nodes.items()
            if node["type"].endswith("/v1/agent/run")
        }
        self.assertEqual(
            writer_nodes,
            {"agent", "agent_fallback", "agent2", "agent3", "agent_rn1", "agent_rn2"},
        )
        self.assertEqual(
            [n["id"] for n in graph["spec"]["nodes"] if n["type"].endswith("/v1/slice/route/fallback")],
            ["fallback_route"],
        )

    def test_implement_slice_active_resumes_are_pinned_to_successful_receipt(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        expected_receipts = {
            "agent2": ("fallback_receipt", "receipt"),
            "agent_rn1": ("receipt3", "receipt2", "fallback_receipt", "receipt"),
            "agent_rn2": ("receipt_rn1", "receipt3", "receipt2", "fallback_receipt", "receipt"),
        }
        for node_id, receipts in expected_receipts.items():
            config = nodes[node_id]["config"]
            for field in ("launcher", "provider", "model"):
                for receipt in receipts:
                    self.assertIn(f"CTX.{receipt}.session_identity.{field}", config[field])
                self.assertNotIn("TASKS.", config[field])
            for receipt in receipts:
                self.assertIn(f"CTX.{receipt}.session_identity", config["session_identity"])
        for field in ("launcher", "provider", "model"):
            self.assertEqual(
                nodes["agent3"]["config"][field],
                f"{{{{ CTX.receipt2.session_identity.{field} }}}}",
            )
        self.assertEqual(nodes["agent3"]["config"]["session_identity"], "{{ CTX.receipt2.session_identity }}")

        for node_id, slot in (
            ("review1", "reviewer1"), ("review1b", "reviewer1"),
            ("review2", "reviewer2"), ("review2b", "reviewer2"),
        ):
            for field in ("launcher", "provider", "model"):
                value = nodes[node_id]["config"][field]
                self.assertEqual(value, f"{{{{ CTX.active_route.{slot}_{field} }}}}")
                self.assertNotIn("TASKS.", value)

        for node_id, ordinary in (("agent", "route"), ("agent_fallback", "fallback_route")):
            for field in ("launcher", "provider", "model"):
                value = nodes[node_id]["config"][field]
                expected = "CTX.selected_route" if node_id == "agent" else "CTX.fallback_route_choice"
                self.assertEqual(value, f"{{{{ {expected}.{field} }}}}")
            self.assertNotIn("session_identity", nodes[node_id]["config"])
            self.assertNotIn("resume_job_id", nodes[node_id]["config"])

        for walk_id, alias in (("walk", "walk_recovery_input"), ("walk_e2e", "walk_e2e_recovery_input")):
            config = nodes[walk_id]["config"]
            for field in ("recovery_version", "prior_primary_route", "prior_primary_receipt", "prior_fallback_route", "prior_fallback_receipt", "fresh_primary_receipt"):
                self.assertEqual(config[field], f"{{{{ CTX.{alias}.{field} }}}}")
            code = nodes[alias]["config"]["code"]["code"]
            self.assertIn("{% if fallback.status | default('') == 'ok' %}", code)
            self.assertIn("'recovery_version': 'provider-recovery-v1'", code)
            self.assertIn("{% else %}{{ {'recovery_version': ''", code)
            self.assertIn("'fresh_primary_receipt': ''", code)

    def test_implement_slice_corrections_and_reviews_prefer_fallback_route(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        correction_receipts = {
            "agent2": ("fallback_receipt", "receipt"),
            "agent_rn1": ("receipt3", "receipt2", "fallback_receipt", "receipt"),
            "agent_rn2": ("receipt_rn1", "receipt3", "receipt2", "fallback_receipt", "receipt"),
        }
        for node_id, receipts in correction_receipts.items():
            cfg = nodes[node_id]["config"]
            for field in ("launcher", "provider", "model"):
                for receipt in receipts:
                    self.assertIn(f"CTX.{receipt}.session_identity.{field}", cfg[field])
        for field in ("launcher", "provider", "model"):
            self.assertEqual(nodes["agent3"]["config"][field], f"{{{{ CTX.receipt2.session_identity.{field} }}}}")
        for node_id in ("agent2", "agent3", "agent_rn1", "agent_rn2"):
            cfg = nodes[node_id]["config"]
            for field in ("max_turns", "run_budget_seconds"):
                expected = f"{{{{ CTX.receipt2.route.{field} }}}}" if node_id == "agent3" else f"{{{{ CTX.active_route.{field} }}}}"
                self.assertEqual(cfg[field], expected)
        for node_id, slot in (("review1", "reviewer1"), ("review1b", "reviewer1"), ("review2", "reviewer2"), ("review2b", "reviewer2")):
            cfg = nodes[node_id]["config"]
            for field in ("launcher", "provider", "model"):
                self.assertEqual(cfg[field], f"{{{{ CTX.active_route.{slot}_{field} }}}}")
        for node_id, receipts in correction_receipts.items():
            cfg = nodes[node_id]["config"]
            for receipt in receipts:
                self.assertIn(f"CTX.{receipt}.session_identity", cfg["session_identity"])
                self.assertIn(f"CTX.{receipt}.resume_job_id", cfg["resume_job_id"])
        self.assertEqual(nodes["agent3"]["config"]["session_identity"], "{{ CTX.receipt2.session_identity }}")
        self.assertEqual(nodes["agent3"]["config"]["resume_job_id"], "{{ CTX.receipt2.resume_job_id }}")
        self.assertEqual(nodes["fallback_route"]["config"]["primary_receipt"], "{{ CTX.receipt }}")

    def test_implement_slice_correction_failures_end_in_canned_leaf_receipts(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        writers = {"agent2", "agent3", "agent_rn1", "agent_rn2"}
        waits = {"wait2", "wait3", "wait_rn1", "wait_rn2"}
        cases = {
            **{(writer, "failure"): ("correction_action_fail", "error", "correction_writer_action")
               for writer in writers},
            **{(wait, "timeout"): ("wait2_timeout", "timeout", "correction_writer_wait")
               for wait in waits},
            **{(wait, "failure"): ("wait2_fail", "error", "correction_writer_wait")
               for wait in waits},
        }
        writer_actions = {
            node_id for node_id, node in nodes.items()
            if node["type"].endswith("/v1/agent/run")
        }

        for (source, handle), (terminal, status, stage) in cases.items():
            starts = {
                edge["target"] for edge in edges
                if edge["source"] == source and edge.get("sourceHandle") == handle
            }
            self.assertEqual(len(starts), 1, (source, handle))
            seen, leaves, pending = set(), set(), list(starts)
            while pending:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                targets = [edge["target"] for edge in edges if edge["source"] == current]
                if targets:
                    pending.extend(targets)
                else:
                    leaves.add(current)
            self.assertIn(terminal, seen, (source, handle, seen))
            self.assertEqual(
                leaves,
                {
                    "durable_outcome_written",
                    "durable_outcome_write_failed",
                    "durable_outcome_readback_failed",
                    "durable_outcome_readback_mismatch",
                },
                (source, handle, leaves),
            )
            self.assertTrue(writer_actions.isdisjoint(seen), (source, handle, writer_actions & seen))
            self.assertNotIn("fallback_route", seen, (source, handle))

            node = nodes[terminal]
            self.assertEqual(node["type"], "transforms.objectBuilder")
            mappings = node["config"]["mappings"]
            self.assertEqual([m["id"] for m in mappings], [f"m{i}" for i in range(1, 6)])
            outputs = {m["output"]: m["expression"] for m in mappings}
            self.assertEqual(
                outputs,
                {
                    "diagnostic_version": {"kind": "literal", "value": "diagnostic-v1"},
                    "status": {"kind": "literal", "value": status},
                    "workflow": {"kind": "literal", "value": "implement-slice"},
                    "stage": {"kind": "literal", "value": stage},
                    "summary": {
                        "kind": "literal",
                        "value": {
                            "correction_action_fail": "correction writer action failed",
                            "wait2_timeout": "correction writer receipt timed out",
                            "wait2_fail": "correction writer wait failed",
                        }[terminal],
                    },
                },
            )
            dumped = json.dumps(mappings).lower()
            for unsafe in ("getfield", "tasks.", "request.body", "callback", "token", "stdout",
                           "stderr", "trace", "log_ref", "provider_output"):
                self.assertNotIn(unsafe, dumped, (source, handle, unsafe))

    def test_implement_slice_fallback_failure_stops_with_sanitized_evidence(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        terminals = {
            ("fallback_route", "failure"): ("fallback_route_fail", "fallback_route"),
            ("wait_fallback", "timeout"): ("fallback_wait_timeout", "fallback_wait_timeout"),
            ("wait_fallback", "failure"): ("fallback_wait_fail", "fallback_wait_failure"),
            ("agent_fallback", "failure"): ("fallback_action_fail", "fallback_action"),
            ("if_fallback_receipt_ok", "fail"): ("fallback_receipt_fail", "fallback_receipt"),
        }
        self.assertNotIn("join_fallback_fail", nodes)
        self.assertNotIn("fallback_fail", nodes)
        for (source, handle), (terminal, stage) in terminals.items():
            self.assertEqual(
                {e["target"] for e in edges if e["source"] == source and e.get("sourceHandle") == handle},
                {terminal},
                (source, handle),
            )
            node = nodes[terminal]
            self.assertEqual(node["type"], "transforms.objectBuilder")
            mappings = node["config"]["mappings"]
            self.assertEqual(
                [m["id"] for m in mappings],
                [f"m{i}" for i in range(1, len(mappings) + 1)],
            )
            outputs = {m["output"]: m["expression"] for m in mappings}
            self.assertEqual(outputs["stage"], {"kind": "literal", "value": stage})
            self.assertEqual(
                outputs["primary_diagnostic"],
                {"kind": "getField", "path": "CTX.receipt.diagnostic"},
            )
            dumped = json.dumps(outputs).lower()
            for unsafe in ("raw_wait", "request.body", "callback", "token", "stdout", "stderr",
                           "trace", "log_ref", "provider_output"):
                self.assertNotIn(unsafe, dumped)

        action_outputs = {
            m["output"]: m["expression"]
            for m in nodes["fallback_action_fail"]["config"]["mappings"]
        }
        self.assertEqual(
            action_outputs["fallback_diagnostic"],
            {"kind": "getField", "path": "TASKS.agent_fallback.data.diagnostic"},
        )
        receipt_outputs = {
            m["output"]: m["expression"]
            for m in nodes["fallback_receipt_fail"]["config"]["mappings"]
        }
        self.assertEqual(
            receipt_outputs["fallback_diagnostic"],
            {"kind": "getField", "path": "CTX.fallback_receipt.diagnostic"},
        )
        writer_ids = {n_id for n_id, node in nodes.items() if node["type"].endswith("/v1/agent/run")}
        for start, _stage in terminals.values():
            seen, stack = set(), [start]
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                stack.extend(e["target"] for e in edges if e["source"] == current)
            self.assertTrue(writer_ids.isdisjoint(seen), (start, writer_ids & seen))
        done = {m["output"]: m["expression"] for m in nodes["done"]["config"]["mappings"]}
        for field, path in (("primary_receipt", "CTX.receipt"), ("fallback_receipt", "CTX.fallback_receipt"), ("primary_route", "CTX.receipt.route"), ("fallback_route", "CTX.fallback_receipt.route")):
            self.assertEqual(done[field], {"kind": "getField", "path": path})
        self.assertEqual(done["primary_diagnostic"], {"kind": "getField", "path": "CTX.receipt.diagnostic"})
        self.assertEqual(done["fallback_diagnostic"], {"kind": "getField", "path": "CTX.fallback_receipt.diagnostic"})

    def test_implement_slice_carries_diagnostics_without_new_routing(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        triples = {(e["source"], e.get("sourceHandle"), e["target"]) for e in edges}
        aliases = [n.get("config", {}).get("alias") for n in nodes.values()]
        aliases = [alias for alias in aliases if alias]
        self.assertEqual(len(aliases), len(set(aliases)))

        for receipt, wait_id in (("record", "wait"), ("record_fallback", "wait_fallback"), ("record2", "wait2")):
            mappings = {m["output"]: m["expression"] for m in nodes[receipt]["config"]["mappings"]}
            self.assertEqual(
                mappings["diagnostic"],
                {"kind": "getField", "path": f"TASKS.{wait_id}.request.body.diagnostic"},
            )
            self.assertEqual(
                mappings["summary"],
                {"kind": "getField", "path": f"TASKS.{wait_id}.request.body.summary"},
            )

        self.assertEqual(nodes["primary_action_fail"]["type"], "transforms.objectBuilder")
        self.assertIn(("agent", "failure", "primary_action_fail"), triples)
        terminals = (
            "primary_action_fail", "timeout", "fail", "receipt_fail", "fallback_route_fail",
            "fallback_wait_timeout", "fallback_wait_fail", "fallback_action_fail",
            "fallback_receipt_fail",
        )
        for terminal in terminals:
            self.assertEqual(nodes[terminal]["type"], "transforms.objectBuilder", terminal)
            mappings = nodes[terminal]["config"]["mappings"]
            self.assertEqual(
                [m["id"] for m in mappings],
                [f"m{i}" for i in range(1, len(mappings) + 1)],
            )
            produced = {m["output"] for m in mappings}
            self.assertIn("diagnostic_version", produced)
            self.assertIn("summary", produced)
            dumped = json.dumps(mappings).lower()
            for unsafe in ("request.body.summary", "raw_wait", "callback", "resumeurl", "resumetoken",
                           "stdout", "stderr", "trace", "log_ref"):
                self.assertNotIn(unsafe, dumped, terminal)

        # Diagnostics converge on one durable write; they cannot route a second fallback or writer.
        for terminal in terminals:
            self.assertEqual(
                [
                    (e.get("sourceHandle"), e["target"])
                    for e in edges
                    if e["source"] == terminal
                ],
                [("out", "join_durable_outcome")],
                terminal,
            )
        self.assertEqual(
            [n["id"] for n in graph["spec"]["nodes"] if n["type"].endswith("/v1/slice/route/fallback")],
            ["fallback_route"],
        )
        dumped_graph = json.dumps(graph)
        for wrong_transform_path in ("TASKS.record", "TASKS.record_fallback", "TASKS.record2"):
            self.assertNotIn(wrong_transform_path, dumped_graph)
        for alias in ("CTX.receipt.", "CTX.fallback_receipt.", "CTX.receipt2."):
            self.assertIn(alias, dumped_graph)
        using = (Path(server.__file__).parent / "docs" / "USING.md").read_text()
        self.assertNotIn("A node result is exposed as `TASKS.<node>.data.<field>`", using)
        self.assertIn("`action.graphwing` results", using)
        self.assertIn("`CTX.<alias>`", using)

    def test_verify_stack_uses_action_data_contract_and_sanitized_failure_receipts(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "verify-stack.json").read_text())
        spec = json.loads((Path(server.__file__).parent / "openapi.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        triples = {(e["source"], e.get("sourceHandle"), e["target"]) for e in edges}
        aliases = [n.get("config", {}).get("alias") for n in nodes.values()]
        aliases = [alias for alias in aliases if alias]
        self.assertEqual(len(aliases), len(set(aliases)))
        self.assertEqual(nodes["ports"]["config"]["port"], "{{ CTX.INPUT.port }}")
        self.assertNotIn("ports", nodes["ports"]["config"])
        self.assertEqual(
            {
                ("trigger", "out", "stack"),
                ("stack", "success", "ports"),
                ("ports", "success", "record"),
            },
            {triple for triple in triples if triple[2] in {"stack", "ports", "record"}},
        )

        action_contracts = {}
        for node_id in ("stack", "ports"):
            action = nodes[node_id]
            method_path = action["type"].removeprefix("action.graphwing.")
            method, path = method_path.split(":", 1)
            response = spec["paths"][path][method.lower()]["responses"]["200"]
            schema_ref = response["content"]["application/json"]["schema"]["$ref"]
            schema_name = schema_ref.rsplit("/", 1)[-1]
            action_contracts[node_id] = set(spec["components"]["schemas"][schema_name]["properties"])

        mappings = nodes["record"]["config"]["mappings"]
        self.assertEqual({mapping["output"] for mapping in mappings},
                         {"stack", "healthy", "ports", "stack_diagnostic", "port_diagnostic"})
        mapping_paths = {mapping["expression"]["path"] for mapping in mappings}
        self.assertNotIn("TASKS.stack.stack", mapping_paths)
        self.assertNotIn("TASKS.ports.ports", mapping_paths)
        for mapping in mappings:
            expression = mapping["expression"]
            self.assertEqual(expression["kind"], "getField")
            root, source, envelope, field = expression["path"].split(".")
            self.assertEqual((root, envelope), ("TASKS", "data"))
            self.assertIn(source, action_contracts)
            self.assertIn(field, action_contracts[source])

        self.assertIn(("stack", "failure", "stack_diagnostic"), triples)
        self.assertIn(("ports", "failure", "ports_diagnostic"), triples)
        for node_id, stage in (
            ("stack_diagnostic", "stack_status"),
            ("ports_diagnostic", "port_check"),
        ):
            node = nodes[node_id]
            self.assertEqual(node["type"], "transforms.objectBuilder")
            self.assertFalse(any(e["source"] == node_id for e in edges))
            self.assertEqual(
                [m["id"] for m in node["config"]["mappings"]],
                [f"m{i}" for i in range(1, len(node["config"]["mappings"]) + 1)],
            )
            mappings = {m["output"]: m["expression"] for m in node["config"]["mappings"]}
            self.assertEqual(mappings["diagnostic_version"], {"kind": "literal", "value": "diagnostic-v1"})
            self.assertEqual(mappings["stage"], {"kind": "literal", "value": stage})
            self.assertEqual(
                mappings["summary"],
                {"kind": "literal", "value": f"{stage} action failed"},
            )
            dumped = json.dumps(mappings).lower()
            self.assertNotIn(".data.diagnostic", dumped)
            for unsafe in ("data.error", "stdout", "stderr", "trace", "token", "url", "cwd", "secret_path"):
                self.assertNotIn(unsafe, dumped)

    def test_implement_slice_routes_direct_native_reviewers(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        for suffix in ("1", "1b", "2", "2b"):
            node = nodes[f"review{suffix}"]
            slot = "reviewer1" if suffix.startswith("1") else "reviewer2"
            config = node["config"]
            self.assertNotIn("reviewer", config)
            self.assertIn(f"CTX.active_route.{slot}_launcher", config["launcher"])
            self.assertIn(f"CTX.active_route.{slot}_launcher", config["launcher"])
            self.assertIn(f"CTX.active_route.{slot}_provider", config["provider"])
            self.assertIn(f"CTX.active_route.{slot}_provider", config["provider"])
            self.assertIn(f"CTX.active_route.{slot}_model", config["model"])
            self.assertIn(f"CTX.active_route.{slot}_model", config["model"])
        for index in (1, 2):
            field = f"reviewer{index}_launcher"
            mapping = nodes[f"map_switch_rev{'' if index == 1 else index}"]["config"]["mappings"]
            self.assertEqual(mapping, [{
                "id": "m1", "output": field,
                "expression": {"kind": "getField", "path": f"CTX.active_route.{field}"},
            }])
            switch = nodes[f"switch_rev{'' if index == 1 else index}"]["config"]
            self.assertEqual(switch["cases"][0]["rules"], [
                {"path": field, "op": "equals", "value": "none"}
            ])
        edges = {edge["id"]: edge for edge in graph["spec"]["edges"]}
        for skip, need, wait in (
            ("e_rev_skip", "e_rev_run", "wait_rev1"),
            ("e_rev2_skip", "e_rev2_need", "wait_rev2"),
        ):
            self.assertEqual(
                (edges[skip]["sourceHandle"], edges[skip]["target"]),
                ("case-0", "join_commit"),
            )
            self.assertEqual(
                (edges[need]["sourceHandle"], edges[need]["target"]),
                ("default", wait),
            )

    def test_graph_receipts_keep_route_provenance(self):
        root = Path(server.__file__).parent / "graphs"
        implement = json.loads((root / "implement-slice.json").read_text())
        implement_nodes = {n["id"]: n for n in implement["spec"]["nodes"]}
        mappings = {m["output"]: m["expression"] for m in implement_nodes["record"]["config"]["mappings"]}
        self.assertEqual(mappings["route"], {"kind": "getField", "path": "CTX.selected_route"})
        retry_mappings = {
            m["output"]: m["expression"] for m in implement_nodes["record2"]["config"]["mappings"]
        }
        self.assertEqual(retry_mappings["route"], {"kind": "getField", "path": "CTX.active_route"})
        self.assertEqual(
            retry_mappings["fallback_route"],
            {"kind": "getField", "path": "CTX.fallback_route_choice"},
        )
        fallback_mappings = {
            m["output"]: m["expression"]
            for m in implement_nodes["record_fallback"]["config"]["mappings"]
        }
        self.assertEqual(
            fallback_mappings["route"],
            {"kind": "getField", "path": "CTX.fallback_route_choice"},
        )
        done_mappings = {
            m["output"]: m["expression"]
            for m in implement_nodes["done"]["config"]["mappings"]
        }
        self.assertEqual(
            done_mappings["fallback_route"],
            {"kind": "getField", "path": "CTX.fallback_receipt.route"},
        )

        drive = json.loads((root / "pr-drive.json").read_text())
        drive_nodes = {n["id"]: n for n in drive["spec"]["nodes"]}
        self.assertEqual(
            drive_nodes["route"]["config"]["work_kind"],
            "{{ CTX.INPUT.work_kind | default('go_coding') }}",
        )
        self.assertIn("work_kind", drive_nodes["form"]["config"]["inputs"])
        mappings = drive_nodes["receipt"]["config"]["mappings"]
        route = next(m for m in mappings if m["output"] == "route")
        self.assertEqual(route["expression"], {"kind": "getField", "path": "TASKS.route.data"})

    def _queue_closed_review(self, root, *, effort=None, extra=None, active_count=0):
        repo = self._scratch_git(root)
        jobs = root / "jobs"
        launcher = root / "claude-review-wrapper"
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        request = {
            "repo": "scratch", "launcher": "claude", "provider": "anthropic",
            "model": "claude-sonnet-5", "prompt": "bounded ticket", "async": True,
        }
        if effort is not None:
            request["effort"] = effort
        if extra:
            request.update(extra)
        patches = (
            mock.patch.object(server, "JOBS_DIR", jobs),
            mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}),
            mock.patch.object(server, "active_job_count", return_value=active_count),
            mock.patch.object(server, "resolve_launcher_binary_now", return_value=launcher),
            mock.patch.object(server, "enqueue_review"),
        )
        entered = [patcher.start() for patcher in patches]
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(patches)])
        status, payload, _ = server.dispatch(
            "POST", "/v1/review/run", {}, True, json.dumps(request).encode()
        )
        if status == 202 and isinstance(payload, dict):
            self.addCleanup(server.release_review_authority, payload["job_id"])
        return status, payload, jobs, repo, launcher, entered[-1]

    def test_review_run_rejects_hostile_extras_before_review_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(
                server, "resolve_launcher_binary_now",
                side_effect=AssertionError("launcher boundary must not be reached"),
            ), mock.patch.object(
                server, "native_review_result",
                side_effect=AssertionError("review boundary must not be reached"),
            ):
                status, payload, _ = server.dispatch(
                    "POST", "/v1/review/run", {}, True,
                    json.dumps({
                        "repo": "scratch", "launcher": "claude", "provider": "anthropic",
                        "model": "claude-sonnet-5", "prompt": "ticket",
                        "hostile_extra": "TOKEN_SECRET /private/path",
                    }).encode(),
                )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "unexpected_fields")
        self.assertNotIn("TOKEN_SECRET", json.dumps(payload))

    def test_sync_review_receipt_identity_matches_command_and_pin_closes_fds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            launcher = root / "claude-review-wrapper"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            launcher.chmod(0o755)
            body = json.dumps({
                "repo": "scratch", "launcher": "claude", "provider": "anthropic",
                "model": "claude-sonnet-5", "prompt": "bounded ticket", "effort": "medium",
            }).encode()
            captured = []

            def pass_review(*args, **kwargs):
                captured.append((args, kwargs))
                return {"ok": True, "verdict": "PASS", "no_verdict": False}

            before = len(list(Path("/proc/self/fd").iterdir()))
            receipts = []
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=launcher), \
                 mock.patch.object(server, "native_review_result", side_effect=pass_review):
                for _ in range(30):
                    status, receipt, _ = server.dispatch(
                        "POST", "/v1/review/run", {}, True, body
                    )
                    self.assertEqual(status, 200, receipt)
                    receipts.append(receipt)
            after = len(list(Path("/proc/self/fd").iterdir()))
        self.assertEqual(after, before)
        self.assertEqual(len(captured), 30)
        for receipt, (args, kwargs) in zip(receipts, captured):
            identity = receipt["execution_identity"]
            self.assertEqual(kwargs, {})
            self.assertEqual(len(args), 1)
            context = args[0]
            self.assertIsInstance(context, server.NativeReviewExecutionContext)
            self.assertEqual(context.execution_identity, identity)
            self.assertEqual(context.authority_identity, identity)
            self.assertEqual(context.requested_effort, identity["requested_effort"])
            self.assertEqual(context.effective_effort, identity["effective_effort"])
            self.assertEqual(context.effort_source, identity["effort_source"])
            self.assertEqual(context.repo, identity["repo"])
            self.assertEqual(context.resolved, repo.resolve())
            self.assertEqual(context.run_budget_seconds, identity["run_budget_seconds"])
            self.assertEqual(context.max_turns, identity["max_turns"])
            self.assertEqual(context.permission_profile, identity["permission_profile"])
            self.assertEqual(context.pinned_launcher.path.parent, Path("/proc/self/fd"))
            self.assertEqual(context.pinned_launcher.fingerprint, identity["launcher_version"])
            adapter_job = {
                "launcher": context.launcher, "provider": context.provider,
                "model": context.model,
                "requested_effort": identity["requested_effort"],
                "effective_effort": identity["effective_effort"],
                "effort_source": identity["effort_source"],
                "max_turns": identity["max_turns"],
            }
            command = server.claude_command(
                adapter_job, "fixture", str(repo), context.pinned_launcher.path,
                permission_mode="plan", output_format="text",
            )
            self.assertEqual(command[command.index("--effort") + 1], "medium")
            self.assertEqual(command[command.index("--permission-mode") + 1], "plan")

    def test_review_async_poll_wait_lifecycle_is_closed_and_sanitized(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, _launcher, _enqueued = self._queue_closed_review(
                root, effort="medium"
            )
            self.assertEqual(status, 202, accepted)
            self.assertEqual(accepted["poll"], f"/v1/review/jobs/{accepted['job_id']}")
            identity = accepted["execution_identity"]
            self.assertEqual(set(identity), {
                "version", "kind", "launcher", "provider", "model",
                "requested_effort", "effective_effort", "effort_source",
                "launcher_version", "repo", "branch", "starting_head",
                "prompt_sha256", "diff_sha256", "run_budget_seconds", "max_turns",
                "permission_profile",
            })
            self.assertEqual(identity["version"], "review-execution-v1")
            self.assertEqual(identity["kind"], "review")
            self.assertRegex(identity["launcher_version"], r"^sha256:[0-9a-f]{64}$")

            poll_status, queued, _ = server.dispatch(
                "GET", accepted["poll"], {}, True, b""
            )
            self.assertEqual((poll_status, queued["status"]), (200, "queued"))
            self.assertEqual(queued["execution_identity"], identity)
            self.assertIsNone(queued["receipt"])

            authority = server.review_authority(accepted["job_id"])
            self.assertIsNotNone(authority)
            snapshot = authority.snapshot()
            self.assertIsNotNone(snapshot)
            stored = server.review_mark_running(accepted["job_id"], snapshot, authority)
            self.assertIsNotNone(stored)
            poll_status, running, _ = server.dispatch(
                "GET", accepted["poll"], {}, True, b""
            )
            self.assertEqual((poll_status, running["status"]), (200, "running"))
            with mock.patch.object(server.time, "monotonic", side_effect=[0.0, 2.0]), \
                 mock.patch.object(server.time, "sleep"):
                wait_status, timeout, _ = server.dispatch(
                    "POST", "/v1/review/jobs/wait", {}, True,
                    json.dumps({"job_id": accepted["job_id"], "timeout_seconds": 1}).encode(),
                )
            self.assertEqual(wait_status, 408, timeout)
            self.assertEqual(timeout["code"], "job_wait_timeout")

            raw_result = {
                "ok": True, "verdict": "PASS", "no_verdict": False,
                "summary": "TOKEN_SECRET /private/provider/log", "compact": "raw provider text",
                "returncode": 0,
            }
            receipt = server.review_receipt(
                stored, raw_result, identity=identity
            )
            terminal_record, installed = server.review_terminal_transition(
                accepted["job_id"], snapshot, authority, receipt
            )
            self.assertTrue(installed)
            self.assertIsNotNone(terminal_record)
            wait_status, terminal, _ = server.dispatch(
                "POST", "/v1/review/jobs/wait", {}, True,
                json.dumps({"job_id": accepted["job_id"], "timeout_seconds": 1}).encode(),
            )
            self.assertEqual((wait_status, terminal["status"]), (200, "completed"))
            self.assertEqual(terminal["execution_identity"], identity)
            self.assertEqual(terminal["receipt"]["execution_identity"], identity)
            public = json.dumps(terminal, sort_keys=True)
            self.assertNotIn("TOKEN_SECRET", public)
            self.assertNotIn("/private", public)
            self.assertNotIn("raw provider text", public)
            self.assertEqual(terminal["receipt"]["summary"], "review passed")

    def test_review_terminal_encoder_recursion_preserves_running_cas_and_suppresses_callback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(
                root, extra={"response_webhook_url": "https://example.invalid/review"}
            )
            self.assertEqual(status, 202, accepted)
            job_id = accepted["job_id"]
            path = jobs / job_id / "job.json"
            encoder_patch = mock.patch.object(
                server.json, "dumps", side_effect=RecursionError("provider recursion")
            )
            observed = {}

            def inject_after_launch(*_args, **_kwargs):
                observed["running_bytes"] = path.read_bytes()
                authority = server.review_authority(job_id)
                self.assertIsNotNone(authority)
                assert authority is not None
                observed["sealed_bytes"] = authority.running_record_bytes
                observed["sealed_hash"] = authority.running_record_hash
                encoder_patch.start()
                return {
                    "ok": True, "verdict": "PASS", "no_verdict": False,
                    "summary": "TOKEN_SECRET /private/provider/output",
                }

            try:
                with mock.patch.object(
                    server, "native_review_result", side_effect=inject_after_launch
                ), mock.patch.object(server, "deliver_webhook") as callback, \
                     mock.patch.object(server, "herdr_job_done"):
                    server.run_review_job(job_id)
            finally:
                encoder_patch.stop()
            callback.assert_not_called()
            saved = json.loads(path.read_text())
            self.assertEqual(saved["status"], "running")
            self.assertIsNone(saved["receipt"])
            self.assertNotIn("TOKEN_SECRET", path.read_text())
            self.assertEqual(path.read_bytes(), observed["running_bytes"])
            self.assertEqual(
                server._review_snapshot_bytes(saved), observed["sealed_bytes"],
            )
            self.assertEqual(
                hashlib.sha256(observed["sealed_bytes"]).hexdigest(),
                observed["sealed_hash"],
            )
            self.assertIsNone(server.review_authority(job_id))
            response_status, payload = server.review_job_response(job_id)
            self.assertEqual(
                (response_status, payload["code"]),
                (409, "review_authority_unavailable"),
            )

    def test_sanitized_review_receipt_encoder_recursion_returns_no_projection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(root)
            self.assertEqual(status, 202, accepted)
            job_id = accepted["job_id"]
            with mock.patch.object(server, "native_review_result", return_value={
                "ok": True, "verdict": "PASS", "no_verdict": False,
            }), mock.patch.object(server, "deliver_webhook", return_value=None), \
                 mock.patch.object(server, "herdr_job_done"):
                server.run_review_job(job_id)
            stored = json.loads((jobs / job_id / "job.json").read_text())
            with mock.patch.object(
                server.json, "dumps", side_effect=RecursionError("provider recursion")
            ):
                projected = server.sanitized_review_receipt(stored)
            self.assertIsNone(projected)

    def test_review_job_response_encoder_recursion_is_stable_sanitized_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, _jobs, _repo, _launcher, _ = self._queue_closed_review(root)
            self.assertEqual(status, 202, accepted)
            with mock.patch.object(
                server, "_review_snapshot_bytes",
                side_effect=RecursionError("provider recursion"),
            ):
                response_status, payload = server.review_job_response(accepted["job_id"])
            self.assertEqual(response_status, 409)
            self.assertEqual(payload, {
                "error": "review record is not canonical",
                "code": "review_record_invalid",
            })
            self.assertNotIn("provider recursion", str(payload))

    def test_review_async_returns_accepted_snapshot_when_worker_finishes_during_enqueue(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            launcher = root / "claude-review-wrapper"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            launcher.chmod(0o755)
            request = {
                "repo": "scratch", "launcher": "claude", "provider": "anthropic",
                "model": "claude-sonnet-5", "prompt": "bounded ticket", "async": True,
            }
            observed = {}

            def finish_before_enqueue_returns(job):
                observed["accepted"] = json.loads(
                    server._review_snapshot_bytes(server.public_review_job(job))
                )
                server.run_review_job(job["job_id"])

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "active_job_count", return_value=0), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=launcher), \
                 mock.patch.object(server, "enqueue_review", side_effect=finish_before_enqueue_returns), \
                 mock.patch.object(server, "native_review_result", return_value={
                     "ok": True, "verdict": "PASS", "no_verdict": False,
                 }), mock.patch.object(server, "deliver_webhook", return_value=None), \
                 mock.patch.object(server, "herdr_job_done"):
                status, accepted, _ = server.dispatch(
                    "POST", "/v1/review/run", {}, True, json.dumps(request).encode()
                )
                poll_status, terminal, _ = server.dispatch(
                    "GET", observed["accepted"]["poll"], {}, True, b""
                )

        self.assertEqual(status, 202, accepted)
        self.assertEqual(accepted, observed["accepted"])
        self.assertEqual(set(accepted), {
            "ok", "job_id", "kind", "status", "execution_identity", "created_at",
            "started_at", "finished_at", "receipt", "poll",
        })
        self.assertEqual((accepted["kind"], accepted["status"]), ("review", "queued"))
        self.assertIsNone(accepted["started_at"])
        self.assertIsNone(accepted["finished_at"])
        self.assertIsNone(accepted["receipt"])
        self.assertEqual((poll_status, terminal["status"]), (200, "completed"))
        self.assertEqual(terminal["job_id"], accepted["job_id"])
        self.assertEqual(terminal["execution_identity"], accepted["execution_identity"])
        self.assertEqual(terminal["receipt"]["status"], "ok")

    def test_review_async_does_not_enqueue_when_accepted_projection_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            launcher = root / "claude-review-wrapper"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            launcher.chmod(0o755)
            request = {
                "repo": "scratch", "launcher": "claude", "provider": "anthropic",
                "model": "claude-sonnet-5", "prompt": "bounded ticket", "async": True,
            }
            invalid = {
                "error": "review record is not canonical", "code": "review_record_invalid",
            }
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "active_job_count", return_value=0), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=launcher), \
                 mock.patch.object(server, "public_review_job", return_value=invalid), \
                 mock.patch.object(server, "enqueue_review") as enqueue, \
                 mock.patch.object(server, "native_review_result") as launch:
                status, payload, _ = server.dispatch(
                    "POST", "/v1/review/run", {}, True, json.dumps(request).encode()
                )

        self.assertEqual((status, payload), (409, invalid))
        enqueue.assert_not_called()
        launch.assert_not_called()
        self.assertFalse(server.REVIEW_AUTHORITIES)

    def test_review_poll_and_wait_reject_hostile_timestamps_without_leaking(self):
        hostile = "TOKEN_SECRET /private/review https://evil.invalid/provider-output"
        for field, status_value in (("started_at", "running"), ("finished_at", "completed")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(root)
                self.assertEqual(status, 202, accepted)
                job_id = accepted["job_id"]
                path = jobs / job_id / "job.json"
                stored = json.loads(path.read_text())
                if status_value == "running":
                    stored.update({"status": "running", field: hostile})
                else:
                    receipt = server.review_receipt(
                        stored,
                        {"ok": True, "verdict": "PASS", "no_verdict": False},
                        identity=accepted["execution_identity"],
                    )
                    stored.update({
                        "status": "completed", "started_at": "2026-08-28T00:00:00Z",
                        field: hostile, "receipt": receipt,
                    })
                path.write_text(json.dumps(stored))

                poll_status, poll, _ = server.dispatch(
                    "GET", f"/v1/review/jobs/{job_id}", {}, True, b""
                )
                wait_status, waited, _ = server.dispatch(
                    "POST", "/v1/review/jobs/wait", {}, True,
                    json.dumps({"job_id": job_id, "timeout_seconds": 1}).encode(),
                )
                self.assertEqual((poll_status, poll["code"]), (409, "review_record_invalid"))
                self.assertEqual((wait_status, waited["code"]), (409, "review_record_invalid"))
                self.assertNotIn(hostile, json.dumps(poll))
                self.assertNotIn(hostile, json.dumps(waited))

        for field in (
            "cwd", "response_webhook_token", "response_webhook_url", "provider_output",
            "error", "webhook",
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(root)
                self.assertEqual(status, 202, accepted)
                job_id = accepted["job_id"]
                path = jobs / job_id / "job.json"
                stored = json.loads(path.read_text())
                stored.update({
                    "status": "completed", "started_at": "2026-08-28T00:00:00Z",
                    "finished_at": "2026-08-28T00:00:01Z", field: hostile,
                })
                stored["receipt"] = server.review_receipt(
                    stored,
                    {"ok": True, "verdict": "PASS", "no_verdict": False},
                    identity=accepted["execution_identity"],
                )
                path.write_text(json.dumps(stored))
                poll_status, poll, _ = server.dispatch(
                    "GET", f"/v1/review/jobs/{job_id}", {}, True, b""
                )
                wait_status, waited, _ = server.dispatch(
                    "POST", "/v1/review/jobs/wait", {}, True,
                    json.dumps({"job_id": job_id, "timeout_seconds": 1}).encode(),
                )
                self.assertEqual((poll_status, poll["code"]), (409, "review_record_invalid"))
                self.assertEqual((wait_status, waited["code"]), (409, "review_record_invalid"))
                self.assertNotIn(hostile, json.dumps(poll))
                self.assertNotIn(hostile, json.dumps(waited))

    def test_review_route_source_coordinated_mutation_is_noncanonical(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, repo, launcher, _ = self._queue_closed_review(root)
            self.assertEqual(status, 202, accepted)
            job_id = accepted["job_id"]
            path = jobs / job_id / "job.json"
            stored = json.loads(path.read_text())
            stored["effort_source"] = "route"
            stored["execution_identity"]["effort_source"] = "route"
            stored.update({
                "status": "completed", "started_at": "2026-08-28T00:00:00Z",
                "finished_at": "2026-08-28T00:00:01Z",
            })
            stored["receipt"] = {
                "ok": True, "status": "ok", "job_id": job_id, "kind": "review",
                "verdict": "PASS", "no_verdict": False,
                "execution_identity": deepcopy(stored["execution_identity"]),
                "summary": "review passed", "code": None,
            }
            path.write_text(json.dumps(stored))

            poll_status, poll, _ = server.dispatch(
                "GET", f"/v1/review/jobs/{job_id}", {}, True, b""
            )
            self.assertEqual((poll_status, poll["code"]), (409, "review_record_invalid"))
            authority = server.review_authority(job_id)
            self.assertIsNotNone(authority)
            assert authority is not None
            snapshot = authority.snapshot()
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            canonical = snapshot["execution_identity"]
            with mock.patch.object(
                server, "current_branch_head",
                return_value=(canonical["branch"], canonical["starting_head"], None),
            ), mock.patch.object(
                server.subprocess, "run", side_effect=AssertionError("route review must not spawn")
            ) as spawn:
                result = server.native_review_result(server.NativeReviewExecutionContext(
                    authority_identity=canonical,
                    execution_identity=stored["execution_identity"],
                    repo=snapshot["repo"], resolved=repo.resolve(),
                    prompt=snapshot["prompt"], diff_text="",
                    launcher=snapshot["launcher"], provider=snapshot["provider"],
                    model=snapshot["model"],
                    requested_effort=snapshot["requested_effort"],
                    effective_effort=snapshot["effective_effort"],
                    effort_source=snapshot["effort_source"],
                    route_execution_profile=None,
                    run_budget_seconds=snapshot["run_budget_seconds"],
                    max_turns=snapshot["max_turns"],
                    permission_profile=snapshot["permission_profile"],
                    pinned_launcher=authority.launcher,
                ))
            spawn.assert_not_called()
            self.assertEqual(result["code"], "review_execution_identity_mismatch")

    def test_review_worker_rejects_every_snapshot_drift_before_spawn(self):
        mutations = (
            {"launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol"},
            {"requested_effort": "high", "effective_effort": "high", "effort_source": "explicit"},
            {"effort_source": "explicit"},
            {"launcher_version": "sha256:" + "f" * 64},
            {"execution_identity": None},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                status, accepted, jobs, _repo, launcher, _ = self._queue_closed_review(root)
                self.assertEqual(status, 202, accepted)
                path = jobs / accepted["job_id"] / "job.json"
                stored = json.loads(path.read_text())
                stored.update(mutation)
                if isinstance(mutation.get("execution_identity", object()), dict):
                    pass
                elif "execution_identity" not in mutation:
                    stored["execution_identity"].update(mutation)
                path.write_text(json.dumps(stored))
                replacement_bytes = path.read_bytes()
                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.object(server, "resolve_launcher_binary_now", return_value=launcher), \
                     mock.patch.object(
                         server, "native_review_result",
                         side_effect=AssertionError("invalid profile must not spawn"),
                     ) as spawn, mock.patch.object(server, "herdr_job_done"):
                    server.run_review_job(accepted["job_id"])
                spawn.assert_not_called()
                self.assertEqual(path.read_bytes(), replacement_bytes)
                saved = json.loads(path.read_text())
                self.assertEqual(saved, stored)
                self.assertEqual(saved["status"], "queued")

    def test_review_worker_executes_accepted_pin_after_launcher_path_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, launcher, _ = self._queue_closed_review(root)
            self.assertEqual(status, 202, accepted)
            accepted_fingerprint = accepted["execution_identity"]["launcher_version"]
            launcher.write_text("#!/bin/sh\nexit 99\n")
            self.assertNotEqual(
                server.launcher_version_fingerprint(launcher), accepted_fingerprint
            )
            captured_fingerprints = []

            def pass_with_pinned_launcher(context):
                self.assertIsInstance(context, server.NativeReviewExecutionContext)
                captured_fingerprints.append(
                    server.launcher_version_fingerprint(context.pinned_launcher.path)
                )
                self.assertEqual(
                    context.authority_identity["launcher_version"], accepted_fingerprint
                )
                self.assertEqual(context.execution_identity, context.authority_identity)
                return {"ok": True, "verdict": "PASS", "no_verdict": False}

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=launcher), \
                 mock.patch.object(
                     server, "native_review_result", side_effect=pass_with_pinned_launcher
                 ) as spawn, mock.patch.object(server, "herdr_job_done"), \
                 mock.patch.object(server, "deliver_webhook", return_value=None):
                server.run_review_job(accepted["job_id"])
            spawn.assert_called_once()
            self.assertEqual(captured_fingerprints, [accepted_fingerprint])
            saved = json.loads((jobs / accepted["job_id"] / "job.json").read_text())
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["receipt"]["status"], "ok")

    def test_review_terminal_transition_preserves_source_drift_without_callback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, launcher, _ = self._queue_closed_review(root)
            self.assertEqual(status, 202, accepted)
            path = jobs / accepted["job_id"] / "job.json"

            def drift_then_pass(*args, **kwargs):
                stored = json.loads(path.read_text())
                stored["effort_source"] = "explicit"
                stored["execution_identity"]["effort_source"] = "explicit"
                path.write_text(json.dumps(stored))
                return {"ok": True, "verdict": "PASS", "no_verdict": False}

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=launcher), \
                 mock.patch.object(server, "native_review_result", side_effect=drift_then_pass), \
                 mock.patch.object(server, "herdr_job_done"), \
                 mock.patch.object(server, "deliver_webhook") as callback:
                server.run_review_job(accepted["job_id"])
            callback.assert_not_called()
            saved = json.loads(path.read_text())
            self.assertEqual(saved["status"], "running")
            self.assertEqual(saved["effort_source"], "explicit")
            self.assertEqual(saved["execution_identity"]["effort_source"], "explicit")
            self.assertIsNone(saved["receipt"])

    def test_review_running_record_drift_is_preserved_byte_exact_without_callback(self):
        mutations = (
            ("started_at", "2026-08-28T23:59:59Z"),
            ("finished_at", "2026-08-28T23:59:59Z"),
            ("receipt", {"status": "ok", "provider_output": "hostile"}),
            ("error", "hostile operational replacement"),
            ("webhook", {"ok": True, "status": 204}),
            ("provider_output", "hostile unknown replacement"),
        )
        for field, value in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                status, accepted, jobs, _repo, launcher, _ = self._queue_closed_review(
                    root, extra={"response_webhook_url": "https://example.invalid/review"}
                )
                self.assertEqual(status, 202, accepted)
                job_id = accepted["job_id"]
                path = jobs / job_id / "job.json"
                replacement_bytes = None

                def drift_then_pass(*_args, **_kwargs):
                    nonlocal replacement_bytes
                    replacement = json.loads(path.read_text())
                    self.assertEqual(replacement["status"], "running")
                    replacement[field] = deepcopy(value)
                    replacement_bytes = (json.dumps(replacement, indent=3) + "\n").encode()
                    path.write_bytes(replacement_bytes)
                    return {"ok": True, "verdict": "PASS", "no_verdict": False}

                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.object(server, "resolve_launcher_binary_now", return_value=launcher), \
                     mock.patch.object(server, "native_review_result", side_effect=drift_then_pass), \
                     mock.patch.object(server, "herdr_job_done"), \
                     mock.patch.object(server, "deliver_webhook") as callback:
                    server.run_review_job(job_id)
                callback.assert_not_called()
                self.assertIsNotNone(replacement_bytes)
                self.assertEqual(path.read_bytes(), replacement_bytes)
                poll_status, poll, _ = server.dispatch(
                    "GET", f"/v1/review/jobs/{job_id}", {}, True, b""
                )
                self.assertNotEqual(poll_status, 200, poll)
                self.assertNotEqual(poll.get("status"), "completed", poll)
                self.assertNotEqual((poll.get("receipt") or {}).get("status"), "ok", poll)

    def test_review_ordinary_equal_writes_are_rejected_without_touching_queued_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(root)
            self.assertEqual(status, 202, accepted)
            path = jobs / accepted["job_id"] / "job.json"
            queued = json.loads(path.read_text())
            original_bytes = path.read_bytes()
            original_stat = path.stat()

            with mock.patch.object(
                server, "_write_job_unlocked", wraps=server._write_job_unlocked
            ) as atomic_write:
                for candidate in (queued, dict(reversed(list(queued.items())))):
                    with self.subTest(order=list(candidate)):
                        self.assertFalse(server.write_job(candidate))
                        self.assertEqual(path.read_bytes(), original_bytes)
                        current_stat = path.stat()
                        self.assertEqual(current_stat.st_ino, original_stat.st_ino)
                        self.assertEqual(current_stat.st_mtime_ns, original_stat.st_mtime_ns)
                atomic_write.assert_not_called()

    def test_review_reordered_running_write_is_noop_and_completion_callback_stays_consistent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(
                root, extra={"response_webhook_url": "https://example.invalid/review"}
            )
            self.assertEqual(status, 202, accepted)
            job_id = accepted["job_id"]
            path = jobs / job_id / "job.json"
            write_results = []
            callback_receipts = []

            def attempt_equal_writes(*_args, **_kwargs):
                running = json.loads(path.read_text())
                self.assertEqual(running["status"], "running")
                original_bytes = path.read_bytes()
                original_stat = path.stat()
                authority = server.review_authority(job_id)
                self.assertIsNotNone(authority)
                sealed_bytes = authority.running_record_bytes
                sealed_hash = authority.running_record_hash
                for candidate in (running, dict(reversed(list(running.items())))):
                    write_results.append(server.write_job(candidate))
                    self.assertEqual(path.read_bytes(), original_bytes)
                    current_stat = path.stat()
                    self.assertEqual(current_stat.st_ino, original_stat.st_ino)
                    self.assertEqual(current_stat.st_mtime_ns, original_stat.st_mtime_ns)
                    self.assertEqual(authority.running_record_bytes, sealed_bytes)
                    self.assertEqual(authority.running_record_hash, sealed_hash)
                return {
                    "ok": True, "verdict": "PASS", "no_verdict": False,
                    "usage": {
                        "usage_version": "normalized-usage-v1",
                        "fresh_input_tokens": 7, "cached_input_tokens": 2,
                        "cache_write_tokens": 0, "output_tokens": 3,
                        "reasoning_tokens": 0, "provider_cost_usd": None,
                        "wall_seconds": 0.25, "turns_observed": 1,
                    },
                    "usage_diagnostic": None,
                }

            with mock.patch.object(
                server, "native_review_result", side_effect=attempt_equal_writes
            ), mock.patch.object(
                server, "deliver_webhook",
                side_effect=lambda _job, receipt: callback_receipts.append(deepcopy(receipt))
                or {"ok": True, "status": 204},
            ) as callback, mock.patch.object(server, "herdr_job_done"):
                server.run_review_job(job_id)

            self.assertEqual(write_results, [False, False])
            callback.assert_called_once()
            self.assertEqual(len(callback_receipts), 1)
            saved = json.loads(path.read_text())
            self.assertEqual((saved["kind"], saved["status"]), ("review", "completed"))
            self.assertEqual(saved["receipt"], callback_receipts[0])
            self.assertEqual(saved["webhook"], {"ok": True, "status": 204})
            poll_status, polled, _ = server.dispatch(
                "GET", f"/v1/review/jobs/{job_id}", {}, True, b""
            )
            self.assertEqual(poll_status, 200, polled)
            self.assertIsInstance(polled, dict)
            assert isinstance(polled, dict)
            self.assertEqual(polled["receipt"]["usage"], saved["receipt"]["usage"])
            self.assertNotIn("provider_payload", json.dumps(polled))

    def test_review_ordinary_write_rejects_immutable_drift_and_guarded_lifecycle_works(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(root)
            self.assertEqual(status, 202, accepted)
            job_id = accepted["job_id"]
            path = jobs / job_id / "job.json"
            original = json.loads(path.read_text())
            original_bytes = path.read_bytes()
            original_stat = path.stat()
            identity_drift = deepcopy(original)
            identity_drift["prompt"] = "coordinated trusted drift"
            identity_drift["prompt_sha256"] = hashlib.sha256(
                identity_drift["prompt"].encode()
            ).hexdigest()
            identity_drift["execution_identity"]["prompt_sha256"] = identity_drift[
                "prompt_sha256"
            ]
            operational_drift = deepcopy(original)
            operational_drift["error"] = "trusted operational drift"
            unknown_drift = deepcopy(original)
            unknown_drift["provider_output"] = "trusted unknown drift"
            for label, drift in (
                ("identity", identity_drift),
                ("operational", operational_drift),
                ("unknown", unknown_drift),
            ):
                with self.subTest(mutation=label):
                    self.assertFalse(server.write_job(drift))
                    self.assertEqual(path.read_bytes(), original_bytes)
                    current_stat = path.stat()
                    self.assertEqual(current_stat.st_ino, original_stat.st_ino)
                    self.assertEqual(current_stat.st_mtime_ns, original_stat.st_mtime_ns)

            with mock.patch.object(server, "native_review_result", return_value={
                "ok": True, "verdict": "PASS", "no_verdict": False,
            }), mock.patch.object(server, "deliver_webhook", return_value=None), \
                 mock.patch.object(server, "herdr_job_done"):
                server.run_review_job(job_id)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["receipt"]["status"], "ok")

    def test_review_endpoints_reject_nonreview_kinds_and_agent_endpoint_rejects_review(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            with mock.patch.object(server, "JOBS_DIR", jobs):
                for index, kind in enumerate(("agent", "test", "script", "rr", "alien", None), 1):
                    job_id = f"{index:02x}" * 16
                    (jobs / job_id).mkdir(parents=True)
                    (jobs / job_id / "job.json").write_text(json.dumps({
                        "job_id": job_id, "kind": kind, "status": "queued",
                    }))
                    status, payload, _ = server.dispatch(
                        "GET", f"/v1/review/jobs/{job_id}", {}, True, b""
                    )
                    self.assertEqual(status, 404, (kind, payload))
                review_id = "aa" * 16
                (jobs / review_id).mkdir(parents=True)
                (jobs / review_id / "job.json").write_text(json.dumps({
                    "job_id": review_id, "kind": "review", "status": "queued",
                }))
                status, _, _ = server.dispatch(
                    "GET", f"/v1/agent/jobs/{review_id}", {}, True, b""
                )
                self.assertEqual(status, 404)
                for bad_id in ("bad", "ff" * 16):
                    status, payload, _ = server.dispatch(
                        "GET", f"/v1/review/jobs/{bad_id}", {}, True, b""
                    )
                    self.assertEqual(status, 400 if bad_id == "bad" else 404, payload)

    def test_review_coordinated_job_and_disk_snapshot_rewrite_cannot_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(root)
            self.assertEqual(status, 202, accepted)
            job_id = accepted["job_id"]
            job_path = jobs / job_id / "job.json"
            snapshot_path = jobs / job_id / "review-snapshot.json"
            stored = json.loads(job_path.read_text())
            snapshot = json.loads(snapshot_path.read_text())
            hostile_prompt = "coordinated replacement TOKEN_SECRET /private/review"
            hostile_hash = hashlib.sha256(hostile_prompt.encode()).hexdigest()
            for record in (stored, snapshot):
                record["prompt"] = hostile_prompt
                record["prompt_sha256"] = hostile_hash
                record["execution_identity"]["prompt_sha256"] = hostile_hash
            snapshot_raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
            snapshot_path.write_bytes(snapshot_raw)
            stored["review_snapshot_sha256"] = hashlib.sha256(snapshot_raw).hexdigest()
            job_path.write_text(json.dumps(stored))
            replacement_bytes = job_path.read_bytes()

            with mock.patch.object(
                server, "native_review_result",
                side_effect=AssertionError("coordinated mutable rewrite must not spawn"),
            ) as spawn, mock.patch.object(server, "deliver_webhook") as callback, \
                 mock.patch.object(server, "herdr_job_done"):
                server.run_review_job(job_id)
            spawn.assert_not_called()
            callback.assert_not_called()
            self.assertEqual(job_path.read_bytes(), replacement_bytes)
            saved = json.loads(job_path.read_text())
            self.assertEqual(saved, stored)
            self.assertEqual(saved["status"], "queued")
            self.assertNotIn(job_id, server.REVIEW_AUTHORITIES)

    def test_review_missing_process_authority_fails_closed_without_spawn_or_callback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(
                root, extra={"response_webhook_url": "https://example.invalid/review"}
            )
            self.assertEqual(status, 202, accepted)
            job_id = accepted["job_id"]
            server.release_review_authority(job_id)
            with mock.patch.object(server, "native_review_result") as spawn, \
                 mock.patch.object(server, "deliver_webhook") as callback, \
                 mock.patch.object(server, "herdr_job_done"):
                server.run_review_job(job_id)
            spawn.assert_not_called()
            callback.assert_not_called()
            saved = json.loads((jobs / job_id / "job.json").read_text())
            self.assertEqual(saved["status"], "failed")
            self.assertIsNone(saved["receipt"])
            poll_status, poll, _ = server.dispatch(
                "GET", f"/v1/review/jobs/{job_id}", {}, True, b""
            )
            self.assertEqual((poll_status, poll["code"]), (409, "review_authority_unavailable"))
            self.assertNotIn("prompt", json.dumps(poll))

    def test_review_terminal_is_sealed_and_guarded_bookkeeping_still_works(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(root)
            self.assertEqual(status, 202, accepted)
            job_id = accepted["job_id"]
            with mock.patch.object(
                server, "native_review_result",
                return_value={"ok": True, "verdict": "PASS", "no_verdict": False},
            ), mock.patch.object(server, "deliver_webhook", return_value=None), \
                 mock.patch.object(server, "herdr_job_done"):
                server.run_review_job(job_id)
            path = jobs / job_id / "job.json"
            terminal = json.loads(path.read_text())
            receipt = deepcopy(terminal["receipt"])
            terminal_bytes = path.read_bytes()
            terminal_stat = path.stat()
            for candidate in (terminal, dict(reversed(list(terminal.items())))):
                with self.subTest(order=list(candidate)):
                    self.assertFalse(server.write_job(candidate))
                    self.assertEqual(path.read_bytes(), terminal_bytes)
                    current_stat = path.stat()
                    self.assertEqual(current_stat.st_ino, terminal_stat.st_ino)
                    self.assertEqual(current_stat.st_mtime_ns, terminal_stat.st_mtime_ns)
            hostile = deepcopy(terminal)
            hostile.update({
                "prompt": "TOKEN_SECRET", "cwd": "/private/path", "status": "failed",
                "receipt": {"provider_output": "hostile"},
            })
            self.assertFalse(server.write_job(hostile))
            stale_revision = deepcopy(terminal)
            stale_revision["started_at"] = "2026-08-28T23:59:59Z"
            self.assertFalse(server.review_set_webhook_result(
                job_id, stale_revision, {"ok": True, "status": 204}
            ))
            self.assertEqual(
                json.loads((jobs / job_id / "job.json").read_text()), terminal
            )
            self.assertTrue(server.review_set_webhook_result(
                job_id, terminal, {"ok": True, "status": 204}
            ))
            saved = json.loads((jobs / job_id / "job.json").read_text())
            self.assertEqual(saved["receipt"], receipt)
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["webhook"], {"ok": True, "status": 204})

    def test_review_terminal_cas_rejects_reentrant_and_queued_replacements(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(
                root, extra={"response_webhook_url": "https://example.invalid/review"}
            )
            self.assertEqual(status, 202, accepted)
            job_id = accepted["job_id"]
            alien = {
                "job_id": job_id, "kind": "script", "status": "failed",
                "receipt": {"status": "error", "job_id": job_id},
                "prompt": "TOKEN_SECRET", "cwd": "/private/path",
            }
            original_write = server._write_job_unlocked
            reentrant_results = []
            queued_results = []
            attempted = threading.Event()
            done = threading.Event()
            writer_thread = None

            def queued_writer():
                attempted.set()
                queued_results.append(server.write_job(alien))
                done.set()

            def guarded_write(candidate):
                nonlocal writer_thread
                if candidate.get("kind") == "review" and candidate.get("status") == "completed":
                    reentrant_results.append(server.write_job(alien))
                    if writer_thread is None:
                        writer_thread = threading.Thread(target=queued_writer, daemon=True)
                        writer_thread.start()
                        self.assertTrue(attempted.wait(0.5))
                original_write(candidate)

            callbacks = []
            with mock.patch.object(
                server, "native_review_result",
                return_value={"ok": True, "verdict": "PASS", "no_verdict": False},
            ), mock.patch.object(server, "_write_job_unlocked", side_effect=guarded_write), \
                 mock.patch.object(
                     server, "deliver_webhook",
                     side_effect=lambda _job, receipt: callbacks.append(deepcopy(receipt)) or None,
                 ), mock.patch.object(server, "herdr_job_done"):
                server.run_review_job(job_id)
            self.assertIsNotNone(writer_thread)
            writer_thread.join(timeout=1)
            self.assertTrue(done.is_set())
            self.assertEqual(reentrant_results, [False])
            self.assertEqual(queued_results, [False])
            self.assertEqual(len(callbacks), 1)
            saved = json.loads((jobs / job_id / "job.json").read_text())
            self.assertEqual((saved["kind"], saved["status"]), ("review", "completed"))
            self.assertEqual(saved["receipt"], callbacks[0])

    def test_review_duplicate_worker_and_callback_failure_are_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(
                root, extra={"response_webhook_url": "https://example.invalid/review"}
            )
            self.assertEqual(status, 202, accepted)
            job_id = accepted["job_id"]
            entered = threading.Event()
            release = threading.Event()

            def blocked_pass(*_args, **_kwargs):
                entered.set()
                self.assertTrue(release.wait(1))
                return {"ok": True, "verdict": "PASS", "no_verdict": False}

            with mock.patch.object(
                server, "native_review_result", side_effect=blocked_pass
            ) as spawn, mock.patch.object(
                server, "deliver_webhook", return_value={"ok": False, "status": 503}
            ) as callback, mock.patch.object(server, "herdr_job_done"):
                first = threading.Thread(target=server.run_review_job, args=(job_id,), daemon=True)
                first.start()
                self.assertTrue(entered.wait(1))
                duplicate = threading.Thread(
                    target=server.run_review_job, args=(job_id,), daemon=True
                )
                duplicate.start()
                duplicate.join(timeout=1)
                self.assertFalse(duplicate.is_alive())
                release.set()
                first.join(timeout=1)
                self.assertFalse(first.is_alive())
            spawn.assert_called_once()
            callback.assert_called_once()
            saved = json.loads((jobs / job_id / "job.json").read_text())
            receipt = deepcopy(saved["receipt"])
            self.assertEqual((saved["status"], receipt["status"]), ("completed", "ok"))
            self.assertEqual(saved["webhook"], {"ok": False, "status": 503})
            self.assertEqual(saved["receipt"], receipt)

    def test_review_hostile_stored_receipt_is_not_polled_or_leaked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(root)
            self.assertEqual(status, 202, accepted)
            job_id = accepted["job_id"]
            path = jobs / job_id / "job.json"
            stored = json.loads(path.read_text())
            stored.update({"status": "completed", "finished_at": "2026-08-28T00:00:00Z"})
            stored["receipt"] = {
                "ok": True, "status": "ok", "job_id": job_id, "kind": "review",
                "verdict": "PASS", "no_verdict": False,
                "execution_identity": accepted["execution_identity"],
                "summary": "review passed", "code": None,
                "prompt": "TOKEN_SECRET", "path": "/private/review", "provider_output": "raw",
            }
            path.write_text(json.dumps(stored))
            poll_status, payload, _ = server.dispatch(
                "GET", f"/v1/review/jobs/{job_id}", {}, True, b""
            )
            self.assertEqual((poll_status, payload["code"]), (409, "review_record_invalid"))
            public = json.dumps(payload)
            for secret in ("TOKEN_SECRET", "/private/review", "raw"):
                self.assertNotIn(secret, public)
            server.release_review_authority(job_id)

    def test_review_startup_recovery_never_callbacks_mutable_review_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            cases = {
                "a1" * 16: {"kind": "review", "status": "queued"},
                "a2" * 16: {"kind": "review", "status": "running", "review_snapshot_sha256": "0" * 64},
                "a3" * 16: {"kind": "script", "status": "running"},
                "a4" * 16: {"status": "running"},
            }
            for job_id, extra in cases.items():
                path = jobs / job_id
                path.mkdir(parents=True)
                (path / "job.json").write_text(json.dumps({
                    "job_id": job_id, "response_webhook_url": "https://example.invalid/stale",
                    "response_webhook_token": "TOKEN_SECRET", "prompt": "/private/review", **extra,
                }))
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "deliver_webhook") as callback:
                recovered = server.recover_interrupted_agent_jobs()
            self.assertEqual(callback.call_count, 1)
            self.assertEqual(callback.call_args.args[0]["kind"], "script")
            self.assertEqual(recovered, 3)
            for job_id in ("a1" * 16, "a2" * 16):
                saved = json.loads((jobs / job_id / "job.json").read_text())
                self.assertEqual(saved["status"], "failed")
                self.assertIsNone(saved["receipt"])
                self.assertEqual(saved["review_recovery_code"], "review_authority_unavailable")
            self.assertEqual(
                json.loads((jobs / ("a4" * 16) / "job.json").read_text())["status"], "running"
            )

    def test_review_run_409_and_501_actual_bodies_match_exact_openapi_branches(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:  # catalog CI intentionally installs no third-party packages
            Draft202012Validator = None

        spec = json.loads(server.openapi_bytes())
        responses = spec["paths"]["/v1/review/run"]["post"]["responses"]
        response_409 = responses["409"]["content"]["application/json"]["schema"]
        self.assertEqual(
            {item["$ref"] for item in response_409["oneOf"]},
            {"#/components/schemas/ReviewError", "#/components/schemas/ReviewReceipt"},
        )
        def dereference(value):
            if isinstance(value, dict):
                if set(value) == {"$ref"} and value["$ref"].startswith("#/"):
                    target = spec
                    for part in value["$ref"][2:].split("/"):
                        target = target[part.replace("~1", "/").replace("~0", "~")]
                    return dereference(target)
                return {key: dereference(item) for key, item in value.items()}
            if isinstance(value, list):
                return [dereference(item) for item in value]
            return value

        def subset_valid(schema, instance):
            """Draft 2020-12 semantics for every keyword used by these closed shapes."""
            schema = dereference(schema)
            if "oneOf" in schema and sum(subset_valid(item, instance) for item in schema["oneOf"]) != 1:
                return False
            if "anyOf" in schema and not any(subset_valid(item, instance) for item in schema["anyOf"]):
                return False
            expected = schema.get("type")
            if expected is not None:
                expected = [expected] if isinstance(expected, str) else expected
                matches_type = {
                    "object": lambda: isinstance(instance, dict),
                    "array": lambda: isinstance(instance, list),
                    "string": lambda: isinstance(instance, str),
                    "integer": lambda: type(instance) is int,
                    "number": lambda: type(instance) in (int, float),
                    "boolean": lambda: type(instance) is bool,
                    "null": lambda: instance is None,
                }
                if not any(matches_type[item]() for item in expected):
                    return False
            if "const" in schema and instance != schema["const"]:
                return False
            if "enum" in schema and instance not in schema["enum"]:
                return False
            if isinstance(instance, str):
                if len(instance) < schema.get("minLength", 0):
                    return False
                if "maxLength" in schema and len(instance) > schema["maxLength"]:
                    return False
                if "pattern" in schema and re.search(schema["pattern"], instance) is None:
                    return False
            if type(instance) in (int, float):
                if "minimum" in schema and instance < schema["minimum"]:
                    return False
                if "maximum" in schema and instance > schema["maximum"]:
                    return False
            if isinstance(instance, dict):
                if not set(schema.get("required", ())) <= set(instance):
                    return False
                properties = schema.get("properties", {})
                if schema.get("additionalProperties") is False and not set(instance) <= set(properties):
                    return False
                if any(
                    key in properties and not subset_valid(properties[key], value)
                    for key, value in instance.items()
                ):
                    return False
            if isinstance(instance, list) and "items" in schema:
                if not all(subset_valid(schema["items"], item) for item in instance):
                    return False
            return True

        def valid(schema, instance):
            resolved = dereference(schema)
            portable = subset_valid(resolved, instance)
            if Draft202012Validator is None:
                return portable
            authoritative = not list(Draft202012Validator(resolved).iter_errors(instance))
            self.assertEqual(portable, authoritative)
            return authoritative

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, busy, *_ = self._queue_closed_review(
                root, active_count=server.AGENT_MAX_CONCURRENT
            )
            self.assertEqual((status, set(busy)), (409, {"error", "code"}))

            sync_root = root / "sync"
            sync_root.mkdir()
            repo = self._scratch_git(sync_root)
            launcher = root / "sync-review-wrapper"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            launcher.chmod(0o755)
            body = json.dumps({
                "repo": "scratch", "launcher": "claude", "provider": "anthropic",
                "model": "claude-sonnet-5", "prompt": "bounded ticket",
            }).encode()
            hostile_body = json.dumps({
                "repo": "TOKEN_SECRET/private/review", "launcher": "claude",
                "provider": "anthropic", "model": "claude-sonnet-5",
                "prompt": "bounded ticket",
            }).encode()
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                invalid_status, invalid, _ = server.dispatch(
                    "POST", "/v1/review/run", {}, True, hostile_body
                )
            self.assertEqual(invalid_status, 400)
            self.assertNotIn("TOKEN_SECRET", json.dumps(invalid))
            identity_mismatch = {
                "ok": False, "verdict": "NACK", "no_verdict": True,
                "code": "review_execution_identity_mismatch",
            }
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=launcher), \
                 mock.patch.object(
                     server, "native_review_result", return_value=identity_mismatch
                 ) as boundary:
                mismatch_status, mismatch, _ = server.dispatch(
                    "POST", "/v1/review/run", {}, True, body
                )
            boundary.assert_called_once()
            self.assertEqual(mismatch_status, 409)
            self.assertEqual(set(mismatch), set(server.REVIEW_RECEIPT_FIELDS))
            self.assertEqual(mismatch["code"], "review_execution_identity_mismatch")

            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=launcher), \
                 mock.patch.object(server, "pin_launcher", side_effect=FileNotFoundError), \
                 mock.patch.object(server, "native_review_result") as unavailable_spawn:
                unavailable_status, unavailable, _ = server.dispatch(
                    "POST", "/v1/review/run", {}, True, body
                )
            unavailable_spawn.assert_not_called()
            self.assertEqual((unavailable_status, set(unavailable)), (501, {"error", "code"}))

        for instance in (busy, mismatch):
            self.assertTrue(valid(response_409, instance))
            self.assertEqual(sum(valid(branch, instance) for branch in response_409["oneOf"]), 1)
        response_400 = responses["400"]["content"]["application/json"]["schema"]
        self.assertTrue(valid(response_400, invalid))
        response_501 = responses["501"]["content"]["application/json"]["schema"]
        self.assertTrue(valid(response_501, unavailable))

    def test_review_alien_replacement_at_terminal_cas_is_preserved_without_callback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, _repo, _launcher, _ = self._queue_closed_review(
                root, extra={"response_webhook_url": "https://example.invalid/review"}
            )
            self.assertEqual(status, 202, accepted)
            job_id = accepted["job_id"]
            alien = {
                "job_id": job_id, "kind": "script", "status": "failed",
                "receipt": {"status": "error", "job_id": job_id},
                "error": "trusted replacement",
            }

            def replace_then_pass(*_args, **_kwargs):
                _replace_persisted_job_fixture(job_id, alien)
                return {"ok": True, "verdict": "PASS", "no_verdict": False}

            with mock.patch.object(
                server, "native_review_result", side_effect=replace_then_pass
            ), mock.patch.object(server, "deliver_webhook") as callback, \
                 mock.patch.object(server, "herdr_job_done"):
                server.run_review_job(job_id)
            callback.assert_not_called()
            self.assertEqual(json.loads((jobs / job_id / "job.json").read_text()), alien)
            self.assertNotIn(job_id, server.REVIEW_AUTHORITIES)

    def test_review_authority_memfds_are_sealed_and_async_cleanup_has_no_fd_leak(self):
        baseline = len(list(Path("/proc/self/fd").iterdir()))
        job_ids = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(12):
                case_root = root / f"case-{index}"
                case_root.mkdir()
                status, accepted, _jobs, _repo, _launcher, _ = self._queue_closed_review(
                    case_root
                )
                self.assertEqual(status, 202, accepted)
                job_id = accepted["job_id"]
                job_ids.append(job_id)
                authority = server.review_authority(job_id)
                self.assertIsNotNone(authority)
                seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
                self.assertEqual(
                    fcntl.fcntl(authority.snapshot_fd, fcntl.F_GET_SEALS) & seals, seals
                )
                self.assertEqual(
                    fcntl.fcntl(authority.launcher.fd, fcntl.F_GET_SEALS) & seals, seals
                )
                with self.assertRaises(OSError):
                    os.pwrite(authority.snapshot_fd, b"x", 0)
                with self.assertRaises(OSError):
                    os.pwrite(authority.launcher.fd, b"x", 0)

                def pass_after_running_seal(*_args, **_kwargs):
                    self.assertGreaterEqual(authority.running_record_fd, 0)
                    self.assertEqual(
                        fcntl.fcntl(authority.running_record_fd, fcntl.F_GET_SEALS) & seals,
                        seals,
                    )
                    self.assertIsNotNone(authority.running_record())
                    with self.assertRaises(OSError):
                        os.pwrite(authority.running_record_fd, b"x", 0)
                    return {"ok": True, "verdict": "PASS", "no_verdict": False}

                with mock.patch.object(
                    server, "native_review_result", side_effect=pass_after_running_seal
                ), mock.patch.object(server, "deliver_webhook", return_value=None), \
                     mock.patch.object(server, "herdr_job_done"):
                    server.run_review_job(job_id)
                self.assertNotIn(job_id, server.REVIEW_AUTHORITIES)
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), baseline)
        self.assertFalse(set(job_ids) & set(server.REVIEW_AUTHORITIES))

    def test_review_run_is_async_when_given_a_webhook(self):
        body = json.dumps({
            "repo": "scratch", "launcher": "claude", "provider": "anthropic",
            "model": "claude-sonnet-5", "prompt": "ticket",
            "response_webhook_url": "https://example.invalid/resume",
        }).encode()
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            jobs = Path(td) / "jobs"
            launcher = Path(td) / "claude-review-wrapper"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            launcher.chmod(0o755)
            launcher_version = "sha256:" + hashlib.sha256(launcher.read_bytes()).hexdigest()
            with mock.patch.dict(os.environ, {"GRAPHWING_CLAUDE_BIN": str(launcher)}), \
                 mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "active_job_count", return_value=0), \
                 mock.patch.object(server, "enqueue_review") as enq:
                status, payload, _ = server.dispatch("POST", "/v1/review/run", {}, True, body)
                if status == 202 and isinstance(payload, dict):
                    self.addCleanup(server.release_review_authority, payload["job_id"])
        assert isinstance(payload, dict)
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["kind"], "review")
        self.assertEqual(
            tuple(payload["execution_identity"][key] for key in ("launcher", "provider", "model")),
            ("claude", "anthropic", "claude-sonnet-5"),
        )
        self.assertEqual(payload["execution_identity"]["launcher_version"], launcher_version)
        self.assertTrue(payload["job_id"])
        queued = enq.call_args.args[0]
        self.assertNotIn("reviewer", queued)
        self.assertEqual(
            (queued["requested_effort"], queued["effective_effort"], queued["effort_source"]),
            ("default", "default", "launcher_default"),
        )
        self.assertEqual(queued["run_budget_seconds"], server.REVIEW_DEFAULT_BUDGET_SECONDS)
        self.assertEqual(queued["max_turns"], server.REVIEW_MAX_TURNS)

    def test_review_run_can_queue_durable_job_without_webhook(self):
        body = json.dumps({
            "repo": "scratch", "launcher": "claude", "provider": "anthropic",
            "model": "claude-sonnet-5", "prompt": "ticket", "async": True,
            "run_budget_seconds": 600, "max_turns": 20,
        }).encode()
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            jobs = Path(td) / "jobs"
            launcher = Path(td) / "claude-review-wrapper"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            launcher.chmod(0o755)
            launcher_version = "sha256:" + hashlib.sha256(launcher.read_bytes()).hexdigest()
            with mock.patch.dict(os.environ, {"GRAPHWING_CLAUDE_BIN": str(launcher)}), \
                 mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "active_job_count", return_value=0), \
                 mock.patch.object(server, "enqueue_review") as enq:
                response = server.dispatch("POST", "/v1/review/run", {}, True, body)
                status, payload = response[0], response[1]
                assert isinstance(payload, dict)
                if status == 202:
                    self.addCleanup(server.release_review_authority, payload["job_id"])
            saved = json.loads((jobs / payload["job_id"] / "job.json").read_text())
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(saved["run_budget_seconds"], 600)
        self.assertEqual(saved["max_turns"], 20)
        self.assertEqual(saved["effective_effort"], "default")
        self.assertEqual(saved["launcher_version"], launcher_version)
        self.assertIsNone(saved["response_webhook_url"])
        enq.assert_called_once()

    def test_review_run_validates_async_and_bounded_budget(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                for field, value, code in (
                    ("async", "true", "bad_async"),
                    ("run_budget_seconds", True, "bad_run_budget"),
                    ("run_budget_seconds", 29, "bad_run_budget"),
                    ("run_budget_seconds", 601, "bad_run_budget"),
                    ("max_turns", True, "bad_max_turns"),
                    ("max_turns", 0, "bad_max_turns"),
                    ("max_turns", 31, "bad_max_turns"),
                ):
                    request = {
                        "repo": "scratch", "launcher": "claude", "provider": "anthropic",
                        "model": "claude-sonnet-5", "prompt": "ticket", field: value,
                    }
                    response = server.dispatch(
                        "POST", "/v1/review/run", {}, True, json.dumps(request).encode()
                    )
                    status, payload = response[0], response[1]
                    assert isinstance(payload, dict)
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["code"], code)

    def test_review_job_uses_persisted_budget_and_retains_timeout_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status, accepted, jobs, repo, _launcher, _ = self._queue_closed_review(
                root, effort="high", extra={"run_budget_seconds": 600, "max_turns": 20}
            )
            assert isinstance(accepted, dict)
            self.assertEqual(status, 202, accepted)
            job_id = accepted["job_id"]
            timed_out = {
                "ok": False, "verdict": "NACK", "no_verdict": True,
                "summary": "provider timeout detail", "compact": "timeout",
                "returncode": 1, "code": "timeout",
            }
            with mock.patch.object(server, "native_review_result", return_value=timed_out) as review, \
                 mock.patch.object(server, "herdr_job_done"):
                server.run_review_job(job_id)
            saved = json.loads((jobs / job_id / "job.json").read_text())
        review.assert_called_once()
        args, kwargs = review.call_args
        self.assertEqual(kwargs, {})
        self.assertEqual(len(args), 1)
        context = args[0]
        self.assertIsInstance(context, server.NativeReviewExecutionContext)
        self.assertEqual(context.execution_identity, accepted["execution_identity"])
        self.assertEqual(context.authority_identity, accepted["execution_identity"])
        self.assertEqual((context.launcher, context.provider, context.model), (
            "claude", "anthropic", "claude-sonnet-5",
        ))
        self.assertEqual(context.repo, "scratch")
        self.assertEqual(context.resolved, repo.resolve())
        self.assertEqual(context.prompt, "bounded ticket")
        self.assertEqual(context.job_id, job_id)
        self.assertEqual(context.run_budget_seconds, 600)
        self.assertEqual(context.max_turns, 20)
        self.assertEqual(context.requested_effort, "high")
        self.assertEqual(context.effective_effort, "high")
        self.assertEqual(context.effort_source, "explicit")
        self.assertEqual(context.permission_profile, "claude-plan")
        self.assertEqual(context.pinned_launcher.path.parent, Path("/proc/self/fd"))
        self.assertEqual(
            context.pinned_launcher.fingerprint,
            accepted["execution_identity"]["launcher_version"],
        )
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["receipt"]["status"], "error")
        self.assertEqual(saved["receipt"]["code"], "timeout")
        self.assertTrue(saved["receipt"]["no_verdict"])
        self.assertEqual(saved["receipt"]["summary"], "review timed out")
        self.assertIsNone(saved["receipt"]["usage"])
        self.assertEqual(saved["receipt"]["usage_diagnostic"], "usage_not_reported")

    def test_review_run_rejects_profile_named_or_mismatched_reviewers(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                for request, code in (
                    ({"repo": "scratch", "reviewer": "terra", "prompt": "x"}, "unexpected_fields"),
                    ({"repo": "scratch", "launcher": "codex", "provider": "anthropic", "model": "gpt-5.6-sol", "prompt": "x"}, "bad_model_identity"),
                ):
                    status, payload, _ = server.dispatch(
                        "POST", "/v1/review/run", {}, True, json.dumps(request).encode()
                    )
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["code"], code)

    def test_direct_review_commands_are_read_only_and_explicit(self):
        commands = []

        class FakeProc:
            stderr = b""
            returncode = 0

            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(cmd, **kwargs):
            commands.append((list(cmd), kwargs))
            if "exec" in cmd:
                receipt = json.dumps({
                    "status": "ok", "sha": None, "pr_url": None,
                    "summary": "VERDICT: PASS",
                })
                event = {"type": "item.completed", "item": {"type": "agent_message", "text": receipt}}
                return FakeProc((json.dumps(event) + "\n").encode())
            return FakeProc(b"VERDICT: PASS\n")

        with tempfile.TemporaryDirectory() as td:
            codex_bin = Path(td) / "codex"
            claude_bin = Path(td) / "claude"
            codex_bin.write_text("fixture")
            claude_bin.write_text("fixture")
            with self._native_review_context(
                "codex", "openai", "gpt-5.6-sol", codex_bin
            ) as codex_context, self._native_review_context(
                "claude", "anthropic", "claude-sonnet-5", claude_bin
            ) as claude_context, mock.patch.object(
                server, "JOBS_DIR", Path(td) / "jobs"
            ), mock.patch.object(server.subprocess, "run", fake_run):
                codex = server.native_review_result(codex_context)
                claude = server.native_review_result(claude_context)
        grok, grok_capture = self._run_grok_review_fixture({"usage": {
            "totalTokens": 14, "inputTokens": 10, "outputTokens": 4,
            "thoughtTokens": 1, "cachedReadTokens": 0, "cachedWriteTokens": 0,
        }})
        self.assertEqual(
            (codex["verdict"], claude["verdict"], grok["verdict"]),
            ("PASS", "PASS", "PASS"),
        )
        self.assertEqual(grok["usage"], {
            "usage_version": "normalized-usage-v1",
            "fresh_input_tokens": 10,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 4,
            "reasoning_tokens": 1,
            "provider_cost_usd": None,
            "wall_seconds": grok["usage"]["wall_seconds"],
            "turns_observed": None,
        })
        self.assertIsNone(grok["usage_diagnostic"])
        self.assertNotIn("totalTokens", json.dumps(grok))
        codex_cmd, codex_kwargs = commands[0]
        self.assertEqual(codex_cmd[codex_cmd.index("--sandbox") + 1], "read-only")
        self.assertNotIn("workspace-write", codex_cmd)
        self.assertIn("model_reasoning_effort=high", codex_cmd)
        self.assertEqual(codex_cmd[codex_cmd.index("--model") + 1], "gpt-5.6-sol")
        self.assertIn("input", codex_kwargs)
        claude_cmd, _ = commands[1]
        self.assertEqual(claude_cmd[claude_cmd.index("--permission-mode") + 1], "plan")
        self.assertNotIn("acceptEdits", claude_cmd)
        self.assertEqual(claude_cmd[claude_cmd.index("--model") + 1], "claude-sonnet-5")
        self.assertEqual(claude_cmd[claude_cmd.index("--max-turns") + 1], str(server.REVIEW_MAX_TURNS))
        self.assertEqual(
            grok_capture["argv"],
            ["agent", "--model", "grok-4.6", "--reasoning-effort", "high", "stdio"],
        )
        initialize = next(
            request for request in grok_capture["requests"]
            if request["method"] == "initialize"
        )
        self.assertEqual(initialize["params"]["clientCapabilities"], {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        })

    def test_grok_review_permission_request_is_denied_and_fails_closed(self):
        result, capture = self._run_grok_review_fixture({"permission_request": True})
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["no_verdict"], result)
        self.assertEqual(result["summary"], "Grok review requested tool permission")
        self.assertEqual(capture["permission_response"], {
            "jsonrpc": "2.0",
            "id": 99,
            "result": {"outcome": {"outcome": "cancelled"}},
        })

    def test_direct_review_failures_never_pass(self):
        class FakeProc:
            stderr = b""

            def __init__(self, stdout, returncode=0):
                self.stdout = stdout
                self.returncode = returncode

        receipt = json.dumps({
            "status": "ok", "sha": None, "pr_url": None,
            "summary": "VERDICT: PASS",
        })
        event = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": receipt},
        }).encode()
        cases = (
            (FakeProc(event, returncode=7), None),
            (FakeProc(b"not a verdict"), None),
            (subprocess.TimeoutExpired("codex", 200), "timeout"),
            (OSError("spawn failed"), "review_failed"),
        )
        with tempfile.TemporaryDirectory() as td:
            codex = Path(td) / "codex"
            codex.write_text("fixture")
            for outcome, code in cases:
                effect = outcome if isinstance(outcome, BaseException) else None
                with self.subTest(outcome=type(outcome).__name__, code=code), \
                     self._native_review_context(
                         "codex", "openai", "gpt-5.6-sol", codex
                     ) as context, \
                     mock.patch.object(server, "JOBS_DIR", Path(td) / "jobs"), \
                     mock.patch.object(
                         server.subprocess, "run",
                         side_effect=effect,
                         return_value=None if effect else outcome,
                     ):
                    result = server.native_review_result(context)
                self.assertFalse(result["ok"], result)
                if code:
                    self.assertEqual(result["code"], code)
            missing_binary = Path(td) / "missing-codex"
            with mock.patch.object(
                server, "load_repos", return_value={"scratch": str(self.scratch)}
            ), mock.patch.object(
                server, "resolve_launcher_binary_now", return_value=missing_binary
            ), mock.patch.object(
                server, "native_review_result",
                side_effect=AssertionError("missing launcher must not reach native review"),
            ) as spawn:
                missing_status, missing, _ = server.dispatch(
                    "POST", "/v1/review/run", {}, True,
                    json.dumps({
                        "repo": "scratch", "launcher": "codex", "provider": "openai",
                        "model": "gpt-5.6-sol", "prompt": "ticket",
                    }).encode(),
                )
            spawn.assert_not_called()
        self.assertEqual(missing_status, 501, missing)
        self.assertIsInstance(missing, dict)
        assert isinstance(missing, dict)
        self.assertEqual(missing["code"], "not_implemented")

    def test_grok_review_reuses_the_direct_acp_launcher(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            grok = Path(td) / "grok"
            grok.write_text("fixture")
            job_id = "ab" * 16
            (jobs / job_id).mkdir(parents=True)

            def fake_acp(job, binary, *, mode):
                self.assertEqual(binary.parent, Path("/proc/self/fd"))
                self.assertEqual(mode, "review")
                self.assertEqual(job["response_webhook_url"], "https://example.invalid/resume")
                path = jobs / job["job_id"]
                path.joinpath("last-message.txt").write_text(json.dumps({
                    "status": "ok", "sha": None, "pr_url": None,
                    "summary": "VERDICT: PASS",
                }))
                return 0, False, "grok-review-session", None

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "run_grok_acp", fake_acp):
                server.write_job({
                    "job_id": job_id,
                    "response_webhook_url": "https://example.invalid/resume",
                })
                with self._native_review_context(
                    "grok", "xai", "grok-4.6", grok, job_id=job_id
                ) as context:
                    out = server.native_review_result(context)
        self.assertEqual(out["verdict"], "PASS", out)
        self.assertEqual((out["launcher"], out["model"]), ("grok", "grok-4.6"))

    def test_async_ops_declare_webhook_fields_in_the_spec(self):
        # The graph sent response_webhook_url to reviewRun, the server read it,
        # and it still went down the sync path: ReviewRunRequest did not declare
        # the field, so Rewst dropped it building the request. The wait node
        # then blocked on a callback nobody would ever send. Any operation the
        # graph hands a webhook to must declare it, or the field vanishes
        # between the graph and the server with no error anywhere.
        spec = json.loads(server.openapi_bytes())
        for route in ("/v1/review/run", "/v1/test/run", "/v1/script/run", "/v1/agent/run"):
            body = spec["paths"][route]["post"]["requestBody"]["content"]["application/json"]["schema"]
            name = body["$ref"].rsplit("/", 1)[-1]
            props = spec["components"]["schemas"][name].get("properties", {})
            self.assertIn("response_webhook_url", props, route)
            self.assertIn("response_webhook_token", props, route)

    def test_herdr_tab_lingers_before_closing(self):
        # A finished job's tab used to close the instant it completed, so the
        # output you wanted to read disappeared as it arrived.
        closed = []
        with mock.patch.object(server, "herdr_cli", lambda args, **kw: closed.append(args) or {}):
            server.herdr_close_tab_later("tab-1", 60)
            self.assertTrue(server.herdr_tab_lingering("tab-1"))
            self.assertEqual(closed, [], "must not close while lingering")
            # prune leaves a lingering done tab alone...
            listed = {"tabs": [{"tab_id": "tab-1", "label": "gw-x", "agent_status": "done"}]}
            with mock.patch.object(server, "herdr_cli", lambda args, **kw: listed if args[:2] == ["tab", "list"] else closed.append(args) or {}):
                server.herdr_prune_job_tabs()
            self.assertEqual(closed, [], "prune closed a tab still inside its window")
            # ...and reaps it once the window passes
            with server.HERDR_LINGER_LOCK:
                server.HERDR_LINGER["tab-1"] = time.time() - 1
            self.assertFalse(server.herdr_tab_lingering("tab-1"))
            with mock.patch.object(server, "herdr_cli", lambda args, **kw: listed if args[:2] == ["tab", "list"] else closed.append(args) or {}):
                server.herdr_prune_job_tabs()
            self.assertIn(["tab", "close", "tab-1"], closed)

    def test_herdr_linger_is_configurable(self):
        self.assertGreaterEqual(server.HERDR_TAB_LINGER_SECONDS, 0)

    def test_git_checkout_path_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                for branch in ("../foo", "/etc/passwd", "foo/../../etc", "--force"):
                    status, payload, _ = server.dispatch(
                        "POST",
                        "/v1/git/checkout",
                        {},
                        True,
                        json.dumps({"repo": "scratch", "branch": branch}).encode(),
                    )
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["code"], "bad_branch")

    def test_git_restore_keeps_untracked_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            (repo / "README").write_text("failed experiment\n")
            subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, capture_output=True)
            (repo / "untracked.txt").write_text("keep me\n")
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "POST", "/v1/git/restore", {}, True, json.dumps({"repo": "scratch"}).encode()
                )
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["ok"])
            self.assertEqual((repo / "README").read_text(), "hi\n")
            self.assertEqual((repo / "untracked.txt").read_text(), "keep me\n")

    def test_git_restore_cleans_untracked_when_requested(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            (repo / ".gitignore").write_text("ignored.txt\n")
            subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "ignore fixture"], check=True, capture_output=True)
            (repo / "README").write_text("failed experiment\n")
            (repo / "untracked.txt").write_text("remove me\n")
            (repo / "ignored.txt").write_text("keep ignored\n")
            untracked_dir = repo / "untracked-dir"
            untracked_dir.mkdir()
            (untracked_dir / "file.txt").write_text("remove me too\n")
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/restore",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "clean_untracked": True}).encode(),
                )
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["ok"])
            self.assertEqual((repo / "README").read_text(), "hi\n")
            self.assertFalse((repo / "untracked.txt").exists())
            self.assertFalse(untracked_dir.exists())
            self.assertEqual((repo / "ignored.txt").read_text(), "keep ignored\n")

    def test_git_restore_rejects_missing_and_path_repo(self):
        with mock.patch.object(server, "load_repos", return_value={"scratch": "/tmp/scratch"}):
            for request, code in (({}, "missing_repo"), ({"repo": "/tmp/scratch"}, "bad_repo")):
                status, payload, _ = server.dispatch(
                    "POST", "/v1/git/restore", {}, True, json.dumps(request).encode()
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], code)

    def test_git_restore_rejects_force(self):
        with mock.patch.object(server, "load_repos", return_value={"scratch": "/tmp/scratch"}):
            for field in ("force", "force_with_lease"):
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/restore",
                    {},
                    True,
                    json.dumps({"repo": "scratch", field: True}).encode(),
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "force_rejected")
            for field in ("remote", "ref", "delete"):
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/restore",
                    {},
                    True,
                    json.dumps({"repo": "scratch", field: "rejected"}).encode(),
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "extra_field")

    def test_git_commit_add_and_staged_only_require_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            (repo / "tracked.txt").write_text("a\n")
            (repo / "left.txt").write_text("leave me\n")
            subprocess.run(["git", "-C", str(repo), "add", "left.txt"], check=True, capture_output=True)
            requests = (
                {"repo": "scratch", "message": "add tracked", "add": ["tracked.txt"]},
                {"repo": "scratch", "message": "staged only"},
            )
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                for request in requests:
                    status, payload, _ = server.dispatch(
                        "POST", "/v1/git/commit", {}, True, json.dumps(request).encode()
                    )
                    self.assertEqual((status, payload["code"]), (400, "writer_job_required"), payload)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
                    check=True, capture_output=True, text=True,
                ).stdout.strip(),
                "1",
            )

    def test_git_commit_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/commit",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "message": "   "}).encode(),
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "empty_message")
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/commit",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "message": "x", "add": ["../secret"]}).encode(),
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "bad_path")
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/commit",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "message": "x", "add": ["/tmp/x"]}).encode(),
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "bad_path")
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/commit",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "message": "x", "add_all": True}).encode(),
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "add_all_disabled")
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/commit",
                    {},
                    True,
                    json.dumps({"repo": "/tmp/graphwing-not-a-repo", "message": "x"}).encode(),
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "bad_repo")

    def test_git_push_local_remote_and_force_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                for ref in ("+main", "main:main", ":main", "--force"):
                    status, payload, _ = server.dispatch(
                        "POST",
                        "/v1/git/push",
                        {},
                        True,
                        json.dumps({"repo": "scratch", "ref": ref}).encode(),
                    )
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["code"], "bad_ref")
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/push",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "force": True, "ref": "main"}).encode(),
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "force_rejected")
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/push",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "remote": "--force", "ref": "main"}).encode(),
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "bad_remote")
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/push",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "ref": "main"}).encode(),
                )
                self.assertEqual(status, 200, payload)
                self.assertTrue(payload["ok"])

    def test_script_unknown_name(self):
        status, payload, _ = server.dispatch(
            "POST", "/v1/script/run", {}, True, b'{"name":"nope"}'
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "unknown_script")

    def test_script_compile_server(self):
        status, payload, _ = server.dispatch(
            "POST", "/v1/script/run", {}, True, b'{"name":"compile-server"}'
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["returncode"], 0)
        self.assertEqual(payload["name"], "compile-server")

    def test_script_selftest_accepted_async(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            with mock.patch.object(server, "JOBS_DIR", jobs):
                with mock.patch.object(server, "enqueue_script", lambda job: None):
                    status, payload, _ = server.dispatch(
                        "POST", "/v1/script/run", {}, True, b'{"name":"graphwing-selftest"}'
                    )
            self.assertEqual(status, 202, payload)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "queued")
            self.assertEqual(payload["name"], "graphwing-selftest")
            self.assertRegex(payload["job_id"], r"^[0-9a-f]{32}$")

    def test_webhook_forces_sync_named_commands_async_and_delivers_receipts(self):
        for kind, returncode, expected_status in (
            ("script", 0, "ok"),
            ("test", 1, "error"),
            ("rr", 0, "ok"),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                jobs = root / "jobs"
                spec = {
                    "sync": {
                        "name": "sync",
                        "argv": [sys.executable, "-c", f"raise SystemExit({returncode})"],
                        "cwd": root,
                        "timeout_seconds": 5,
                        "async": False,
                    }
                }
                body = json.dumps({
                    "name": "sync",
                    "response_webhook_url": "https://example.invalid/resume",
                    "response_webhook_token": "token",
                }).encode()
                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.object(server, "post_receipt", return_value={"ok": True, "status": 200}) as posted, \
                     mock.patch.object(server, "enqueue_script") as enqueued:
                    status, payload = server.named_cmd_run(
                        kind, spec, body, f"{kind}.json", f"unknown_{kind}", {}
                    )
                    self.assertEqual(status, 202, payload)
                    enqueued.assert_called_once()
                    queued = enqueued.call_args.args[0]
                    self.assertEqual(queued["job_id"], payload["job_id"])
                    server.run_script_job(queued["job_id"])
                    saved = server.read_job(payload["job_id"])
                self.assertEqual(saved["receipt"]["status"], expected_status)
                posted.assert_called_once_with(
                    "https://example.invalid/resume", saved["receipt"], token="token"
                )

    def test_script_argv_forbidden(self):
        status, payload, _ = server.dispatch(
            "POST",
            "/v1/script/run",
            {},
            True,
            b'{"name":"compile-server","argv":["rm","-rf","/"]}',
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "argv_forbidden")

    def test_herdr_disabled_skips_log(self):
        with mock.patch.object(server, "herdr_log") as log:
            server.dispatch("GET", "/v1/git/status", {"repo": ["nope"]}, True, b"")
        log.assert_not_called()

    def test_herdr_announce_git_status(self):
        os.environ["GRAPHWING_HERDR"] = "1"
        self.addCleanup(lambda: os.environ.__setitem__("GRAPHWING_HERDR", "0"))
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                with mock.patch.object(server, "herdr_log") as log:
                    server.dispatch("GET", "/v1/git/status", {"repo": ["scratch"]}, True, b"")
                    for _ in range(40):
                        if log.called:
                            break
                        __import__("time").sleep(0.05)
                    self.assertTrue(log.called)
                    self.assertIn("/v1/git/status", log.call_args[0][0])

    def test_herdr_skips_health(self):
        os.environ["GRAPHWING_HERDR"] = "1"
        self.addCleanup(lambda: os.environ.__setitem__("GRAPHWING_HERDR", "0"))
        with mock.patch.object(server, "herdr_log") as log:
            server.dispatch("GET", "/v1/health", {}, False, b"")
            __import__("time").sleep(0.15)
        log.assert_not_called()

    def test_herdr_job_done_closes_tab(self):
        os.environ["GRAPHWING_HERDR"] = "1"
        self.addCleanup(lambda: os.environ.__setitem__("GRAPHWING_HERDR", "0"))
        calls: list[list[str]] = []

        def fake_cli(args: list[str], timeout: int = 10):
            calls.append(list(args))
            return {}

        job = {
            "job_id": "ab" * 16,
            "status": "completed",
            "herdr_tab_id": "w1:t9",
            "receipt": {"summary": "ok"},
        }
        with mock.patch.object(server, "herdr_cli", fake_cli):
            with mock.patch.object(server, "herdr_log"):
                with mock.patch.object(server, "HERDR_TAB_LINGER_SECONDS", 0):
                    server.herdr_job_done(job)
        # linger 0 closes inline; the default keeps the tab open to be read
        self.assertIn(["tab", "close", "w1:t9"], calls)

    def test_herdr_prune_closes_done_tabs(self):
        calls: list[list[str]] = []

        def fake_cli(args: list[str], timeout: int = 10):
            calls.append(list(args))
            if args[:2] == ["tab", "list"]:
                return {
                    "tabs": [
                        {"label": "gw-a-deadbeef", "tab_id": "w1:t5", "agent_status": "done"},
                        {"label": "graph", "tab_id": "w1:t3", "agent_status": "idle"},
                        {"label": "gw-a-working1", "tab_id": "w1:t6", "agent_status": "working"},
                    ]
                }
            return {}

        with mock.patch.object(server, "herdr_cli", fake_cli):
            server.herdr_prune_job_tabs()
        self.assertIn(["tab", "close", "w1:t5"], calls)
        self.assertNotIn(["tab", "close", "w1:t3"], calls)
        self.assertNotIn(["tab", "close", "w1:t6"], calls)

    def test_stack_status_dispatch_diagnostics_success_unknown_not_configured(self):
        stack_spec = {
            "demo": {"cwd": Path("."), "compose_file": "", "health": [], "ports": [8645]}
        }
        compose = {"ok": True, "stdout": json.dumps({"Name": "demo", "State": "running"})}
        with mock.patch.object(server, "load_stacks", return_value=(stack_spec, {8645})), \
             mock.patch.object(server, "run_cmd", return_value=compose):
            success = server.dispatch("GET", "/v1/stack/status", {"stack": ["demo"]}, True, b"")
            unknown = server.dispatch("GET", "/v1/stack/status", {"stack": ["nope"]}, True, b"")
        with mock.patch.object(server, "load_stacks", return_value=({}, set())):
            not_configured = server.dispatch("GET", "/v1/stack/status", {}, True, b"")

        for (status, payload, _), code in (
            (success, "none"), (unknown, "unknown_stack"), (not_configured, "not_configured")
        ):
            self.assertEqual(
                set(payload["diagnostic"]),
                {"diagnostic_version", "category", "component", "code", "evidence", "summary"},
            )
            self.assertEqual(payload["diagnostic"]["code"], code)
        self.assertEqual(success[0], 200)
        self.assertTrue(success[1]["healthy"])
        self.assertEqual(unknown[0], 400)
        self.assertEqual(not_configured[0], 501)

    def test_stack_status_diagnostic_precedence(self):
        stack_spec = {
            "demo": {
                "cwd": Path("."), "compose_file": "", "ports": [],
                "health": [
                    {"name": "ok", "url": "http://127.0.0.1:1/ok"},
                    {"name": "first-red", "url": "http://127.0.0.1:1/first"},
                    {"name": "second-red", "url": "http://127.0.0.1:1/second"},
                ],
            }
        }
        health = (
            {"ok": True, "diagnostic": server.compact_diagnostic("none")},
            {"ok": False, "diagnostic": server.compact_diagnostic("health_unreachable")},
            {"ok": False, "diagnostic": server.compact_diagnostic("health_bad_status")},
        )
        with mock.patch.object(server, "load_stacks", return_value=(stack_spec, set())):
            with mock.patch.object(server, "run_cmd", return_value={
                "ok": False, "code": "cmd_failed", "error": "raw compose failure",
            }), mock.patch.object(server, "probe_loopback", side_effect=health):
                compose_failure = server.stack_status("demo")
            with mock.patch.object(server, "run_cmd", return_value={
                "ok": True, "stdout": json.dumps({"Name": "demo", "State": "running"}),
            }), mock.patch.object(server, "probe_loopback", side_effect=health):
                first_health_failure = server.stack_status("demo")

        no_health = {**stack_spec["demo"], "health": []}
        with mock.patch.object(server, "load_stacks", return_value=({"demo": no_health}, set())), \
             mock.patch.object(server, "run_cmd", return_value={
                 "ok": True, "stdout": json.dumps({"Name": "demo", "State": "exited"}),
             }):
            otherwise_unhealthy = server.stack_status("demo")

        self.assertEqual(compose_failure["diagnostic"]["code"], "cmd_failed")
        self.assertEqual(first_health_failure["diagnostic"]["code"], "health_unreachable")
        self.assertEqual(otherwise_unhealthy["diagnostic"]["code"], "stack_unhealthy")

    def test_stack_and_port_probes_emit_only_compact_structured_diagnostics(self):
        stack_spec = {
            "demo": {
                "cwd": Path("."), "compose_file": "", "health": [], "ports": [8645]
            }
        }
        failed = {
            "ok": False, "code": "cmd_failed", "status": 400,
            "error": "command failed: /secret/provider/bin", "stdout": "raw", "stderr": "trace",
        }
        with mock.patch.object(server, "load_stacks", return_value=(stack_spec, {8645})), \
             mock.patch.object(server, "run_cmd", return_value=failed):
            stack = server.stack_status("demo")
            ports = server.port_check("8645")
        for got in (stack["diagnostic"], ports["diagnostic"], ports["ports"][0]["diagnostic"]):
            self.assertEqual(
                (got["diagnostic_version"], got["category"], got["component"], got["code"]),
                ("diagnostic-v1", "local_infrastructure", "command", "cmd_failed"),
            )
            dumped = json.dumps(got)
            for raw in ("/secret", "stdout", "stderr", "trace"):
                self.assertNotIn(raw, dumped)

    def test_missing_local_probe_binaries_are_not_provider_failures(self):
        stack_spec = {
            "demo": {"cwd": Path("."), "compose_file": "", "health": [], "ports": [8645]}
        }
        with mock.patch.object(server, "load_stacks", return_value=(stack_spec, {8645})), \
             mock.patch.object(server.subprocess, "run", side_effect=FileNotFoundError):
            stack = server.dispatch(
                "GET", "/v1/stack/status", {"stack": ["demo"]}, True, b""
            )
            ports = server.dispatch(
                "GET", "/v1/port/check", {"port": ["8645"]}, True, b""
            )

        self.assertEqual((stack[0], ports[0]), (200, 200))
        diagnostics = (
            stack[1]["diagnostic"],
            ports[1]["diagnostic"],
            ports[1]["ports"][0]["diagnostic"],
        )
        for diagnostic in diagnostics:
            self.assertEqual(diagnostic["code"], "local_binary_missing")
            self.assertEqual(diagnostic["category"], "local_infrastructure")
            self.assertNotEqual(diagnostic["category"], "provider_availability")

    def test_loopback_probe_network_diagnostic_is_structured_not_exception_text(self):
        with mock.patch.object(server, "urlopen", side_effect=ConnectionRefusedError("token=secret")):
            got = server.probe_loopback("http://127.0.0.1:9999/health")
        self.assertFalse(got["ok"])
        self.assertEqual(got["diagnostic"]["category"], "local_infrastructure")
        self.assertEqual(got["diagnostic"]["component"], "network")
        self.assertNotIn("token=secret", json.dumps(got["diagnostic"]))

        response = mock.MagicMock(status=503)
        response.__enter__.return_value = response
        with mock.patch.object(server, "urlopen", return_value=response):
            bad_status = server.probe_loopback("http://127.0.0.1:9999/health")
        self.assertEqual(bad_status["diagnostic"]["category"], "local_infrastructure")
        self.assertEqual(bad_status["diagnostic"]["component"], "health")
        self.assertEqual(bad_status["diagnostic"]["code"], "health_bad_status")

    def test_port_check_dispatch_diagnostics_success_unknown_not_configured(self):
        with mock.patch.object(server, "load_stacks", return_value=({}, {8645})), \
             mock.patch.object(server, "run_cmd", return_value={"ok": True, "stdout": "LISTEN"}):
            success = server.dispatch("GET", "/v1/port/check", {"port": ["8645"]}, True, b"")
            unknown = server.dispatch("GET", "/v1/port/check", {"port": ["1"]}, True, b"")
        with mock.patch.object(server, "load_stacks", return_value=({}, set())):
            not_configured = server.dispatch("GET", "/v1/port/check", {"port": ["8645"]}, True, b"")

        for (status, payload, _), code in (
            (success, "none"), (unknown, "unknown_port"), (not_configured, "not_configured")
        ):
            self.assertEqual(
                set(payload["diagnostic"]),
                {"diagnostic_version", "category", "component", "code", "evidence", "summary"},
            )
            self.assertEqual(payload["diagnostic"]["code"], code)
        self.assertEqual(success[0], 200)
        self.assertEqual(success[1]["ports"][0]["diagnostic"]["code"], "none")
        self.assertEqual(unknown[0], 400)
        self.assertEqual(not_configured[0], 501)
        self.assertFalse(hasattr(server, "port_listening"))

    def test_test_run_compile(self):
        status, payload, _ = server.dispatch(
            "POST", "/v1/test/run", {}, True, b'{"name":"graphwing-compile"}'
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["kind"], "test")
        self.assertEqual(payload.get("compact"), "ok")

    def test_test_run_always_fail_has_compact_not_only_stdout(self):
        status, payload, _ = server.dispatch(
            "POST", "/v1/test/run", {}, True, b'{"name":"always-fail"}'
        )
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload.get("compact"))
        self.assertLessEqual(len(payload["compact"]), server.COMPACT_MAX_CHARS)

    def test_load_tests_repo_short_name_cwd(self):
        extra = Path(self.scratch) / ".githooks"
        extra.mkdir()
        (extra / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
        recipes = {
            "tests": [
                {
                    "name": "local-gates",
                    "argv": ["bash", ".githooks/pre-commit"],
                    "cwd": "scratch",
                    "timeout_seconds": 15,
                    "async": False,
                },
                {
                    "name": "missing-repo",
                    "argv": ["true"],
                    "cwd": "nope",
                    "timeout_seconds": 5,
                    "async": False,
                },
            ]
        }
        path = Path(self.scratch) / "tests.json"
        path.write_text(json.dumps(recipes))
        with mock.patch.object(server, "TESTS_PATH", path):
            catalog = server.load_tests({"scratch": str(self.scratch)})
        self.assertIn("local-gates", catalog)
        self.assertEqual(catalog["local-gates"]["cwd"], Path(self.scratch).resolve())
        self.assertNotIn("missing-repo", catalog)

    def test_test_run_unknown_and_argv(self):
        status, payload, _ = server.dispatch(
            "POST", "/v1/test/run", {}, True, b'{"name":"go-test-everything"}'
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "unknown_test")
        status, payload, _ = server.dispatch(
            "POST", "/v1/test/run", {}, True, b'{"name":"graphwing-compile","argv":["id"]}'
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "argv_forbidden")

    def test_rr_not_configured(self):
        with mock.patch.object(server, "RR_PATH", Path("/nope/rr.json")):
            status, payload, _ = server.dispatch(
                "POST", "/v1/rr/run", {}, True, b'{"name":"status"}'
            )
        self.assertEqual(status, 501)
        self.assertEqual(payload["code"], "not_configured")

    def test_rr_plugin_local_recipe(self):
        with tempfile.TemporaryDirectory() as td:
            recipes = Path(td) / "rr.json"
            recipes.write_text(
                json.dumps(
                    {
                        "recipes": [
                            {
                                "name": "echo",
                                "argv": ["python3", "-c", "print('plugin-ok')"],
                                "cwd": ".",
                                "timeout_seconds": 10,
                                "async": False,
                            }
                        ]
                    }
                )
            )
            with mock.patch.object(server, "RR_PATH", recipes):
                status, payload, _ = server.dispatch(
                    "POST", "/v1/rr/run", {}, True, b'{"name":"echo"}'
                )
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["kind"], "rr")
                self.assertIn("plugin-ok", payload.get("stdout") or "")
                status, payload, _ = server.dispatch(
                    "POST", "/v1/rr/run", {}, True, b'{"name":"make-test"}'
                )
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], "unknown_rr")

    def test_load_key_from_env(self):
        with mock.patch.dict(os.environ, {"GRAPHWING_KEY": "env-secret-key"}):
            with mock.patch.object(server, "KEY_PATH", Path("/nope/api.key")):
                self.assertEqual(server.load_key(), b"env-secret-key")

    def test_openapi_url_from_tunnel_meta(self):
        with tempfile.TemporaryDirectory() as td:
            meta = Path(td) / "cloudflared-meta.json"
            meta.write_text(json.dumps({"hostname": "gw.example.com"}) + "\n")
            with mock.patch.object(server, "HOME", Path(td)):
                os.environ.pop("GRAPHWING_PUBLIC_URL", None)
                self.assertEqual(server.public_base_url(), "https://gw.example.com")
        with mock.patch.dict(os.environ, {"GRAPHWING_PUBLIC_URL": "https://override.example"}):
            self.assertEqual(server.public_base_url(), "https://override.example")

    def test_graphs_fan_in_targets_are_joins(self):
        graphs = Path(__file__).resolve().parent / "graphs"
        for graph_path in sorted(graphs.glob("*.json")):
            graph = json.loads(graph_path.read_text())
            node_rows = graph["spec"]["nodes"]
            edge_rows = graph["spec"]["edges"]
            node_ids = [node.get("id") for node in node_rows]
            edge_ids = [edge.get("id") for edge in edge_rows]
            self.assertTrue(all(isinstance(uid, str) and uid for uid in node_ids), graph_path.name)
            self.assertTrue(all(isinstance(uid, str) and uid for uid in edge_ids), graph_path.name)
            self.assertEqual(len(node_ids), len(set(node_ids)), f"{graph_path.name} duplicate node id")
            self.assertEqual(len(edge_ids), len(set(edge_ids)), f"{graph_path.name} duplicate edge id")
            nodes = {node["id"]: node for node in node_rows}
            incoming: dict[str, int] = {}
            outgoing: dict[str, list[str]] = {}
            for edge in edge_rows:
                self.assertIn(edge.get("source"), nodes, f"{graph_path.name} dangling source {edge}")
                self.assertIn(edge.get("target"), nodes, f"{graph_path.name} dangling target {edge}")
                incoming[edge["target"]] = incoming.get(edge["target"], 0) + 1
                outgoing.setdefault(edge["source"], []).append(edge["target"])
            for nid, count in incoming.items():
                if count < 2:
                    continue
                ntype = nodes[nid]["type"]
                self.assertTrue(
                    ntype.startswith("logic.join"),
                    f"{graph_path.name} node {nid} type={ntype} has {count} incoming edges",
                )
            color = {nid: 0 for nid in nodes}
            stack: list[str] = []

            def dfs(uid: str) -> None:
                color[uid] = 1
                stack.append(uid)
                for vid in outgoing.get(uid, []):
                    if color[vid] == 1:
                        self.fail(
                            f"{graph_path.name} unbounded cycle: {' -> '.join(stack + [vid])}"
                        )
                    if color[vid] == 0:
                        dfs(vid)
                stack.pop()
                color[uid] = 2

            for nid in nodes:
                if color[nid] == 0:
                    dfs(nid)

    def test_catalog_docs_inputs_and_publish_all_writeback_cover_every_graph(self):
        root = Path(__file__).resolve().parent
        paths = sorted((root / "graphs").glob("*.json"))
        graphs = {path.stem: json.loads(path.read_text()) for path in paths}
        slugs = {graph["slug"] for graph in graphs.values()}
        self.assertEqual(len(slugs), len(graphs), "graph slugs must be unique")

        field_ref = re.compile(r"(?<![A-Za-z0-9_])CTX\.INPUT\.([a-z][a-z0-9_]*)(?![A-Za-z0-9_-])")
        def input_fields(graph):
            found = set()
            def walk(value):
                if isinstance(value, str):
                    found.update(field_ref.findall(value))
                elif isinstance(value, list):
                    for item in value: walk(item)
                elif isinstance(value, dict):
                    for item in value.values(): walk(item)
            for node in graph["spec"]["nodes"]:
                walk(node.get("config", {}))
            return found

        inputs = {stem: input_fields(graph) for stem, graph in graphs.items()}
        for stem, graph in graphs.items():
            payload = re.search(r"\bPayload:\s*([^.]*)", graph["description"])
            if payload:
                enumerated = set(re.findall(r"\b[a-z][a-z0-9_]*\b", payload.group(1))) - {"optional"}
                self.assertEqual(enumerated, inputs[stem], f"{stem} stale description payload")
        for stem in ("implement-slice", "pr-drive"):
            description = graphs[stem]["description"].lower()
            self.assertNotIn("payload:", description)
            self.assertIn("declared trigger inputs", description)
            self.assertIn("one bounded", description)
            self.assertIn("known-disabled", description)

        def table_rows(path):
            rows = {}
            for line in path.read_text().splitlines():
                cells = [cell.strip() for cell in line.split("|")[1:-1]]
                if cells and re.fullmatch(r"`graphwing-[a-z0-9-]+`", cells[0]):
                    rows[cells[0].strip("`")] = cells
            return rows

        catalogs = {}
        for rel in ("README.md", "graphs/README.md"):
            rows = catalogs[rel] = table_rows(root / rel)
            self.assertEqual(set(rows), slugs, rel)

        for rel, catalog in catalogs.items():
            for stem, graph in graphs.items():
                documented = set(re.findall(r"`([a-z][a-z0-9_]*)`", catalog[graph["slug"]][1]))
                self.assertEqual(documented, inputs[stem], f"{rel} {graph['slug']} inputs")

        from scripts import publish_graphs as pg

        seen, saved = [], []
        def upsert(_mcp, _name, slug, _description, _spec):
            seen.append(slug)
            return f"workflow-{slug}", f"version-{slug}", slug

        install = {
            "org_id": "tenant-fixture", "instance_id": "instance-fixture",
            "pr_status_repo": "repo-fixture", "custom_integration_id": "integration-fixture",
        }
        with (
            mock.patch.object(sys, "argv", ["publish_graphs.py", "--only", "all", "--no-run"]),
            mock.patch.object(pg, "load_install", return_value=install.copy()),
            mock.patch.object(pg, "rewst_mcp", return_value="fixture-token"),
            mock.patch.object(pg, "public_openapi_url", return_value="https://fixture.invalid/openapi.json"),
            mock.patch.object(pg, "upsert_workflow", side_effect=upsert),
            mock.patch.object(pg, "save_install", side_effect=lambda value: saved.append(deepcopy(value))),
            mock.patch("builtins.print"),
        ):
            pg.main()
        self.assertEqual(len(seen), len(set(seen)), "all mode published a graph more than once")
        self.assertEqual(set(seen), slugs)
        self.assertEqual(len(saved), 1)
        for stem, graph in graphs.items():
            self.assertEqual(saved[0][stem.replace("-", "_")]["slug"], graph["slug"])

    def test_rollout_docs_name_proof_levels_commands_and_safety_limits(self):
        root = Path(__file__).resolve().parent
        readme_source = (root / "README.md").read_text()
        operator_source = (root / "docs" / "HUMAN-LOOP.md").read_text()
        operator = operator_source.lower()
        for label in ("fixture proof", "catalog/import proof", "published proof", "live canary proof"):
            self.assertIn(label, operator)
        for command in ("graphwing_home=. python3 test_server.py", "python3 -m py_compile", "git diff --check"):
            self.assertIn(command, operator)
        for truth in (
            "fable and terra", "expiry or provenance mismatch parks", "not os isolation",
            "persisted final named-test job", "nonempty", "no visual proof",
        ):
            self.assertIn(truth, operator)

        focused = (
            "test_server.DispatchTests.test_graphs_fan_in_targets_are_joins",
            "test_server.DispatchTests.test_catalog_docs_inputs_and_publish_all_writeback_cover_every_graph",
            "test_server.DispatchTests.test_rollout_docs_name_proof_levels_commands_and_safety_limits",
            "test_server.DispatchTests.test_implement_slice_initial_availability_fallback_topology",
            "test_server.DispatchTests.test_implement_slice_recovery_is_only_a_later_initial_boundary",
            "test_server.DispatchTests.test_pr_drive_has_one_final_evidence_leg_and_no_merge_bypass",
            "test_server.DispatchTests.test_pr_status_reports_remote_only_states_without_side_effects",
            "test_server.CodeOffTests.test_codeoff_graph_is_bounded_waited_fanned_in_and_terminal_gated",
        )
        documented = tuple(re.findall(
            r"(?m)^\s{2}(test_server\.(?:DispatchTests|CodeOffTests)\.test_[a-z0-9_]+)(?:\s+\\)?\s*$",
            operator_source,
        ))
        self.assertEqual(documented, focused)
        loader = unittest.TestLoader()
        self.assertEqual(loader.loadTestsFromNames(documented).countTestCases(), len(focused))
        self.assertFalse(loader.errors, loader.errors)

        topology_caption = (
            "*Topology-only: webhook starts are known-disabled; use the documented "
            "manual/form/API starts for `implement-slice` and `pr-drive`, and the API "
            "start for `pr-status`.*"
        )
        self.assertIn(
            '<img src="docs/images/architecture.svg" '
            'alt="Your computer runs Graphwing. Rewst in the cloud calls it. '
            'You start work and merge." width="100%">\n</p>\n\n' + topology_caption,
            readme_source,
        )
        self.assertIn(
            "| `graphwing-pr-status` | `pr` | Read remote-only PR state through an API "
            "start. Webhook starts remain disabled. |",
            readme_source,
        )

        catalog_source = (root / "graphs" / "README.md").read_text()
        self.assertNotIn("`kick_url` starts the next", catalog_source)
        self.assertIn(
            "Use a manual/form or `/workflows/{slug}/run` start with "
            '`{ "input": {...} }` for `implement-slice` and `pr-drive`; only the API '
            "start is proven for `pr-status`.",
            catalog_source,
        )
        self.assertIn(
            "`.github/workflows/pr-drive-doorbell.yml` has two independent defects: "
            "the current webhook-to-`CTX.INPUT` mapping is broken, and it sends `pr` "
            "while `pr-drive` consumes `pr_number`.",
            catalog_source,
        )
        doorbell_source = (root / ".github" / "workflows" / "pr-drive-doorbell.yml").read_text()
        doorbell_body = next(line for line in doorbell_source.splitlines() if "printf -v body" in line)
        self.assertIn('"pr":"', doorbell_body)
        self.assertNotIn('"pr_number":"', doorbell_body)

        pr_drive = json.loads((root / "graphs" / "pr-drive.json").read_text())
        nodes = {node["id"]: node for node in pr_drive["spec"]["nodes"]}
        self.assertEqual(nodes["view"]["config"]["number"], "{{ CTX.INPUT.pr_number }}")
        self.assertIn(
            "| `graphwing-pr-status` | `pr` | API-started remote-only classification; "
            "webhook starts remain disabled; performs no named-test "
            "job or git write |",
            catalog_source,
        )

        using_source = (root / "docs" / "USING.md").read_text()
        self.assertIn("generic catalog wiring", using_source)
        self.assertIn("cannot continue", using_source)
        self.assertIn(
            "| `graphwing-pr-status` | Read remote-only PR state with "
            '`{ "input": { "pr": 3624 } }` through an API start. Webhook '
            "starts remain disabled because they do not create `CTX.INPUT`. |",
            using_source,
        )

        pr_status_path = root / "graphs" / "pr-status.json"
        pr_status_raw = pr_status_path.read_bytes()
        pr_status = json.loads(pr_status_raw)
        old_description = (
            "Read-only remote PR status. Never creates named-test evidence or writes git; "
            "remote_ready still requires final named-test evidence before merge. Payload: pr."
        )
        expected_description = (
            "Read-only remote PR status. Start through the API with input.pr; webhook starts "
            "are disabled because they do not create CTX.INPUT. Never "
            "creates named-test evidence or writes git; remote_ready still requires final "
            "named-test evidence before merge. Payload: pr."
        )
        self.assertEqual(pr_status["description"], expected_description)
        self.assertNotEqual(pr_status["description"], old_description)
        compact = pr_status_raw.rstrip(b"\r\n")
        spec_marker = b'"spec":'
        spec_bytes = compact[compact.index(spec_marker) + len(spec_marker):-1]
        self.assertEqual(json.loads(spec_bytes), pr_status["spec"])
        self.assertEqual(
            hashlib.sha256(spec_bytes).hexdigest(),
            "7d2024cae9a8897bcbb1c1ff7ba7272fd800ea1e3e1be1ddd542221099e18d1b",
        )

    def test_pr_status_graph_is_read_only_and_webhook_disabled(self):
        graph_path = Path(__file__).resolve().parent / "graphs" / "pr-status.json"
        graph = json.loads(graph_path.read_text())
        nodes = graph["spec"]["nodes"]
        webhook = next(node for node in nodes if node["id"] == "hook")
        self.assertFalse(webhook["config"]["enabled"])
        banned = ("agent", "git/commit", "git/push", "git/checkout", "git/restore")
        node_types = [node["type"].lower() for node in nodes]
        for term in banned:
            self.assertFalse(any(term in node_type for node_type in node_types), term)
        self.assertTrue(any("gh/pr/view" in node_type for node_type in node_types))
        self.assertTrue(any("gh/pr/checks" in node_type for node_type in node_types))

    def test_implement_slice_keeps_files_on_red(self):
        graph_path = Path(__file__).resolve().parent / "graphs" / "implement-slice.json"
        graph = json.loads(graph_path.read_text())
        node_types = [node["type"].lower() for node in graph["spec"]["nodes"]]
        self.assertFalse(any("git/restore" in node_type for node_type in node_types))
        edges = {edge["id"]: edge for edge in graph["spec"]["edges"]}
        self.assertEqual(edges["e18"]["source"], "if_test_ok")
        self.assertEqual(edges["e18"]["sourceHandle"], "fail")
        self.assertEqual(edges["e18"]["target"], "wait2")
        self.assertEqual(edges["e31"]["source"], "if_test_ok2")
        self.assertEqual(edges["e31"]["sourceHandle"], "fail")
        self.assertEqual(edges["e31"]["target"], "wait3")
        self.assertEqual(edges["e31i"]["target"], "join_wait_human")
        self.assertEqual(edges["e_join_wait_human"]["target"], "wait_human")
        self.assertEqual(edges["e_rev1_nack"]["target"], "wait_rn1")
        self.assertEqual(edges["e_rev2_nack"]["target"], "wait_rn2")
        self.assertEqual(edges["e_r1b_nack"]["target"], "join_wait_human")
        self.assertEqual(edges["e_r2b_nack"]["target"], "join_wait_human")
        rn1 = next(node for node in graph["spec"]["nodes"] if node["id"] == "agent_rn1")
        self.assertIn("TASKS.wait_rev1.request.body.compact", rn1["config"]["prompt"])
        self.assertEqual(edges["e_rn1_tbad"]["target"], "join_wait_human")
        self.assertEqual(edges["e_rn2_tbad"]["target"], "join_wait_human")
        self.assertNotIn("switch_retry", {node["id"] for node in graph["spec"]["nodes"]})
        self.assertEqual(edges["e_e2e_auto"]["target"], "walk_e2e_recovery_input")
        self.assertEqual(edges["e_walk_e2e_ok"]["target"], "join_slices_complete")
        agent2 = next(node for node in graph["spec"]["nodes"] if node["id"] == "agent2")
        self.assertIn("CTX.fallback_receipt.session_identity", agent2["config"]["session_identity"])
        self.assertIn("CTX.receipt.session_identity", agent2["config"]["session_identity"])
        self.assertIn("CTX.fallback_receipt.resume_job_id", agent2["config"]["resume_job_id"])
        self.assertIn("CTX.receipt.resume_job_id", agent2["config"]["resume_job_id"])
        self.assertIn("TASKS.wait_test.request.body.summary", agent2["config"]["prompt"])
        self.assertIn("Continue this slice", agent2["config"]["prompt"])
        self.assertIn("TASKS.ticket_head.data.text", agent2["config"]["prompt"])
        self.assertNotIn("CTX.INPUT.prompt", agent2["config"]["prompt"])

    def test_correction_receipt_templates_have_exact_precedence_and_gates(self):
        graph = json.loads((Path(__file__).resolve().parent / "graphs" / "implement-slice.json").read_text())
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        edges = {(edge["source"], edge.get("sourceHandle"), edge["target"]) for edge in graph["spec"]["edges"]}
        agent2 = nodes["agent2"]["config"]
        self.assertEqual(agent2["session_identity"], "{{ CTX.fallback_receipt.session_identity | default(CTX.receipt.session_identity) }}")
        self.assertEqual(agent2["resume_job_id"], "{{ CTX.fallback_receipt.resume_job_id | default(CTX.receipt.resume_job_id) }}")
        for field in ("launcher", "provider", "model"):
            self.assertEqual(agent2[field], f"{{{{ CTX.fallback_receipt.session_identity.{field} | default(CTX.receipt.session_identity.{field}) }}}}")
        chains = {
            "agent_rn1": ("receipt3", "receipt2", "fallback_receipt", "receipt"),
            "agent_rn2": ("receipt_rn1", "receipt3", "receipt2", "fallback_receipt", "receipt"),
        }
        for node_id, aliases in chains.items():
            config = nodes[node_id]["config"]
            for field in ("session_identity", "resume_job_id"):
                expected = f"CTX.{aliases[0]}.{field}" + "".join(f" | default(CTX.{alias}.{field})" for alias in aliases[1:])
                self.assertEqual(config[field], f"{{{{ {expected} }}}}")
            for field in ("launcher", "provider", "model"):
                expected = f"CTX.{aliases[0]}.session_identity.{field}" + "".join(
                    f" | default(CTX.{alias}.session_identity.{field})" for alias in aliases[1:]
                )
                self.assertEqual(config[field], f"{{{{ {expected} }}}}")
        commit = nodes["commit"]["config"]["writer_job_id"]
        aliases = ("receipt_rn2", "receipt_rn1", "receipt3", "receipt2", "fallback_receipt", "receipt")
        expected = f"CTX.{aliases[0]}.job_id" + "".join(f" | default(CTX.{alias}.job_id)" for alias in aliases[1:])
        self.assertEqual(commit, f"{{{{ {expected} }}}}")
        for record, wait, filt in (
            ("record3", "wait3", "if_receipt_ok3"),
            ("record_rn1", "wait_rn1", "if_receipt_ok_rn1"),
            ("record_rn2", "wait_rn2", "if_receipt_ok_rn2"),
        ):
            mappings = {m["output"]: m["expression"] for m in nodes[record]["config"]["mappings"]}
            self.assertEqual(mappings, {
                "status": {"kind": "getField", "path": f"TASKS.{wait}.request.body.status"},
                "job_id": {"kind": "getField", "path": f"TASKS.{wait}.request.body.job_id"},
                "resume_job_id": {"kind": "getField", "path": f"TASKS.{wait}.request.body.job_id"},
                "session_identity": {"kind": "getField", "path": f"TASKS.{wait}.request.body.session_identity"},
                "usage": {"kind": "getField", "path": f"TASKS.{wait}.request.body.usage"},
                "usage_diagnostic": {"kind": "getField", "path": f"TASKS.{wait}.request.body.usage_diagnostic"},
            })
            self.assertEqual(nodes[filt]["config"], {"group": "AND", "rules": [{"path": "status", "op": "equals", "value": "ok"}]})
            self.assertIn((wait, "out", record), edges)
            self.assertIn((record, "out", filt), edges)

    def test_pr_drive_keeps_files_on_red(self):
        graph_path = Path(__file__).resolve().parent / "graphs" / "pr-drive.json"
        graph = json.loads(graph_path.read_text())
        node_types = [node["type"].lower() for node in graph["spec"]["nodes"]]
        self.assertFalse(any("git/restore" in node_type for node_type in node_types))
        edges = {edge["id"]: edge for edge in graph["spec"]["edges"]}
        self.assertEqual(edges["e18d"]["target"], "herdr_test_fail")
        fail = next(node for node in graph["spec"]["nodes"] if node["id"] == "herdr_test_fail")
        self.assertIn("files kept", fail["label"])
        commit = next(node for node in graph["spec"]["nodes"] if node["id"] == "fix_commit")
        self.assertEqual(commit["config"]["writer_job_id"], "{{ CTX.receipt.job_id }}")

    def test_implement_slice_writer_sees_ticket_file(self):
        graph_path = Path(__file__).resolve().parent / "graphs" / "implement-slice.json"
        graph = json.loads(graph_path.read_text())
        form = next(node for node in graph["spec"]["nodes"] if node["id"] == "form")
        inputs = form["config"]["inputs"]
        self.assertTrue(inputs["index"]["required"])
        self.assertFalse(inputs["ticket"].get("required"))
        self.assertNotIn("prompt", inputs)
        head = next(node for node in graph["spec"]["nodes"] if node["id"] == "ticket_head")
        self.assertIn("file/head", head["type"])
        # `rel`, not `path`: the connector eats a query param called `path`.
        self.assertEqual(head["config"]["rel"], "{{ TASKS.frontier.data.path }}")
        self.assertNotIn("path", head["config"])
        agent = next(node for node in graph["spec"]["nodes"] if node["id"] == "agent")
        self.assertEqual(agent["config"]["prompt"], "{{ TASKS.ticket_head.data.text }}")
        self.assertNotIn("CTX.INPUT.prompt", agent["config"]["prompt"])
        edges = {edge["id"]: edge for edge in graph["spec"]["edges"]}
        self.assertEqual(edges["e7"]["target"], "frontier")
        self.assertEqual(edges["e7j"]["target"], "route")
        self.assertEqual(edges["e7k"]["target"], "normal_primary_candidate")
        self.assertEqual(edges["e_normal_primary_candidate"]["target"], "ticket_head")
        self.assertEqual(edges["e7b"]["source"], "ticket_head")
        self.assertEqual(edges["e7b"]["target"], "recovery_marker")
        self.assertEqual(edges["e_primary_start"]["target"], "selected_route")
        self.assertEqual(edges["e_selected_route"]["target"], "wait")
        self.assertEqual(edges["e7c"]["target"], "ticket_fail")
        self.assertEqual(edges["e_commit_out"]["target"], "complete")
        self.assertEqual(edges["e12"]["target"], "walk_recovery_input")
        self.assertNotIn("e_join_walk", edges)
        self.assertNotIn("join_walk", {node["id"] for node in graph["spec"]["nodes"]})
        self.assertEqual(edges["e_walk_recovery_input"]["target"], "walk")
        self.assertEqual(edges["e7h"]["target"], "join_e2e")
        self.assertEqual(edges["e12d"]["target"], "join_e2e")
        self.assertEqual(edges["e_join_e2e"]["target"], "e2e")
        commit = next(node for node in graph["spec"]["nodes"] if node["id"] == "commit")
        self.assertEqual(commit["config"]["add"], "{{ CTX.INPUT.index }}")
        writer_job = commit["config"]["writer_job_id"]
        for alias in ("receipt_rn2", "receipt_rn1", "receipt3", "receipt2", "fallback_receipt", "receipt"):
            self.assertIn(f"CTX.{alias}.job_id", writer_job)
        self.assertNotIn("TASKS.", writer_job)
        self.assertEqual(edges["e_rn1_out"]["target"], "record_rn1")
        self.assertEqual(edges["e_rn2_out"]["target"], "record_rn2")
        self.assertEqual(edges["e31c"]["target"], "record3")
        agent = next(n for n in graph["spec"]["nodes"] if n["id"] == "agent")
        self.assertEqual(agent["config"]["launcher"], "{{ CTX.selected_route.launcher }}")
        agent2 = next(n for n in graph["spec"]["nodes"] if n["id"] == "agent2")
        self.assertIn("CTX.fallback_receipt.session_identity.launcher", agent2["config"]["launcher"])
        self.assertIn("CTX.receipt.session_identity.launcher", agent2["config"]["launcher"])
        self.assertEqual(edges["e7j"]["target"], "route")
        self.assertEqual(edges["e_commit_first"]["target"], "join_switch_rev")
        self.assertEqual(edges["e_join_switch_rev"]["target"], "map_switch_rev")
        self.assertEqual(edges["e_map_switch_rev"]["target"], "switch_rev")
        review1 = next(n for n in graph["spec"]["nodes"] if n["id"] == "review1")
        self.assertIn("review/run", review1["type"])

    def test_slice_route_table(self):
        def route(work_kind, cls="mechanical", size="M", **extra):
            body = {"class": cls, "size": size, "work_kind": work_kind, **extra}
            status, payload, _ = server.dispatch(
                "POST", "/v1/slice/route", {}, True, json.dumps(body).encode()
            )
            self.assertEqual(status, 200, payload)
            return payload

        go = route("go_coding", size="S")
        self.assertEqual(
            (go["launcher"], go["provider"], go["model"]),
            ("codex", "openai", "gpt-5.6-sol"),
        )
        self.assertEqual(go["reviewer1_launcher"], "none")
        self.assertEqual(go["max_turns"], 10)

        go_sensitive = route("go_coding", cls="sensitive")
        self.assertEqual(
            (go_sensitive["reviewer1_launcher"], go_sensitive["reviewer1_model"]),
            ("claude", "claude-sonnet-5"),
        )
        self.assertEqual(
            (go_sensitive["reviewer2_launcher"], go_sensitive["reviewer2_model"]),
            ("grok", "grok-4.6"),
        )

        typescript = route("typescript_coding")
        self.assertEqual(
            (typescript["launcher"], typescript["provider"], typescript["model"]),
            ("claude", "anthropic", "claude-opus-5"),
        )
        self.assertEqual(
            (typescript["reviewer1_launcher"], typescript["reviewer1_model"]),
            ("codex", "gpt-5.6-sol"),
        )

        research = route("research_ops")
        self.assertEqual(
            (research["launcher"], research["provider"], research["model"]),
            ("grok", "xai", "grok-4.6"),
        )
        self.assertEqual(
            (research["reviewer1_launcher"], research["reviewer1_model"]),
            ("codex", "gpt-5.6-sol"),
        )

        research_sensitive = route("research_ops", cls="sensitive")
        self.assertEqual(
            (research_sensitive["reviewer1_launcher"], research_sensitive["reviewer1_model"]),
            ("codex", "gpt-5.6-sol"),
        )
        self.assertEqual(
            (research_sensitive["reviewer2_launcher"], research_sensitive["reviewer2_model"]),
            ("claude", "claude-opus-5"),
        )

        bumped = route("go_coding", size="S", ac_count=6)
        self.assertEqual((bumped["size"], bumped["size_floor"]), ("M", "S"))

        status, payload, _ = server.dispatch(
            "POST", "/v1/slice/route", {}, True,
            b'{"class":"mechanical","size":"M","work_kind":"nope"}',
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "bad_work_kind")
        status, payload, _ = server.dispatch(
            "POST", "/v1/slice/route", {}, True,
            b'{"class":"mechanical","size":"M"}',
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "bad_work_kind")
        for body, code in (
            ({"class": "nope", "size": "M", "work_kind": "go_coding"}, "bad_class"),
            ({"class": "mechanical", "size": "XL", "work_kind": "go_coding"}, "bad_size"),
        ):
            status, payload, _ = server.dispatch(
                "POST", "/v1/slice/route", {}, True, json.dumps(body).encode()
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], code)

    def test_route_provenance_is_declared_in_openapi(self):
        spec = json.loads(server.openapi_bytes())
        request = spec["components"]["schemas"]["SliceRouteRequest"]
        self.assertIn("work_kind", request["required"])
        route = spec["components"]["schemas"]["SliceRoute"]["properties"]
        for field in (
            "route_version", "launcher", "provider", "model", "effort", "reason",
            "reviewer1_launcher", "reviewer1_provider", "reviewer1_model",
            "reviewer1_effort", "reviewer1_reason", "reviewer2_launcher",
            "reviewer2_provider", "reviewer2_model", "reviewer2_effort",
            "reviewer2_reason", "role", "primary_route_version", "primary_reason",
            "fallback_reason", "fallback_code", "selected_alternate_route",
        ):
            self.assertIn(field, route)
        self.assertEqual(
            set(route["route_version"]["enum"]),
            {"normal-v1", "availability-fallback-v1"},
        )
        fallback_request = spec["components"]["schemas"]["SliceRouteFallbackRequest"]
        self.assertEqual(set(fallback_request["required"]), {"work_kind", "primary_route", "primary_receipt"})

    def test_provider_recovery_contract_is_declared_in_openapi(self):
        spec = json.loads(server.openapi_bytes())
        operation = spec["paths"]["/v1/slice/route/recovery"]["post"]
        self.assertEqual(operation["operationId"], "sliceRouteRecovery")
        self.assertEqual(
            operation["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/SliceRouteRecoveryRequest",
        )
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/SliceRouteRecovery",
        )
        request = spec["components"]["schemas"]["SliceRouteRecoveryRequest"]
        self.assertEqual(
            set(request["required"]),
            {
                "work_kind", "primary_route", "primary_receipt", "fallback_route",
                "fallback_receipt",
            },
        )
        self.assertIn("fresh_primary_receipt", request["properties"])
        response = spec["components"]["schemas"]["SliceRouteRecovery"]
        self.assertEqual(
            response["properties"]["recovery_version"]["enum"],
            ["provider-recovery-v1"],
        )
        self.assertEqual(
            set(response["properties"]["decision"]["enum"]),
            {"primary_recovered", "fallback_retained"},
        )
        evidence = spec["components"]["schemas"]["ProviderRecoveryEvidence"]
        self.assertFalse(evidence["additionalProperties"])
        self.assertEqual(
            set(evidence["required"]),
            {"recovery_version", "basis", "failure_code", "primary", "fallback"},
        )
        self.assertEqual(
            set(evidence["properties"]["failure_code"]["enum"]),
            set(server.PROVIDER_AVAILABILITY_CODES),
        )
        identity = spec["components"]["schemas"]["RecoveryRouteIdentity"]
        self.assertFalse(identity["additionalProperties"])
        self.assertEqual(
            set(identity["required"]),
            {"job_id", "route_version", "launcher", "provider", "model", "effective_effort", "launcher_version"},
        )
        continued = spec["components"]["schemas"]["SliceContinueRequest"]["properties"]
        for field in (
            "recovery_version", "prior_primary_route", "prior_primary_receipt",
            "prior_fallback_route", "prior_fallback_receipt", "fresh_primary_receipt",
        ):
            self.assertIn(field, continued)
        self.assertEqual(
            set(spec["components"]["schemas"]["SliceRoute"]["properties"]["route_version"]["enum"]),
            {"normal-v1", "availability-fallback-v1"},
        )

    def test_parse_review_verdict(self):
        self.assertEqual(server.parse_review_verdict("noise\nVERDICT: PASS\n")[0], "PASS")
        self.assertEqual(server.parse_review_verdict("VERDICT: NACK\nbad seam")[0], "NACK")
        self.assertEqual(server.parse_review_verdict("nope")[0], "NACK")

    def test_agent_run_rejects_bad_launcher(self):
        status, payload, _ = server.dispatch(
            "POST",
            "/v1/agent/run",
            {},
            True,
            b'{"prompt":"x","launcher":"unknown"}',
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "bad_launcher")


    def test_receipt_normalization_requires_complete_execution_profile_agreement(self):
        profile = _fixture_execution_profile(
            "codex", "openai", "gpt-5.6-sol", requested="high", source="explicit"
        )
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "a" * 40, "native_session_id": "native-123",
        }
        job = {
            "job_id": "a1" * 16, "kind": "agent", "launcher": "codex",
            "provider": "openai", "model": "gpt-5.6-sol", **profile,
            "repo": "scratch", "session_identity": identity,
        }
        valid = server.normalize_receipt(job, {"status": "ok"}, 0, False)
        self.assertEqual((valid["status"], valid["failure_code"]), ("ok", "none"), valid)
        self.assertEqual(valid["session_identity"], identity)

        drifts = (
            {"requested_effort": "default", "effort_source": "launcher_default"},
            {"requested_effort": "default"},
            {"effort_source": "launcher_default"},
        )
        for drift in drifts:
            with self.subTest(drift=drift):
                contradictory = deepcopy(job)
                contradictory["session_identity"].update(drift)
                receipt = server.normalize_receipt(
                    contradictory, {"status": "ok", "summary": "TOKEN_SECRET"}, 0, False
                )
                self.assertEqual(
                    (receipt["status"], receipt["failure_code"]),
                    ("error", "session_provenance_invalid"),
                    receipt,
                )
                self.assertIsNone(receipt["session_identity"])
                self.assertTrue(
                    all(receipt[field] is None for field in server.AGENT_PROFILE_FIELDS), receipt
                )
                self.assertNotIn("TOKEN_SECRET", json.dumps(receipt))

    def test_native_agent_jobs_have_explicit_kind_and_classifier_rejects_absence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            binary = root / "codex"
            binary.write_text("fixture")
            body = json.dumps({
                "prompt": "bounded", "cwd": "scratch", "launcher": "codex",
                "provider": "openai", "model": "gpt-5.6-sol",
            }).encode()
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=binary), \
                 mock.patch.object(server, "enqueue_agent"):
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, body)
            self.assertEqual(status, 202, payload)
            self.assertEqual(payload.get("kind"), "agent", payload)
            stored = json.loads((jobs / payload["job_id"] / "job.json").read_text())
            self.assertEqual(stored.get("kind"), "agent", stored)
            self.assertTrue(server.is_agent_job_record(stored))
            for kind in (None, "alien", 7, {"agent": True}):
                candidate = deepcopy(stored)
                if kind is None:
                    candidate.pop("kind", None)
                else:
                    candidate["kind"] = kind
                with self.subTest(kind=kind):
                    self.assertFalse(server.is_agent_job_record(candidate))
                    with mock.patch.object(server, "read_job", return_value=candidate):
                        get_status, get_payload, _ = server.dispatch(
                            "GET", f"/v1/agent/jobs/{stored['job_id']}", {}, True, b""
                        )
                        wait_status, wait_payload = server.agent_job_wait(json.dumps({
                            "job_id": stored["job_id"], "timeout_seconds": 1,
                        }).encode())
                    self.assertEqual((get_status, wait_status), (404, 404))
                    self.assertEqual((get_payload["code"], wait_payload["code"]), ("not_found", "not_found"))
                    callback = server.normalize_receipt(candidate, {"status": "ok"}, 0, False)
                    self.assertEqual(
                        (callback["status"], callback["failure_code"]),
                        ("error", "session_provenance_invalid"),
                    )

    def test_non_agent_records_cannot_authorize_resume_fallback_or_recovery(self):
        profile = _fixture_execution_profile(
            "codex", "openai", "gpt-5.6-sol", requested="high", source="route"
        )
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "a" * 40, "native_session_id": "native-123",
        }
        resume_id = "a2" * 16
        resume_record = {
            "job_id": resume_id, "kind": "agent", "status": "completed", "repo": "scratch",
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **profile, "session_identity": identity,
            "receipt": {
                "status": "ok", "job_id": resume_id, "launcher": "codex",
                "provider": "openai", "model": "gpt-5.6-sol", **profile,
                "session_identity": identity,
            },
        }
        hostile_kinds = ("test", "script", "rr", "review", "alien", None, 9)
        provenance_mutations = (
            ("repo", "https://private.example/repo?token=TOKEN_SECRET"),
            ("repo", "../private\nTOKEN_SECRET"), ("repo", "other"),
            ("branch", "other"), ("starting_head", "b" * 40),
        )
        for kind in hostile_kinds:
            prior = deepcopy(resume_record)
            if kind is None:
                prior.pop("kind")
            else:
                prior["kind"] = kind
            with self.subTest(surface="resume", kind=kind), \
                 mock.patch.object(server, "read_job", return_value=prior):
                error = server.validate_native_resume_provenance(
                    {"resume_job_id": resume_id}, identity, "codex"
                )
            self.assertEqual(error.get("code"), "untraceable_resume_session", error)
        for field, value in provenance_mutations:
            prior = deepcopy(resume_record)
            prior[field] = value
            with self.subTest(surface="resume_provenance", field=field, value=value), \
                 mock.patch.object(server, "read_job", return_value=prior):
                error = server.validate_native_resume_provenance(
                    {"resume_job_id": resume_id}, identity, "codex"
                )
            self.assertEqual(error.get("code"), "untraceable_resume_session", error)
            self.assertNotIn("TOKEN_SECRET", json.dumps(error))

        route = server.slice_route_lookup("mechanical", "M", work_kind="go_coding")
        fallback_identity = {
            **identity,
            "native_session_id": None,
            "route_execution_profile": route["writer_execution_profile"],
        }
        fallback_receipt = {
            "status": "error", "job_id": "a3" * 16, "role": "primary",
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **profile, "session_identity": fallback_identity,
            "failure_class": "provider_availability", "failure_code": "provider_rate_limit",
            "failover_eligible": True,
        }
        fallback_record = {
            "job_id": fallback_receipt["job_id"], "kind": "agent", "status": "failed",
            "repo": "scratch", "launcher": "codex", "provider": "openai",
            "model": "gpt-5.6-sol", **profile, "session_identity": fallback_identity,
            "receipt": {key: value for key, value in fallback_receipt.items() if key != "role"},
        }
        request = {
            "class": "mechanical", "size": "M", "work_kind": "go_coding",
            "primary_route": route, "primary_receipt": fallback_receipt,
        }
        for kind in hostile_kinds:
            stored = deepcopy(fallback_record)
            if kind is None:
                stored.pop("kind")
            else:
                stored["kind"] = kind
            with self.subTest(surface="fallback", kind=kind), \
                 mock.patch.object(server, "read_job", return_value=stored):
                status, payload = server.derive_slice_fallback_route(request)
            self.assertEqual((status, payload.get("code")), (400, "primary_receipt_mismatch"), payload)
        for field, value in provenance_mutations:
            stored = deepcopy(fallback_record)
            stored[field] = value
            with self.subTest(surface="fallback_provenance", field=field, value=value), \
                 mock.patch.object(server, "read_job", return_value=stored):
                status, payload = server.derive_slice_fallback_route(request)
            self.assertEqual((status, payload.get("code")), (400, "primary_receipt_mismatch"), payload)
            self.assertNotIn("TOKEN_SECRET", json.dumps(payload))

        for kind in hostile_kinds:
            with self.subTest(surface="recovery", kind=kind), tempfile.TemporaryDirectory() as td:
                jobs = Path(td) / "jobs"
                recovery = self._provider_recovery_fixture(jobs, "provider_network")
                primary_path = jobs / recovery["primary_receipt"]["job_id"] / "job.json"
                primary = json.loads(primary_path.read_text())
                if kind is None:
                    primary.pop("kind", None)
                else:
                    primary["kind"] = kind
                primary_path.write_text(json.dumps(primary) + "\n")
                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.object(
                         server, "resolve_launcher_binary_now",
                         side_effect=AssertionError("provider access forbidden"),
                     ):
                    status, payload = server.derive_slice_recovery_route(recovery)
                self.assertEqual(
                    (status, payload.get("code")), (400, "primary_receipt_mismatch"), payload
                )
        for field, value in provenance_mutations:
            with self.subTest(surface="recovery_provenance", field=field, value=value), tempfile.TemporaryDirectory() as td:
                jobs = Path(td) / "jobs"
                recovery = self._provider_recovery_fixture(jobs, "provider_network")
                primary_path = jobs / recovery["primary_receipt"]["job_id"] / "job.json"
                primary = json.loads(primary_path.read_text())
                primary[field] = value
                primary_path.write_text(json.dumps(primary) + "\n")
                with mock.patch.object(server, "JOBS_DIR", jobs), \
                     mock.patch.object(
                         server, "resolve_launcher_binary_now",
                         side_effect=AssertionError("provider access forbidden"),
                     ):
                    status, payload = server.derive_slice_recovery_route(recovery)
                self.assertEqual(
                    (status, payload.get("code")), (400, "primary_receipt_mismatch"), payload
                )
                self.assertNotIn("TOKEN_SECRET", json.dumps(payload))

    def test_agent_trust_boundaries_bind_top_level_repository_provenance(self):
        profile = _fixture_execution_profile("codex", "openai", "gpt-5.6-sol")
        identity = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **_fixture_identity_profile(profile), "repo": "scratch", "branch": "main",
            "starting_head": "a" * 40, "native_session_id": "native-123",
        }
        job_id = "a4" * 16
        valid = {
            "job_id": job_id, "kind": "agent", "status": "completed", "repo": "scratch",
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            **profile, "session_identity": identity,
            "receipt": {
                "status": "ok", "job_id": job_id, "launcher": "codex",
                "provider": "openai", "model": "gpt-5.6-sol", **profile,
                "session_identity": identity, "failure_class": "none", "failure_code": "none",
                "failover_eligible": False,
            },
        }
        self.assertTrue(server.is_agent_job_record(valid))
        self.assertEqual(server.public_job(valid)["repo"], "scratch")
        mutations = (
            ("repo", "https://private.example/repo?token=TOKEN_SECRET"),
            ("repo", "other"),
            ("branch", "other"),
            ("starting_head", "b" * 40),
        )
        for field, value in mutations:
            hostile = deepcopy(valid)
            hostile[field] = value
            with self.subTest(field=field, value=value):
                self.assertFalse(server.is_agent_job_record(hostile))
                with mock.patch.object(server, "read_job", return_value=hostile):
                    get_status, get_payload, _ = server.dispatch(
                        "GET", f"/v1/agent/jobs/{job_id}", {}, True, b""
                    )
                    wait_status, wait_payload = server.agent_job_wait(json.dumps({
                        "job_id": job_id, "timeout_seconds": 1,
                    }).encode())
                rendered = json.dumps({"get": get_payload, "wait": wait_payload})
                self.assertEqual((get_status, wait_status), (404, 404))
                self.assertNotIn("TOKEN_SECRET", rendered)
                self.assertNotIn("private.example", rendered)
                callback = server.normalize_receipt(
                    hostile, {"status": "ok", "summary": "TOKEN_SECRET"}, 0, False
                )
                self.assertEqual(
                    (callback["status"], callback["failure_code"]),
                    ("error", "session_provenance_invalid"),
                    callback,
                )
                self.assertIsNone(callback["session_identity"])
                self.assertNotIn("TOKEN_SECRET", json.dumps(callback))

    def test_branch_runtime_and_openapi_share_the_documented_safe_subset(self):
        corpus = {
            "main": True,
            "feature/model-routing": True,
            "release/2026.08": True,
            "user_name/topic-1": True,
            "feature/a+b": False,
            "#123": False,
            "a!b": False,
            "a=b": False,
            "a,b": False,
            "a;b": False,
            "a(b)": False,
            "éclair": False,
            "feature.lock/child": False,
            "FEATURE.LOCK/child": False,
            "feature/child.lock": False,
            "a..b": False,
            "a@{b": False,
            "a b": False,
            "a~b": False,
            "a^b": False,
            "a:b": False,
            "a?b": False,
            "a*b": False,
            "a[b": False,
            "a\\b": False,
            "/a": False,
            "a/": False,
            "a//b": False,
            ".a": False,
            "a/.b": False,
            "a.": False,
            "@": False,
            "-branch": False,
            "bad\x1fbranch": False,
            "bad\x7fbranch": False,
            "b" * 255: True,
            "b" * 256: False,
        }
        spec = json.loads(server.openapi_bytes())
        branch_schema = spec["components"]["schemas"]["BranchName"]
        pattern = re.compile(branch_schema["pattern"])
        self.assertEqual((branch_schema["minLength"], branch_schema["maxLength"]), (1, 255))
        for name, expected in corpus.items():
            with self.subTest(name=name):
                schema_accepts = (
                    branch_schema["minLength"] <= len(name) <= branch_schema["maxLength"]
                    and pattern.fullmatch(name) is not None
                )
                self.assertEqual(server.valid_branch(name), expected)
                self.assertEqual(schema_accepts, expected)
        self.assertEqual(
            spec["components"]["schemas"]["SessionIdentity"]["properties"]["branch"],
            {"$ref": "#/components/schemas/BranchName"},
        )
        self.assertEqual(
            spec["components"]["schemas"]["GitCheckoutRequest"]["properties"]["branch"],
            {"$ref": "#/components/schemas/BranchName"},
        )
        self.assertEqual(
            spec["components"]["schemas"]["SliceContinueRequest"]["properties"]["branch"],
            {"$ref": "#/components/schemas/BranchName"},
        )


class CodeOffTests(unittest.TestCase):
    """Issue 67 is a bounded protocol; no fixture may launch a model or use network."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "codeoff@test")
        self._git("config", "user.name", "code-off-test")
        (self.repo / "README.md").write_text("base\n")
        (self.repo / "delete-me.txt").write_text("delete\n")
        (self.repo / "rename-me.txt").write_text("rename\n")
        self._git("add", ".")
        self._git("commit", "-m", "base")
        self.base_sha = self._git("rev-parse", "HEAD").stdout.strip()
        self.records = root / "codeoffs"
        self.workspaces = root / "codeoff-workspaces"
        self.jobs = root / "jobs"
        self.tests_path = root / "tests.json"
        self.tests_path.write_text(json.dumps({"tests": [
            {"name": "fixture-pass", "argv": [sys.executable, "-c", "import subprocess; from pathlib import Path; assert subprocess.check_output(['git','rev-parse','--is-inside-work-tree'], text=True).strip() == 'true'; assert Path('README.md').is_file()"], "cwd": "scratch", "timeout_seconds": 10, "async": False},
            {"name": "fixture-fail", "argv": [sys.executable, "-c", "raise SystemExit(7)"], "cwd": "scratch", "timeout_seconds": 10, "async": False},
            {"name": "fixture-git-fail", "argv": [sys.executable, "-c", "import subprocess; assert subprocess.check_output(['git','rev-parse','--is-inside-work-tree'], text=True).strip() == 'true'; raise SystemExit(7)"], "cwd": "scratch", "timeout_seconds": 10, "async": False},
            {"name": "fixture-import", "argv": [sys.executable, "-c", "import candidate_module; assert candidate_module.VALUE == 1"], "cwd": "scratch", "timeout_seconds": 10, "async": False},
            {"name": "fixture-mutates", "argv": [sys.executable, "-c", "from pathlib import Path; Path('recipe-output.tmp').write_text('mutation\\n')"], "cwd": "scratch", "timeout_seconds": 10, "async": False},
        ]}) + "\n")
        self.production_manifest = deepcopy(server.CODEOFF_MODEL_MANIFEST)
        proven = deepcopy(server.CODEOFF_MODEL_MANIFEST)
        proven["as_of"] = "2026-08-26"
        proven["max_age_days"] = 36500
        for identity in proven["models"].values():
            identity["runnable"] = True
            identity["proven"] = True
            identity["reason"] = "fixture-approved"
        patches = (
            mock.patch.object(server, "CODEOFFS_DIR", self.records),
            mock.patch.object(server, "CODEOFF_WORKSPACES_DIR", self.workspaces),
            mock.patch.object(server, "JOBS_DIR", self.jobs),
            mock.patch.object(server, "TESTS_PATH", self.tests_path),
            mock.patch.object(server, "CODEOFF_MODEL_MANIFEST", proven),
            mock.patch.object(server, "load_repos", return_value={"scratch": str(self.repo)}),
            mock.patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        )

    def _body(self, experiment_id="experiment-0001", seed=None, **overrides):
        body = {
            "experiment_id": experiment_id,
            "repo": "scratch",
            "base_sha": self.base_sha,
            "seed": seed or ("00" * 32),
            "category": "backend",
            "tags": ["testing", "api", "testing"],
            "category_source": "issue-67-fixture",
            "prompt": "Implement the fixture without network.\n",
            "tests": ["fixture-pass"],
            "toolchain": {"python": sys.version.split()[0], "git": "fixture"},
            "budgets": {"author_seconds": 60, "judge_seconds": 60, "test_seconds": 20, "max_turns": 4},
            "commit_message": "feat: promote code-off winner",
        }
        body.update(overrides)
        return body

    def _post(self, path, body, authed=True):
        return server.dispatch("POST", path, {}, authed, json.dumps(body).encode())[:2]

    def _prepare(self, experiment_id="experiment-0001", **overrides):
        status, payload = self._post("/v1/code-off/prepare", self._body(experiment_id, **overrides))
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "prepared", payload)
        return payload

    def _launch_done(self, experiment_id, slot):
        manifest = json.loads((self.records / experiment_id / "manifest.json").read_text())
        identity = manifest["identities"][slot]
        body = self._agent_body(experiment_id, slot)
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "enqueue_agent") as enqueue:
            status, payload = self._post("/v1/agent/run", body)
        self.assertEqual(status, 202, payload)
        enqueue.assert_called_once()
        job = server.read_job(payload["job_id"])
        self.assertIsNotNone(job)
        identity = dict(job["session_identity"], native_session_id=f"fixture-{slot}")
        execution_identity = self._execution_identity(identity)
        job.update({
            "status": "completed", "started_at": "2026-08-26T12:00:00Z",
            "finished_at": "2026-08-26T12:00:01Z", "session_identity": identity,
            "execution_identity": execution_identity,
            "receipt": {
                "status": "ok", "job_id": job["job_id"], "session_identity": identity,
                "launcher": job["launcher"], "provider": job["provider"],
                "model": job["model"],
                **{key: job[key] for key in server.AGENT_PROFILE_FIELDS[3:]},
                "execution_identity": execution_identity, "summary": "fixture",
            },
        })
        server.write_job(job)
        self.assertTrue(server._codeoff_record_terminal_manifest(job))
        return job["job_id"]

    @staticmethod
    def _execution_identity(session_identity):
        source = {
            "claude": "claude-result-v1",
            "codex": "codex-rollout-v1",
            "grok": "grok-acp-v1",
        }[session_identity["launcher"]]
        return {"version": "code-off-execution-identity-v1", "source": source, **{
            key: session_identity[key] for key in ("launcher", "provider", "model", "native_session_id")
        }}

    @staticmethod
    def _codex_stdout(native_session_id):
        receipt = json.dumps({"status": "ok", "sha": None, "pr_url": None, "summary": "done"})
        return "".join(json.dumps(event) + "\n" for event in (
            {"type": "thread.started", "thread_id": native_session_id},
            {"type": "item.completed", "item": {"type": "agent_message", "text": receipt}},
        ))

    def _run_native_terminal_fixture(
        self, experiment_id, slot, stdout, *, native_session_id, before_run=None,
        expect_native_session=True,
    ):
        body = self._agent_body(experiment_id, slot)
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "enqueue_agent"):
            status, launched = self._post("/v1/agent/run", body)
        self.assertEqual(status, 202, launched)
        job_id = launched["job_id"]
        path = self.jobs / job_id
        (path / "stdout.log").write_text(stdout)
        if before_run is not None:
            before_run(server.read_job(job_id))

        process = mock.Mock(pid=76, returncode=0)
        process.wait.return_value = 0

        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "spawn_writer", return_value=(process, None)), \
             mock.patch.object(server, "herdr_job_done"):
            server.run_agent_job(job_id)
        job = server.read_job(job_id)
        expected_session = native_session_id if expect_native_session else None
        self.assertEqual(job["session_identity"]["native_session_id"], expected_session, job)
        return job

    def _agent_body(self, experiment_id, slot):
        manifest = json.loads((self.records / experiment_id / "manifest.json").read_text())
        identity = manifest["identities"][slot]
        return {
            "codeoff_workspace": {"experiment_id": experiment_id, "slot": slot},
            "launcher": identity["launcher"], "provider": identity["provider"],
            "model": identity["exact_model"],
            "max_turns": manifest["budgets"]["max_turns"],
            "run_budget_seconds": (
                manifest["budgets"]["author_seconds"] if slot.startswith("author-")
                else manifest["budgets"]["judge_seconds"]
            ),
        }

    def _freeze_and_test(self, experiment_id, slot):
        job_id = self._launch_done(experiment_id, slot)
        status, frozen = self._post("/v1/code-off/candidate", {
            "experiment_id": experiment_id, "slot": slot, "action": "freeze", "job_id": job_id,
        })
        self.assertEqual(status, 200, frozen)
        status, tested = self._post("/v1/code-off/candidate", {
            "experiment_id": experiment_id, "slot": slot, "action": "test",
        })
        self.assertEqual(status, 200, tested)
        return frozen, tested

    def _ready_to_finalize(self, experiment_id="experiment-0001"):
        self._prepare(experiment_id)
        winner = server.codeoff_workspace_path(experiment_id, "author-1")
        (winner / "README.md").write_text("winner\n")
        for slot in ("author-1", "author-2"):
            frozen, tested = self._freeze_and_test(experiment_id, slot)
            self.assertTrue(tested["tests_pass"], (frozen, tested))
        status, blind = self._post("/v1/code-off/blind", {"experiment_id": experiment_id})
        self.assertEqual(status, 200, blind)
        for judge in ("judge-fable", "judge-1", "judge-2"):
            self._judge_done(experiment_id, judge, "author-1")
        status, aggregate = self._post("/v1/code-off/aggregate", {"experiment_id": experiment_id})
        self.assertEqual(status, 200, aggregate)
        self.assertTrue(aggregate["promotion_eligible"], aggregate)
        return aggregate

    def _assert_no_staging(self, experiment_id):
        workspace = self.workspaces / experiment_id
        if workspace.exists():
            self.assertFalse([path for path in workspace.iterdir() if path.name.startswith(".")], list(workspace.iterdir()))
        self.assertFalse([path for path in self.repo.parent.iterdir() if ".codeoff-" in path.name])
        listed = self._git("worktree", "list", "--porcelain").stdout
        self.assertNotIn(".verify-", listed)
        self.assertNotIn(".codeoff-", listed)

    def _target_snapshot(self):
        return server.codeoff_tree_snapshot(self.repo, self.base_sha)["manifest_hash"]

    def _ready_for_judges(self, experiment_id="experiment-0001", **overrides):
        self._prepare(experiment_id, **overrides)
        for slot in ("author-1", "author-2"):
            self._freeze_and_test(experiment_id, slot)
        status, blind = self._post("/v1/code-off/blind", {"experiment_id": experiment_id})
        self.assertEqual(status, 200, blind)
        return blind

    @staticmethod
    def _scores(total_bias=0):
        return {"correctness": 30 + total_bias, "tests": 15, "quality": 15, "scope": 8, "maintainability": 8}

    def _judge_done(self, experiment_id, judge_slot, prefer_author, score_a=0, score_b=0):
        job_id = self._launch_done(experiment_id, judge_slot)
        private = json.loads((self.records / experiment_id / "private" / "blinding.json").read_text())
        labels = private["judges"][judge_slot]["labels"]
        preferred = next(label for label, slot in labels.items() if slot == prefer_author)
        result = {
            "rubric_version": "code-off-rubric-v1",
            "judge_snapshot_hash": private["judges"][judge_slot]["judge_snapshot_hash"],
            "candidates": {
                label: {"scores": self._scores(score_a if slot == "author-1" else score_b), "pass": True, "rationale": f"bounded {label}"}
                for label, slot in labels.items()
            },
            "preference": preferred,
            "rationale": "bounded comparison",
        }
        workspace = server.codeoff_workspace_path(experiment_id, judge_slot)
        (workspace / "judge-result.json").write_text(json.dumps(result) + "\n")
        status, receipt = self._post("/v1/code-off/judge", {
            "experiment_id": experiment_id, "slot": judge_slot, "job_id": job_id,
        })
        self.assertEqual(status, 200, receipt)
        return receipt

    def test_fixed_seed_selection_is_versioned_canonical_and_without_replacement(self):
        draw = server.codeoff_draw("00" * 32)
        self.assertEqual(server.CODEOFF_AUTHOR_POOL, ["grok", "sol", "terra", "sonnet", "opus"])
        self.assertEqual(draw["authors"], ["sol", "sonnet"])
        self.assertEqual(draw["random_judges"], ["opus", "grok"])
        self.assertEqual(len(set(draw["authors"] + draw["random_judges"])), 4)
        self.assertTrue(set(draw["authors"]).isdisjoint(draw["random_judges"]))
        self.assertEqual(draw, server.codeoff_draw("00" * 32))
        self.assertEqual(draw["algorithm_version"], "code-off-draw-v1")
        self.assertEqual(draw["policy_version"], "code-off-policy-v1")
        self.assertEqual(server.CODEOFF_CATEGORIES, {"ui", "backend", "full_stack", "data", "infrastructure", "developer_tooling", "mobile", "documentation"})
        self.assertEqual(server.CODEOFF_TAGS, {"api", "database", "auth", "security", "accessibility", "testing", "migration", "performance", "observability", "build_ci", "bug_fix", "refactor"})

    def test_production_manifest_v2_has_exact_six_proven_current_identities(self):
        manifest = self.production_manifest
        expected = {
            "grok": ("grok", "xai", "grok-4.6"), "sol": ("codex", "openai", "gpt-5.6-sol"),
            "terra": ("codex", "openai", "gpt-5.6-terra"), "sonnet": ("claude", "anthropic", "claude-sonnet-5"),
            "opus": ("claude", "anthropic", "claude-opus-5"), "fable": ("claude", "anthropic", "claude-fable-5"),
        }
        self.assertEqual(manifest["version"], "approved-model-manifest-v2")
        self.assertEqual(set(manifest["models"]), set(expected))
        age = (datetime.now(timezone.utc).date() - datetime.fromisoformat(manifest["as_of"]).date()).days
        # After 30 days this intentionally fails to force policy review; it is not calendar rot.
        self.assertTrue(0 <= age <= manifest["max_age_days"] <= 30)
        for logical, (launcher, provider, model) in expected.items():
            with self.subTest(logical=logical):
                identity = manifest["models"][logical]
                self.assertEqual((identity["launcher"], identity["provider"], identity["exact_model"]), (launcher, provider, model))
                self.assertEqual((identity["runnable"], identity["proven"]), (True, True))
                self.assertTrue(0 < len(identity["reason"].strip()) <= 240)

    def test_prepare_snapshots_production_manifest_hash_and_check_before_selection(self):
        # This production snapshot intentionally expires with the embedded policy so review cannot silently drift.
        production = deepcopy(self.production_manifest)
        expected_snapshot = deepcopy(production)
        real_draw = server.codeoff_draw
        def mutate_policy_source_during_draw(seed):
            draw = real_draw(seed)
            server.CODEOFF_MODEL_MANIFEST["limitation"] = "fixture mutation after selection began"
            return draw

        with mock.patch.object(server, "CODEOFF_MODEL_MANIFEST", production), \
             mock.patch.object(server, "codeoff_draw", side_effect=mutate_policy_source_during_draw):
            status, payload = self._post(
                "/v1/code-off/prepare", self._body("production-snapshot-0001")
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "prepared", payload)
        manifest = json.loads((self.records / "production-snapshot-0001" / "manifest.json").read_text())
        self.assertEqual(manifest["approved_model_manifest"], expected_snapshot)
        self.assertEqual(
            manifest["approved_model_manifest_hash"],
            hashlib.sha256(server.codeoff_canonical_json(expected_snapshot)).hexdigest(),
        )
        self.assertIsNotNone(datetime.fromisoformat(manifest["approved_model_manifest_checked_at"].replace("Z", "+00:00")).tzinfo)
        self.assertEqual(manifest["selection"]["policy_version"], server.CODEOFF_POLICY_VERSION)
        self.assertEqual(manifest["selection"]["authors"], ["sol", "sonnet"])

    def test_prepare_locks_category_tags_source_seed_and_exact_snapshots(self):
        payload = self._prepare()
        manifest = json.loads((self.records / "experiment-0001" / "manifest.json").read_text())
        self.assertEqual(manifest["classification"]["version"], "code-category-v1")
        self.assertEqual(manifest["classification"]["category"], "backend")
        self.assertEqual(manifest["classification"]["tags"], ["api", "testing"])
        self.assertEqual(manifest["original_classification"]["tags"], ["testing", "api", "testing"])
        self.assertEqual(manifest["category_source"], "issue-67-fixture")
        self.assertEqual(manifest["selection"]["seed"], "00" * 32)
        self.assertEqual(manifest["selection"]["ordered_pool"], server.CODEOFF_AUTHOR_POOL)
        self.assertRegex(manifest["selection"]["seed_commitment"], r"^[0-9a-f]{64}$")
        for slot, identity in manifest["identities"].items():
            self.assertEqual(identity["manifest_version"], server.CODEOFF_MODEL_MANIFEST["version"], slot)
            self.assertTrue(identity["manifest_current"], slot)
            self.assertIn(identity["exact_model"], {"gpt-5.6-sol", "gpt-5.6-terra", "claude-sonnet-5", "claude-opus-5", "claude-fable-5", "grok-4.6"})
        self.assertEqual(payload["prompt_hash"], manifest["prompt_hash"])
        self.assertNotIn("prompt", payload)

    def test_public_audit_is_a_non_reconstructing_commitment_projection(self):
        self._ready_to_finalize("audit-projection-1")
        root = self.records / "audit-projection-1"
        manifest = json.loads((root / "manifest.json").read_text())
        private = json.loads((root / "private" / "blinding.json").read_text())
        status, audit, _ = server.dispatch(
            "GET", "/v1/code-off/audit", {"experiment_id": ["audit-projection-1"]}, True, b"",
        )
        self.assertEqual(status, 200, audit)
        projection = audit["selection"]
        self.assertNotIn("seed", projection)
        self.assertEqual(projection["seed_commitment"], manifest["selection"]["seed_commitment"])
        self.assertEqual(projection["authors"], manifest["selection"]["authors"])
        self.assertEqual(projection["random_judges"], manifest["selection"]["random_judges"])
        self.assertEqual(audit["aggregation"]["winner"], "author-1")
        self.assertNotIn("preferences", audit["aggregation"])
        self.assertTrue(all(set(receipt) == {"receipt_hash"} for receipt in audit["judges"].values()))
        self.assertGreaterEqual(audit["event_count"], 16)
        self.assertRegex(audit["prompt_hash"], r"^[0-9a-f]{64}$")
        rendered = json.dumps(audit, sort_keys=True)
        self.assertNotIn(manifest["selection"]["seed"], rendered)
        self.assertNotIn('"labels"', rendered)
        self.assertFalse(any(item["labels"] == projection for item in private["judges"].values()))
        with self.assertRaises((KeyError, TypeError, ValueError)):
            server.codeoff_draw(projection["seed"])

    def test_graph_shaped_json_strings_are_bounded_parsed_and_strict(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "code-off.json").read_text())
        prepare = next(node for node in graph["spec"]["nodes"] if node["id"] == "prepare")["config"]
        native = self._body("graph-values-0001")
        shaped = {
            key: json.dumps(native[key], separators=(",", ":")) if key in {"tags", "tests", "toolchain", "budgets"} else native[key]
            for key in native
        }
        for key in native:
            self.assertEqual(prepare[key], f"{{{{ CTX.INPUT.{key} }}}}")
        status, payload = self._post("/v1/code-off/prepare", shaped)
        self.assertEqual(status, 200, payload)
        manifest = json.loads((self.records / "graph-values-0001" / "manifest.json").read_text())
        self.assertEqual(manifest["classification"]["tags"], ["api", "testing"])
        self.assertEqual(manifest["tests"], native["tests"])
        self.assertEqual(manifest["toolchain"], native["toolchain"])
        self.assertEqual(manifest["budgets"], native["budgets"])
        agent_body = self._agent_body("graph-values-0001", "author-1")
        agent_body.update({key: str(agent_body[key]) for key in ("max_turns", "run_budget_seconds")})
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "enqueue_agent") as enqueue:
            status, receipt = self._post("/v1/agent/run", agent_body)
        self.assertEqual(status, 202, receipt)
        enqueue.assert_called_once()
        bad = (
            ("tags", '"api"'), ("tests", "{}"), ("toolchain", "[]"),
            ("budgets", '{"author_seconds":60}'), ("tags", "[" + '"api",' * 600 + '"api"]'),
        )
        for index, (field, value) in enumerate(bad):
            body = self._body(f"graph-bad-{index:04d}")
            body[field] = value
            status, receipt = self._post("/v1/code-off/prepare", body)
            self.assertEqual(status, 400, (field, receipt))
            self.assertIn(receipt["code"], {f"bad_{field}", "bad_json_value"})

    def test_categories_and_opaque_inputs_fail_closed(self):
        cases = (
            ({"category": "frontend"}, "bad_category"),
            ({"tags": ["api", "unknown"]}, "bad_tags"),
            ({"category_source": ""}, "bad_category_source"),
            ({"seed": "00" * 31}, "bad_seed"),
            ({"experiment_id": "../escape"}, "bad_experiment_id"),
            ({"workspace_path": "/tmp/escape"}, "unexpected_fields"),
        )
        for i, (change, code) in enumerate(cases):
            with self.subTest(code=code):
                body = self._body(f"bad-input-{i:04d}")
                body.update(change)
                status, payload = self._post("/v1/code-off/prepare", body)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], code)

    def test_prepare_snapshot_failure_is_transactional_and_same_id_retries(self):
        real_snapshot = server.codeoff_tree_snapshot

        def fail_after_artifacts(path, base_sha=None):
            if Path(path).name == "author-1":
                raise OSError("fixture snapshot interruption")
            return real_snapshot(path, base_sha)

        with mock.patch.object(server, "codeoff_tree_snapshot", side_effect=fail_after_artifacts):
            status, payload = self._post("/v1/code-off/prepare", self._body("prepare-retry-01"))
        self.assertEqual(status, 500, payload)
        self.assertEqual(payload["code"], "codeoff_runtime_failure")
        self.assertTrue(payload["retryable"])
        self.assertFalse((self.records / "prepare-retry-01").exists())
        self.assertFalse((self.workspaces / "prepare-retry-01").exists())
        self.assertNotIn("prepare-retry-01", self._git("worktree", "list", "--porcelain").stdout)
        self._prepare("prepare-retry-01")

    def test_event_and_state_write_interruptions_recover_for_retry(self):
        for index, target in enumerate(("_codeoff_event", "_codeoff_write_state")):
            experiment_id = f"write-retry-{index:04d}"
            with self.subTest(target=target):
                self._prepare(experiment_id)
                job_id = self._launch_done(experiment_id, "author-1")
                with mock.patch.object(server, target, side_effect=OSError("fixture interrupted write")):
                    status, payload = self._post("/v1/code-off/candidate", {
                        "experiment_id": experiment_id, "slot": "author-1", "action": "freeze", "job_id": job_id,
                    })
                self.assertEqual(status, 500, payload)
                self.assertEqual(payload["code"], "codeoff_runtime_failure")
                self.assertTrue(payload["retryable"])
                audit_status, audit, _ = server.dispatch(
                    "GET", "/v1/code-off/audit", {"experiment_id": [experiment_id]}, True, b"",
                )
                self.assertEqual(audit_status, 200, audit)
                self.assertFalse(audit["candidates"]["author-1"])
                retry_status, retry = self._post("/v1/code-off/candidate", {
                    "experiment_id": experiment_id, "slot": "author-1", "action": "freeze", "job_id": job_id,
                })
                self.assertEqual(retry_status, 200, retry)

    def test_blind_state_interruption_cleans_uncommitted_judge_workspaces_for_retry(self):
        self._prepare()
        for slot in ("author-1", "author-2"):
            self._freeze_and_test("experiment-0001", slot)
        with mock.patch.object(server, "_codeoff_write_state", side_effect=OSError("fixture blind state interruption")):
            status, payload = self._post("/v1/code-off/blind", {"experiment_id": "experiment-0001"})
        self.assertEqual((status, payload["code"]), (500, "codeoff_runtime_failure"))
        status, payload = self._post("/v1/code-off/blind", {"experiment_id": "experiment-0001"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "blinded")

    def test_candidate_infrastructure_interruption_is_retryable_not_terminal(self):
        self._prepare()
        job_id = self._launch_done("experiment-0001", "author-1")
        status, frozen = self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "freeze", "job_id": job_id,
        })
        self.assertEqual(status, 200, frozen)
        with mock.patch.object(server, "_codeoff_verify_artifact", side_effect=OSError("fixture staging outage")):
            status, payload = self._post("/v1/code-off/candidate", {
                "experiment_id": "experiment-0001", "slot": "author-1", "action": "test",
            })
        self.assertEqual((status, payload["code"], payload["retryable"]), (500, "codeoff_runtime_failure", True))
        self.assertFalse(json.loads((self.records / "experiment-0001" / "state.json").read_text())["finalized"])
        self.assertEqual(self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "test",
        })[0], 200)

    def test_duplicate_blind_after_judge_launch_preserves_frozen_workspaces(self):
        self._ready_for_judges()
        self._launch_done("experiment-0001", "judge-fable")
        marker = server.codeoff_workspace_path("experiment-0001", "judge-fable") / "live-marker"
        marker.write_text("must survive duplicate blind\n")
        status, payload = self._post("/v1/code-off/blind", {"experiment_id": "experiment-0001"})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "already_blinded")
        self.assertTrue(marker.exists())

    def test_current_fixture_unproven_fable_and_selected_terra_park_without_redraw_or_launch(self):
        unproven = deepcopy(self.production_manifest)
        for logical in ("fable", "terra"):
            unproven["models"][logical].update({
                "runnable": False, "proven": False, "reason": "fixture unproven identity",
            })
        with mock.patch.object(server, "CODEOFF_MODEL_MANIFEST", unproven), \
             mock.patch.object(server, "resolve_launcher_binary_now", side_effect=AssertionError("launcher lookup forbidden")), \
             mock.patch.object(server, "enqueue_agent", side_effect=AssertionError("launch forbidden")):
            status, first = self._post("/v1/code-off/prepare", self._body("park-fable-0001", seed="00" * 32))
            status2, terra = self._post("/v1/code-off/prepare", self._body("park-terra-0001", seed=(4).to_bytes(32, "big").hex()))
        self.assertEqual((status, status2), (200, 200))
        self.assertEqual(first["status"], "parked")
        self.assertIn("fable", " ".join(first["reasons"]))
        self.assertEqual(first["authors"], ["sol", "sonnet"])
        self.assertEqual(terra["authors"], ["terra", "sol"])
        self.assertIn("terra", " ".join(terra["reasons"]))
        self.assertFalse((self.workspaces / "park-fable-0001").exists())
        self.assertTrue(json.loads((self.records / "park-fable-0001" / "state.json").read_text())["finalized"])

    def test_stale_manifest_parks_before_worktree_or_launch(self):
        stale = deepcopy(server.CODEOFF_MODEL_MANIFEST)
        stale.update({"as_of": "2020-01-01", "max_age_days": 1})
        with mock.patch.object(server, "CODEOFF_MODEL_MANIFEST", stale), \
             mock.patch.object(server, "run_git", wraps=server.run_git) as git, \
             mock.patch.object(server, "codeoff_draw", wraps=server.codeoff_draw) as draw, \
             mock.patch.object(server, "resolve_launcher_binary_now", side_effect=AssertionError("fallback launch forbidden")), \
             mock.patch.object(server, "enqueue_agent", side_effect=AssertionError("fallback launch forbidden")):
            status, payload = self._post("/v1/code-off/prepare", self._body("stale-manifest-1"))
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "parked")
        self.assertEqual(payload["reasons"], ["approved_model_manifest_stale"])
        self.assertEqual(payload["authors"], ["sol", "sonnet"])
        draw.assert_called_once_with("00" * 32)
        self.assertFalse(any("worktree" in call.args[1] for call in git.call_args_list))

    def test_newer_bounded_catalog_fable_identity_parks_before_worktree_or_launch(self):
        current = deepcopy(self.production_manifest)
        inventory = {
            "version": "bounded-model-catalog-v1",
            "models": {
                logical: identity["exact_model"]
                for logical, identity in current["models"].items()
            },
        }
        inventory["models"]["fable"] = "claude-fable-6"
        self.assertTrue(server._codeoff_manifest_current(current))
        with mock.patch.object(server, "CODEOFF_MODEL_MANIFEST", current), \
             mock.patch.object(server, "CODEOFF_CATALOG_INVENTORY", inventory, create=True), \
             mock.patch.object(server, "run_git", wraps=server.run_git) as git, \
             mock.patch.object(server, "codeoff_draw", wraps=server.codeoff_draw) as draw, \
             mock.patch.object(server, "resolve_launcher_binary_now") as launcher, \
             mock.patch.object(server, "enqueue_agent") as enqueue:
            status, payload = self._post("/v1/code-off/prepare", self._body("newer-fable-0001"))
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "parked")
        self.assertEqual(payload["reasons"], ["catalog_identity_newer:fable:claude-fable-6"])
        self.assertEqual((payload["authors"], payload["random_judges"]), (["sol", "sonnet"], ["opus", "grok"]))
        draw.assert_called_once_with("00" * 32)
        launcher.assert_not_called()
        enqueue.assert_not_called()
        self.assertFalse(any("worktree" in call.args[1] for call in git.call_args_list))
        self.assertFalse((self.workspaces / "newer-fable-0001").exists())

    def test_prepare_creates_two_isolated_git_worktrees_and_agent_run_resolves_only_opaque_ref(self):
        self._prepare()
        one = server.codeoff_workspace_path("experiment-0001", "author-1")
        two = server.codeoff_workspace_path("experiment-0001", "author-2")
        self.assertNotEqual(one, two)
        self.assertTrue((one / ".git").exists())
        self.assertTrue((two / ".git").exists())
        job_id = self._launch_done("experiment-0001", "author-1")
        job = server.read_job(job_id)
        self.assertEqual(job["codeoff_workspace"], {"experiment_id": "experiment-0001", "slot": "author-1"})
        self.assertEqual((self.jobs / job_id / "prompt.txt").read_bytes(), (self.records / "experiment-0001" / "artifacts" / job["prompt_hash"]).read_bytes())
        status, payload = self._post("/v1/agent/run", {
            "prompt": "escape", "cwd": str(one), "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
        })
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "bad_cwd")
        status, payload = self._post("/v1/agent/run", {
            "codeoff_workspace": {"experiment_id": "experiment-0001", "slot": "author-1", "path": str(one)},
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
        })
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "bad_codeoff_workspace")
        status, payload = self._post("/v1/agent/run", {
            "codeoff_workspace": {"experiment_id": "experiment-0001", "slot": "author-2"},
            "launcher": "claude", "provider": "anthropic", "model": "moving-latest-alias",
            "max_turns": 4, "run_budget_seconds": 60,
        })
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "codeoff_identity_mismatch")

    def test_agent_run_rejects_caller_prompt_or_cwd_for_codeoff_workspace(self):
        self._prepare("opaque-exclusivity")
        for forbidden, slot in (("prompt", "author-1"), ("cwd", "author-2")):
            with self.subTest(forbidden=forbidden):
                body = self._agent_body("opaque-exclusivity", slot)
                body[forbidden] = "caller-controlled"
                status, payload = self._post("/v1/agent/run", body)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "bad_codeoff_workspace")
                self.assertEqual(
                    payload["error"],
                    "code-off jobs resolve prompt and cwd from the opaque reference",
                )

    def test_prepare_replays_only_an_exact_untouched_experiment(self):
        first = self._prepare()
        state_path = self.records / "experiment-0001" / "state.json"
        state_before = state_path.read_bytes()
        record_before = {
            path.relative_to(self.records / "experiment-0001").as_posix(): path.read_bytes()
            for path in (self.records / "experiment-0001").rglob("*") if path.is_file()
        }
        workspaces_before = {
            slot: server.codeoff_tree_snapshot(server.codeoff_workspace_path("experiment-0001", slot), self.base_sha)["manifest_hash"]
            for slot in ("author-1", "author-2")
        }
        worktree_list_before = self._git("worktree", "list", "--porcelain").stdout

        status, replay = self._post("/v1/code-off/prepare", self._body())
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay, first)
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual({
            path.relative_to(self.records / "experiment-0001").as_posix(): path.read_bytes()
            for path in (self.records / "experiment-0001").rglob("*") if path.is_file()
        }, record_before)
        self.assertEqual({
            slot: server.codeoff_tree_snapshot(server.codeoff_workspace_path("experiment-0001", slot), self.base_sha)["manifest_hash"]
            for slot in ("author-1", "author-2")
        }, workspaces_before)
        self.assertEqual(self._git("worktree", "list", "--porcelain").stdout, worktree_list_before)

        mismatches = {
            "prompt": "different immutable task",
            "seed": "11" * 32,
            "category": "data",
            "tags": ["security"],
            "category_source": "different source",
            "tests": ["fixture-fail"],
            "toolchain": {"python": "different"},
            "budgets": {"author_seconds": 61, "judge_seconds": 60, "test_seconds": 20, "max_turns": 4},
            "commit_message": "different commit",
        }
        for field, value in mismatches.items():
            with self.subTest(field=field):
                status, payload = self._post(
                    "/v1/code-off/prepare", self._body(**{field: value})
                )
                self.assertEqual(status, 409, payload)
                self.assertEqual(payload["code"], "experiment_exists")
        with mock.patch.object(
            server, "load_repos",
            return_value={"scratch": str(self.repo), "alias": str(self.repo)},
        ):
            status, payload = self._post(
                "/v1/code-off/prepare", self._body(repo="alias")
            )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "experiment_exists")

        self._launch_done("experiment-0001", "author-1")
        status, payload = self._post("/v1/code-off/prepare", self._body())
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "experiment_exists")

    def test_prepare_replay_rejects_partial_state_or_changed_workspaces(self):
        partials = {
            "resume-status": {"status": "authors_running"},
            "resume-reason": {"reason": "partial failure"},
            "resume-aggregation": {"aggregation": {"decision": "winner"}},
            "resume-judges": {"judges": {"judge-fable": {"receipt_hash": "x"}}},
            "resume-candidate": {"candidates": {"author-1": {"artifact_hash": "x"}, "author-2": {}}},
        }
        for experiment_id, changes in partials.items():
            with self.subTest(experiment_id=experiment_id):
                self._prepare(experiment_id)
                state_path = self.records / experiment_id / "state.json"
                state = json.loads(state_path.read_text())
                state.update(changes)
                state_path.write_text(json.dumps(state))
                status, payload = self._post("/v1/code-off/prepare", self._body(experiment_id))
                self.assertEqual(status, 409, payload)
                self.assertEqual(payload["code"], "experiment_exists")

        self._prepare("resume-event")
        event_root = self.records / "resume-event"
        event_state = json.loads((event_root / "state.json").read_text())
        server._codeoff_commit_event(event_root, event_state, "diagnostic", {})
        status, payload = self._post("/v1/code-off/prepare", self._body("resume-event"))
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "experiment_exists")

        self._prepare("resume-workspaces")
        first = server.codeoff_workspace_path("resume-workspaces", "author-1")
        (first / "README.md").write_text("changed\n")
        status, payload = self._post("/v1/code-off/prepare", self._body("resume-workspaces"))
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "experiment_exists")
        (first / "README.md").write_text("base\n")
        server._codeoff_remove_worktree(self.repo, server.codeoff_workspace_path("resume-workspaces", "author-2"))
        status, payload = self._post("/v1/code-off/prepare", self._body("resume-workspaces"))
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "experiment_exists")

    def test_prepare_replay_rejects_a_finalized_experiment(self):
        stale = deepcopy(server.CODEOFF_MODEL_MANIFEST)
        stale["as_of"] = "2000-01-01"
        stale["max_age_days"] = 1
        with mock.patch.object(server, "CODEOFF_MODEL_MANIFEST", stale):
            status, prepared = self._post(
                "/v1/code-off/prepare", self._body("resume-finalized")
            )
            self.assertEqual(status, 200, prepared)
            self.assertEqual(prepared["status"], "parked")
            status, payload = self._post(
                "/v1/code-off/prepare", self._body("resume-finalized")
            )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "experiment_exists")

    def test_claude_codeoff_provenance_accepts_one_exact_first_party_model_usage(self):
        self._prepare("claude-provenance-good")
        model = self._agent_body("claude-provenance-good", "author-2")["model"]
        native_session_id = "claude-codeoff-76"
        result = {
            "type": "result", "subtype": "success", "is_error": False,
            "session_id": native_session_id,
            "result": json.dumps({"status": "ok", "sha": None, "pr_url": None, "summary": "done"}),
            "modelUsage": {
                model: {"canonicalModel": model, "provider": "firstParty", "inputTokens": 1},
            },
            "fixtureRawMarker": "must-not-enter-compact-execution-identity",
        }
        job = self._run_native_terminal_fixture(
            "claude-provenance-good", "author-2", json.dumps(result) + "\n",
            native_session_id=native_session_id,
        )
        self.assertEqual(job["status"], "completed", job)
        compact_identity = job["receipt"]["execution_identity"]
        self.assertEqual(compact_identity, self._execution_identity(job["session_identity"]))
        self.assertEqual(compact_identity["source"], "claude-result-v1")
        compact = json.dumps(compact_identity, sort_keys=True)
        self.assertNotIn("fixtureRawMarker", compact)
        self.assertNotIn(str(Path(job["cwd"]).resolve()), compact)

    def test_claude_codeoff_provenance_rejects_missing_multiple_and_mismatched_usage(self):
        cases = (
            "missing", "multiple_results", "canonical_model", "provider",
            "multiple_usage", "session_model",
        )
        for index, case in enumerate(cases):
            experiment_id = f"claude-bad-{index:04d}"
            with self.subTest(case=case):
                self._prepare(experiment_id)
                model = self._agent_body(experiment_id, "author-2")["model"]
                native_session_id = f"claude-bad-{index}"
                detail = {"canonicalModel": model, "provider": "firstParty"}
                usage = {model: detail}
                if case == "missing": usage = None
                if case == "canonical_model": detail["canonicalModel"] = "claude-moving-alias"
                if case == "provider": detail["provider"] = "bedrock"
                if case == "multiple_usage": usage["claude-other"] = detail
                event = {"type": "result", "subtype": "success", "is_error": False,
                         "session_id": native_session_id, "modelUsage": usage,
                         "result": json.dumps({"status": "ok", "sha": None, "pr_url": None, "summary": "done"})}
                events = [event, deepcopy(event)] if case == "multiple_results" else [event]

                def alter_session_model(job):
                    if case == "session_model":
                        job["session_identity"]["model"] = "claude-moving-alias"
                        server.write_job(job)

                job = self._run_native_terminal_fixture(
                    experiment_id, "author-2", "".join(json.dumps(item) + "\n" for item in events),
                    native_session_id=native_session_id, before_run=alter_session_model,
                    expect_native_session=case != "session_model",
                )
                self.assertEqual((job["status"], job["receipt"]["failure_code"]), ("failed", "session_provenance_invalid"), job)
                self.assertNotIn("execution_identity", job["receipt"])

    def test_codex_codeoff_provenance_accepts_exact_native_rollout_metadata(self):
        self.assertEqual(server.CODEOFF_PROVENANCE_MAX_FILES, 65536)
        for index, model_provider in enumerate(("openai", "openai-api")):
            experiment_id = f"codex-rollout-good-{index:02d}"
            native_session_id = f"019c0000-0000-7000-8000-00000000000{index}"
            codex_home = Path(self.td.name) / f"codex-home-good-{index}"
            model = "gpt-5.6-sol"
            self._prepare(experiment_id)

            def write_rollout(job, provider=model_provider, home=codex_home):
                rollout = home / "sessions" / "2026" / "08" / "26" / f"rollout-fixture-{native_session_id}.jsonl"
                rollout.parent.mkdir(parents=True, exist_ok=True)
                cwd = str(Path(job["cwd"]).resolve())
                records = [
                    {"type": "session_meta", "payload": {
                        "id": native_session_id, "originator": "codex_exec", "source": "exec",
                        "model_provider": provider, "cwd": cwd,
                        "git": {"commit_hash": job["session_identity"]["starting_head"]},
                    }},
                    {"type": "turn_context", "payload": {"cwd": cwd, "model": model}},
                    {"type": "turn_context", "payload": {"cwd": cwd, "model": model}},
                ]
                rollout.write_text("".join(json.dumps(item) + "\n" for item in records))

            with self.subTest(model_provider=model_provider), \
                 mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                job = self._run_native_terminal_fixture(
                    experiment_id, "author-1", self._codex_stdout(native_session_id), native_session_id=native_session_id,
                    before_run=write_rollout,
                )
                self.assertEqual(job["status"], "completed", job)
                compact = job["receipt"]["execution_identity"]
                self.assertEqual(compact, self._execution_identity(job["session_identity"]))
                self.assertEqual(compact["source"], "codex-rollout-v1")
                self.assertFalse(any(secret in json.dumps(compact) for secret in (str(codex_home), job["cwd"], "session_meta")))

    def test_codex_codeoff_provenance_rejects_missing_ambiguous_drift_and_context_mismatch(self):
        cases = (
            "missing", "ambiguous", "native_id_mismatch", "model_drift",
            "cwd_mismatch", "meta_cwd_mismatch", "commit_mismatch", "missing_context",
        )
        for index, case in enumerate(cases):
            experiment_id = f"codex-rollout-bad-{index:02d}"
            native_session_id = f"019c0000-0000-7000-9000-00000000000{index}"
            codex_home = Path(self.td.name) / f"codex-home-bad-{index}"
            self._prepare(experiment_id)

            def write_rollout(job):
                if case == "missing":
                    return
                cwd = str(Path(job["cwd"]).resolve())
                meta = {"type": "session_meta", "payload": {
                    "id": native_session_id, "originator": "codex_exec", "source": "exec",
                    "model_provider": "openai", "cwd": cwd,
                    "git": {"commit_hash": job["session_identity"]["starting_head"]},
                }}
                if case == "native_id_mismatch":
                    meta["payload"]["id"] = "019cffff-ffff-7fff-8fff-ffffffffffff"
                if case == "commit_mismatch":
                    meta["payload"]["git"]["commit_hash"] = "f" * 40
                if case == "meta_cwd_mismatch":
                    meta["payload"]["cwd"] = str(self.repo)
                turns = [{"type": "turn_context", "payload": {"cwd": cwd, "model": "gpt-5.6-sol"}}]
                if case == "model_drift":
                    turns.append({"type": "turn_context", "payload": {"cwd": cwd, "model": "gpt-5.6-terra"}})
                if case == "cwd_mismatch":
                    turns[0]["payload"]["cwd"] = str(self.repo)
                if case == "missing_context":
                    records = [meta]
                else:
                    records = [meta, *turns]
                folder = codex_home / "sessions" / "2026" / "08" / "26"
                folder.mkdir(parents=True, exist_ok=True)
                count = 2 if case == "ambiguous" else 1
                for duplicate in range(count):
                    path = folder / f"rollout-{duplicate}-{native_session_id}.jsonl"
                    path.write_text("".join(json.dumps(item) + "\n" for item in records))

            with self.subTest(case=case), mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                job = self._run_native_terminal_fixture(
                    experiment_id, "author-1", self._codex_stdout(native_session_id), native_session_id=native_session_id,
                    before_run=write_rollout,
                )
                self.assertEqual((job["status"], job["receipt"]["failure_code"]), ("failed", "session_provenance_invalid"), job)

    def test_codex_rollout_walk_error_discards_an_earlier_match(self):
        codex_home = Path(self.td.name) / "codex-home-walk-error"
        sessions = codex_home / "sessions"
        sessions.mkdir(parents=True)
        native_session_id = "019c0000-0000-7000-b000-000000000076"

        def walk_with_late_error(root, *, onerror=None, followlinks=False):
            self.assertEqual(Path(root), sessions)
            self.assertFalse(followlinks)
            yield str(sessions), [], [f"rollout-fixture-{native_session_id}.jsonl"]
            if onerror is not None:
                onerror(OSError("deterministic traversal failure"))

        job = {"session_identity": {"native_session_id": native_session_id}}
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}), \
             mock.patch.object(server.os, "walk", side_effect=walk_with_late_error):
            self.assertIsNone(server._codeoff_codex_rollout(job))

    def test_codex_judge_provenance_is_bound_to_the_blind_workspace_starting_head(self):
        for index, use_starting_head in enumerate((True, False)):
            experiment_id = f"codex-judge-head-{index:02d}"
            native_session_id = f"019c0000-0000-7000-a000-00000000000{index}"
            codex_home = Path(self.td.name) / f"codex-judge-home-{index}"
            self._ready_for_judges(experiment_id, seed=(2).to_bytes(32, "big").hex())
            body = self._agent_body(experiment_id, "judge-2")
            self.assertEqual((body["launcher"], body["model"]), ("codex", "gpt-5.6-terra"))

            def write_rollout(job):
                self.assertNotEqual(job["session_identity"]["starting_head"], self.base_sha)
                rollout = codex_home / "sessions" / "2026" / "08" / "26" / f"rollout-judge-{native_session_id}.jsonl"
                rollout.parent.mkdir(parents=True, exist_ok=True)
                cwd = str(Path(job["cwd"]).resolve())
                commit_hash = job["session_identity"]["starting_head"] if use_starting_head else self.base_sha
                rollout.write_text("".join(json.dumps(item) + "\n" for item in (
                    {"type": "session_meta", "payload": {
                        "id": native_session_id, "originator": "codex_exec", "source": "exec",
                        "model_provider": "openai", "cwd": cwd,
                        "git": {"commit_hash": commit_hash},
                    }},
                    {"type": "turn_context", "payload": {"cwd": cwd, "model": body["model"]}},
                )))

            with self.subTest(use_starting_head=use_starting_head), \
                 mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                job = self._run_native_terminal_fixture(
                    experiment_id, "judge-2", self._codex_stdout(native_session_id),
                    native_session_id=native_session_id, before_run=write_rollout,
                )
            expected = "completed" if use_starting_head else "failed"
            self.assertEqual(job["status"], expected, job)
            if use_starting_head:
                self.assertEqual(
                    job["receipt"]["execution_identity"],
                    self._execution_identity(job["session_identity"]),
                )
            else:
                self.assertEqual(job["receipt"]["failure_code"], "session_provenance_invalid")

    def test_grok_codeoff_keeps_acp_session_as_compact_execution_identity(self):
        experiment_id = "grok-execution-receipt"
        self._prepare(experiment_id, seed=(1).to_bytes(32, "big").hex())
        body = self._agent_body(experiment_id, "author-2")
        self.assertEqual(body["model"], "grok-4.6")
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "enqueue_agent"):
            status, launched = self._post("/v1/agent/run", body)
        self.assertEqual(status, 202, launched)
        job_id = launched["job_id"]
        (self.jobs / job_id / "last-message.txt").write_text(
            json.dumps({"status": "ok", "sha": None, "pr_url": None, "summary": "done"}))
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "run_grok_acp", return_value=(0, False, "grok-codeoff-76", None)), \
             mock.patch.object(server, "herdr_job_done"):
            server.run_agent_job(job_id)
        job = server.read_job(job_id)
        expected = self._execution_identity(job["session_identity"])
        self.assertEqual(job["receipt"]["execution_identity"], expected)
        self.assertEqual((expected["native_session_id"], expected["source"], set(expected)), ("grok-codeoff-76", "grok-acp-v1", {"version", "source", "launcher", "provider", "model", "native_session_id"}))

    def test_grok_codeoff_adapter_failure_evidence_invalidates_success(self):
        experiment_id = "grok-adapter-evidence"
        self._prepare(experiment_id, seed=(1).to_bytes(32, "big").hex())
        body = self._agent_body(experiment_id, "author-2")
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "enqueue_agent"):
            status, launched = self._post("/v1/agent/run", body)
        self.assertEqual(status, 202, launched)
        job_id = launched["job_id"]
        (self.jobs / job_id / "last-message.txt").write_text(
            json.dumps({"status": "ok", "sha": None, "pr_url": None, "summary": "done"}))

        def adapter_success_with_failure_evidence(job, *_args, **_kwargs):
            job["_adapter_failure_code"] = "adapter_contract_invalid"
            return 0, False, "grok-adapter-evidence-76", None

        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "run_grok_acp", side_effect=adapter_success_with_failure_evidence), \
             mock.patch.object(server, "herdr_job_done"):
            server.run_agent_job(job_id)
        job = server.read_job(job_id)
        self.assertEqual((job["status"], job["receipt"]["failure_code"]), ("failed", "session_provenance_invalid"))
        self.assertNotIn("execution_identity", job)
        self.assertNotIn("execution_identity", job["receipt"])

    def test_codeoff_execution_identity_rejects_unknown_launcher(self):
        job = {
            "launcher": "future-launcher",
            "session_identity": {
                "launcher": "future-launcher", "provider": "future-provider",
                "model": "future-model", "native_session_id": "future-session",
            },
        }
        self.assertIsNone(server._codeoff_execution_identity(job, None))

    def test_compact_execution_identity_rejects_top_level_session_divergence(self):
        base = {
            "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
            "session_identity": {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "native_session_id": "fixture-identity",
            },
        }
        self.assertIsNotNone(server._codeoff_compact_execution_identity(base))
        for field, forged in (
            ("launcher", "claude"), ("provider", "anthropic"), ("model", "forged-model"),
        ):
            with self.subTest(field=field):
                job = deepcopy(base)
                job["session_identity"][field] = forged
                self.assertIsNone(server._codeoff_compact_execution_identity(job))

    def test_ordinary_agent_run_never_acquires_the_codeoff_lock(self):
        class ForbiddenLock:
            def __enter__(self):
                raise AssertionError("ordinary agent_run acquired CODEOFF_LOCK")

            def __exit__(self, *_args):
                return False

        body = {
            "prompt": "bounded ordinary fixture", "cwd": "scratch", "launcher": "codex",
            "provider": "openai", "model": "gpt-5.6-sol", "max_turns": 1, "run_budget_seconds": 30,
        }
        with mock.patch.object(server, "CODEOFF_LOCK", ForbiddenLock()), \
             mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "enqueue_agent") as enqueue:
            status, payload = self._post("/v1/agent/run", body)
        self.assertEqual(status, 202, payload)
        enqueue.assert_called_once()

    def test_ordinary_terra_and_fable_stay_rejected_but_exact_codeoff_slots_launch(self):
        ordinary = (
            ("codex", "openai", "gpt-5.6-terra"),
            ("claude", "anthropic", "claude-fable-5"),
        )
        for launcher, provider, model in ordinary:
            with self.subTest(model=model):
                status, payload = self._post("/v1/agent/run", {
                    "prompt": "ordinary requests cannot opt into code-off-only identities",
                    "cwd": "scratch", "launcher": launcher, "provider": provider, "model": model,
                    "max_turns": 1, "run_budget_seconds": 30,
                })
                self.assertEqual((status, payload["code"]), (400, "bad_model_identity"))

        self._prepare("terra-exact-launch", seed=(4).to_bytes(32, "big").hex())
        self._ready_for_judges("fable-exact-launch")
        for experiment_id, slot, expected in (
            ("terra-exact-launch", "author-1", "gpt-5.6-terra"),
            ("fable-exact-launch", "judge-fable", "claude-fable-5"),
        ):
            body = self._agent_body(experiment_id, slot)
            mismatched = {**body, "provider": "anthropic" if body["provider"] == "openai" else "openai"}
            with self.subTest(model=expected), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
                 mock.patch.object(server, "enqueue_agent") as enqueue:
                mismatch_status, mismatch = self._post("/v1/agent/run", mismatched)
                status, payload = self._post("/v1/agent/run", body)
            self.assertEqual((mismatch_status, mismatch["code"]), (400, "codeoff_identity_mismatch"))
            self.assertEqual((body["model"], status), (expected, 202), payload)
            enqueue.assert_called_once()

    def test_freeze_and_judge_artifacts_persist_matching_execution_identity(self):
        experiment_id = "persist-execution-identity"
        self._prepare(experiment_id)
        for slot in ("author-1", "author-2"):
            self._freeze_and_test(experiment_id, slot)
        self.assertEqual(self._post("/v1/code-off/blind", {"experiment_id": experiment_id})[0], 200)
        for slot in ("judge-fable", "judge-1", "judge-2"):
            self._judge_done(experiment_id, slot, "author-1")
        state = json.loads((self.records / experiment_id / "state.json").read_text())
        root = self.records / experiment_id
        receipts = [(slot, state["candidates"][slot]["freeze_receipt_hash"]) for slot in ("author-1", "author-2")]
        receipts += [(slot, state["judges"][slot]["receipt_hash"]) for slot in ("judge-fable", "judge-1", "judge-2")]
        for slot, receipt_hash in receipts:
            with self.subTest(slot=slot):
                artifact = json.loads((root / "artifacts" / receipt_hash).read_bytes())
                job_id = artifact["job_id"]
                job = server.read_job(job_id)
                expected = self._execution_identity(job["session_identity"])
                self.assertEqual(job["receipt"]["execution_identity"], expected)
                self.assertEqual(artifact["execution_identity"], expected)
                self.assertNotIn(str(Path(job["cwd"]).resolve()), json.dumps(artifact["execution_identity"]))

    def test_duplicate_budget_resume_and_terminal_receipt_mismatches_fail_closed(self):
        self._prepare()
        wrong_budget = self._agent_body("experiment-0001", "author-2")
        wrong_budget["max_turns"] += 1
        status, payload = self._post("/v1/agent/run", wrong_budget)
        self.assertEqual((status, payload["code"]), (400, "codeoff_budget_mismatch"))

        job_id = self._launch_done("experiment-0001", "author-1")
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "enqueue_agent"):
            status, payload = self._post("/v1/agent/run", self._agent_body("experiment-0001", "author-1"))
        self.assertEqual((status, payload["code"]), (409, "slot_already_launched"))

        resume_id = "f" * 32
        resume_body = self._agent_body("experiment-0001", "author-2")
        branch, head, err = server.current_branch_head(server.codeoff_workspace_path("experiment-0001", "author-2"))
        self.assertIsNone(err)
        profile = _fixture_execution_profile(
            resume_body["launcher"], resume_body["provider"], resume_body["model"], Path(__file__)
        )
        resume_identity = {
            "launcher": resume_body["launcher"], "provider": resume_body["provider"], "model": resume_body["model"],
            **_fixture_identity_profile(profile),
            "repo": "codeoff:experiment-0001:author-2", "branch": branch, "starting_head": head,
            "native_session_id": "fixture-resume",
        }
        server.write_job({
            "job_id": resume_id, "kind": "agent", "status": "completed",
            "repo": resume_identity["repo"],
            "launcher": resume_body["launcher"], "provider": resume_body["provider"],
            "model": resume_body["model"], **profile, "session_identity": resume_identity,
            "codeoff_workspace": {"experiment_id": "experiment-0001", "slot": "author-2"},
            "receipt": {"status": "ok", "job_id": resume_id,
                        "launcher": resume_body["launcher"], "provider": resume_body["provider"],
                        "model": resume_body["model"], **profile,
                        "session_identity": resume_identity},
        })
        resume_body.update({"resume_job_id": resume_id, "session_identity": resume_identity})
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)):
            status, payload = self._post("/v1/agent/run", resume_body)
        self.assertEqual((status, payload["code"]), (400, "codeoff_resume_mismatch"), payload)

        for index, case in enumerate(("receipt_job_id", "execution_model", "execution_source")):
            experiment_id = f"terminal-mismatch-{index}"
            with self.subTest(case=case):
                self._prepare(experiment_id)
                mismatch_job_id = self._launch_done(experiment_id, "author-1")
                mismatch_job = server.read_job(mismatch_job_id)
                if case == "receipt_job_id":
                    mismatch_job["receipt"]["job_id"] = "e" * 32
                elif case == "execution_model":
                    mismatch_job["receipt"]["execution_identity"]["model"] = "moving-alias"
                else:
                    mismatch_job["execution_identity"]["source"] = "forged-source-v1"
                    mismatch_job["receipt"]["execution_identity"]["source"] = "forged-source-v1"
                _replace_persisted_job_fixture(mismatch_job_id, mismatch_job)
                status, payload = self._post("/v1/code-off/candidate", {
                    "experiment_id": experiment_id, "slot": "author-1",
                    "action": "freeze", "job_id": mismatch_job_id,
                })
                self.assertEqual((status, payload["code"]), (422, "agent_receipt_mismatch"))
                state = json.loads((self.records / experiment_id / "state.json").read_text())
                self.assertTrue(state["finalized"])
                self.assertEqual(self._post("/v1/code-off/cleanup", {"experiment_id": experiment_id})[0], 200)

    def test_terminal_validation_rejects_identity_divergence_at_every_surface(self):
        experiment_id = "terminal-identity-surfaces"
        self._prepare(experiment_id)
        job_id = self._launch_done(experiment_id, "author-1")
        pristine = server.read_job(job_id)
        state = json.loads((self.records / experiment_id / "state.json").read_text())
        manifest = json.loads((self.records / experiment_id / "manifest.json").read_text())
        forged = {"launcher": "claude", "provider": "anthropic", "model": "forged-model"}

        surface_paths = {
            "job": lambda job, _manifest, field, value: job.__setitem__(field, value),
            "session": lambda job, _manifest, field, value: job["session_identity"].__setitem__(field, value),
            "receipt_session": lambda job, _manifest, field, value: job["receipt"]["session_identity"].__setitem__(field, value),
            "execution": lambda job, _manifest, field, value: job["execution_identity"].__setitem__(field, value),
            "receipt_execution": lambda job, _manifest, field, value: job["receipt"]["execution_identity"].__setitem__(field, value),
            "manifest": lambda _job, manifest, field, value: manifest["identities"]["author-1"].__setitem__(
                "exact_model" if field == "model" else field, value
            ),
        }
        for surface, mutate in surface_paths.items():
            for field, value in forged.items():
                with self.subTest(surface=surface, field=field):
                    job = deepcopy(pristine)
                    checked_manifest = deepcopy(manifest)
                    mutate(job, checked_manifest, field, value)
                    _replace_persisted_job_fixture(job_id, job)
                    accepted, error = server._codeoff_validate_job(
                        job_id, state, checked_manifest, "author-1"
                    )
                    self.assertIsNone(accepted)
                    self.assertEqual(error["code"], "agent_receipt_mismatch")

        for field, value in forged.items():
            with self.subTest(surface="coherent_forged_session", field=field):
                job = deepcopy(pristine)
                job["session_identity"][field] = value
                job["receipt"]["session_identity"][field] = value
                job["execution_identity"][field] = value
                job["receipt"]["execution_identity"][field] = value
                _replace_persisted_job_fixture(job_id, job)
                accepted, error = server._codeoff_validate_job(job_id, state, manifest, "author-1")
                self.assertIsNone(accepted)
                self.assertEqual(error["code"], "agent_receipt_mismatch")
        _replace_persisted_job_fixture(job_id, pristine)

    def test_codeoff_terminal_validation_binds_complete_job_receipt_profile(self):
        experiment_id = "terminal-complete-profile"
        self._prepare(experiment_id)
        job_id = self._launch_done(experiment_id, "author-1")
        pristine = server.read_job(job_id)
        state = json.loads((self.records / experiment_id / "state.json").read_text())
        manifest = json.loads((self.records / experiment_id / "manifest.json").read_text())
        accepted, error = server._codeoff_validate_job(job_id, state, manifest, "author-1")
        self.assertIsNone(error)
        self.assertEqual(accepted, pristine)
        contradictions = {
            "launcher": "claude",
            "provider": "anthropic",
            "model": "claude-opus-5",
            "requested_effort": "high",
            "effective_effort": "default",
            "effort_source": "explicit",
            "launcher_version": "sha256:" + "f" * 64,
        }
        for field, value in contradictions.items():
            with self.subTest(field=field):
                job = deepcopy(pristine)
                job["receipt"][field] = value
                _replace_persisted_job_fixture(job_id, job)
                accepted, error = server._codeoff_validate_job(
                    job_id, state, manifest, "author-1"
                )
                self.assertIsNone(accepted)
                self.assertEqual(error["code"], "agent_receipt_mismatch")
        for field in server.AGENT_PROFILE_FIELDS:
            with self.subTest(missing_field=field):
                job = deepcopy(pristine)
                job["receipt"].pop(field)
                _replace_persisted_job_fixture(job_id, job)
                accepted, error = server._codeoff_validate_job(
                    job_id, state, manifest, "author-1"
                )
                self.assertIsNone(accepted)
                self.assertEqual(error["code"], "agent_receipt_mismatch")
        coordinated = deepcopy(pristine)
        coordinated["receipt"].update({
            "requested_effort": "high", "effective_effort": "high",
            "effort_source": "explicit",
        })
        coordinated["receipt"]["session_identity"].update({
            "requested_effort": "high", "effective_effort": "high",
            "effort_source": "explicit",
        })
        _replace_persisted_job_fixture(job_id, coordinated)
        accepted, error = server._codeoff_validate_job(job_id, state, manifest, "author-1")
        self.assertIsNone(accepted)
        self.assertEqual(error["code"], "agent_receipt_mismatch")
        _replace_persisted_job_fixture(job_id, pristine)

    def test_codeoff_terminal_validation_rejects_coordinated_complete_manifest_drift(self):
        experiment_id = "terminal-manifest-drift"
        self._prepare(experiment_id)
        job_id = self._launch_done(experiment_id, "author-1")
        pristine = server.read_job(job_id)
        state = json.loads((self.records / experiment_id / "state.json").read_text())
        manifest = json.loads((self.records / experiment_id / "manifest.json").read_text())
        accepted, error = server._codeoff_validate_job(job_id, state, manifest, "author-1")
        self.assertIsNone(error)
        self.assertEqual(accepted, pristine)

        def mutate_identity(job, field, value):
            job["session_identity"][field] = value
            job["receipt"]["session_identity"][field] = value

        def mutate_complete_profile(job):
            if job["launcher"] == "claude":
                profile = {
                    "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                    "requested_effort": "high", "effective_effort": "high",
                    "effort_source": "explicit", "launcher_version": "sha256:" + "e" * 64,
                }
                source = "codex-rollout-v1"
            else:
                profile = {
                    "launcher": "claude", "provider": "anthropic", "model": "claude-opus-5",
                    "requested_effort": "default", "effective_effort": "default",
                    "effort_source": "explicit", "launcher_version": "sha256:" + "e" * 64,
                }
                source = "claude-result-v1"
            job.update(profile)
            job["receipt"].update(profile)
            job["session_identity"].update(profile)
            job["receipt"]["session_identity"].update(profile)
            job["execution_identity"].update({
                "launcher": profile["launcher"], "provider": profile["provider"],
                "model": profile["model"], "source": source,
            })
            job["receipt"]["execution_identity"].update(job["execution_identity"])

        cases = {
            "complete_profile": mutate_complete_profile,
            "repo": lambda job: (
                job.__setitem__("repo", "forged-repo"),
                mutate_identity(job, "repo", "forged-repo"),
            ),
            "branch": lambda job: mutate_identity(job, "branch", "forged/branch"),
            "starting_head": lambda job: mutate_identity(job, "starting_head", "f" * 40),
            "requested_effective_source": lambda job: (
                job.update({
                    "requested_effort": "high", "effective_effort": "high",
                    "effort_source": "explicit",
                }),
                job["receipt"].update({
                    "requested_effort": "high", "effective_effort": "high",
                    "effort_source": "explicit",
                }),
                job["session_identity"].update({
                    "requested_effort": "high", "effective_effort": "high",
                    "effort_source": "explicit",
                }),
                job["receipt"]["session_identity"].update({
                    "requested_effort": "high", "effective_effort": "high",
                    "effort_source": "explicit",
                }),
            ),
            "launcher_version": lambda job: (
                job.__setitem__("launcher_version", "sha256:" + "f" * 64),
                job["receipt"].__setitem__("launcher_version", "sha256:" + "f" * 64),
                mutate_identity(job, "launcher_version", "sha256:" + "f" * 64),
            ),
            "native_session_id": lambda job: (
                mutate_identity(job, "native_session_id", "forged-native-session"),
                job["execution_identity"].__setitem__("native_session_id", "forged-native-session"),
                job["receipt"]["execution_identity"].__setitem__("native_session_id", "forged-native-session"),
            ),
            "launch_native_session_id": lambda job: job.__setitem__(
                "launch_native_session_id", "forged-launch-session"
            ),
            "missing_launch_native_session_id": lambda job: job.pop("launch_native_session_id"),
            "missing_top_level_branch": lambda job: job.pop("branch"),
            "job_id": lambda job: (
                job.__setitem__("job_id", "e" * 32),
                job["receipt"].__setitem__("job_id", "e" * 32),
            ),
        }
        for case, mutate in cases.items():
            with self.subTest(case=case):
                forged = deepcopy(pristine)
                mutate(forged)
                # Keep the original storage location: this is mutation of one
                # mutable record, not creation of another job.
                (self.jobs / job_id / "job.json").write_text(json.dumps(forged))
                accepted, error = server._codeoff_validate_job(
                    job_id, state, manifest, "author-1"
                )
                self.assertIsNone(accepted)
                self.assertEqual(error["code"], "agent_receipt_mismatch")

        for case, mutate_state in {
            "missing_manifest": lambda checked: checked.pop("execution_manifests"),
            "unknown_manifest": lambda checked: checked["execution_manifests"]["author-1"][job_id].update({
                "launch_manifest_hash": "f" * 64,
            }),
        }.items():
            with self.subTest(case=case):
                checked_state = deepcopy(state)
                mutate_state(checked_state)
                accepted, error = server._codeoff_validate_job(
                    job_id, checked_state, manifest, "author-1"
                )
                self.assertIsNone(accepted)
                self.assertEqual(error["code"], "agent_receipt_mismatch")
        (self.jobs / job_id / "job.json").write_text(json.dumps(pristine))

    def test_nonterminal_agent_receipt_remains_retryable(self):
        self._prepare()
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)), \
             mock.patch.object(server, "enqueue_agent"):
            status, launched = self._post("/v1/agent/run", self._agent_body("experiment-0001", "author-1"))
        self.assertEqual(status, 202, launched)
        status, payload = self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "freeze", "job_id": launched["job_id"],
        })
        self.assertEqual((status, payload["code"]), (409, "agent_not_terminal"))
        self.assertFalse(json.loads((self.records / "experiment-0001" / "state.json").read_text())["finalized"])

    def test_freeze_preserves_complete_tree_fidelity_and_detects_mutation(self):
        self._prepare()
        work = server.codeoff_workspace_path("experiment-0001", "author-1")
        (work / "README.md").write_bytes(b"changed\x00binary\xff")
        (work / "untracked.bin").write_bytes(bytes(range(256)))
        (work / "executable.sh").write_text("#!/bin/sh\nexit 0\n")
        (work / "executable.sh").chmod(0o755)
        (work / "delete-me.txt").unlink()
        subprocess.run(["git", "-C", str(work), "mv", "rename-me.txt", "renamed.txt"], check=True)
        (work / "link").symlink_to("renamed.txt")
        job_id = self._launch_done("experiment-0001", "author-1")
        status, frozen = self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "freeze", "job_id": job_id,
        })
        self.assertEqual(status, 200, frozen)
        raw = (self.records / "experiment-0001" / "artifacts" / frozen["tree_manifest_hash"]).read_bytes()
        tree = json.loads(raw)
        entries = {entry["path"]: entry for entry in tree["entries"]}
        self.assertEqual(entries["README.md"]["sha256"], hashlib.sha256(b"changed\x00binary\xff").hexdigest())
        self.assertEqual(entries["untracked.bin"]["size"], 256)
        self.assertEqual(entries["executable.sh"]["mode"], "0755")
        self.assertEqual(entries["link"]["type"], "symlink")
        self.assertEqual(entries["link"]["target"], "renamed.txt")
        self.assertNotIn("delete-me.txt", entries)
        self.assertTrue(any(c["status"].startswith("R") for c in tree["changes"]))
        self.assertTrue(any(c["status"] == "D" and c["path"] == "delete-me.txt" for c in tree["changes"]))
        (work / "renamed.txt").write_text("mutated after freeze\n")
        status, payload = self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "test",
        })
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload["code"], "candidate_mutated")
        self.assertEqual(self._post("/v1/code-off/cleanup", {"experiment_id": "experiment-0001"})[0], 200)

    def test_extraction_rejects_absolute_and_escaping_symlink_targets(self):
        def symlink_tar(name, target):
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w") as archive:
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                archive.addfile(info)
            return raw.getvalue()

        for index, (name, target) in enumerate((("absolute", "/etc/passwd"), ("nested/escape", "../../outside"))):
            with self.subTest(target=target):
                destination = Path(self.td.name) / f"unsafe-{index}"
                with self.assertRaisesRegex(RuntimeError, "unsafe candidate artifact"):
                    server._codeoff_extract(symlink_tar(name, target), destination)
                self.assertFalse((destination / name).exists())
        destination = Path(self.td.name) / "safe-relative"
        server._codeoff_extract(symlink_tar("nested/link", "../inside"), destination)
        self.assertTrue((destination / "nested" / "link").is_symlink())

    def test_freeze_parks_absolute_and_escaping_symlinks_before_artifact_creation(self):
        for index, target in enumerate(("/etc/passwd", "../../outside")):
            experiment_id = f"unsafe-link-{index:04d}"
            with self.subTest(target=target):
                self._prepare(experiment_id)
                work = server.codeoff_workspace_path(experiment_id, "author-1")
                (work / "nested").mkdir()
                (work / "nested" / "escape").symlink_to(target)
                job_id = self._launch_done(experiment_id, "author-1")
                status, payload = self._post("/v1/code-off/candidate", {
                    "experiment_id": experiment_id, "slot": "author-1", "action": "freeze", "job_id": job_id,
                })
                self.assertEqual((status, payload["code"]), (422, "candidate_unsafe_symlink"))
                state = json.loads((self.records / experiment_id / "state.json").read_text())
                self.assertTrue(state["finalized"])
                self.assertFalse(state["candidates"]["author-1"])
                self.assertEqual(self._post("/v1/code-off/cleanup", {"experiment_id": experiment_id})[0], 200)

    def test_freeze_parks_unencodable_symlink_metadata(self):
        self._prepare()
        work = server.codeoff_workspace_path("experiment-0001", "author-1")
        os.symlink(b"\xff", os.fsencode(work / "opaque-link"))
        job_id = self._launch_done("experiment-0001", "author-1")
        status, payload = self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "freeze", "job_id": job_id,
        })
        self.assertEqual((status, payload["code"]), (422, "candidate_freeze_failed"))
        self.assertEqual(self._post("/v1/code-off/cleanup", {"experiment_id": "experiment-0001"})[0], 200)

    def test_parallel_candidate_tests_do_not_serialize_on_global_lock(self):
        self._prepare()
        for slot in ("author-1", "author-2"):
            job_id = self._launch_done("experiment-0001", slot)
            status, payload = self._post("/v1/code-off/candidate", {
                "experiment_id": "experiment-0001", "slot": slot, "action": "freeze", "job_id": job_id,
            })
            self.assertEqual(status, 200, payload)
        barrier = threading.Barrier(2)
        original = server._codeoff_run_tests
        results, failures = {}, []

        def concurrent_tests(*args, **kwargs):
            barrier.wait(timeout=0.75)
            return original(*args, **kwargs)

        def worker(slot):
            try:
                results[slot] = self._post("/v1/code-off/candidate", {
                    "experiment_id": "experiment-0001", "slot": slot, "action": "test",
                })
            except BaseException as exc:  # capture thread evidence for the main assertion
                failures.append(exc)

        with mock.patch.object(server, "_codeoff_run_tests", side_effect=concurrent_tests):
            threads = [threading.Thread(target=worker, args=(slot,)) for slot in ("author-1", "author-2")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
        self.assertFalse(failures, failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual({slot: result[0] for slot, result in results.items()}, {"author-1": 200, "author-2": 200})
        state = json.loads((self.records / "experiment-0001" / "state.json").read_text())
        self.assertEqual(state["status"], "candidates_tested")
        self.assertTrue(all(state["candidates"][slot]["tests"]["pass"] for slot in results))

    def test_allowlisted_python_recipe_cannot_create_bytecode_mutation(self):
        self._prepare(tests=["fixture-import"])
        work = server.codeoff_workspace_path("experiment-0001", "author-1")
        (work / "candidate_module.py").write_text("VALUE = 1\n")
        frozen, tested = self._freeze_and_test("experiment-0001", "author-1")
        self.assertTrue(tested["tests_pass"], frozen)
        self.assertFalse((work / "__pycache__").exists())

    def test_recipe_tree_mutation_parks_and_allows_cleanup(self):
        self._prepare(tests=["fixture-mutates"])
        job_id = self._launch_done("experiment-0001", "author-1")
        self.assertEqual(self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "freeze", "job_id": job_id,
        })[0], 200)
        status, payload = self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "test",
        })
        self.assertEqual((status, payload["code"]), (422, "candidate_test_mutated"))
        self.assertTrue(json.loads((self.records / "experiment-0001" / "state.json").read_text())["finalized"])
        self.assertEqual(self._post("/v1/code-off/cleanup", {"experiment_id": "experiment-0001"})[0], 200)

    def test_corrupt_content_addressed_candidate_artifact_parks_and_cleans(self):
        self._prepare()
        job_id = self._launch_done("experiment-0001", "author-1")
        status, frozen = self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "freeze", "job_id": job_id,
        })
        self.assertEqual(status, 200, frozen)
        (self.records / "experiment-0001" / "artifacts" / frozen["artifact_hash"]).write_bytes(b"corrupt")
        status, payload = self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "test",
        })
        self.assertEqual((status, payload["code"]), (422, "candidate_artifact_invalid"))
        self.assertEqual(self._post("/v1/code-off/cleanup", {"experiment_id": "experiment-0001"})[0], 200)

    def test_corrupt_content_addressed_tree_manifest_parks_and_cleans(self):
        self._prepare()
        job_id = self._launch_done("experiment-0001", "author-1")
        status, frozen = self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "freeze", "job_id": job_id,
        })
        self.assertEqual(status, 200, frozen)
        (self.records / "experiment-0001" / "artifacts" / frozen["tree_manifest_hash"]).write_bytes(b"corrupt")
        status, payload = self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "test",
        })
        self.assertEqual((status, payload["code"]), (422, "candidate_artifact_invalid"))
        self.assertEqual(self._post("/v1/code-off/cleanup", {"experiment_id": "experiment-0001"})[0], 200)

    def test_candidate_extraction_integrity_failure_parks_and_cleans(self):
        self._prepare()
        job_id = self._launch_done("experiment-0001", "author-1")
        self.assertEqual(self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "freeze", "job_id": job_id,
        })[0], 200)
        with mock.patch.object(server, "_codeoff_extract", side_effect=server.CodeOffArtifactError("unsafe candidate artifact")):
            status, payload = self._post("/v1/code-off/candidate", {
                "experiment_id": "experiment-0001", "slot": "author-1", "action": "test",
            })
        self.assertEqual((status, payload["code"]), (422, "candidate_artifact_invalid"))
        self.assertEqual(self._post("/v1/code-off/cleanup", {"experiment_id": "experiment-0001"})[0], 200)

    def test_blind_gate_candidate_mutation_parks_and_cleans(self):
        self._prepare()
        for slot in ("author-1", "author-2"):
            self._freeze_and_test("experiment-0001", slot)
        (server.codeoff_workspace_path("experiment-0001", "author-1") / "README.md").write_text("late mutation\n")
        status, payload = self._post("/v1/code-off/blind", {"experiment_id": "experiment-0001"})
        self.assertEqual((status, payload["code"]), (422, "candidate_mutated"))
        self.assertTrue(json.loads((self.records / "experiment-0001" / "state.json").read_text())["finalized"])
        self.assertEqual(self._post("/v1/code-off/cleanup", {"experiment_id": "experiment-0001"})[0], 200)

    def test_blind_gate_late_artifact_corruption_parks_and_cleans(self):
        self._prepare()
        for slot in ("author-1", "author-2"):
            self._freeze_and_test("experiment-0001", slot)
        state = json.loads((self.records / "experiment-0001" / "state.json").read_text())
        digest = state["candidates"]["author-1"]["tree_manifest_hash"]
        (self.records / "experiment-0001" / "artifacts" / digest).write_bytes(b"late corruption")
        status, payload = self._post("/v1/code-off/blind", {"experiment_id": "experiment-0001"})
        self.assertEqual((status, payload["code"]), (422, "candidate_artifact_invalid"))
        self.assertEqual(self._post("/v1/code-off/cleanup", {"experiment_id": "experiment-0001"})[0], 200)

    def test_ignored_candidate_secret_parks_before_commit_eligibility(self):
        self._prepare()
        work = server.codeoff_workspace_path("experiment-0001", "author-1")
        (work / ".gitignore").write_text("secret.env\n")
        (work / "secret.env").write_text("TOKEN=private\n")
        job_id = self._launch_done("experiment-0001", "author-1")
        status, payload = self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "freeze", "job_id": job_id,
        })
        self.assertEqual((status, payload["code"]), (422, "candidate_ignored_paths"))
        state = json.loads((self.records / "experiment-0001" / "state.json").read_text())
        self.assertTrue(state["finalized"])
        self.assertIsNone(state.get("final_verification"))
        self.assertEqual(self._post("/v1/code-off/cleanup", {"experiment_id": "experiment-0001"})[0], 200)

    def test_clean_blind_bundles_ignore_generic_words_and_use_independent_maps(self):
        self._prepare(seed=(1).to_bytes(32, "big").hex())
        first = server.codeoff_workspace_path("experiment-0001", "author-1")
        (first / "ordinary-docs.txt").write_text("This branch uses an OpenAI SDK; worktree and session are ordinary engineering words.\n")
        for slot in ("author-1", "author-2"):
            self._freeze_and_test("experiment-0001", slot)
        status, payload = self._post("/v1/code-off/blind", {"experiment_id": "experiment-0001"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["leakage_findings"], 0)
        private = json.loads((self.records / "experiment-0001" / "private" / "blinding.json").read_text())
        orders = [tuple(item["labels"].values()) for item in private["judges"].values()]
        self.assertGreater(len(set(orders)), 1)
        for judge in payload["bundles"]:
            self.assertRegex(judge["bundle_hash"], r"^[0-9a-f]{64}$")
            self.assertNotIn("artifact", judge)

    def test_baseline_model_names_are_not_candidate_introduced_leakage(self):
        baseline = self.repo / "server_like.py"
        padding = "# server-like baseline padding\n" * 10000
        baseline.write_text('EXACT_MODEL = "gpt-5.6-sol"\n' + padding + "VALUE = 1\n")
        self._git("add", "server_like.py")
        self._git("commit", "-m", "server-like baseline")
        self.base_sha = self._git("rev-parse", "HEAD").stdout.strip()
        self._prepare()
        first = server.codeoff_workspace_path("experiment-0001", "author-1")
        (first / "server_like.py").write_text('EXACT_MODEL = "gpt-5.6-sol"\n' + padding + "VALUE = 2\n")
        for slot in ("author-1", "author-2"):
            self._freeze_and_test("experiment-0001", slot)
        status, payload = self._post("/v1/code-off/blind", {"experiment_id": "experiment-0001"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["leakage_findings"], 0)

    def test_judge_prompt_contains_locked_task_and_category_without_identity_metadata(self):
        task = "Repair the deterministic cache invalidation path.\n"
        self._prepare(prompt=task, category="backend", tags=["bug_fix", "testing"])
        for slot in ("author-1", "author-2"):
            self._freeze_and_test("experiment-0001", slot)
        self.assertEqual(self._post("/v1/code-off/blind", {"experiment_id": "experiment-0001"})[0], 200)
        resolved, err = server._resolve_codeoff_agent({"experiment_id": "experiment-0001", "slot": "judge-fable"})
        self.assertIsNone(err)
        prompt = resolved["prompt"].decode()
        self.assertIn(task.strip(), prompt)
        self.assertIn("backend", prompt)
        self.assertIn("bug_fix", prompt)
        for secret in ("author-1", "author-2", "logical_model", "exact_model", "gpt-5.6-sol", "claude-fable-5"):
            self.assertNotIn(secret, prompt)

    def test_targeted_identity_leakage_parks_before_any_judge_can_launch(self):
        self._prepare()
        first = server.codeoff_workspace_path("experiment-0001", "author-1")
        (first / "self-id.txt").write_text("I am the OpenAI Codex author using exact_model gpt-5.6-sol.\n")
        for slot in ("author-1", "author-2"):
            self._freeze_and_test("experiment-0001", slot)
        status, payload = self._post("/v1/code-off/blind", {"experiment_id": "experiment-0001"})
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload["code"], "candidate_identity_leakage")
        self.assertGreater(payload["leakage_findings"], 0)
        self.assertNotIn("openai", json.dumps(payload).lower())
        state = json.loads((self.records / "experiment-0001" / "state.json").read_text())
        self.assertTrue(state["finalized"])
        self.assertEqual(state["reason"], "candidate_identity_leakage")
        for slot in ("judge-fable", "judge-1", "judge-2"):
            self.assertFalse(server.codeoff_workspace_path("experiment-0001", slot).exists())
            resolved, err = server._resolve_codeoff_agent({"experiment_id": "experiment-0001", "slot": slot})
            self.assertIsNone(resolved)
            self.assertEqual(err["code"], "experiment_finalized")

    def test_strict_judge_parser_rejects_prose_ranges_types_keys_and_snapshot(self):
        good = {
            "rubric_version": "code-off-rubric-v1", "judge_snapshot_hash": "a" * 64,
            "candidates": {
                "candidate-1": {"scores": self._scores(), "pass": True, "rationale": "one"},
                "candidate-2": {"scores": self._scores(), "pass": False, "rationale": "two"},
            },
            "preference": "abstain", "rationale": "close",
        }
        parsed, err = server.parse_codeoff_judge_result(good, "a" * 64)
        self.assertIsNone(err)
        self.assertEqual(parsed["candidates"]["candidate-1"]["total"], 76)
        bad = [
            "candidate one seems better",
            {**good, "extra": 1},
            {**good, "judge_snapshot_hash": "b" * 64},
            {**good, "candidates": {**good["candidates"], "candidate-1": {**good["candidates"]["candidate-1"], "scores": {**self._scores(), "correctness": 41}}}},
            {**good, "candidates": {**good["candidates"], "candidate-1": {**good["candidates"]["candidate-1"], "pass": 1}}},
        ]
        for item in bad:
            with self.subTest(item=item):
                parsed, err = server.parse_codeoff_judge_result(item, "a" * 64)
                self.assertIsNone(parsed)
                self.assertIsNotNone(err)

    def test_aggregation_vote_score_tie_missing_and_test_conflict(self):
        mapping = {
            judge: {"candidate-1": "author-1", "candidate-2": "author-2"}
            for judge in ("judge-fable", "judge-1", "judge-2")
        }

        def receipt(pref, a, b):
            return {"preference": pref, "candidates": {
                "candidate-1": {"total": a}, "candidate-2": {"total": b},
            }}

        votes = {
            "judge-fable": receipt("candidate-1", 10, 90),
            "judge-1": receipt("candidate-1", 10, 90),
            "judge-2": receipt("candidate-2", 10, 90),
        }
        out, err = server.aggregate_codeoff_results(mapping, votes, {"author-1": True, "author-2": True})
        self.assertIsNone(err)
        self.assertEqual((out["winner"], out["basis"]), ("author-1", "preference-majority"))
        scores = {
            "judge-fable": receipt("candidate-1", 20, 80),
            "judge-1": receipt("candidate-2", 20, 80),
            "judge-2": receipt("abstain", 20, 80),
        }
        out, err = server.aggregate_codeoff_results(mapping, scores, {"author-1": True, "author-2": True})
        self.assertEqual((out["winner"], out["basis"]), ("author-2", "summed-scores"))
        tied = {judge: receipt("abstain", 50, 50) for judge in mapping}
        out, err = server.aggregate_codeoff_results(mapping, tied, {"author-1": True, "author-2": True})
        self.assertEqual(out["decision"], "parked")
        self.assertEqual(out["reason"], "exact_score_tie")
        out, err = server.aggregate_codeoff_results(mapping, dict(list(votes.items())[:2]), {"author-1": True, "author-2": True})
        self.assertIsNone(out)
        self.assertEqual(err["code"], "missing_judges")
        out, err = server.aggregate_codeoff_results(mapping, votes, {"author-1": False, "author-2": True})
        self.assertEqual(out["winner"], "author-1")
        self.assertFalse(out["promotion_eligible"])
        self.assertEqual(out["tests"], {"author-1": False, "author-2": True})

    def test_git_dependent_recipe_has_identical_candidate_and_final_failure_semantics(self):
        self._prepare(tests=["fixture-git-fail"])
        job_id = self._launch_done("experiment-0001", "author-1")
        status, frozen = self._post("/v1/code-off/candidate", {
            "experiment_id": "experiment-0001", "slot": "author-1", "action": "freeze", "job_id": job_id,
        })
        self.assertEqual(status, 200, frozen)
        root, manifest, state, err = server._codeoff_load("experiment-0001", active=True)
        self.assertIsNone(err)
        outcomes = [
            server._codeoff_verify_artifact(root, manifest, self.repo, state["candidates"]["author-1"], phase)
            for phase in ("candidate", "final")
        ]
        self.assertEqual([result["tests"]["pass"] for result in outcomes], [False, False])
        self.assertTrue(all(result["git_faithful"] for result in outcomes))
        self._assert_no_staging("experiment-0001")

    def test_finalize_rejects_mutated_winner_without_touching_target(self):
        self._ready_to_finalize()
        before = self._target_snapshot()
        winner = server.codeoff_workspace_path("experiment-0001", "author-1")
        (winner / "README.md").write_text("mutated after aggregation\n")
        status, payload = self._post("/v1/code-off/finalize", {"experiment_id": "experiment-0001"})
        self.assertEqual((status, payload["code"]), (409, "winner_mutated"))
        self.assertEqual(self._target_snapshot(), before)
        self._assert_no_staging("experiment-0001")

    def test_finalize_rejects_mutated_base_without_changing_it_further(self):
        self._ready_to_finalize()
        (self.repo / "README.md").write_text("operator mutation\n")
        before = self._target_snapshot()
        status, payload = self._post("/v1/code-off/finalize", {"experiment_id": "experiment-0001"})
        self.assertEqual((status, payload["code"]), (409, "promotion_base_mutated"))
        self.assertEqual(self._target_snapshot(), before)
        self._assert_no_staging("experiment-0001")

    def test_finalize_rejects_staged_base_index_without_touching_target(self):
        self._ready_to_finalize()
        (self.repo / "README.md").write_text("staged operator mutation\n")
        self._git("add", "README.md")
        self._git("restore", "--worktree", "--source=HEAD", "README.md")
        before = self._git("status", "--porcelain=v1").stdout
        self.assertTrue(before)
        status, payload = self._post("/v1/code-off/finalize", {"experiment_id": "experiment-0001"})
        self.assertEqual((status, payload["code"]), (409, "promotion_base_mismatch"))
        self.assertEqual(self._git("status", "--porcelain=v1").stdout, before)
        self._assert_no_staging("experiment-0001")

    def test_finalize_test_failure_leaves_target_pristine_and_cleans_staging(self):
        self._ready_to_finalize()
        before = self._target_snapshot()
        original = server.run_cmd

        def fail_final_recipe(args, *pargs, **kwargs):
            if args and args[0] == sys.executable:
                return {"ok": False, "returncode": 7, "stdout": "", "stderr": "final fixture failure", "truncated": False}
            return original(args, *pargs, **kwargs)

        with mock.patch.object(server, "run_cmd", side_effect=fail_final_recipe):
            status, payload = self._post("/v1/code-off/finalize", {"experiment_id": "experiment-0001"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "parked")
        self.assertFalse(payload["tests_pass"])
        self.assertEqual(self._target_snapshot(), before)
        self._assert_no_staging("experiment-0001")

    def test_finalize_apply_failure_rolls_target_back_and_cleans_staging(self):
        self._ready_to_finalize()
        before = self._target_snapshot()

        def partial_apply(_staging, target, _base_sha):
            (target / "README.md").write_text("partial broken apply\n")
            raise OSError("fixture apply interruption")

        with mock.patch.object(server, "_codeoff_apply_verified", side_effect=partial_apply):
            status, payload = self._post("/v1/code-off/finalize", {"experiment_id": "experiment-0001"})
        self.assertEqual((status, payload["code"]), (500, "promotion_apply_failed"))
        self.assertEqual(self._target_snapshot(), before)
        self._assert_no_staging("experiment-0001")

    def test_finalize_receipt_persistence_failure_restores_base_and_retries(self):
        self._ready_to_finalize()
        before = self._target_snapshot()
        with mock.patch.object(server, "_codeoff_write_state", side_effect=OSError("fixture final state interruption")):
            status, payload = self._post("/v1/code-off/finalize", {"experiment_id": "experiment-0001"})
        self.assertEqual((status, payload["code"]), (500, "codeoff_runtime_failure"))
        self.assertEqual(self._target_snapshot(), before)
        self._assert_no_staging("experiment-0001")
        status, payload = self._post("/v1/code-off/finalize", {"experiment_id": "experiment-0001"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["commit_eligible"])

    def test_manifest_event_state_and_final_record_tampering_fail_closed(self):
        for index, filename in enumerate(("manifest.json", "event", "state.json")):
            experiment_id = f"tamper-record-{index:02d}"
            with self.subTest(filename=filename):
                self._prepare(experiment_id)
                root = self.records / experiment_id
                path = root / filename
                if filename == "manifest.json":
                    value = json.loads(path.read_text())
                    value["category_source"] = "tampered"
                    path.write_text(json.dumps(value) + "\n")
                    expected = "manifest_tampered"
                elif filename == "event":
                    state = json.loads((root / "state.json").read_text())
                    path = root / "events" / f"{state['event_count']:08d}-{state['event_head']}.json"
                    value = json.loads(path.read_text())
                    value["kind"] = "tampered"
                    path.write_text(json.dumps(value) + "\n")
                    expected = "event_chain_tampered"
                else:
                    path.write_text("{not-json\n")
                    expected = "record_unreadable"
                status, payload, _ = server.dispatch(
                    "GET", "/v1/code-off/audit", {"experiment_id": [experiment_id]}, True, b"",
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], expected)

    def test_deleted_final_record_fails_closed_and_audit_is_read_only(self):
        self._ready_to_finalize()
        status, final = self._post("/v1/code-off/finalize", {"experiment_id": "experiment-0001"})
        self.assertEqual(status, 200, final)
        final_path = self.records / "experiment-0001" / "final.json"
        final_path.unlink()
        status, payload, _ = server.dispatch(
            "GET", "/v1/code-off/audit", {"experiment_id": ["experiment-0001"]}, True, b"",
        )
        self.assertEqual((status, payload["code"]), (400, "final_tampered"))
        self.assertFalse(final_path.exists())

    def test_codeoff_git_gate_rejects_repo_message_unavailable_and_wrong_tree(self):
        self._ready_to_finalize()
        status, final = self._post("/v1/code-off/finalize", {"experiment_id": "experiment-0001"})
        self.assertEqual(status, 200, final)
        gate = {"codeoff_experiment_id": "experiment-0001", "final_verification_hash": final["receipt_hash"], "message": "feat: promote code-off winner"}
        self.assertEqual(server._codeoff_git_gate(gate, "different", self.repo, "commit")["code"], "codeoff_repo_mismatch")
        self.assertEqual(server._codeoff_git_gate({**gate, "message": "wrong"}, "scratch", self.repo, "commit")["code"], "codeoff_commit_mismatch")
        with mock.patch.object(server, "codeoff_tree_snapshot", side_effect=RuntimeError("fixture unreadable tree")):
            self.assertEqual(server._codeoff_git_gate(gate, "scratch", self.repo, "commit")["code"], "codeoff_tree_unavailable")
        (self.repo / "README.md").write_text("wrong promoted tree\n")
        self.assertEqual(server._codeoff_git_gate(gate, "scratch", self.repo, "commit")["code"], "codeoff_tree_mismatch")

    def test_full_receipts_aggregate_promote_finalize_audit_hash_chain_and_cleanup(self):
        aggregate = self._ready_to_finalize()
        self.assertEqual(aggregate["winner"], "author-1")
        self.assertTrue(aggregate["promotion_eligible"])
        status, final = self._post("/v1/code-off/finalize", {"experiment_id": "experiment-0001"})
        self.assertEqual(status, 200, final)
        self.assertTrue(final["commit_eligible"])
        self.assertTrue(final["push_eligible"])
        state = json.loads((self.records / "experiment-0001" / "state.json").read_text())
        winner = state["candidates"]["author-1"]
        promoted = server.codeoff_tree_snapshot(self.repo)
        self.assertEqual(promoted["manifest_hash"], winner["tree_manifest_hash"])
        events = server._codeoff_events(self.records / "experiment-0001", state)
        previous = "0" * 64
        for number, event in enumerate(events, 1):
            self.assertEqual(event["sequence"], number)
            self.assertEqual(event["previous_hash"], previous)
            unhashed = {k: v for k, v in event.items() if k != "hash"}
            self.assertEqual(event["hash"], hashlib.sha256(server.codeoff_canonical_json(unhashed)).hexdigest())
            previous = event["hash"]
        status, audit, _ = server.dispatch("GET", "/v1/code-off/audit", {"experiment_id": ["experiment-0001"]}, True, b"")
        self.assertEqual(status, 200, audit)
        rendered = json.dumps(audit)
        self.assertNotIn("Implement the fixture", rendered)
        self.assertNotIn(str(self.repo), rendered)
        self.assertNotIn("blinding", rendered.lower())
        self.assertEqual(audit["event_head"], previous)
        gate = {"codeoff_experiment_id": "experiment-0001", "final_verification_hash": final["receipt_hash"], "message": "feat: promote code-off winner"}
        self.assertIsNone(server._codeoff_git_gate(gate, "scratch", self.repo, "commit"))
        rejected = server._codeoff_git_gate({**gate, "final_verification_hash": "0" * 64}, "scratch", self.repo, "commit")
        self.assertEqual(rejected["code"], "codeoff_gate_rejected")
        rejected = server._codeoff_git_gate(gate, "scratch", self.repo, "push")
        self.assertEqual(rejected["code"], "codeoff_head_mismatch")
        commit_status, committed = self._post("/v1/git/commit", {**gate, "repo": "scratch", "add": "."})
        self.assertEqual(commit_status, 200, committed)
        self.assertIsNone(server._codeoff_git_gate(gate, "scratch", self.repo, "push"))
        correct_head = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("rm", "--cached", "README.md")
        self._git("commit", "--amend", "--no-edit")
        rejected = server._codeoff_git_gate(gate, "scratch", self.repo, "push")
        self.assertEqual(rejected["code"], "codeoff_push_mismatch")
        self._git("reset", "--hard", correct_head)
        self._git("commit", "--allow-empty", "-m", "unexpected extra commit")
        rejected = server._codeoff_git_gate(gate, "scratch", self.repo, "push")
        self.assertEqual(rejected["code"], "codeoff_push_mismatch")
        final_path = self.records / "experiment-0001" / "final.json"
        final_bytes = final_path.read_bytes()
        final_path.write_text("{not-json\n")
        status, payload, _ = server.dispatch("GET", "/v1/code-off/audit", {"experiment_id": ["experiment-0001"]}, True, b"")
        self.assertEqual((status, payload["code"]), (400, "final_tampered"))
        final_path.write_bytes(final_bytes)
        status, payload = self._post("/v1/code-off/blind", {"experiment_id": "experiment-0001"})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "experiment_finalized")
        status, cleaned = self._post("/v1/code-off/cleanup", {"experiment_id": "experiment-0001"})
        self.assertEqual(status, 200, cleaned)
        self.assertFalse((self.workspaces / "experiment-0001").exists())
        self.assertTrue((self.records / "experiment-0001" / "final.json").is_file())

    def test_graph_failure_terminalizes_active_experiment_and_allows_cleanup(self):
        self._prepare("graph-failure-0001")
        status, terminal = self._post("/v1/code-off/terminal", {
            "experiment_id": "graph-failure-0001", "outcome": "failed",
        })
        self.assertEqual(status, 200, terminal)
        self.assertEqual((terminal["status"], terminal["reason"], terminal["finalized"]),
                         ("parked", "graph_terminal_failure", True))
        status, cleaned = self._post("/v1/code-off/cleanup", {"experiment_id": "graph-failure-0001"})
        self.assertEqual(status, 200, cleaned)

    def test_graph_failure_waits_for_launched_jobs_before_finalizing(self):
        experiment_id = "graph-drain-0001"
        self._prepare(experiment_id)
        state_path = self.records / experiment_id / "state.json"
        state = json.loads(state_path.read_text())
        job_id = "ab" * 16
        state["agent_jobs"]["author-1"] = [job_id]
        server._codeoff_write_state(self.records / experiment_id, state)
        running = _minimal_agent_record(job_id, "running")
        completed = _minimal_agent_record(job_id, "completed")
        with mock.patch.object(server, "read_job", side_effect=[running, completed]), \
             mock.patch.object(server.time, "sleep") as sleep:
            status, terminal = self._post("/v1/code-off/terminal", {
                "experiment_id": experiment_id, "outcome": "failed",
            })
        self.assertEqual(status, 200, terminal)
        self.assertTrue(terminal["finalized"])
        sleep.assert_called_once()

    def test_graph_failure_drains_every_launched_job_before_finalizing(self):
        experiment_id = "graph-drain-all-0001"
        self._prepare(experiment_id)
        state_path = self.records / experiment_id / "state.json"
        state = json.loads(state_path.read_text())
        first, second = "aa" * 16, "bb" * 16
        state["agent_jobs"]["author-1"] = [first]
        state["agent_jobs"]["author-2"] = [second]
        server._codeoff_write_state(self.records / experiment_id, state)
        reads = [
            _minimal_agent_record(first, "completed"), _minimal_agent_record(second, "running"),
            _minimal_agent_record(first, "completed"), _minimal_agent_record(second, "failed"),
        ]
        with mock.patch.object(server, "read_job", side_effect=reads), \
             mock.patch.object(server.time, "sleep") as sleep:
            status, terminal = self._post("/v1/code-off/terminal", {
                "experiment_id": experiment_id, "outcome": "failed",
            })
        self.assertEqual(status, 200, terminal)
        self.assertTrue(terminal["finalized"])
        sleep.assert_called_once()

    def test_graph_failure_refuses_missing_or_changed_launch_evidence(self):
        for index, failure in enumerate(("missing", "changed")):
            experiment_id = f"graph-launch-evidence-{index}"
            self._prepare(experiment_id)
            state_path = self.records / experiment_id / "state.json"
            state = json.loads(state_path.read_text())
            state["agent_jobs"]["author-1"] = ["ef" * 16]
            server._codeoff_write_state(self.records / experiment_id, state)
            if failure == "missing":
                context = mock.patch.object(server, "read_job", return_value=None)
            else:
                def change_launches(_state, path=state_path, root=self.records / experiment_id):
                    changed = json.loads(path.read_text())
                    changed["agent_jobs"]["author-2"] = ["ab" * 16]
                    server._codeoff_write_state(root, changed)
                    return None
                context = mock.patch.object(server, "_codeoff_wait_for_jobs", side_effect=change_launches)
            with context:
                status, terminal = self._post("/v1/code-off/terminal", {
                    "experiment_id": experiment_id, "outcome": "failed",
                })
            expected = "agent_job_missing" if failure == "missing" else "agent_jobs_changed"
            self.assertEqual((status, terminal["code"]), (409, expected))
            self.assertFalse(json.loads(state_path.read_text())["finalized"])

    def test_graph_failure_refuses_to_finalize_while_launched_job_remains_active(self):
        experiment_id = "graph-active-0001"
        self._prepare(experiment_id)
        state_path = self.records / experiment_id / "state.json"
        state = json.loads(state_path.read_text())
        job_id = "cd" * 16
        state["agent_jobs"]["author-1"] = [job_id]
        server._codeoff_write_state(self.records / experiment_id, state)
        with mock.patch.object(server, "read_job", return_value=_minimal_agent_record(job_id, "running")), \
             mock.patch.object(server.time, "monotonic", side_effect=[0, server.CODEOFF_TERMINAL_DRAIN_SECONDS + 1]):
            status, terminal = self._post("/v1/code-off/terminal", {
                "experiment_id": experiment_id, "outcome": "failed",
            })
        self.assertEqual((status, terminal["code"]), (409, "agent_jobs_active"))
        state = json.loads(state_path.read_text())
        self.assertFalse(state["finalized"])

    def test_graph_success_terminal_is_idempotent_after_durable_finalization(self):
        self._prepare("active-success-0001")
        state_path = self.records / "active-success-0001" / "state.json"
        before = state_path.read_bytes()
        status, active = self._post("/v1/code-off/terminal", {
            "experiment_id": "active-success-0001", "outcome": "succeeded",
        })
        self.assertEqual((status, active["code"]), (409, "not_finalized"))
        self.assertEqual(state_path.read_bytes(), before)

        status, missing = self._post("/v1/code-off/terminal", {
            "experiment_id": "missing-success-0001", "outcome": "succeeded",
        })
        self.assertEqual((status, missing["code"]), (409, "not_finalized"))

        self._ready_to_finalize("graph-success-0001")
        status, finalized = self._post("/v1/code-off/finalize", {"experiment_id": "graph-success-0001"})
        self.assertEqual(status, 200, finalized)
        status, terminal = self._post("/v1/code-off/terminal", {
            "experiment_id": "graph-success-0001", "outcome": "succeeded",
        })
        self.assertEqual(status, 200, terminal)
        self.assertEqual((terminal["status"], terminal["finalized"]), ("completed", True))

    def test_cleanup_refuses_before_finalization_and_auth_is_required(self):
        self._prepare()
        status, payload = self._post("/v1/code-off/cleanup", {"experiment_id": "experiment-0001"})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "not_finalized")
        status, payload = self._post("/v1/code-off/audit", {"experiment_id": "experiment-0001"}, authed=False)
        self.assertEqual(status, 401)

    def test_codeoff_openapi_identity_contract_matches_runtime_manifest(self):
        spec = json.loads(server.openapi_bytes())
        schemas = spec["components"]["schemas"]

        def tuples(name, fields):
            return {
                tuple(variant["properties"][field]["const"] for field in fields)
                for variant in schemas[name]["oneOf"]
            }

        codeoff = {
            (identity["launcher"], identity["provider"], identity["exact_model"])
            for identity in server.CODEOFF_MODEL_MANIFEST["models"].values()
        }
        request = schemas["AgentRunRequest"]
        self.assertNotIn("oneOf", request)
        self.assertNotIn("allOf", request)
        self.assertEqual(tuples("CodeOffIdentityTuple", ("launcher", "provider", "model")), codeoff)
        self.assertEqual(
            schemas["SessionIdentity"]["allOf"],
            [{"$ref": "#/components/schemas/CodeOffIdentityTuple"}],
        )
        self.assertNotIn(("codex", "anthropic", "gpt-5.6-terra"), codeoff)
        self.assertNotIn(("claude", "openai", "claude-fable-5"), codeoff)

        execution = schemas["CodeOffExecutionIdentity"]
        self.assertFalse(execution["additionalProperties"])
        self.assertEqual(
            set(execution["required"]),
            {"version", "source", "launcher", "provider", "model", "native_session_id"},
        )
        self.assertEqual(
            tuples("CodeOffExecutionIdentity", ("launcher", "provider", "model", "source")),
            {(*identity, server._codeoff_provenance_source(identity[0])) for identity in codeoff},
        )
        self.assertEqual(
            schemas["AgentReceipt"]["properties"]["execution_identity"]["$ref"],
            "#/components/schemas/CodeOffExecutionIdentity",
        )
        for name in ("AgentJob", "CodeOffCompact"):
            self.assertNotIn("execution_identity", schemas[name]["properties"])

    def test_codeoff_openapi_is_strict_authenticated_and_agent_workspace_has_no_path(self):
        spec = json.loads(server.openapi_bytes())
        expected = {
            "/v1/code-off/prepare": "codeOffPrepare",
            "/v1/code-off/candidate": "codeOffCandidate",
            "/v1/code-off/blind": "codeOffBlind",
            "/v1/code-off/judge": "codeOffJudge",
            "/v1/code-off/aggregate": "codeOffAggregate",
            "/v1/code-off/finalize": "codeOffFinalize",
            "/v1/code-off/audit": "codeOffAudit",
            "/v1/code-off/cleanup": "codeOffCleanup",
            "/v1/code-off/terminal": "codeOffTerminal",
        }
        for path, operation_id in expected.items():
            method = "get" if path.endswith("audit") else "post"
            self.assertEqual(spec["paths"][path][method]["operationId"], operation_id)
        for name in ("CodeOffPrepareRequest", "CodeOffCandidateRequest", "CodeOffJudgeRequest", "CodeOffExperimentRequest", "CodeOffTerminalRequest", "CodeOffTerminalResult", "CodeOffWorkspaceRef", "CodeOffJudgeResult", "CodeOffPrepareResult", "CodeOffCompact"):
            self.assertFalse(spec["components"]["schemas"][name]["additionalProperties"], name)
        workspace = spec["components"]["schemas"]["CodeOffWorkspaceRef"]
        self.assertEqual(set(workspace["required"]), {"experiment_id", "slot"})
        self.assertNotIn("path", workspace["properties"])
        prepare = spec["components"]["schemas"]["CodeOffPrepareRequest"]["properties"]
        for field, native_type in (("tags", "array"), ("tests", "array"), ("toolchain", "object"), ("budgets", "object")):
            variants = prepare[field]["oneOf"]
            self.assertEqual({item.get("type") for item in variants}, {native_type, "string"}, field)
            string_variant = next(item for item in variants if item.get("type") == "string")
            self.assertLessEqual(string_variant["maxLength"], 8192)
        selection = spec["components"]["schemas"]["CodeOffPublicSelection"]
        self.assertFalse(selection["additionalProperties"])
        self.assertNotIn("seed", selection["properties"])
        self.assertIn("seed_commitment", selection["required"])
        agent = spec["components"]["schemas"]["AgentRunRequest"]["properties"]
        self.assertIn("codeoff_workspace", agent)
        for field in ("max_turns", "run_budget_seconds"):
            self.assertEqual({item["type"] for item in agent[field]["oneOf"]}, {"integer", "string"})

    def test_codeoff_graph_is_bounded_waited_fanned_in_and_terminal_gated(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "code-off.json").read_text())
        self.assertEqual(graph["slug"], "graphwing-code-off")
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        triples = {(e["source"], e.get("sourceHandle"), e["target"]) for e in edges}

        allowed_handles = {
            "trigger.manual": {"out"},
            "logic.filter": {"pass", "fail"},
            "logic.switch": {"case-0", "default"},
            "logic.join.any": {"out"},
            "logic.join.all": {"out"},
            "action.wait.webhook": {"pending", "out", "timeout", "failure"},
        }
        for edge in edges:
            self.assertEqual(edge.get("targetHandle"), "in", edge)
            source_type = nodes[edge["source"]]["type"]
            expected = {"success", "failure"} if source_type.startswith("action.graphwing.") else allowed_handles.get(source_type)
            self.assertIsNotNone(expected, edge)
            self.assertIn(edge.get("sourceHandle"), expected, edge)

        self.assertIn(("trigger", "out", "prepare"), triples)
        self.assertIn(("prepare", "success", "switch_prepare"), triples)
        self.assertIn(("switch_prepare", "default", "wait_author_1"), triples)
        self.assertIn(("switch_prepare", "case-0", "join_terminal"), triples)
        waits = {
            "wait_author_1": "author_1", "wait_author_2": "author_2",
            "wait_judge_fable": "judge_fable", "wait_judge_1": "judge_1", "wait_judge_2": "judge_2",
        }
        wait_filters = {
            "wait_author_1": "author_ok_1", "wait_author_2": "author_ok_2",
            "wait_judge_fable": "judge_ok_fable", "wait_judge_1": "judge_ok_1", "wait_judge_2": "judge_ok_2",
        }
        success_targets = {
            "author_1": {"wait_author_2"}, "author_2": set(),
            "judge_fable": {"wait_judge_1"}, "judge_1": {"wait_judge_2"}, "judge_2": set(),
        }
        targets = lambda source, handle: {
            edge["target"] for edge in edges if edge["source"] == source and edge["sourceHandle"] == handle
        }
        self.assertFalse(any(node["type"] == "action.graphwing.POST:/v1/agent/jobs/wait" for node in nodes.values()))
        for wait, launch in waits.items():
            self.assertEqual(nodes[wait]["type"], "action.wait.webhook")
            self.assertEqual(nodes[wait]["config"], {
                "alias": wait, "allowedMethods": ["POST"], "authMode": "token",
                "responseStatus": 200, "timeoutSeconds": 1320,
            })
            self.assertEqual(nodes[launch]["config"]["response_webhook_url"], f"{{{{ TASKS.{wait}.pending.resumeUrl }}}}")
            self.assertEqual(nodes[launch]["config"]["response_webhook_token"], f"{{{{ TASKS.{wait}.pending.resumeToken }}}}")
            self.assertEqual(targets(wait, "pending"), {launch})
            self.assertEqual(targets(wait, "out"), {wait_filters[wait]})
            self.assertEqual(targets(wait, "failure"), {"join_terminal"})
            self.assertEqual(targets(wait, "timeout"), {"join_terminal"})
            self.assertEqual(targets(launch, "success"), success_targets[launch])
            self.assertEqual(
                {(edge["source"], edge["sourceHandle"]) for edge in edges if edge["target"] == wait_filters[wait]},
                {(wait, "out")},
            )
            self.assertEqual(nodes[wait_filters[wait]]["config"], {
                "group": "AND", "rules": [{"path": "request.body.status", "op": "equals", "value": "ok"}],
            })
            self.assertGreaterEqual(nodes[wait]["config"]["timeoutSeconds"], server.CODEOFF_TERMINAL_DRAIN_SECONDS)
        self.assertIn(("author_1", "success", "wait_author_2"), triples)
        self.assertIn(("blind", "success", "wait_judge_fable"), triples)
        self.assertIn(("judge_fable", "success", "wait_judge_1"), triples)
        self.assertIn(("judge_1", "success", "wait_judge_2"), triples)
        self.assertEqual(nodes["join_candidates"]["type"], "logic.join.all")
        self.assertEqual(nodes["join_judges"]["type"], "logic.join.all")
        incoming = lambda target: {
            (edge["source"], edge["sourceHandle"]) for edge in edges if edge["target"] == target
        }
        self.assertEqual(incoming("join_candidates"), {("test_1", "success"), ("test_2", "success")})
        self.assertEqual(incoming("blind"), {("join_candidates", "out")})
        self.assertEqual(incoming("join_judges"), {
            ("receipt_fable", "success"), ("receipt_1", "success"), ("receipt_2", "success"),
        })
        self.assertEqual(incoming("aggregate"), {("join_judges", "out")})
        self.assertEqual(incoming("eligible"), {("aggregate", "success")})
        self.assertEqual(set(nodes) - {edge["target"] for edge in edges}, {"trigger"})
        for filter_id in ("author_ok_1", "author_ok_2", "judge_ok_fable", "judge_ok_1", "judge_ok_2", "eligible", "final_verified"):
            self.assertIn((filter_id, "fail", "join_terminal"), triples, filter_id)
        forward = {}
        for edge in edges:
            forward.setdefault(edge["source"], []).append(edge["target"])
        def paths_to(target):
            found = []
            def walk(node, path):
                if node == target:
                    found.append(path + [node])
                    return
                for child in forward.get(node, ()):
                    if child not in path:
                        walk(child, path + [node])
            walk("trigger", [])
            return found
        self.assertTrue(all("author_1" in path for path in paths_to("author_2")))
        self.assertTrue(all({"judge_fable", "judge_1"} <= set(path) for path in paths_to("judge_2")))
        leaves = {node_id for node_id in nodes if not forward.get(node_id)}
        self.assertEqual(leaves, {"done", "terminal", "terminal_error", "success_confirmation_error"})
        for leaf in leaves:
            self.assertTrue(all({"terminalize_failed", "terminalize_success"} & set(path) for path in paths_to(leaf)), leaf)
        self.assertIn(("eligible", "pass", "finalize"), triples)
        self.assertIn(("final_verified", "pass", "commit"), triples)
        self.assertIn(("push", "success", "terminalize_success"), triples)
        self.assertIn(("terminalize_success", "success", "done"), triples)
        self.assertIn(("join_terminal", "out", "terminalize_failed"), triples)
        self.assertIn(("terminalize_failed", "success", "terminal"), triples)
        self.assertIn(("terminalize_failed", "failure", "terminal_error"), triples)
        self.assertIn(("terminalize_success", "failure", "success_confirmation_error"), triples)
        self.assertEqual(nodes["terminal_error"]["type"], "transforms.objectBuilder")
        self.assertEqual(nodes["success_confirmation_error"]["type"], "transforms.objectBuilder")
        self.assertGreater(nodes["terminalize_failed"]["config"]["timeout"], server.CODEOFF_TERMINAL_DRAIN_SECONDS)
        self.assertFalse(any("merge" in node["type"].lower() for node in nodes.values()))

        openapi = json.loads(server.openapi_bytes())
        openapi_paths = openapi["paths"]
        graphwing_actions = {node_id: node for node_id, node in nodes.items() if node["type"].startswith("action.graphwing.")}

        def accepts(value, schema, where):
            if "$ref" in schema:
                schema = openapi["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
            if "oneOf" in schema:
                failures = []
                for variant in schema["oneOf"]:
                    try:
                        accepts(value, variant, where)
                        return
                    except AssertionError as exc:
                        failures.append(str(exc))
                self.fail(f"{where} matches no schema variant: {failures}")
            expected = schema.get("type")
            if isinstance(expected, list):
                self.assertIn(type(value).__name__.replace("NoneType", "null"), expected, where)
            elif expected == "object":
                self.assertIsInstance(value, dict, where)
                properties = schema.get("properties", {})
                self.assertTrue(set(schema.get("required", ())) <= set(value), where)
                if schema.get("additionalProperties") is False:
                    self.assertTrue(set(value) <= set(properties), where)
                for key, item in value.items():
                    if key in properties:
                        accepts(item, properties[key], f"{where}.{key}")
            elif expected == "array": self.assertIsInstance(value, list, where)
            elif expected == "string":
                self.assertIsInstance(value, str, where)
                if "{{" not in value and "enum" in schema: self.assertIn(value, schema["enum"], where)
            elif expected == "integer": self.assertIs(type(value), int, where)
            elif expected == "boolean": self.assertIs(type(value), bool, where)

        for node_id, node in graphwing_actions.items():
            action, endpoint = node["type"].removeprefix("action.graphwing.").split(":", 1)
            self.assertIn(endpoint, openapi_paths, node_id)
            request_schema = openapi_paths[endpoint][action.lower()]["requestBody"]["content"]["application/json"]["schema"]
            accepts({key: value for key, value in node["config"].items() if key not in {"integrationInstanceId", "timeout"}}, request_schema, node_id)
            expected_target = {"terminalize_failed": "terminal_error", "terminalize_success": "success_confirmation_error"}.get(node_id, "join_terminal")
            self.assertIn((node_id, "failure", expected_target), triples, node_id)

        self.assertEqual(nodes["freeze_1"]["config"]["job_id"], "{{ TASKS.wait_author_1.request.body.job_id }}")
        self.assertEqual(nodes["freeze_2"]["config"]["job_id"], "{{ TASKS.wait_author_2.request.body.job_id }}")
        self.assertEqual(nodes["receipt_fable"]["config"]["job_id"], "{{ TASKS.wait_judge_fable.request.body.job_id }}")
        self.assertEqual(nodes["receipt_1"]["config"]["job_id"], "{{ TASKS.wait_judge_1.request.body.job_id }}")
        self.assertEqual(nodes["receipt_2"]["config"]["job_id"], "{{ TASKS.wait_judge_2.request.body.job_id }}")
        self.assertEqual(nodes["commit"]["config"]["final_verification_hash"], "{{ TASKS.finalize.data.receipt_hash }}")
        self.assertEqual(nodes["push"]["config"]["final_verification_hash"], "{{ TASKS.finalize.data.receipt_hash }}")

        outgoing = {}
        for edge in edges: outgoing.setdefault(edge["source"], []).append(edge["target"])
        color = {node: 0 for node in nodes}
        def visit(node):
            color[node] = 1
            for target in outgoing.get(node, []):
                self.assertNotEqual(color[target], 1, f"cycle through {target}")
                if color[target] == 0: visit(target)
            color[node] = 2
        for node in nodes:
            if color[node] == 0: visit(node)
        self.assertNotIn("retry", json.dumps(graph).lower())

    def test_publish_catalog_includes_codeoff_and_validated_implement_slice(self):
        source = (Path(server.__file__).parent / "scripts" / "publish_graphs.py").read_text()
        self.assertIn('"code-off"', source)
        self.assertIn('install["code_off"]', source)
        implement = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        spec = json.dumps(implement["spec"], sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(spec).hexdigest(), "f5ebdd51a46a33f3bb129612e301b4d12f46d3d48aa1545d35349194e7bf8db9")


class InstallTests(unittest.TestCase):
    def test_runtime_and_templates_have_no_hardcoded_home(self):
        root = Path(__file__).resolve().parent
        for rel in (
            "server.py",
            "install.py",
            "start.sh",
            "setup_tunnel.py",
            "scripts/publish_graphs.py",
            "systemd/graphwing-api.service",
            "systemd/graphwing-herdr.service",
            "systemd/graphwing-tunnel.service",
            "systemd/graphwing-tunnel-demo.service",
        ):
            text = (root / rel).read_text()
            self.assertNotIn("/home/tim", text, rel)

    def test_install_noninteractive_tmpdir(self):
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "gw"
            units = Path(td) / "units"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(root / "install.py"),
                    "--home",
                    str(home),
                    "--unit-dir",
                    str(units),
                    "--non-interactive",
                    "--no-claude-plugin",
                    "--no-omarchy-plugin",
                    "--tunnel",
                    "none",
                ],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "GRAPHWING_GH_BIN": "/fixture/bin/gh"},
            )
            self.assertTrue((home / "server.py").is_file())
            self.assertTrue((home / "openapi.json").is_file())
            key = (home / "api.key").read_text().strip()
            self.assertTrue(key)
            self.assertEqual((home / "api.key").stat().st_mode & 0o777, 0o600)
            repos = json.loads((home / "repos.json").read_text())
            self.assertEqual(repos, {})
            self.assertTrue((home / "repos.example.json").is_file())
            self.assertFalse((home / "rr.json").exists())
            self.assertTrue((home / "rr.example.json").is_file())
            stacks = json.loads((home / "stacks.json").read_text())
            self.assertEqual(stacks["stacks"][0]["name"], "graphwing")
            api_unit = (units / "graphwing-api.service").read_text()
            self.assertIn(str(home), api_unit)
            self.assertNotIn("@GRAPHWING_HOME@", api_unit)
            self.assertIn("Environment=GRAPHWING_GH_BIN=/fixture/bin/gh", api_unit)
            self.assertNotIn("@GH@", api_unit)
            self.assertFalse((units / "graphwing-tunnel.service").exists())
            self.assertNotIn("copied secrets", proc.stdout.lower())
            spec = json.loads((home / "openapi.json").read_text())
            self.assertEqual(spec["servers"][0]["url"], "http://127.0.0.1:8645")
            self.assertFalse((home / "cloudflared-meta.json").exists())
            self.assertFalse((home / "rewst-install.json").exists())
            self.assertTrue((home / "rewst-install.example.json").is_file())
            self.assertTrue((home / "doorbell.example.json").is_file())
            self.assertFalse((home / "SOUL.md").exists())
            self.assertFalse((home / "profiles.json").exists())
            self.assertFalse((home / "bin" / "graphwing").exists())

    def test_installer_prefers_mise_selected_native_binary(self):
        import install

        with tempfile.TemporaryDirectory() as td:
            native = Path(td) / "native-gh"
            native.write_text("fixture")
            native.chmod(0o755)
            selected = subprocess.CompletedProcess(
                ["/usr/bin/mise", "which", "gh"], 0, stdout=f"{native}\n", stderr=""
            )
            with mock.patch.dict(os.environ, {"GRAPHWING_GH_BIN": ""}), mock.patch.object(
                install.shutil,
                "which",
                side_effect=lambda name: "/usr/bin/mise" if name == "mise" else "/shim/gh",
            ), mock.patch.object(install.subprocess, "run", return_value=selected):
                self.assertEqual(install.which_or("gh", Path("/fallback/gh")), native)

    def test_ensure_repos_noninteractive_empty(self):
        import install

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            repos = install.ensure_repos(home, Path(td) / "clone", True, extra=[])
            self.assertEqual(repos, {})
            self.assertEqual(json.loads((home / "repos.json").read_text()), {})

    def test_ensure_repos_flag(self):
        import install

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            clone = Path(td) / "clone"
            clone.mkdir()
            (clone / ".git").mkdir()
            repos = install.ensure_repos(
                home, Path("/unused"), True, extra=[f"app={clone}"]
            )
            self.assertEqual(repos, {"app": str(clone.resolve())})

    def test_start_sh_yes_no_start_tmpdir(self):
        root = Path(__file__).resolve().parent
        script = root / "start.sh"
        subprocess.run(["bash", "-n", str(script)], check=True)
        text = script.read_text()
        self.assertNotIn("/home/tim", text)
        self.assertNotIn("model install", text.lower())
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "gw"
            units = Path(td) / "units"
            proc = subprocess.run(
                [
                    "bash",
                    str(script),
                    "--yes",
                    "--no-herdr",
                    "--tunnel",
                    "none",
                    "--home",
                    str(home),
                    "--unit-dir",
                    str(units),
                    "--no-start",
                ],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "HOME": td},
            )
            self.assertTrue((home / "server.py").is_file())
            self.assertTrue((home / "api.key").read_text().strip())
            self.assertFalse((home / "rr.json").exists())
            self.assertTrue((home / "rr.example.json").is_file())
            self.assertFalse((units / "graphwing-tunnel.service").exists())
            self.assertIn("not starting", proc.stdout)
            self.assertIn("native model launchers must already be installed", proc.stdout)
            self.assertNotIn("installing herdr", proc.stdout)


if __name__ == "__main__":
    unittest.main()
