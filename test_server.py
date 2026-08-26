#!/usr/bin/env python3
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
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

os.environ["GRAPHWING_HOME"] = str(Path(__file__).resolve().parent)
os.environ["GRAPHWING_HERDR"] = "0"

import server  # noqa: E402


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
            self.assertEqual(gp["prompt"], "ping")
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
        self.assertEqual(spawn.call_args.args[1], current)

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
                captured.append(command)
                output_index = command.index("--output-last-message") + 1
                Path(command[output_index]).write_text(
                    '{"status":"ok","sha":null,"pr_url":null,"summary":"VERDICT: PASS"}'
                )
                return FakeCompleted()

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                 mock.patch.object(server, "git_diff", return_value={"diff": "d"}), \
                 mock.patch.object(server.subprocess, "run", fake_run):
                result = server.native_review_result(
                    "codex", "openai", "gpt-5.6-sol", "ticket", root
                )
        self.assertTrue(result["ok"], result)
        self.assertEqual(captured[0][0], str(codex))

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
            identity = {
                "launcher": "grok", "provider": "xai", "model": "grok-4.6",
                "repo": "scratch", "branch": branch, "starting_head": head,
                "native_session_id": "grok-123",
            }
            prior_job_id = "ab" * 16
            prior_dir = jobs / prior_job_id
            prior_dir.mkdir(parents=True)
            prior = {
                "job_id": prior_job_id, "status": "completed", "launcher": "grok",
                "repo": "scratch", "cwd": str(repo), "session_identity": identity,
                "receipt": {"status": "ok", "session_identity": identity, "summary": "done"},
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
                    self.assertEqual(payload["code"], "session_identity_mismatch", key)
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
            identity = {"launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", "repo": "scratch", "branch": branch, "starting_head": head, "native_session_id": "codex-123"}
            prior_job_id = "ab" * 16
            prior_dir = jobs / prior_job_id
            prior_dir.mkdir(parents=True)
            prior = {
                "job_id": prior_job_id, "status": "completed", "launcher": "codex",
                "repo": "scratch", "cwd": str(repo), "session_identity": identity,
                "receipt": {"status": "ok", "session_identity": identity, "summary": "done"},
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
                    self.assertEqual(payload["code"], "resume_repository_mismatch", mismatch)
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
            identity = {
                "launcher": "claude", "provider": "anthropic", "model": "claude-opus-5",
                "repo": "scratch", "branch": branch, "starting_head": head,
                "native_session_id": "claude-123",
            }
            prior_job_id = "ab" * 16
            prior_dir = jobs / prior_job_id
            prior_dir.mkdir(parents=True)
            prior = {
                "job_id": prior_job_id, "status": "completed", "launcher": "claude",
                "repo": "scratch", "cwd": str(repo), "session_identity": identity,
                "receipt": {"status": "ok", "session_identity": identity, "summary": "done"},
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
            identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "repo": "scratch", "branch": branch, "starting_head": old_head,
                "native_session_id": "codex-123",
            }
            prior_job_id = "ab" * 16
            prior_dir = jobs / prior_job_id
            prior_dir.mkdir(parents=True)
            prior = {
                "job_id": prior_job_id, "status": "completed", "launcher": "codex",
                "repo": "scratch", "cwd": str(repo), "session_identity": identity,
                "receipt": {"status": "ok", "session_identity": identity, "summary": "done"},
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
            "result": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}',
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
            "session_provenance": {"session_identity_missing", "session_identity_mismatch", "session_provenance_invalid"},
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
                "unknown_stack",
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
        job = {"job_id": "ab" * 16, "launcher": "codex", "log_ref": "/tmp/log", "session_identity": {"native_session_id": "native-123"}}
        receipts = (
            (server.normalize_receipt(job, {"status": "ok", "summary": "done"}, 0, False), "none", "none"),
            (server.normalize_receipt(job, None, 0, False), "malformed_contract", "invalid_receipt"),
            (server.normalize_receipt(job, {"status": "error", "summary": "specific"}, 0, False), "model_execution", "model_reported_error"),
            (server.normalize_receipt(job, {"status": "timeout", "summary": "budget"}, 0, False), "timeout_budget", "reported_timeout"),
        )
        for receipt, failure_class, failure_code in receipts:
            self.assertEqual((receipt["failure_class"], receipt["failure_code"]), (failure_class, failure_code))
        self.assertEqual(receipts[-2][0]["summary"], "specific")

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
                self.assertIn(f"missing {launcher} binary", receipt["summary"])

    def test_wrap_prompt_locks_cwd_and_leaves_staging_to_graph(self):
        text = server.wrap_prompt("ab" * 16, "ping", "/home/tim/work/gw-real-slice")
        self.assertIn("/home/tim/work/gw-real-slice", text)
        self.assertIn("only inside that directory", text)
        self.assertNotIn("git add", text)
        self.assertIn("Graphwing stages", text)
        self.assertIn("Do not git commit, git push", text)
        self.assertIn("Do not `git checkout`", text)

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
            job = {
                "job_id": job_id, "status": "queued", "repo": "scratch", "cwd": str(repo),
                "prompt": "x", "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "session_identity": {"launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", "repo": "scratch", "branch": "main", "starting_head": head, "native_session_id": None},
                "created_at": "t", "started_at": None, "finished_at": None, "max_turns": 1,
                "run_budget_seconds": 30, "receipt": None, "log_ref": str(jdir / "stdout.log"),
                "error": None, "webhook": None, "response_webhook_url": "https://example.invalid/resume",
                "git_baseline": {"dirty": {}}, "writer_parent_paths": [],
            }
            (jdir / "job.json").write_text(json.dumps(job))
            (jdir / "prompt.txt").write_text("x")

            class FakeProc:
                pid = 42
                returncode = 0
                def wait(self, timeout=None):
                    (jdir / "stdout.log").write_text(
                        '{"type":"thread.started","thread_id":"codex-123"}\n'
                        + json.dumps({"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}'}})
                    )
                    return 0

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=root / "codex"), \
                 mock.patch.object(Path, "is_file", return_value=True), \
                 mock.patch.object(server, "spawn_writer", return_value=(FakeProc(), None)), \
                 mock.patch.object(server, "stage_writer_changes", side_effect=OSError("private detail")), \
                 mock.patch.object(server, "post_receipt", return_value={"ok": True, "status": 200}) as posted:
                server.run_agent_job(job_id)
                saved = server.read_job(job_id)
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["receipt"]["failure_class"], "repository_state")
            self.assertEqual(saved["receipt"]["failure_code"], "repository_mismatch")
            self.assertNotIn("private detail", json.dumps(saved["receipt"]))
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
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "resolve_launcher_binary_now", return_value=binary), \
                 mock.patch.object(server, "spawn_writer", return_value=(FakeProc(), None)):
                server.run_agent_job(job_id)
                saved = server.read_job(job_id)
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["receipt"]["summary"], "writer changed pre-existing dirty paths: dirty.txt")
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
                "job_id": interrupted_id, "status": "running", "repo": "scratch", "cwd": str(repo),
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "session_identity": {"launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", "repo": "scratch", "branch": "main", "starting_head": "0" * 40, "native_session_id": "codex-old"},
                "created_at": "t", "started_at": "t", "finished_at": None, "receipt": None,
                "log_ref": str(jdir / "stdout.log"), "error": "raw secret must disappear",
                "response_webhook_url": "https://example.invalid/resume", "response_webhook_token": "tok_secret", "webhook": None,
            }
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
            self.assertEqual(saved["receipt"]["summary"], "agent job interrupted by Graphwing restart")
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

    def test_startup_recovery_closes_every_process_job_kind_but_not_codeoff(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._scratch_git(root)
            jobs = root / "jobs"
            fixtures = {
                "95" * 16: {"launcher": "codex", "session_identity": {"native_session_id": "codex-old"}},
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
            (codeoff_dir / "job.json").write_text(json.dumps({
                "job_id": codeoff_id, "status": "running", "cwd": str(root / "codeoff"),
                "launcher": "codex", "codeoff_workspace": {"experiment_id": "experiment-0001", "slot": "author-1"},
            }))
            persisted_before_delivery = []
            def post(_url, receipt, token=None):
                persisted_before_delivery.append(json.loads((jobs / receipt["job_id"] / "job.json").read_text())["status"])
                return {"ok": True, "status": 200}
            with mock.patch.object(server, "JOBS_DIR", jobs), mock.patch.object(server, "post_receipt", side_effect=post) as posted:
                self.assertEqual(server.recover_interrupted_agent_jobs(), 5)
            self.assertEqual(persisted_before_delivery, ["failed"] * 5)
            self.assertEqual(posted.call_count, 5)
            for job_id, extra in fixtures.items():
                saved = json.loads((jobs / job_id / "job.json").read_text())
                self.assertEqual(saved["status"], "failed", extra)
                self.assertLessEqual(len(saved["receipt"]["summary"]), 500)
                self.assertNotIn("secret", json.dumps(saved["receipt"]))
                if extra.get("kind") == "review":
                    self.assertEqual(saved["receipt"]["status"], "nack")
                    self.assertTrue(saved["receipt"]["no_verdict"])
                elif extra.get("kind") in {"script", "test", "rr"}:
                    self.assertEqual(saved["receipt"]["status"], "error")
                    self.assertEqual(saved["receipt"]["log_ref"], saved["log_ref"])
                else:
                    self.assertEqual(saved["receipt"]["failure_code"], "cancelled")
                    self.assertEqual(saved["receipt"]["session_identity"], extra["session_identity"])
            self.assertEqual(json.loads((codeoff_dir / "job.json").read_text())["status"], "running")
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

    def test_empty_writer_job_id_uses_legacy_staged_commit(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            (repo / "staged.txt").write_text("staged\n")
            subprocess.run(["git", "-C", str(repo), "add", "--", "staged.txt"], check=True)
            status, payload = server.git_commit(json.dumps({
                "repo": "scratch", "message": "staged", "writer_job_id": "",
            }).encode(), {"scratch": str(repo)})
            self.assertEqual(status, 200, payload)
            self.assertIn("staged.txt", subprocess.run(
                ["git", "-C", str(repo), "show", "--format=", "--name-only", "HEAD"], check=True, capture_output=True, text=True,
            ).stdout.splitlines())

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
            parent = {"job_id": parent_id, "status": "completed", "receipt": {"status": "ok"}}
            children = [
                {"job_id": job_id, "writer_parent_job_id": parent_id, "status": "completed", "receipt": {"status": "ok"}}
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
            job = {"job_id": job_id, "cwd": td, "model": "gpt-5.6-sol", "launcher": "codex", "session_identity": {"native_session_id": "codex-123"}}
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

    def test_spawn_claude_uses_json_resume_contract(self):
        captured = {}

        class FakePopen:
            pid = 7

            def __init__(self, cmd, **kwargs):
                captured["cmd"] = list(cmd)

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
                "launcher": "claude", "max_turns": 12,
                "session_identity": {"native_session_id": "claude-123"},
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

    def _write_grok_acp_fixture(self, root: Path) -> Path:
        fixture = root / "grok-fixture"
        fixture.write_text("""#!/usr/bin/env python3
import json, os, sys, time
cfg = json.loads(os.environ.get("ACP_TEST_FIXTURE", "{}"))
capture = os.environ["ACP_TEST_CAPTURE"]
seen = {"pid":os.getpid(), "argv":sys.argv[1:], "cwd":os.getcwd(), "env":{key:os.environ.get(key) for key in ["GROK_DISABLE_AUTOUPDATER","PWD","TERMINAL_CWD","GRAPHWING_JOB_ID","GIT_TERMINAL_PROMPT","GH_PROMPT_DISABLED"]}, "requests":[]}
def save():
    with open(capture, "w") as fh: json.dump(seen, fh)
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
    elif method == "session/new": result = {} if cfg.get("missing_session") else {"sessionId":cfg.get("session_id", "grok-123")}
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
        for text in cfg.get("chunks", ['{"status":"ok","sha":null,"pr_url":null,"summary":"done"}']):
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"agent_message_chunk","content":{"type":cfg.get("chunk_content_type", "text"),"text":text}}}})
        result = {"stopReason":cfg.get("stop_reason", "end_turn")}
    else: result = {}
    send({"jsonrpc":"2.0","id":response_id(request),"result":result})
    if cfg.get("exit_after_method") == method: sys.exit(cfg.get("exit_code", 7))
    if cfg.get("hang_after_method") == method:
        while True: time.sleep(1)
""")
        fixture.chmod(0o755)
        return fixture

    def _run_grok_fixture(
        self, cfg=None, resume=False, api_key=True, prompt="fixture prompt", model="grok-4.6"
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
        identity = {
            "launcher": "grok", "provider": "xai", "model": model,
            "repo": "scratch", "branch": "main", "starting_head": "0" * 40,
            "native_session_id": native_session_id,
        }
        job = {
            "job_id": job_id, "status": "queued", "repo": "scratch",
            "cwd": str(root), "prompt": prompt,
            "launcher": "grok", "provider": "xai", "model": model,
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
        with mock.patch.object(server, "JOBS_DIR", jobs), \
             mock.patch.dict(os.environ, env, clear=False):
            if not api_key:
                os.environ.pop("XAI_API_KEY", None)
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
             mock.patch.object(server, "git_diff", return_value={"diff": "d"}), \
             mock.patch.dict(os.environ, env, clear=False):
            result = server.native_review_result(
                "grok", "xai", "grok-4.6", "ticket", root
            )
        return result, json.loads(capture.read_text())

    def test_grok_acp_exact_argv_env_wire_and_successful_receipt(self):
        saved, capture, jdir = self._run_grok_fixture()
        self.assertEqual(capture["argv"], ["agent", "--always-approve", "--model", "grok-4.6", "stdio"])
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
        self.assertEqual(saved["receipt"]["summary"], "done")
        self.assertEqual(saved["receipt"]["failure_class"], "none")
        self.assertEqual(saved["receipt"]["failure_code"], "none")
        self.assertFalse(saved["receipt"]["failover_eligible"])
        self.assertEqual((jdir / "last-message.txt").read_text(), '{"status":"ok","sha":null,"pr_url":null,"summary":"done"}')

    def test_grok_acp_command_uses_the_requested_immutable_model(self):
        saved, capture, _ = self._run_grok_fixture(model="grok-fixture-immutable")
        self.assertEqual(
            capture["argv"],
            ["agent", "--always-approve", "--model", "grok-fixture-immutable", "stdio"],
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
        self.assertEqual(saved["receipt"]["summary"], "current")
        changed, _, _ = self._run_grok_fixture({
            "load_result": {"sessionId": "grok-other"},
        }, resume=True)
        self.assertEqual(changed["status"], "failed")
        self.assertIn("changed during load", changed["receipt"]["summary"])

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
                self.assertEqual(
                    saved["receipt"]["summary"],
                    "malformed Grok ACP authentication methods",
                )
                self.assertNotIn("authenticate", [r["method"] for r in capture["requests"]])

    def test_grok_acp_rejects_selected_terminal_or_unknown_authentication(self):
        for method_type in ("terminal", "future-method"):
            with self.subTest(method_type=method_type):
                saved, capture, _ = self._run_grok_fixture({
                    "auth_methods": [{"id": "xai.api_key", "type": method_type}],
                })
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(
                    saved["receipt"]["summary"],
                    "unsupported Grok ACP authentication",
                )
                self.assertNotIn("authenticate", [r["method"] for r in capture["requests"]])

    def test_grok_acp_rejects_boolean_response_id_for_integer_request(self):
        saved, _, _ = self._run_grok_fixture({
            "response_id_method": "initialize", "response_id": True,
        })
        self.assertEqual(saved["status"], "failed")
        self.assertIn("unexpected Grok ACP response", saved["receipt"]["summary"])

    def test_grok_acp_rejects_non_text_agent_message_chunk(self):
        saved, _, _ = self._run_grok_fixture({"chunk_content_type": "image"})
        self.assertEqual(saved["status"], "failed")
        self.assertIn("malformed Grok agent message chunk", saved["receipt"]["summary"])

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
                self.assertIn(summary, saved["receipt"]["summary"])
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
        self.assertIn("exited 7", saved["receipt"]["summary"])

    def test_grok_acp_protocol_error_preserves_summary_and_child_exit(self):
        saved, _, _ = self._run_grok_fixture({
            "error_method": "session/prompt",
            "exit_after_method": "session/prompt",
            "exit_code": 7,
        })
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["returncode"], 7)
        self.assertEqual(saved["receipt"]["summary"], "Grok ACP session/prompt error")

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
        self.assertEqual(saved["receipt"]["summary"], "Grok ACP session/prompt error")
        self.assertEqual(len(stderr), server.FILE_MAX_BYTES)
        self.assertTrue(stderr.endswith(b"Grok ACP session/prompt error\n"))

    def test_grok_acp_timeout_fails_closed_and_cleans_up(self):
        saved, capture, _ = self._run_grok_fixture({"budget": 1, "sleep": 1.2})
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["receipt"]["status"], "timeout")
        with self.assertRaises(ProcessLookupError):
            os.kill(capture["pid"], 0)

    def test_grok_acp_cleanup_timeout_fails_valid_turn_and_kills_child(self):
        saved, capture, _ = self._run_grok_fixture({"hang_after_method": "session/prompt"})
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["receipt"]["status"], "timeout")
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
        with mock.patch.dict(os.environ, {"TERMINAL_CWD": "/home/tim/rewst/riftwing", "PWD": "/home/tim"}):
            env = server.native_job_env(
                {"job_id": "ab" * 16, "cwd": "/home/tim/work/gw-real-slice"}
            )
        self.assertEqual(env["TERMINAL_CWD"], "/home/tim/work/gw-real-slice")
        self.assertEqual(env["PWD"], "/home/tim/work/gw-real-slice")
        self.assertNotEqual(env.get("TERMINAL_CWD"), "/home/tim/rewst/riftwing")

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
                "session_identity": {"native_session_id": None},
                "response_webhook_url": "https://example.invalid/resume",
                "response_webhook_token": "tok_secret", "created_at": "t",
                "started_at": None, "finished_at": None, "max_turns": 1,
                "run_budget_seconds": 5, "receipt": None,
                "log_ref": str(jdir / "stdout.log"), "error": None, "webhook": None,
            }
            (jdir / "job.json").write_text(json.dumps(job))
            specific = "missing codex binary: /fixture/codex"
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                 mock.patch.object(server, "spawn_writer", return_value=(None, {"error": specific, "code": "missing_binary"})), \
                 mock.patch.object(server, "post_receipt", return_value={"ok": True, "status": 200}) as posted:
                server.run_agent_job(job_id)
            saved = json.loads((jdir / "job.json").read_text())
            self.assertEqual(saved["receipt"]["summary"], specific)
            posted.assert_called_once()
            self.assertEqual(posted.call_args.args[1]["summary"], specific)
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
        self.assertEqual(saved["receipt"]["summary"], "done")
        self.assertEqual(saved["session_identity"]["native_session_id"], "codex-123")
        self.assertEqual(saved["receipt"]["session_identity"]["native_session_id"], "codex-123")

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
        self.assertEqual(saved["receipt"]["summary"], "done")
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
        self.assertIn("real staged index", commit_op["summary"])
        self.assertIn("isolated index", commit_op["summary"])
        self.assertIn("real staged index", commit_request["add"]["description"])
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
        self.assertNotIn("oneOf", request_schema)
        self.assertNotIn("allOf", request_schema)
        self.assertFalse(request_schema["additionalProperties"])
        for schema_name in ("AgentRunAccepted", "AgentJob", "AgentReceipt"):
            identity = spec["components"]["schemas"][schema_name]["properties"]["session_identity"]
            self.assertEqual(identity["$ref"], "#/components/schemas/SessionIdentity")
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
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["ok"])

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
            identity = {
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol",
                "repo": "r", "branch": "feature", "starting_head": "0" * 40,
                "native_session_id": "session-7",
            }
            writer = {
                "job_id": writer_id, "status": "completed", "repo": "r", "cwd": str(repo.resolve()),
                "launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", "session_identity": identity,
                "receipt": {"status": "ok", "job_id": writer_id, "session_identity": identity, "failure_class": "none", "failure_code": "none", "failover_eligible": False},
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
        self.assertEqual(payload.get("code"), "nothing_staged", payload)

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
            "ee0d6e700b8664c66282034a58048f9ef6435dac173382dbc4cc86f59e4c3450",
        )

    def test_availability_fallback_route_locked_matrix_and_reviewers(self):
        expected = {
            "go_coding": (("codex", "openai", "gpt-5.6-sol"), ("claude", "anthropic", "claude-opus-5", "default")),
            "typescript_coding": (("claude", "anthropic", "claude-opus-5"), ("codex", "openai", "gpt-5.6-sol", "high")),
            "research_ops": (("grok", "xai", "grok-4.6"), ("claude", "anthropic", "claude-sonnet-5", "default")),
        }
        for work_kind, (primary, fallback) in expected.items():
            primary_route = server.slice_route_lookup("sensitive", "M", work_kind=work_kind)
            self.assertEqual(tuple(primary_route[k] for k in ("launcher", "provider", "model")), primary)
            receipt = {"status": "error", "job_id": "a" * 32, "role": "primary", "failure_class": "provider_availability", "failure_code": "provider_rate_limit", "failover_eligible": True, "session_identity": {"launcher": primary[0], "provider": primary[1], "model": primary[2]}}
            stored_job = {
                "launcher": primary[0], "provider": primary[1], "model": primary[2],
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
        receipt = {"status": "error", "job_id": "a" * 32, "role": "primary", "failure_class": "provider_availability", "failure_code": "missing_binary", "failover_eligible": True, "session_identity": {"launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol"}}
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
            ({"primary_receipt": {**receipt, "provider": "openai", "session_identity": {"launcher": "claude", "provider": "openai", "model": "gpt-5.6-sol"}}}, "primary_provider_mismatch"),
            ({"primary_receipt": {**receipt, "provider": "openai", "session_identity": {"launcher": "codex", "provider": "anthropic", "model": "gpt-5.6-sol"}}}, "primary_provider_mismatch"),
            ({"primary_receipt": {**receipt, "session_identity": {"launcher": "codex", "provider": "openai", "model": "claude-opus-5"}}}, "primary_provider_mismatch"),
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
        identity = {"launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol"}
        receipt = {"status": "error", "job_id": "a" * 32, "role": "primary", "failure_class": "provider_availability", "failure_code": "missing_binary", "failover_eligible": True, "session_identity": identity}
        body = json.dumps({"class": "mechanical", "size": "M", "work_kind": "go_coding", "primary_route": primary_route, "primary_receipt": receipt}).encode()
        stored_receipt = {key: value for key, value in receipt.items() if key != "role"}
        valid_job = {"launcher": "codex", "provider": "openai", "model": "gpt-5.6-sol", "status": "failed", "session_identity": identity, "receipt": stored_receipt}
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

        def identity(route, native_session_id, starting_head=initial_head):
            return {
                "launcher": route["launcher"],
                "provider": route["provider"],
                "model": route["model"],
                "repo": "scratch",
                "branch": "main",
                "starting_head": starting_head,
                "native_session_id": native_session_id,
            }

        primary_identity = identity(
            primary_route, None, primary_starting_head or initial_head
        )
        fallback_identity = identity(
            fallback_route,
            f"fallback-session-{fallback_job_id[:8]}",
            fallback_starting_head or initial_head,
        )
        primary_receipt = {
            "status": "error",
            "job_id": primary_job_id,
            "role": "primary",
            "session_identity": primary_identity,
            "failure_class": "provider_availability",
            "failure_code": failure_code,
            "failover_eligible": True,
            "diagnostic": server.compact_diagnostic(failure_code),
        }
        fallback_receipt = {
            "status": "ok",
            "job_id": fallback_job_id,
            "role": "availability_fallback",
            "session_identity": fallback_identity,
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
                "status": "failed",
                "created_at": primary_created_at,
                "finished_at": primary_finished_at,
                "launcher": primary_route["launcher"],
                "provider": primary_route["provider"],
                "model": primary_route["model"],
                "session_identity": primary_identity,
                "receipt": {key: value for key, value in primary_receipt.items() if key != "role"},
            }
        )
        write(
            {
                "job_id": fallback_receipt["job_id"],
                "status": fallback_status,
                "created_at": fallback_created_at,
                "finished_at": fallback_finished_at if fallback_status == "completed" else None,
                "launcher": fallback_route["launcher"],
                "provider": fallback_route["provider"],
                "model": fallback_route["model"],
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
                "session_identity": fresh_identity,
                "failure_class": "none",
                "failure_code": "none",
                "failover_eligible": False,
                "diagnostic": server.compact_diagnostic("none"),
            }
            write(
                {
                    "job_id": fresh_receipt["job_id"],
                    "status": "completed",
                    "created_at": fresh_created_at,
                    "finished_at": "2026-08-26T12:02:30Z",
                    "launcher": primary_route["launcher"],
                    "provider": primary_route["provider"],
                    "model": primary_route["model"],
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
                if alias not in dominators[node_id]:
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
        for target, wait_id, filt in (
            ("test", "wait_test", "if_test_ok"),
            ("review1", "wait_rev1", "if_review1"),
            ("review1b", "wait_r1b", "if_review1b"),
            ("review2", "wait_rev2", "if_review2"),
            ("review2b", "wait_r2b", "if_review2b"),
        ):
            self.assertEqual(nodes[wait_id]["type"], "action.wait.webhook", wait_id)
            cfg = nodes[target]["config"]
            self.assertEqual(cfg.get("response_webhook_url"), f"{{{{ TASKS.{wait_id}.pending.resumeUrl }}}}")
            self.assertEqual(cfg.get("response_webhook_token"), f"{{{{ TASKS.{wait_id}.pending.resumeToken }}}}")
            # the wait drives the async node, and its delivered body drives the filter
            self.assertIn({"source": wait_id, "handle": "pending", "target": target},
                          [{"source": e["source"], "handle": e.get("sourceHandle"), "target": e["target"]} for e in edges])
            outs = [e["target"] for e in edges if e["source"] == wait_id and e.get("sourceHandle") == "out"]
            self.assertEqual(outs, [filt], wait_id)
            # nothing may reach the async node except its wait
            feeders = {e["source"] for e in edges if e["target"] == target}
            self.assertEqual(feeders, {wait_id}, target)
            self.assertEqual(nodes[filt]["config"]["rules"],
                             [{"path": "request.body.status", "op": "equals", "value": "ok"}], filt)

    def test_every_writer_receipt_gates_followup_tests(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        cases = {"record": ("if_receipt_ok", "join_writer_success"), "record2": ("if_receipt_ok2", "test2"), "record3": ("if_receipt_ok3", "test3"), "record_rn1": ("if_receipt_ok_rn1", "test_rn1"), "record_rn2": ("if_receipt_ok_rn2", "test_rn2")}
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
        self.assertFalse(any(e["source"] == "recovery_route_fail" for e in edges))
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
            self.assertEqual(leaves, {terminal}, (source, handle, leaves))
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

        # Diagnostics terminate; they cannot route a second fallback or writer.
        for terminal in terminals:
            self.assertFalse(any(e["source"] == terminal for e in edges), terminal)
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
        second = nodes["switch_rev2"]["config"]
        self.assertEqual(second["cases"][0]["rules"], [
            {"path": "data.reviewer2_launcher", "op": "equals", "value": "none"}
        ])
        edges = {edge["id"]: edge for edge in graph["spec"]["edges"]}
        self.assertEqual(
            (edges["e_rev2_skip"]["sourceHandle"], edges["e_rev2_skip"]["target"]),
            ("case-0", "join_commit"),
        )
        self.assertEqual(
            (edges["e_rev2_need"]["sourceHandle"], edges["e_rev2_need"]["target"]),
            ("default", "wait_rev2"),
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

    def test_review_run_is_async_when_given_a_webhook(self):
        body = json.dumps({
            "repo": "scratch", "launcher": "claude", "provider": "anthropic",
            "model": "claude-sonnet-5", "prompt": "ticket",
            "response_webhook_url": "https://example.invalid/resume",
        }).encode()
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            jobs = Path(td) / "jobs"
            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "active_job_count", return_value=0), \
                 mock.patch.object(server, "enqueue_review") as enq:
                status, payload, _ = server.dispatch("POST", "/v1/review/run", {}, True, body)
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["kind"], "review")
        self.assertEqual(
            (payload["launcher"], payload["provider"], payload["model"]),
            ("claude", "anthropic", "claude-sonnet-5"),
        )
        self.assertTrue(payload["job_id"])
        queued = enq.call_args.args[0]
        self.assertNotIn("reviewer", queued)

    def test_review_run_rejects_profile_named_or_mismatched_reviewers(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                for request, code in (
                    ({"repo": "scratch", "reviewer": "terra", "prompt": "x"}, "bad_launcher"),
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
            with mock.patch.dict(os.environ, {
                     "GRAPHWING_CODEX_BIN": str(codex_bin),
                     "GRAPHWING_CLAUDE_BIN": str(claude_bin),
                 }), \
                 mock.patch.object(server, "git_diff", return_value={"diff": "d"}), \
                 mock.patch.object(server.subprocess, "run", fake_run):
                codex = server.native_review_result(
                    "codex", "openai", "gpt-5.6-sol", "ticket", Path(td)
                )
                claude = server.native_review_result(
                    "claude", "anthropic", "claude-sonnet-5", "ticket", Path(td)
                )
        grok, grok_capture = self._run_grok_review_fixture()
        self.assertEqual(
            (codex["verdict"], claude["verdict"], grok["verdict"]),
            ("PASS", "PASS", "PASS"),
        )
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
            ["agent", "--model", "grok-4.6", "stdio"],
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
                     mock.patch.dict(os.environ, {"GRAPHWING_CODEX_BIN": str(codex)}), \
                     mock.patch.object(server, "git_diff", return_value={"diff": "d"}), \
                     mock.patch.object(
                         server.subprocess, "run",
                         side_effect=effect,
                         return_value=None if effect else outcome,
                     ):
                    result = server.native_review_result(
                        "codex", "openai", "gpt-5.6-sol", "ticket", Path(td)
                    )
                self.assertFalse(result["ok"], result)
                if code:
                    self.assertEqual(result["code"], code)
            with mock.patch.dict(
                os.environ, {"GRAPHWING_CODEX_BIN": str(Path(td) / "missing-codex")}
            ):
                missing = server.native_review_result(
                    "codex", "openai", "gpt-5.6-sol", "ticket", Path(td)
                )
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["code"], "not_implemented")

    def test_grok_review_reuses_the_direct_acp_launcher(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            grok = Path(td) / "grok"
            grok.write_text("fixture")
            job_id = "ab" * 16
            (jobs / job_id).mkdir(parents=True)

            def fake_acp(job, binary, *, mode):
                self.assertEqual(binary, grok)
                self.assertEqual(mode, "review")
                self.assertEqual(job["response_webhook_url"], "https://example.invalid/resume")
                path = jobs / job["job_id"]
                path.joinpath("last-message.txt").write_text(json.dumps({
                    "status": "ok", "sha": None, "pr_url": None,
                    "summary": "VERDICT: PASS",
                }))
                return 0, False, "grok-review-session", None

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.dict(os.environ, {"GRAPHWING_GROK_BIN": str(grok)}), \
                 mock.patch.object(server, "git_diff", return_value={"diff": "d"}), \
                 mock.patch.object(server, "run_grok_acp", fake_acp):
                server.write_job({
                    "job_id": job_id,
                    "response_webhook_url": "https://example.invalid/resume",
                })
                out = server.native_review_result(
                    "grok", "xai", "grok-4.6", "ticket", Path(td), job_id=job_id
                )
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

    def test_git_commit_add_and_staged_only(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            (repo / "tracked.txt").write_text("a\n")
            (repo / "left.txt").write_text("leave me\n")
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/commit",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "message": "add tracked", "add": ["tracked.txt"]}).encode(),
                )
                self.assertEqual(status, 200, payload)
                self.assertTrue(payload["ok"])
                listed = subprocess.run(
                    ["git", "-C", str(repo), "ls-files"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertIn("tracked.txt", listed.stdout)
                self.assertNotIn("left.txt", listed.stdout)
                subprocess.run(["git", "-C", str(repo), "add", "left.txt"], check=True, capture_output=True)
                (repo / "untracked.txt").write_text("nope\n")
                status, payload, _ = server.dispatch(
                    "POST",
                    "/v1/git/commit",
                    {},
                    True,
                    json.dumps({"repo": "scratch", "message": "staged only"}).encode(),
                )
                self.assertEqual(status, 200, payload)
                listed = subprocess.run(
                    ["git", "-C", str(repo), "ls-files"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertIn("left.txt", listed.stdout)
                self.assertNotIn("untracked.txt", listed.stdout)

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
        self.assertEqual(edges["e12"]["target"], "join_walk")
        self.assertEqual(edges["e_join_walk"]["target"], "walk_recovery_input")
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
            {"job_id", "route_version", "launcher", "provider", "model"},
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
                "execution_identity": execution_identity, "summary": "fixture",
            },
        })
        server.write_job(job)
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
        self, experiment_id, slot, stdout, *, native_session_id, before_run=None
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
        self.assertEqual(job["session_identity"]["native_session_id"], native_session_id, job)
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
        resume_identity = {
            "launcher": resume_body["launcher"], "provider": resume_body["provider"], "model": resume_body["model"],
            "repo": "codeoff:experiment-0001:author-2", "branch": branch, "starting_head": head,
            "native_session_id": "fixture-resume",
        }
        server.write_job({
            "job_id": resume_id, "status": "completed", "launcher": resume_body["launcher"],
            "session_identity": resume_identity,
            "receipt": {"status": "ok", "job_id": resume_id, "session_identity": resume_identity},
        })
        resume_body.update({"resume_job_id": resume_id, "session_identity": resume_identity})
        with mock.patch.object(server, "resolve_launcher_binary_now", return_value=Path(__file__)):
            status, payload = self._post("/v1/agent/run", resume_body)
        self.assertEqual((status, payload["code"]), (400, "codeoff_resume_mismatch"))

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
                server.write_job(mismatch_job)
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
                    server.write_job(job)
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
                server.write_job(job)
                accepted, error = server._codeoff_validate_job(job_id, state, manifest, "author-1")
                self.assertIsNone(accepted)
                self.assertEqual(error["code"], "agent_receipt_mismatch")
        server.write_job(pristine)

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
        }
        for path, operation_id in expected.items():
            method = "get" if path.endswith("audit") else "post"
            self.assertEqual(spec["paths"][path][method]["operationId"], operation_id)
        for name in ("CodeOffPrepareRequest", "CodeOffCandidateRequest", "CodeOffJudgeRequest", "CodeOffExperimentRequest", "CodeOffWorkspaceRef", "CodeOffJudgeResult", "CodeOffPrepareResult", "CodeOffCompact"):
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
        for wait, agent in (("wait_author_1", "author_1"), ("wait_author_2", "author_2"), ("wait_judge_fable", "judge_fable"), ("wait_judge_1", "judge_1"), ("wait_judge_2", "judge_2")):
            self.assertEqual(nodes[wait]["type"], "action.wait.webhook")
            self.assertIn((wait, "pending", agent), triples)
            self.assertIn(wait, nodes[agent]["config"]["response_webhook_url"])
        self.assertEqual(nodes["join_candidates"]["type"], "logic.join.all")
        self.assertEqual(nodes["join_judges"]["type"], "logic.join.all")
        self.assertTrue(nodes["join_candidates"]["config"]["waitForAll"])
        self.assertTrue(nodes["join_judges"]["config"]["waitForAll"])
        self.assertEqual({e["source"] for e in edges if e["target"] == "join_candidates"}, {"test_1", "test_2"})
        self.assertEqual({e["source"] for e in edges if e["target"] == "join_judges"}, {"receipt_fable", "receipt_1", "receipt_2"})
        self.assertIn(("eligible", "pass", "finalize"), triples)
        self.assertIn(("final_verified", "pass", "commit"), triples)
        self.assertTrue(any(e["source"] == "commit" and e["target"] == "push" for e in edges))
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
            elif expected == "array":
                self.assertIsInstance(value, list, where)
            elif expected == "string":
                self.assertIsInstance(value, str, where)
                if "{{" not in value and "enum" in schema:
                    self.assertIn(value, schema["enum"], where)
            elif expected == "integer":
                self.assertIs(type(value), int, where)
            elif expected == "boolean":
                self.assertIs(type(value), bool, where)

        for node_id, node in graphwing_actions.items():
            action, endpoint = node["type"].removeprefix("action.graphwing.").split(":", 1)
            self.assertIn(endpoint, openapi_paths, node_id)
            self.assertIn((node_id, "failure", "join_terminal"), triples, node_id)
            request_schema = openapi_paths[endpoint][action.lower()]["requestBody"]["content"]["application/json"]["schema"]
            accepts({key: value for key, value in node["config"].items() if key not in {"integrationInstanceId", "timeout"}}, request_schema, node_id)
        self.assertEqual(
            {node_id for node_id, node in graphwing_actions.items() if node["type"] in {"action.graphwing.POST:/v1/git/commit", "action.graphwing.POST:/v1/git/push"}},
            {"commit", "push"},
        )
        self.assertEqual(nodes["eligible"]["config"]["rules"][0]["path"], "data.promotion_eligible")
        self.assertEqual(nodes["final_verified"]["config"]["rules"][0]["path"], "data.commit_eligible")
        self.assertEqual(nodes["commit"]["config"]["final_verification_hash"], "{{ TASKS.finalize.data.receipt_hash }}")
        self.assertEqual(nodes["push"]["config"]["final_verification_hash"], "{{ TASKS.finalize.data.receipt_hash }}")
        for suffix in ("1", "2"):
            self.assertEqual(nodes[f"freeze_{suffix}"]["config"]["job_id"], f"{{{{ TASKS.wait_author_{suffix}.request.body.job_id }}}}")
        for node_id, wait_id in (("receipt_fable", "wait_judge_fable"), ("receipt_1", "wait_judge_1"), ("receipt_2", "wait_judge_2")):
            self.assertEqual(nodes[node_id]["config"]["job_id"], f"{{{{ TASKS.{wait_id}.request.body.job_id }}}}")
        # No cycles, and every action failure is terminal rather than a redraw/retry.
        outgoing = {}
        for edge in edges:
            outgoing.setdefault(edge["source"], []).append(edge["target"])
        color = {node: 0 for node in nodes}
        def visit(node):
            color[node] = 1
            for target in outgoing.get(node, []):
                self.assertNotEqual(color[target], 1, f"cycle through {target}")
                if color[target] == 0:
                    visit(target)
            color[node] = 2
        for node in nodes:
            if color[node] == 0:
                visit(node)
        self.assertNotIn("retry", json.dumps(graph).lower())

    def test_publish_catalog_includes_codeoff_and_validated_implement_slice(self):
        source = (Path(server.__file__).parent / "scripts" / "publish_graphs.py").read_text()
        self.assertIn('"code-off"', source)
        self.assertIn('install["code_off"]', source)
        implement = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        spec = json.dumps(implement["spec"], sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(spec).hexdigest(), "118562f8d3c905d15a3f57c474d86010a9718710b43c0188b02043c3c257bf9a")


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
