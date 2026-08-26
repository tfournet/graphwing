#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
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
            with mock.patch.object(server, "CODEX_BIN", codex), mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
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
            with mock.patch.object(server, "CODEX_BIN", codex), \
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
            with mock.patch.object(server, "CLAUDE_BIN", claude), \
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
            with mock.patch.object(server, "GROK_BIN", grok), \
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
                "session_identity": identity,
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
            with mock.patch.object(server, "GROK_BIN", grok), \
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
                "session_identity": identity,
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
            with mock.patch.object(server, "CODEX_BIN", codex), \
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
                status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, json.dumps({**base, "session_identity": identity, "resume_job_id": prior_job_id}).encode())
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
                "session_identity": identity,
                "receipt": {"status": "ok", "session_identity": identity, "summary": "done"},
            }
            (prior_dir / "job.json").write_text(json.dumps(prior))
            body = json.dumps({
                "prompt": "continue", "cwd": "scratch", "launcher": "claude",
                "provider": "anthropic", "model": "claude-opus-5",
                "session_identity": identity, "resume_job_id": prior_job_id,
            }).encode()
            with mock.patch.object(server, "CLAUDE_BIN", claude), \
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
                "session_identity": identity,
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
            with mock.patch.object(server, "CODEX_BIN", codex), \
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

    def test_agent_run_missing_binary(self):
        with mock.patch.object(server, "CODEX_BIN", Path("/nope/codex")):
            status, payload, _ = server.dispatch(
                "POST", "/v1/agent/run", {}, True,
                b'{"prompt":"x","launcher":"codex","provider":"openai","model":"gpt-5.6-sol"}',
            )
        self.assertEqual(status, 501)
        self.assertEqual(payload["code"], "provider_unavailable")
        self.assertEqual(payload["failure_class"], "provider_availability")
        self.assertEqual(payload["failure_code"], "missing_binary")
        self.assertTrue(payload["failover_eligible"])

    def test_grok_agent_run_missing_binary_is_typed(self):
        with mock.patch.object(server, "GROK_BIN", Path("/nope/grok")):
            status, payload, _ = server.dispatch(
                "POST", "/v1/agent/run", {}, True,
                b'{"prompt":"x","cwd":"scratch","launcher":"grok","provider":"xai","model":"grok-4.6"}',
            )
        self.assertEqual(status, 501)
        self.assertEqual(payload["code"], "missing_binary")
        self.assertEqual(payload["failure_class"], "provider_availability")
        self.assertEqual(payload["failure_code"], "missing_binary")
        self.assertTrue(payload["failover_eligible"])

    def test_wrap_prompt_locks_cwd(self):
        text = server.wrap_prompt("ab" * 16, "ping", "/home/tim/work/gw-real-slice")
        self.assertIn("/home/tim/work/gw-real-slice", text)
        self.assertIn("only inside that directory", text)
        self.assertIn("git add --", text)
        self.assertIn("relative paths only", text)
        self.assertIn("Do not git commit, git push", text)
        self.assertIn("Do not `git checkout`", text)

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
            with mock.patch.object(server, "JOBS_DIR", jobs), mock.patch.object(server, "CODEX_BIN", codex), mock.patch.object(server.subprocess, "Popen", FakePopen):
                proc, err = server.spawn_codex(job)
                resume_cmd = captured["cmd"]
                initial_job = dict(job, session_identity={"native_session_id": None})
                initial_proc, initial_err = server.spawn_codex(initial_job)
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
                 mock.patch.object(server, "CLAUDE_BIN", claude), \
                 mock.patch.object(server.subprocess, "Popen", FakePopen):
                proc, err = server.spawn_claude(job)
                resume_cmd = captured["cmd"]
                initial_proc, initial_err = server.spawn_claude(
                    dict(job, session_identity={"native_session_id": None})
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

    def _run_grok_fixture(self, cfg=None, resume=False, api_key=True, prompt="fixture prompt"):
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
            "launcher": "grok", "provider": "xai", "model": "grok-4.6",
            "repo": "scratch", "branch": "main", "starting_head": "0" * 40,
            "native_session_id": native_session_id,
        }
        job = {
            "job_id": job_id, "status": "queued", "repo": "scratch",
            "cwd": str(root), "prompt": prompt,
            "launcher": "grok", "provider": "xai", "model": "grok-4.6",
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
        with mock.patch.object(server, "JOBS_DIR", jobs), \
             mock.patch.object(server, "GROK_BIN", fixture), \
             mock.patch.dict(os.environ, env, clear=False):
            if not api_key:
                os.environ.pop("XAI_API_KEY", None)
            server.run_agent_job(job_id)
        return json.loads((jdir / "job.json").read_text()), json.loads(capture.read_text()), jdir

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
            with mock.patch.object(server, "JOBS_DIR", jobs), mock.patch.object(server, "spawn_writer", return_value=(FakeProc(), None)):
                server.run_agent_job(job_id)
            saved = json.loads((jdir / "job.json").read_text())
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["receipt"]["summary"], "done")
        self.assertEqual(saved["session_identity"]["native_session_id"], "codex-123")
        self.assertEqual(saved["receipt"]["session_identity"]["native_session_id"], "codex-123")

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
            with mock.patch.object(server, "CODEX_BIN", codex), mock.patch.object(server, "JOBS_DIR", jobs):
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
        request_schema = spec["components"]["schemas"]["AgentRunRequest"]
        self.assertEqual(
            set(request_schema["required"]),
            {"prompt", "launcher", "provider", "model"},
        )
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
            {"gpt-5.6-sol", "claude-opus-5", "claude-sonnet-5", "grok-4.6"},
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

    def test_watch_helper_no_key(self):
        helper = Path(__file__).resolve().parent / "plugins" / "graphwing.watch" / "status.py"
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run(
                [sys.executable, str(helper), td, "8645"],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "no_key")

    def test_watch_helper_focus_requires_label(self):
        helper = Path(__file__).resolve().parent / "plugins" / "graphwing.watch" / "status.py"
        proc = subprocess.run(
            [sys.executable, str(helper), ".", "8645", "focus"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "missing_tab")

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
        status, payload, _ = server.dispatch("GET", "/v1/herdr/agents", {}, True, b"")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["session"], "graphwing")

    def test_gh_pr_view_requires_number(self):
        status, payload, _ = server.dispatch("GET", "/v1/gh/pr/view", {}, True, b"")
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "missing_number")

    def test_gh_pr_list(self):
        status, payload, _ = server.dispatch(
            "GET", "/v1/gh/pr/list", {"n": ["1"]}, True, b""
        )
        self.assertIn(status, (200, 400), payload)
        if status == 200:
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
        calls = []

        def fake_gh_json(repo_path, argv):
            calls.append(argv[:3])
            if argv[1] == "view":
                return {"ok": True, "data": {"number": 1, "mergeable": "MERGEABLE",
                                             "isDraft": False, "reviewDecision": "APPROVED",
                                             "mergeStateStatus": "CLEAN", "labels": []}}
            if argv[1] == "checks":
                return {"ok": True, "data": [{"name": "ci", "bucket": "pass"}]}
            return {"ok": True, "data": {}}

        def fake_gh_text(repo_path, argv):
            calls.append(argv[:3])
            return {"ok": True, "stdout": "Squashed and merged pull request #1"}

        with mock.patch.object(server, "gh_json", fake_gh_json), \
             mock.patch.object(server, "gh_text", fake_gh_text):
            status, payload = server.gh_pr_merge(
                json.dumps({"repo": "r", "number": 1, "auto_merge": True,
                            "run_id": "r1"}).encode(),
                {"r": "/tmp"},
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["merged"], payload)
        self.assertTrue(payload["state"]["all_green"], payload["state"])
        self.assertIn(["pr", "merge", "1"], calls)

    def test_pr_merge_does_not_call_gh_merge_when_it_refuses(self):
        # A refusal that still shelled out to `gh pr merge` would be the worst
        # possible version of this bug.
        calls = []

        def fake_gh_json(repo_path, argv):
            calls.append(argv[1])
            if argv[1] == "view":
                return {"ok": True, "data": {"mergeable": "MERGEABLE", "isDraft": False,
                                             "labels": [{"name": "hold:pm-review"}]}}
            return {"ok": True, "data": [{"name": "ci", "bucket": "pass"}]}

        with mock.patch.object(server, "gh_json", fake_gh_json):
            status, payload = server.gh_pr_merge(
                json.dumps({"repo": "r", "number": 1, "auto_merge": True,
                            "run_id": "r1"}).encode(),
                {"r": "/tmp"},
            )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "held")
        self.assertNotIn("merge", calls)

    def test_pr_merge_reports_success_for_a_plain_text_merge(self):
        # `gh pr merge` prints plain text, but the endpoint sent it through
        # gh_json, so a merge that succeeded came back merged=False with
        # code=gh_json. A caller retrying on that error would re-attempt an
        # action that had already happened. This is not hypothetical: it is
        # exactly what riftwing#3523 reported after it had already merged.
        def fake_gh_json(repo_path, argv):
            if argv[1] == "view":
                return {"ok": True, "data": {"mergeable": "MERGEABLE", "isDraft": False,
                                             "labels": []}}
            return {"ok": True, "data": [{"name": "ci", "bucket": "pass"}]}

        def fake_gh_text(repo_path, argv):
            return {"ok": True, "stdout": "Squashed and merged pull request #1\n"}

        with mock.patch.object(server, "gh_json", fake_gh_json), \
             mock.patch.object(server, "gh_text", fake_gh_text):
            status, payload = server.gh_pr_merge(
                json.dumps({"repo": "r", "number": 1, "auto_merge": True,
                            "run_id": "abc"}).encode(),
                {"r": "/tmp"},
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
        cases = {"record": ("if_receipt_ok", "wait_test"), "record2": ("if_receipt_ok2", "test2"), "wait3": ("if_receipt_ok3", "test3"), "wait_rn1": ("if_receipt_ok_rn1", "test_rn1"), "wait_rn2": ("if_receipt_ok_rn2", "test_rn2")}
        for source, (gate, target) in cases.items():
            self.assertEqual(nodes[gate]["type"], "logic.filter")
            self.assertTrue(any(e["source"] == source and e["target"] == gate for e in edges), source)
            self.assertTrue(any(e["source"] == gate and e.get("sourceHandle") == "pass" and e["target"] == target for e in edges), gate)
            self.assertTrue(any(e["source"] == gate and e.get("sourceHandle") == "fail" and e["target"] == "join_receipt_fail" for e in edges), gate)
        self.assertEqual(nodes["join_receipt_fail"]["type"], "logic.join.any")
        self.assertTrue(any(e["source"] == "join_receipt_fail" and e["target"] == "receipt_fail" for e in edges))

    def test_native_retry_writers_use_recorded_receipts_only(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        expected = {
            "agent2": "record",
            "agent3": "record2",
            "agent_rn1": "record",
            "agent_rn2": "record",
        }
        for node_id, receipt in expected.items():
            config = nodes[node_id]["config"]
            self.assertEqual(config["session_identity"], f"{{{{ TASKS.{receipt}.session_identity }}}}")
            self.assertEqual(config["resume_job_id"], f"{{{{ TASKS.{receipt}.resume_job_id }}}}")
            self.assertEqual(config["provider"], "{{ TASKS.route.data.provider }}")
        for receipt in ("record", "record2"):
            outputs = {m["output"] for m in nodes[receipt]["config"]["mappings"]}
            self.assertIn("session_identity", outputs)
            self.assertIn("resume_job_id", outputs)

    def test_implement_slice_routes_direct_native_reviewers(self):
        graph = json.loads((Path(server.__file__).parent / "graphs" / "implement-slice.json").read_text())
        nodes = {n["id"]: n for n in graph["spec"]["nodes"]}
        for suffix in ("1", "1b", "2", "2b"):
            node = nodes[f"review{suffix}"]
            slot = "reviewer1" if suffix.startswith("1") else "reviewer2"
            config = node["config"]
            self.assertNotIn("reviewer", config)
            self.assertEqual(config["launcher"], f"{{{{ TASKS.route.data.{slot}_launcher }}}}")
            self.assertEqual(config["provider"], f"{{{{ TASKS.route.data.{slot}_provider }}}}")
            self.assertEqual(config["model"], f"{{{{ TASKS.route.data.{slot}_model }}}}")
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
        for node_id in ("record", "record2"):
            mappings = implement_nodes[node_id]["config"]["mappings"]
            route = next(m for m in mappings if m["output"] == "route")
            self.assertEqual(route["expression"], {"kind": "getField", "path": "TASKS.route.data"})

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

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(server, "git_diff", return_value={"diff": "d"}), \
             mock.patch.object(server.Path, "is_file", lambda self: True), \
             mock.patch.object(server.subprocess, "run", fake_run):
            codex = server.native_review_result(
                "codex", "openai", "gpt-5.6-sol", "ticket", Path(td)
            )
            claude = server.native_review_result(
                "claude", "anthropic", "claude-sonnet-5", "ticket", Path(td)
            )
        self.assertEqual((codex["verdict"], claude["verdict"]), ("PASS", "PASS"))
        codex_cmd, codex_kwargs = commands[0]
        self.assertEqual(codex_cmd[codex_cmd.index("--sandbox") + 1], "read-only")
        self.assertIn("model_reasoning_effort=high", codex_cmd)
        self.assertEqual(codex_cmd[codex_cmd.index("--model") + 1], "gpt-5.6-sol")
        self.assertIn("input", codex_kwargs)
        claude_cmd, _ = commands[1]
        self.assertEqual(claude_cmd[claude_cmd.index("--permission-mode") + 1], "plan")
        self.assertEqual(claude_cmd[claude_cmd.index("--model") + 1], "claude-sonnet-5")
        self.assertEqual(claude_cmd[claude_cmd.index("--max-turns") + 1], str(server.REVIEW_MAX_TURNS))

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
            for outcome, code in cases:
                effect = outcome if isinstance(outcome, BaseException) else None
                with self.subTest(outcome=type(outcome).__name__, code=code), \
                     mock.patch.object(server, "git_diff", return_value={"diff": "d"}), \
                     mock.patch.object(server.Path, "is_file", lambda self: True), \
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
        with mock.patch.object(server.Path, "is_file", lambda self: False):
            missing = server.native_review_result(
                "codex", "openai", "gpt-5.6-sol", "ticket", Path(td)
            )
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["code"], "not_implemented")

    def test_grok_review_reuses_the_direct_acp_launcher(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            job_id = "ab" * 16
            (jobs / job_id).mkdir(parents=True)

            def fake_acp(job):
                self.assertEqual(job["response_webhook_url"], "https://example.invalid/resume")
                path = jobs / job["job_id"]
                path.joinpath("last-message.txt").write_text(json.dumps({
                    "status": "ok", "sha": None, "pr_url": None,
                    "summary": "VERDICT: PASS",
                }))
                return 0, False, "grok-review-session", None

            with mock.patch.object(server, "JOBS_DIR", jobs), \
                 mock.patch.object(server, "GROK_BIN", Path(td) / "grok"), \
                 mock.patch.object(server.Path, "is_file", lambda self: True), \
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

    def test_stack_status_default(self):
        status, payload, _ = server.dispatch("GET", "/v1/stack/status", {}, True, b"")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stack"], "graphwing")
        self.assertIn("containers", payload)
        self.assertIn("health", payload)

    def test_stack_unknown(self):
        status, payload, _ = server.dispatch(
            "GET", "/v1/stack/status", {"stack": ["nope"]}, True, b""
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "unknown_stack")

    def test_port_check_graphwing(self):
        status, payload, _ = server.dispatch(
            "GET", "/v1/port/check", {"port": ["8645"]}, True, b""
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["ports"][0]["port"], 8645)
        self.assertIn("listening", payload["ports"][0])

    def test_port_check_unknown(self):
        status, payload, _ = server.dispatch(
            "GET", "/v1/port/check", {"port": ["1"]}, True, b""
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "unknown_port")

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

    def test_stack_not_configured(self):
        with mock.patch.object(server, "STACKS_PATH", Path("/nope/stacks.json")):
            status, payload, _ = server.dispatch("GET", "/v1/stack/status", {}, True, b"")
        self.assertEqual(status, 501)
        self.assertEqual(payload["code"], "not_configured")

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
            nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
            incoming: dict[str, int] = {}
            outgoing: dict[str, list[str]] = {}
            for edge in graph["spec"]["edges"]:
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

    def test_pr_status_graph_is_read_only_and_unauthenticated(self):
        graph_path = Path(__file__).resolve().parent / "graphs" / "pr-status.json"
        graph = json.loads(graph_path.read_text())
        nodes = graph["spec"]["nodes"]
        webhook = next(node for node in nodes if node["id"] == "hook")
        self.assertFalse(webhook["config"]["requireAuthHeader"])
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
        self.assertIn("TASKS.review1.data.compact", rn1["config"]["prompt"])
        self.assertEqual(edges["e_rn1_tbad"]["target"], "join_wait_human")
        self.assertEqual(edges["e_rn2_tbad"]["target"], "join_wait_human")
        self.assertNotIn("switch_retry", {node["id"] for node in graph["spec"]["nodes"]})
        self.assertEqual(edges["e_e2e_auto"]["target"], "walk_e2e")
        self.assertEqual(edges["e_walk_e2e_ok"]["target"], "join_slices_complete")
        agent2 = next(node for node in graph["spec"]["nodes"] if node["id"] == "agent2")
        self.assertEqual(agent2["config"]["session_identity"], "{{ TASKS.record.session_identity }}")
        self.assertEqual(agent2["config"]["resume_job_id"], "{{ TASKS.record.resume_job_id }}")
        self.assertIn("TASKS.test.data.compact", agent2["config"]["prompt"])
        self.assertIn("Continue this slice", agent2["config"]["prompt"])
        self.assertIn("TASKS.ticket_head.data.text", agent2["config"]["prompt"])
        self.assertNotIn("CTX.INPUT.prompt", agent2["config"]["prompt"])

    def test_pr_drive_keeps_files_on_red(self):
        graph_path = Path(__file__).resolve().parent / "graphs" / "pr-drive.json"
        graph = json.loads(graph_path.read_text())
        node_types = [node["type"].lower() for node in graph["spec"]["nodes"]]
        self.assertFalse(any("git/restore" in node_type for node_type in node_types))
        edges = {edge["id"]: edge for edge in graph["spec"]["edges"]}
        self.assertEqual(edges["e18d"]["target"], "herdr_test_fail")
        fail = next(node for node in graph["spec"]["nodes"] if node["id"] == "herdr_test_fail")
        self.assertIn("files kept", fail["label"])

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
        self.assertEqual(edges["e7k"]["target"], "ticket_head")
        self.assertEqual(edges["e7b"]["source"], "ticket_head")
        self.assertEqual(edges["e7b"]["target"], "wait")
        self.assertEqual(edges["e7c"]["target"], "ticket_fail")
        self.assertEqual(edges["e_commit_out"]["target"], "complete")
        self.assertEqual(edges["e12"]["target"], "join_walk")
        self.assertEqual(edges["e_join_walk"]["target"], "walk")
        self.assertEqual(edges["e7h"]["target"], "join_e2e")
        self.assertEqual(edges["e12d"]["target"], "join_e2e")
        self.assertEqual(edges["e_join_e2e"]["target"], "e2e")
        commit = next(node for node in graph["spec"]["nodes"] if node["id"] == "commit")
        self.assertEqual(commit["config"]["add"], "{{ CTX.INPUT.index }}")
        agent = next(n for n in graph["spec"]["nodes"] if n["id"] == "agent")
        self.assertEqual(agent["config"]["launcher"], "{{ TASKS.route.data.launcher }}")
        agent2 = next(n for n in graph["spec"]["nodes"] if n["id"] == "agent2")
        self.assertEqual(agent2["config"]["launcher"], "{{ TASKS.route.data.launcher }}")
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
            "reviewer2_reason",
        ):
            self.assertIn(field, route)

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
