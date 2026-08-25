#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

    def test_agent_profiles(self):
        status, payload, _ = server.dispatch("GET", "/v1/agent/profiles", {}, True, b"")
        self.assertEqual(status, 200, payload)
        ids = [p["id"] for p in payload["profiles"]]
        self.assertEqual(ids, ["graphwing"])
        self.assertEqual(payload["profiles"][0]["herdr_session"], "graphwing")
        self.assertEqual(payload["profiles"][0]["hermes_home"], str(server.HOME.resolve()))
        self.assertTrue(payload["profiles"][0]["runnable"])
        for banned in ("executor", "fable", "cheap-exec"):
            self.assertNotIn(banned, ids)

    def test_agent_run_requires_auth(self):
        status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, False, b'{"prompt":"x"}')
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "unauthorized")

    def test_agent_run_missing_prompt(self):
        status, payload, _ = server.dispatch("POST", "/v1/agent/run", {}, True, b"{}")
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "missing_prompt")

    def test_agent_run_unknown_profile(self):
        status, payload, _ = server.dispatch(
            "POST", "/v1/agent/run", {}, True, b'{"profile":"nope","prompt":"x"}'
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "unknown_profile")

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
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                with mock.patch.object(server, "JOBS_DIR", jobs):
                    with mock.patch.object(server, "enqueue_agent", lambda job: None):
                        status, payload, _ = server.dispatch(
                            "POST",
                            "/v1/agent/run",
                            {},
                            True,
                            b'{"prompt":"ping","cwd":"scratch"}',
                        )
            self.assertEqual(status, 202, payload)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "queued")
            self.assertEqual(payload["profile"], "graphwing")
            self.assertEqual(payload["repo"], "scratch")
            job_id = payload["job_id"]
            self.assertRegex(job_id, r"^[0-9a-f]{32}$")
            self.assertEqual(payload["hermes_session"], f"gwslice-{job_id}")
            self.assertEqual(payload["poll"], f"/v1/agent/jobs/{job_id}")
            with mock.patch.object(server, "JOBS_DIR", jobs):
                gstatus, gp, _ = server.dispatch(
                    "GET", f"/v1/agent/jobs/{job_id}", {}, True, b""
                )
            self.assertEqual(gstatus, 200, gp)
            self.assertEqual(gp["status"], "queued")
            self.assertEqual(gp["prompt"], "ping")
            self.assertEqual(gp["hermes_session"], f"gwslice-{job_id}")
            self.assertNotIn("response_webhook_token", gp)
            self.assertNotIn("resume_url", gp)

    def test_agent_run_rejects_bad_hermes_session(self):
        status, payload, _ = server.dispatch(
            "POST",
            "/v1/agent/run",
            {},
            True,
            b'{"prompt":"x","hermes_session":"../evil"}',
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "bad_hermes_session")

    def test_agent_run_accepts_hermes_session(self):
        session = "gwslice-" + ("ab" * 16)
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            jobs = Path(td) / "jobs"
            body = json.dumps({"prompt": "ping", "cwd": "scratch", "hermes_session": session}).encode()
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}):
                with mock.patch.object(server, "JOBS_DIR", jobs):
                    with mock.patch.object(server, "enqueue_agent", lambda job: None):
                        status, payload, _ = server.dispatch(
                            "POST", "/v1/agent/run", {}, True, body
                        )
            self.assertEqual(status, 202, payload)
            self.assertEqual(payload["hermes_session"], session)

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

    def test_agent_run_missing_binary(self):
        with mock.patch.object(server, "HERMES_BIN", Path("/nope/hermes")):
            status, payload, _ = server.dispatch(
                "POST", "/v1/agent/run", {}, True, b'{"prompt":"x"}'
            )
        self.assertEqual(status, 501)
        self.assertEqual(payload["code"], "not_implemented")

    def test_wrap_prompt_locks_cwd(self):
        text = server.wrap_prompt("ab" * 16, "ping", "/home/tim/work/gw-real-slice")
        self.assertIn("/home/tim/work/gw-real-slice", text)
        self.assertIn("only inside that directory", text)
        self.assertIn("git add --", text)
        self.assertIn("relative paths only", text)
        self.assertIn("Do not git commit, git push", text)
        self.assertIn("Do not `git checkout`", text)

    def test_spawn_hermes_continues_named_session(self):
        captured: dict = {}

        class FakePopen:
            pid = 7

            def __init__(self, cmd, **kwargs):
                captured["cmd"] = list(cmd)

        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            job_id = "ab" * 16
            session = "gwslice-" + job_id
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            (jdir / "prompt.txt").write_text("x")
            hermes = Path(td) / "hermes"
            hermes.write_text("#!/bin/sh\n")
            job = {
                "job_id": job_id,
                "cwd": td,
                "max_turns": 1,
                "run_budget_seconds": 5,
                "hermes_session": session,
            }
            with mock.patch.object(server, "JOBS_DIR", jobs):
                with mock.patch.object(server, "HERMES_BIN", hermes):
                    with mock.patch.object(server.subprocess, "Popen", FakePopen):
                        proc, err = server.spawn_hermes(job)
            self.assertIsNone(err)
            self.assertIsNotNone(proc)
            cmd = captured["cmd"]
            self.assertIn("--continue", cmd)
            self.assertEqual(cmd[cmd.index("--continue") + 1], session)
            self.assertIn("--create-if-missing", cmd)
            self.assertIn("--source", cmd)
            self.assertEqual(cmd[cmd.index("--source") + 1], "tool")

    def test_hermes_job_env_overrides_terminal_cwd(self):
        with mock.patch.dict(os.environ, {"TERMINAL_CWD": "/home/tim/rewst/riftwing", "PWD": "/home/tim"}):
            env = server.hermes_job_env(
                {"job_id": "ab" * 16, "cwd": "/home/tim/work/gw-real-slice"}
            )
        self.assertEqual(env["TERMINAL_CWD"], "/home/tim/work/gw-real-slice")
        self.assertEqual(env["PWD"], "/home/tim/work/gw-real-slice")
        self.assertNotEqual(env.get("TERMINAL_CWD"), "/home/tim/rewst/riftwing")

    def test_run_agent_completes_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            job_id = "ab" * 16
            jdir = jobs / job_id
            jdir.mkdir(parents=True)
            job = {
                "job_id": job_id,
                "status": "queued",
                "profile": "graphwing",
                "repo": "riftwing",
                "cwd": str(td),
                "prompt": "ping",
                "hermes_session": "gwslice-" + ("ab" * 16),
                "response_webhook_url": "https://example.com/resume",
                "response_webhook_token": "tok_secret",
                "created_at": "t",
                "started_at": None,
                "finished_at": None,
                "max_turns": 1,
                "run_budget_seconds": 5,
                "receipt": None,
                "log_ref": str(jdir / "stdout.log"),
                "error": None,
                "webhook": None,
            }
            (jdir / "job.json").write_text(json.dumps(job))
            (jdir / "prompt.txt").write_text("x")

            class FakeProc:
                pid = 4242
                returncode = 0

                def wait(self, timeout=None):
                    (jdir / "stdout.log").write_text(
                        '{"status":"ok","sha":null,"pr_url":null,"summary":"pong"}'
                    )
                    return 0

            with mock.patch.object(server, "JOBS_DIR", jobs):
                with mock.patch.object(server, "spawn_hermes", return_value=(FakeProc(), None)):
                    with mock.patch.object(
                        server, "post_receipt", return_value={"ok": True, "status": 200}
                    ) as posted:
                        server.run_agent_job(job_id)
            saved = json.loads((jdir / "job.json").read_text())
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["receipt"]["summary"], "pong")
            self.assertEqual(saved["receipt"]["job_id"], job_id)
            self.assertEqual(saved["receipt"]["hermes_session"], "gwslice-" + ("ab" * 16))
            posted.assert_called_once()
            _args, kwargs = posted.call_args
            self.assertEqual(_args[0], "https://example.com/resume")
            self.assertEqual(kwargs.get("token"), "tok_secret")

    def test_agent_run_stores_webhook_hides_token(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            body = json.dumps(
                {
                    "prompt": "ping",
                    "response_webhook_url": "https://app.rewst.ai/api/callbacks/11111111-1111-1111-1111-111111111111",
                    "response_webhook_token": "tok_secret",
                }
            ).encode()
            with mock.patch.object(server, "JOBS_DIR", jobs):
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

    def test_openapi_file(self):
        status, payload, ctype = server.dispatch("GET", "/openapi.json", {}, False, b"")
        self.assertEqual(status, 200)
        spec = json.loads(payload)
        self.assertEqual(spec["info"]["title"], "graphwing")
        self.assertEqual(spec["info"]["version"], "0.6.0")
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

    def test_agent_run_executor_unknown(self):
        status, payload, _ = server.dispatch(
            "POST", "/v1/agent/run", {}, True, b'{"profile":"executor","prompt":"x"}'
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "unknown_profile")

    def test_agent_run_listed_not_runnable(self):
        fake = [
            {
                "id": "graphwing",
                "kind": "home",
                "hermes_home": "/tmp/graphwing",
                "herdr_session": "graphwing",
                "herdr_agent": "graphwing",
                "runnable": True,
            },
            {
                "id": "executor",
                "kind": "seat",
                "hermes_home": "/tmp/hermes-executor",
                "herdr_session": "executor",
                "herdr_agent": "executor",
                "runnable": False,
            },
        ]
        with mock.patch.object(server, "load_profiles", return_value=fake):
            status, payload, _ = server.dispatch(
                "POST", "/v1/agent/run", {}, True, b'{"profile":"executor","prompt":"x"}'
            )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "not_runnable")

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
        # The claude branch hardcoded --max-turns 1 while hermes used 8. One
        # turn cannot read a diff and answer.
        self.assertGreaterEqual(server.REVIEW_MAX_TURNS, 8)

    def _review_cmd(self, reviewer):
        """Build one review command without running it."""
        seen = {}

        class FakeProc:
            stdout = b"VERDICT: PASS\n"
            stderr = b""
            returncode = 0

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return FakeProc()

        with mock.patch.object(server, "git_diff", return_value={"diff": "diff --git a b"}), \
             mock.patch.object(server.subprocess, "run", fake_run), \
             mock.patch.object(server.Path, "is_file", lambda self: True), \
             mock.patch.object(server, "hermes_job_env", return_value={}):
            server.review_result(reviewer, "ticket text", Path("/tmp"))
        return seen["cmd"]

    def test_all_three_vendors_are_in_the_loop(self):
        # Pulling Sol out for the planner conflict left OpenAI reviewing
        # exactly one class. Opposing-vendor is necessary but not sufficient:
        # each vendor should be reviewing someone.
        VENDOR = {"grok-4.6": "xai", "claude-opus-5": "anthropic",
                  "sonnet": "anthropic", "opus": "anthropic", "fable": "anthropic",
                  "grok": "xai", "terra": "openai", "sol": "openai"}
        reviewing = set()
        for cls in ("mechanical", "visual", "sensitive"):
            r = server.slice_route_lookup(cls, "M")
            for slot in ("reviewer1", "reviewer2"):
                if r[slot] != "none":
                    reviewing.add(VENDOR[r[slot]])
        self.assertEqual(reviewing, {"anthropic", "openai", "xai"},
                         f"only {sorted(reviewing)} review anything")

    def test_reviewer_is_always_an_opposing_vendor(self):
        # The rule is vendor separation, not model separation. Fable grading
        # Opus is Anthropic reviewing Anthropic, which is the thing the rule
        # exists to prevent. Sol is the planner, so xAI is the only vendor that
        # is neither the writer's nor the planner's.
        VENDOR = {
            "grok-4.6": "xai", "claude-opus-5": "anthropic",
            "sonnet": "anthropic", "opus": "anthropic", "fable": "anthropic",
            "grok": "xai", "terra": "openai", "sol": "openai",
        }
        for cls in ("mechanical", "visual", "sensitive"):
            for size in ("S", "M", "L"):
                r = server.slice_route_lookup(cls, size)
                writer = VENDOR[r["model"]]
                for slot in ("reviewer1", "reviewer2"):
                    who = r[slot]
                    if who == "none":
                        continue
                    self.assertNotEqual(VENDOR[who], writer,
                                        f"{cls}/{size}: {who} shares a vendor with the writer")
                    self.assertNotEqual(who, "sol",
                                        f"{cls}/{size}: the planner must not review")

    def test_sol_does_not_review_when_sol_plans(self):
        # Sol is the planning session, so Sol grading slices against its own
        # spec is the failure the opposing-vendor rule exists to prevent, one
        # step earlier in the chain. visual and sensitive both reviewed with
        # Sol; they now review with Anthropic models.
        for cls in ("visual", "sensitive"):
            for size in ("S", "M", "L"):
                r = server.slice_route_lookup(cls, size)
                self.assertNotIn("sol", (r["reviewer1"], r["reviewer2"]),
                                 f"{cls}/{size} still reviews with the planner")

    def test_fable_is_a_usable_reviewer_even_though_unrouted(self):
        # Kept callable for /v1/review/run, but sliceRoute no longer picks it:
        # Fable is Anthropic and so are these writers.
        # Adding a reviewer name the runner cannot launch would nack every
        # slice with not_implemented, which parses as a real rejection.
        seen = {}

        class FakeProc:
            stdout = b"VERDICT: PASS\n"
            stderr = b""
            returncode = 0

        with mock.patch.object(server, "git_diff", return_value={"diff": "d"}), \
             mock.patch.object(server.subprocess, "run",
                               lambda cmd, **kw: (seen.__setitem__("cmd", cmd), FakeProc())[1]), \
             mock.patch.object(server.Path, "is_file", lambda self: True), \
             mock.patch.object(server, "hermes_job_env", return_value={}):
            out = server.review_result("fable", "ticket", Path("/tmp"))
        self.assertEqual(out["verdict"], "PASS", out)
        self.assertIn("--model", seen["cmd"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("--model") + 1], "claude-fable-5")
        # Reviewers are read-only; plan mode is what enforces that.
        self.assertIn("plan", seen["cmd"])

    def test_sol_reviewer_cannot_write_to_the_repo(self):
        # The claude reviewers get --permission-mode plan, which enforces
        # read-only in the runner. The hermes reviewer got --yolo and no
        # toolset restriction, so "do not edit files, commit, or push" was
        # prompt text a model could ignore. Sol gates every visual and
        # sensitive slice, which are the classes least safe to leave writable.
        # The diff and ticket are already in the prompt, so the reviewer needs
        # no file or terminal tools to answer.
        cmd = self._review_cmd("sol")
        self.assertIn("-t", cmd, "sol review must restrict toolsets")
        toolsets = cmd[cmd.index("-t") + 1].split(",")
        for banned in ("file", "terminal", "code_execution", "browser"):
            self.assertNotIn(banned, toolsets)
        # -t '' is silently ignored by hermes: an empty string falls back to
        # the config default and the model keeps its file tools.
        self.assertTrue(toolsets and toolsets[0], "empty -t does not restrict anything")

    def test_sol_reviewer_honours_the_shared_turn_budget(self):
        # REVIEW_MAX_TURNS is env-tunable but only the claude branch read it.
        # The hermes branch passed a literal "8", so raising the knob did
        # nothing for the reviewer that gates visual and sensitive slices.
        cmd = self._review_cmd("sol")
        self.assertIn("--max-turns", cmd)
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], str(server.REVIEW_MAX_TURNS))

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
        # pinned neither, so its writer took whatever the seat's sticky hermes
        # profile pointed at — job.json recorded model: null. A `hermes profile
        # use` anywhere on the box silently changed which model fixes PRs, and
        # the closed class table did not apply to that writer at all.
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

    def test_review_run_is_async_when_given_a_webhook(self):
        body = json.dumps({
            "repo": "scratch", "reviewer": "sonnet", "prompt": "ticket",
            "response_webhook_url": "https://example.invalid/resume",
        }).encode()
        with tempfile.TemporaryDirectory() as td:
            repo = self._scratch_git(Path(td))
            with mock.patch.object(server, "load_repos", return_value={"scratch": str(repo)}), \
                 mock.patch.object(server, "active_job_count", return_value=0), \
                 mock.patch.object(server, "enqueue_review") as enq:
                status, payload, _ = server.dispatch("POST", "/v1/review/run", {}, True, body)
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["kind"], "review")
        self.assertTrue(payload["job_id"])
        enq.assert_called_once()

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

    def test_finished_writer_pane_shows_the_real_session(self):
        # The pane tails stdout.log while the job runs because the headless
        # process owns the session. Once it is done the tail is a dead file,
        # so the linger window should show the session you can actually read.
        sent = []
        job = {"job_id": "cd" * 16, "status": "completed", "herdr_pane_id": "w1:p3",
               "hermes_session": "gwslice-" + "a" * 32, "cwd": "/tmp"}
        with mock.patch.object(server, "herdr_send", lambda pane, text: sent.append((pane, text))):
            server.herdr_show_session(job)
        self.assertEqual(sent[0], ("w1:p3", "\x03"), "must stop tail -F first")
        self.assertIn("--resume", sent[1][1])
        self.assertIn("gwslice-" + "a" * 32, sent[1][1])

    def test_non_writer_panes_keep_the_tail(self):
        # test and review jobs are not hermes and have no session to resume.
        sent = []
        with mock.patch.object(server, "herdr_send", lambda pane, text: sent.append(text)):
            server.herdr_show_session({"herdr_pane_id": "w1:p3", "kind": "test", "hermes_session": None})
            server.herdr_show_session({"herdr_pane_id": "w1:p3", "hermes_session": "not-a-gwslice-name"})
            server.herdr_show_session({"hermes_session": "gwslice-" + "b" * 32})
        self.assertEqual(sent, [])

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

    def test_catalog_soul_is_agnostic(self):
        text = (Path(__file__).resolve().parent / "SOUL.md").read_text()
        self.assertIn("$GRAPHWING_HOME", text)
        self.assertNotIn("/home/tim", text)
        self.assertNotIn("tim-graphwing", text)

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
        self.assertIn("hermes_session", agent2["config"])
        self.assertIn("TASKS.wait.request.body.hermes_session", agent2["config"]["hermes_session"])
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
        status, payload, _ = server.dispatch(
            "POST", "/v1/slice/route", {}, True, b'{"class":"mechanical","size":"S"}'
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["launcher"], "hermes")
        self.assertEqual(payload["reviewer1"], "none")
        self.assertEqual(payload["max_turns"], 10)
        status, payload, _ = server.dispatch(
            "POST", "/v1/slice/route", {}, True, b'{"class":"mechanical","size":"M"}'
        )
        self.assertEqual(payload["reviewer1"], "sonnet")
        self.assertEqual(payload["max_turns"], 30)
        status, payload, _ = server.dispatch(
            "POST",
            "/v1/slice/route",
            {},
            True,
            b'{"class":"mechanical","size":"S","ac_count":6}',
        )
        self.assertEqual(payload["size"], "M")
        self.assertEqual(payload["size_floor"], "S")
        status, payload, _ = server.dispatch(
            "POST", "/v1/slice/route", {}, True, b'{"class":"visual","size":"S"}'
        )
        self.assertEqual(payload["launcher"], "claude")
        self.assertEqual(payload["size"], "M")
        self.assertEqual(payload["reviewer1"], "terra")
        status, payload, _ = server.dispatch(
            "POST", "/v1/slice/route", {}, True, b'{"class":"visual","size":"M"}'
        )
        self.assertEqual(payload["launcher"], "claude")
        self.assertEqual(payload["reviewer1"], "terra")
        status, payload, _ = server.dispatch(
            "POST", "/v1/slice/route", {}, True, b'{"class":"sensitive","size":"M"}'
        )
        self.assertEqual(payload["reviewer1"], "terra")
        self.assertEqual(payload["reviewer2"], "grok")
        status, payload, _ = server.dispatch(
            "POST", "/v1/slice/route", {}, True, b'{"class":"nope","size":"M"}'
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "bad_class")

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
            b'{"prompt":"x","launcher":"codex"}',
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
            "SOUL.md",
            "bin/graphwing",
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
                    "--no-omarchy-plugin",
                    "--no-cli",
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
            clean = [stack for stack in stacks["stacks"] if stack.get("role") == "clean"]
            self.assertEqual(len(clean), 1)
            self.assertEqual(clean[0]["runner"], "git-worktree")
            self.assertTrue((home / clean[0]["cwd"]).is_dir())
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
            soul = (home / "SOUL.md").read_text()
            self.assertIn(str(home), soul)
            self.assertNotIn("$GRAPHWING_HOME", soul)
            self.assertNotIn("tim-graphwing", soul)

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
        self.assertIn("hermes-agent.nousresearch.com/install.sh", text)
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "gw"
            units = Path(td) / "units"
            proc = subprocess.run(
                [
                    "bash",
                    str(script),
                    "--yes",
                    "--no-hermes",
                    "--no-herdr",
                    "--tunnel",
                    "none",
                    "--home",
                    str(home),
                    "--unit-dir",
                    str(units),
                    "--no-cli",
                    "--no-start",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue((home / "server.py").is_file())
            self.assertTrue((home / "api.key").read_text().strip())
            self.assertFalse((home / "rr.json").exists())
            self.assertTrue((home / "rr.example.json").is_file())
            self.assertFalse((units / "graphwing-tunnel.service").exists())
            self.assertIn("not starting", proc.stdout)
            self.assertNotIn("installing Hermes", proc.stdout)
            self.assertNotIn("installing herdr", proc.stdout)


class BuildStateTests(unittest.TestCase):
    """graphwing-pre-pr-build is many bounded Rewst calls against one build.

    Every test here is a retry story: the same call arriving twice, two runs
    racing for the same build, a lease that outlived the caller holding it, a
    service that restarted mid-build. Each one of those, answered wrong,
    duplicates a real side effect — a second writer session, a second PR.
    """

    BUILD = "bld-0000001"
    BRANCH = "feature/issue-52"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self.home = root / "gwhome"
        self.builds = self.home / "builds"
        self.repo = root / "repo"
        self.clean_runner = root / "clean-runner"
        self.clean_runner.mkdir()
        self.repo.mkdir()
        for args in (
            ["git", "init", "-b", "main", str(self.repo)],
            ["git", "-C", str(self.repo), "config", "user.email", "gw@test"],
            ["git", "-C", str(self.repo), "config", "user.name", "graphwing-test"],
            ["git", "-C", str(self.repo), "config", "commit.gpgsign", "false"],
        ):
            subprocess.run(args, check=True, capture_output=True)
        (self.repo / "README").write_text("hi\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "README"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "init"], check=True, capture_output=True)
        # The build's identity is what git says, so the fixture has to be a
        # real checkout of the branch under test. A declared branch the repo
        # is not on, or a made-up head, tests the guard against itself and
        # passes no matter what the checkout is doing.
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-b", self.BRANCH], check=True, capture_output=True
        )
        self.head = self._git("rev-parse", "HEAD")
        self.assertRegex(self.head, r"^[0-9a-f]{40}$")
        bp = mock.patch.object(server, "BUILDS_DIR", self.builds)
        bp.start()
        self.addCleanup(bp.stop)
        jp = mock.patch.object(server, "JOBS_DIR", self.home / "jobs")
        jp.start()
        self.addCleanup(jp.stop)
        rp = mock.patch.object(server, "load_repos", return_value={"scratch": str(self.repo)})
        rp.start()
        self.addCleanup(rp.stop)
        sp = mock.patch.object(
            server,
            "build_stack_catalog",
            return_value={
                "clean-stack": {
                    "name": "clean-stack",
                    "role": "clean",
                    "cwd": self.clean_runner,
                    "runner": "git-worktree",
                }
            },
        )
        sp.start()
        self.addCleanup(sp.stop)

    # -- helpers ------------------------------------------------------------

    def _git(self, *args):
        out = subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True
        )
        return out.stdout.strip()

    def _commit(self, text):
        """Move the worktree HEAD the way a writer agent would."""
        (self.repo / "README").write_text(text)
        self._git("add", "README")
        self._git("commit", "-m", text)
        return self._git("rev-parse", "HEAD")

    def _create_body(self, **over):
        body = {
            "build_id": self.BUILD,
            "story": "issue-52 pre-PR build",
            "repo": "scratch",
            "branch": self.BRANCH,
            "head": self.head,
            "index": "slices/demo/index.json",
            "ticket": "slices/demo/01-build-state.md",
            # Ticket-01 exercises the legacy generic transition surface. Visual
            # is deliberately outside the authoritative mechanical/sensitive
            # path, whose writer and gate assertions are operation-owned.
            "route": {"class": "visual", "launcher": "hermes", "model": "sol"},
            "verification": {"fast": ["graphwing-compile"], "integration": ["graphwing-unit"]},
            "stacks": ["riftwing-52"],
            "budget": {"jobs_max": 12},
        }
        body.update(over)
        return body

    def _create(self, **over):
        body = self._create_body(**over)
        return server.dispatch("POST", "/v1/build/create", {}, True, json.dumps(body).encode())

    def _create_omitting(self, *fields, **over):
        body = self._create_body(**over)
        for field in fields:
            body.pop(field)
        return server.dispatch("POST", "/v1/build/create", {}, True, json.dumps(body).encode())

    def _advance(self, action, event_id, holder="rewst-run-1", **over):
        body = {"build_id": self.BUILD, "event_id": event_id, "action": action, "holder": holder}
        body.update(over)
        return server.dispatch("POST", "/v1/build/advance", {}, True, json.dumps(body).encode())

    def _state(self, build_id=None):
        return server.dispatch("GET", "/v1/build/state", {"build_id": [build_id or self.BUILD]}, True, b"")

    def _post_build(self, operation, **body):
        body.setdefault("build_id", self.BUILD)
        return server.dispatch(
            "POST", f"/v1/build/{operation}", {}, True, json.dumps(body).encode()
        )

    def _next(self, **query):
        query.setdefault("build_id", [self.BUILD])
        return server.dispatch("GET", "/v1/build/next", query, True, b"")

    def _claim(self, event_id="claim-initial", holder="rewst-run-1", kind="initial", **over):
        body = {"event_id": event_id, "holder": holder, "kind": kind}
        body.update(over)
        return self._post_build("claim", **body)

    def _launch(self, claim_id="claim-initial", holder="rewst-run-1", **over):
        body = {
            "claim_id": claim_id,
            "holder": holder,
            "response_webhook_url": "https://example.test/writer",
            "response_webhook_token": "resume-token",
        }
        body.update(over)
        return self._post_build("writer/launch", **body)

    def _complete(
        self,
        event_id,
        launch,
        callback,
        claim_id="claim-initial",
        holder="rewst-run-1",
        **over,
    ):
        body = {
            "event_id": event_id,
            "holder": holder,
            "claim_id": claim_id,
            "job_id": launch["job_id"],
            "hermes_session": launch["hermes_session"],
            "callback": callback,
        }
        body.update(over)
        return self._post_build("writer/complete", **body)

    def _finish_job(self, launch, status="ok", summary="done"):
        job = server.read_job(launch["job_id"])
        self.assertIsNotNone(job)
        receipt = {
            "status": status,
            "job_id": launch["job_id"],
            "profile": job["profile"],
            "hermes_session": launch["hermes_session"],
            "sha": None,
            "pr_url": None,
            "log_ref": job["log_ref"],
            "summary": summary,
        }
        job["receipt"] = receipt
        job["status"] = "completed" if status == "ok" else "failed"
        job["finished_at"] = server.utcnow()
        server.write_job(job)
        return receipt

    def _launch_writer(self, claim_id="claim-initial", holder="rewst-run-1"):
        claimed = self._claim(claim_id, holder=holder)
        self.assertEqual(claimed[0], 200, claimed[1])
        with mock.patch.object(server, "enqueue_agent"):
            status, launch, _ = self._launch(claim_id, holder=holder)
        self.assertEqual(status, 202, launch)
        return launch

    def _start_writer(
        self,
        event_id="evt-1",
        holder="rewst-run-1",
        session=None,
        claim_event_id="claim-initial",
        **over,
    ):
        data = dict(over.pop("data", {}) or {})
        if session is not None:
            data["writer_session"] = session
        return self._advance(
            "start_writer", event_id, holder=holder, data=data, jobs_spent=0, **over
        )

    def _record_fast_pass(self):
        doc = self._read_doc()
        head = self._git("rev-parse", "HEAD")
        change_id = server.build_change_id(self.repo, head)
        doc.setdefault("checks", {})["fast"] = {
            "phase": "fast",
            "head": head,
            "change_id": change_id,
            "runner": str(self.repo),
            "required": ["graphwing-compile"],
            "verdicts": {"graphwing-compile": {"ok": True, "coverage": True}},
            "ok": True,
        }
        self._write_doc(doc)
        return change_id

    def _gate(self, event_id, holder="rewst-run-1", **over):
        doc = self._read_doc()
        if server.build_class(doc) in server.BUILD_PATH_CLASSES:
            return self._post_build("gate", event_id=event_id, holder=holder, **over)
        action = "verify_passed" if doc.get("stage") == "verifying" else "review_passed"
        return self._advance(action, event_id, holder=holder, jobs_spent=0, **over)

    def _ready_finalize_fixture(self, filename="change.txt"):
        self.assertEqual(self._mechanical_create()[0], 201)
        (self.repo / "ticket.md").unlink()
        (self.repo / filename).write_text("ready\n")
        self._git("add", filename)
        change_id = server.build_change_id(self.repo, self.head)
        doc = self._read_doc()
        doc["stage"] = "ready"
        doc["verified_boundary"] = {"head": self.head}
        doc["checks"] = {
            phase: {
                "change_id": change_id,
                "stack": "clean-stack" if phase == "integration" else None,
                "runner": str(self.clean_runner) if phase == "integration" else str(self.repo),
                "required": [phase],
                "verdicts": {phase: {"ok": True, "coverage": True}},
            }
            for phase in ("fast", "integration")
        }
        tree = self._git("write-tree")
        doc["checks"]["integration"].update(
            head=self.head,
            runner=str(self.clean_runner / "recorded-attempt"),
            candidate_tree=tree,
            runner_tree=tree,
            base_head=self.head,
            materialization="a" * 40,
        )
        doc["reviews"] = [
            {"change_id": change_id, "reviewer": "sonnet", "verdict": "PASS", "summary": "clear"}
        ]
        self._write_doc(doc)
        return change_id

    def _prepare_finalize_intent(self, event_id="prepared-finalize", message="prepared message"):
        self._ready_finalize_fixture()
        original = server.run_git

        def stop_after_prepare(path, args, **kwargs):
            if args[0] == "commit":
                return {
                    "ok": False,
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "injected stop after prepare",
                    "error": "injected stop after prepare",
                }
            return original(path, args, **kwargs)

        request = {"event_id": event_id, "holder": "run", "message": message}
        with mock.patch.object(server, "run_git", side_effect=stop_after_prepare):
            stopped = self._post_build("finalize", **request)
        self.assertEqual(stopped[1]["code"], "commit_failed")
        intent = self._read_doc()["pending_finalize"]
        self.assertEqual(intent["version"], 1)
        self.assertEqual(intent["state"], "prepared")
        self.assertNotIn("sha", intent)
        for field in (
            "fingerprint", "verified_head", "expected_parent", "candidate_tree",
            "verified_change_id", "branch", "remote", "message", "authoring_boundary",
        ):
            self.assertTrue(intent.get(field), field)
        return request

    def _mechanical_route(self, **over):
        route = {
            "class": "mechanical",
            "launcher": "hermes",
            "model": "grok-4.6",
            "max_turns": 12,
            "run_budget_seconds": 900,
            "reviewer1": "sonnet",
        }
        route.update(over)
        return route

    def _sensitive_route(self, **over):
        route = {
            "class": "sensitive",
            "launcher": "claude",
            "model": "claude-opus-5",
            "max_turns": 30,
            "run_budget_seconds": 600,
            "reviewer1": "terra",
            "reviewer2": "grok",
        }
        route.update(over)
        return route

    def _recipe_catalog(self, **extra):
        def recipe(name, argv=("python3", "-c", "raise SystemExit(0)"), coverage=True, cwd=None):
            return {
                "name": name,
                "argv": list(argv),
                "cwd": Path(cwd or self.repo),
                "timeout_seconds": 10,
                "kind": "test",
                "coverage": coverage,
                "unresolvable": None,
            }

        catalog = {
            "fast": recipe("fast"),
            "integration": recipe("integration"),
            "path-check": recipe("path-check"),
            "riftwing-local-gates": recipe("riftwing-local-gates", coverage=False),
        }
        catalog.update(extra)
        return catalog

    def _mechanical_create(self, verification=None, **over):
        ticket = self.repo / "ticket.md"
        ticket.write_text("# Mechanical ticket\n\nChange the requested behavior only.\n")
        body = {
            "ticket": "ticket.md",
            "route": self._mechanical_route(),
            "verification": verification
            or {"fast": ["fast"], "integration": ["integration"]},
        }
        body.update(over)
        return self._create(**body)

    def _sensitive_create(self, verification=None, **over):
        ticket = self.repo / "ticket.md"
        ticket.write_text("# Sensitive ticket\n\nPreserve tenant isolation.\n")
        body = {
            "ticket": "ticket.md",
            "route": self._sensitive_route(),
            "verification": verification
            or {"fast": ["fast"], "integration": ["integration"]},
        }
        body.update(over)
        return self._create(**body)

    def _read_doc(self):
        return json.loads(self._doc_path().read_text())

    def _write_doc(self, doc):
        self.assertIsNone(server.write_build(doc, self.BUILD))

    def _doc_path(self, build_id=None):
        return self.builds / (build_id or self.BUILD) / "build.json"

    def _expire_lease(self):
        doc = json.loads(self._doc_path().read_text())
        self.assertIsNotNone(doc["lease"], "test needs a live lease to expire")
        doc["lease"]["expires_epoch"] = time.time() - 1
        self._doc_path().write_text(json.dumps(doc, indent=2) + "\n")

    def _age_out_event_receipt(self, event_id):
        doc = json.loads(self._doc_path().read_text())
        for i in range(server.BUILD_RECEIPTS_KEPT):
            filler_id = f"filler-{i}"
            server.build_record_event(
                doc,
                filler_id,
                f"fingerprint-{i}",
                {"event_id": filler_id, "filler": True},
                200,
            )
        self.assertIsNone(server.write_build(doc, self.BUILD))
        aged = json.loads(self._doc_path().read_text())
        self.assertNotIn(event_id, aged["events"])
        self.assertNotIn(event_id, aged["event_order"])
        self.assertIn(event_id, aged["event_seen"])

    def _through_verify(self, holder="rewst-run-1", **create_over):
        self.assertEqual(self._create(**create_over)[0], 201)
        status, payload, _ = self._start_writer(holder=holder)
        self.assertEqual(status, 200, payload)
        status, payload, _ = self._advance("writer_done", "evt-2", holder=holder, jobs_spent=0)
        self.assertEqual(status, 200, payload)
        self._record_fast_pass()
        status, payload, _ = self._gate("evt-3", holder=holder)
        self.assertEqual(status, 200, payload)
        return payload

    # -- creating -----------------------------------------------------------

    def test_create_records_a_versioned_document_and_the_next_action(self):
        status, payload, _ = self._create()
        self.assertEqual(status, 201, payload)
        self.assertTrue(payload["created"])
        self.assertEqual(payload["version"], server.BUILD_STATE_VERSION)
        self.assertEqual(payload["build_id"], self.BUILD)
        self.assertEqual(payload["stage"], "created")
        self.assertEqual(payload["next_action"], "start_writer")
        self.assertEqual(payload["budget_left"], 12)
        self.assertEqual(payload["worktree"], str(self.repo))
        # A 40-hex git object id is not a secret. Redaction that eats the head
        # would take the build's whole identity guard with it.
        self.assertEqual(payload["head"], self.head)
        self.assertEqual(payload["branch"], self.BRANCH)
        self.assertEqual(payload["stacks"], ["riftwing-52"])
        self.assertEqual(payload["verification"]["integration"], ["graphwing-unit"])
        self.assertEqual(payload["event_count"], 0)
        self.assertEqual(payload["event_capacity"], server.BUILD_EVENT_HARD_MAX)
        self.assertTrue(self._doc_path().is_file())

    def test_create_replayed_with_the_same_input_does_not_start_a_second_build(self):
        first = self._create()[1]
        status, payload, _ = self._create()
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["created"])
        self.assertEqual(payload["created_at"], first["created_at"])

    def test_create_reusing_a_build_id_with_new_input_is_a_conflict(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._create(story="a completely different story")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "idempotency_conflict")
        self.assertEqual(self._state()[1]["story"], "issue-52 pre-PR build")

    def test_create_without_a_build_id_is_refused_rather_than_generating_one(self):
        # A generated id is an id the caller cannot repeat, so every timed-out
        # retry would open a second build against the same worktree, each with
        # its own lease and its own budget.
        status, payload, _ = self._create(build_id=None)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "missing_build_id")
        self.assertFalse(self.builds.exists(), "a refused create must not create state")

    def test_create_refuses_a_worktree_the_repo_allowlist_does_not_back(self):
        status, payload, _ = self._create(worktree="/tmp/not-the-repo")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "worktree_mismatch")
        self.assertFalse(self._doc_path().exists())

    def test_create_refuses_a_branch_the_worktree_is_not_actually_on(self):
        self._git("checkout", "main")
        status, payload, _ = self._create()
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "branch_mismatch")
        self.assertEqual(payload["branch"], "main", "the answer names the live branch, not the declared one")
        self.assertFalse(self._doc_path().exists(), "no state exists yet, so there is nothing to park")

    def test_create_refuses_a_head_the_worktree_is_not_at(self):
        status, payload, _ = self._create(head="b" * 40)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "head_mismatch")
        self.assertEqual(payload["head"], self.head)
        self.assertFalse(self._doc_path().exists())

    def test_create_takes_its_head_from_git_even_when_the_caller_omits_one(self):
        status, payload, _ = self._create(head=None)
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["head"], self.head)

    def test_create_object_fields_reject_falsey_non_objects_and_accept_empty_objects(self):
        for field in ("verification", "route", "budget"):
            for bad in (None, "", False, 0, []):
                with self.subTest(field=field, value=bad):
                    status, payload, _ = self._create(**{field: bad})
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["code"], f"bad_{field}")
                    self.assertFalse(self._doc_path().exists(), "a refused create must not create state")

        status, payload, _ = self._create(verification={}, route={}, budget={})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["verification"]["fast"], [])
        self.assertEqual(payload["verification"]["integration"], [])
        self.assertEqual(payload["route"], {})
        self.assertEqual(payload["budget"], {"jobs_max": None, "jobs_used": 0})

    def test_create_object_field_omission_uses_defaults_and_route_is_null(self):
        status, payload, _ = self._create_omitting(
            "verification", "route", "budget", build_id="bld-0000002"
        )
        self.assertEqual(status, 201, payload)
        self.assertEqual(
            payload["verification"],
            {"fast": [], "integration": [], "needs_visual_proof": False, "human_visual_review": False},
        )
        self.assertIsNone(payload["route"])
        self.assertEqual(payload["budget"], {"jobs_max": None, "jobs_used": 0})

    def test_verification_command_lists_are_strict_and_trim_command_names(self):
        bad_values = (None, "", False, True, 0, 1, {}, "graphwing-unit", [""], ["graphwing-unit", 1])
        for field in ("fast", "integration"):
            for bad in bad_values:
                with self.subTest(field=field, value=bad):
                    verification = {"fast": [], "integration": [], field: bad}
                    status, payload, _ = self._create(verification=verification)
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["code"], "bad_verification")
                    self.assertFalse(self._doc_path().exists(), "a refused create must not create state")

        status, payload, _ = self._create(verification={})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["verification"]["fast"], [])
        self.assertEqual(payload["verification"]["integration"], [])

        status, payload, _ = self._create(
            build_id="bld-0000002", verification={"fast": [], "integration": []}
        )
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["verification"]["fast"], [])
        self.assertEqual(payload["verification"]["integration"], [])

        status, payload, _ = self._create(
            build_id="bld-0000003",
            verification={
                "fast": [" graphwing-compile ", "graphwing-unit"],
                "integration": [" graphwing-integration "],
            },
        )
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["verification"]["fast"], ["graphwing-compile", "graphwing-unit"])
        self.assertEqual(payload["verification"]["integration"], ["graphwing-integration"])

    def test_create_stacks_must_be_an_array_when_supplied(self):
        for bad in (None, "riftwing-52", False, 0, {}):
            with self.subTest(stacks=bad):
                status, payload, _ = self._create(stacks=bad)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "bad_stacks")
                self.assertFalse(self._doc_path().exists(), "a refused create must not create state")

        status, payload, _ = self._create(stacks=[])
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["stacks"], [])

        status, payload, _ = self._create_omitting("stacks", build_id="bld-0000002")
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["stacks"], [])

    def test_create_replay_fingerprints_raw_capability_urls_before_redaction(self):
        capability_url_a = "https://rewst.test/webhooks/custom/9f2c-aaaa1111"
        capability_url_b = "https://rewst.test/webhooks/custom/7b41-bbbb2222"

        def raw_input(url):
            body = {
                "build_id": self.BUILD,
                "story": "issue-52 pre-PR build",
                "repo": "scratch",
                "branch": self.BRANCH,
                "head": self.head,
                "index": "slices/demo/index.json",
                "ticket": "slices/demo/01-build-state.md",
                "route": {"next": url},
                "verification": {"fast": ["graphwing-compile"], "integration": ["graphwing-unit"]},
                "stacks": ["riftwing-52"],
                "budget": {"jobs_max": 12},
            }
            return json.dumps(body).encode()

        first_input = raw_input(capability_url_a)
        status, created, _ = server.dispatch("POST", "/v1/build/create", {}, True, first_input)
        self.assertEqual(status, 201, created)
        self.assertTrue(created["created"])
        self.assertEqual(created["route"], {"next": "[redacted]"})

        status, replay, _ = server.dispatch("POST", "/v1/build/create", {}, True, first_input)
        self.assertEqual(status, 200, replay)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["route"], {"next": "[redacted]"})

        status, conflict, _ = server.dispatch(
            "POST", "/v1/build/create", {}, True, raw_input(capability_url_b)
        )
        self.assertEqual(status, 409, conflict)
        self.assertEqual(conflict["code"], "idempotency_conflict")

        on_disk = self._doc_path().read_text()
        document = json.loads(on_disk)
        self.assertEqual(document["route"], {"next": "[redacted]"})
        self.assertNotIn(capability_url_a, on_disk)
        self.assertNotIn(capability_url_b, on_disk)

    def test_index_and_ticket_are_bounded_repo_relative_paths_or_absent(self):
        bad_values = (
            None,
            "",
            False,
            0,
            ["slices/demo"],
            {"path": "slices/demo"},
            7,
            True,
            "/tmp/ticket",
            "slices/../ticket",
            "x" * 257,
        )
        for field in ("index", "ticket"):
            for bad in bad_values:
                with self.subTest(field=field, value=bad):
                    status, payload, _ = self._create(**{field: bad})
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["code"], f"bad_{field}")
                    self.assertFalse(self._doc_path().exists(), "a refused create must not create state")

        bounded = "x" * server.BUILD_NAME_MAX
        status, payload, _ = self._create(index=bounded, ticket=bounded)
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["index"], bounded)
        self.assertEqual(payload["ticket"], bounded)

        absent_body = {
            "build_id": "bld-0000002",
            "story": "build without planner paths",
            "repo": "scratch",
            "branch": self.BRANCH,
            "head": self.head,
        }
        status, payload, _ = server.dispatch(
            "POST", "/v1/build/create", {}, True, json.dumps(absent_body).encode()
        )
        self.assertEqual(status, 201, payload)
        self.assertIsNone(payload["index"])
        self.assertIsNone(payload["ticket"])

    def test_visual_review_flags_accept_only_actual_booleans_or_omission(self):
        bad_values = ("false", "true", 0, 1, None, [], {})
        for field in ("needs_visual_proof", "human_visual_review"):
            for bad in bad_values:
                with self.subTest(field=field, value=bad):
                    verification = {"fast": [], "integration": [], field: bad}
                    status, payload, _ = self._create(verification=verification)
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["code"], "bad_verification")
                    self.assertFalse(self._doc_path().exists(), "a refused create must not create state")

        status, payload, _ = self._create(
            verification={
                "fast": [],
                "integration": [],
                "needs_visual_proof": True,
                "human_visual_review": False,
            }
        )
        self.assertEqual(status, 201, payload)
        self.assertTrue(payload["verification"]["needs_visual_proof"])
        self.assertFalse(payload["verification"]["human_visual_review"])

        status, payload, _ = self._create(
            build_id="bld-0000002", verification={"fast": [], "integration": []}
        )
        self.assertEqual(status, 201, payload)
        self.assertFalse(payload["verification"]["needs_visual_proof"])
        self.assertFalse(payload["verification"]["human_visual_review"])

    # -- transitions --------------------------------------------------------

    def test_the_whole_path_runs_when_no_visual_proof_is_declared(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._start_writer(session="gwslice-" + "0" * 32)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self._advance("writer_done", "evt-2", jobs_spent=0)[0], 200)
        change_id = self._record_fast_pass()
        self.assertEqual(self._gate("evt-3")[0], 200)
        doc = self._read_doc()
        doc["reviews"] = [{"change_id": change_id, "reviewer": "sonnet", "verdict": "PASS"}]
        self._write_doc(doc)
        status, payload, _ = self._gate("evt-4", release_lease=True)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["stage"], "ready")
        self.assertIsNone(payload["lease"])
        self.assertEqual(payload["writer_session"], "gwslice-" + "0" * 32)
        self.assertEqual(payload["budget_left"], 12)

    def test_evidence_is_a_real_stage_when_visual_proof_is_declared(self):
        self.assertEqual(
            self._create(verification={"fast": [], "integration": [], "needs_visual_proof": True})[0], 201
        )
        self.assertEqual(self._start_writer()[0], 200)
        self.assertEqual(self._advance("writer_done", "evt-2", jobs_spent=0)[0], 200)
        self._record_fast_pass()
        status, payload, _ = self._gate("evt-3")
        self.assertEqual(payload["stage"], "evidence", payload)
        self.assertEqual(payload["next_action"], "evidence_captured")
        status, payload, _ = self._advance(
            "evidence_captured", "evt-4", data={"round": {"n": 1, "shots": ["home.png"]}}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["stage"], "review")
        self.assertEqual(payload["evidence_rounds"][0]["round"], 1)
        self.assertEqual(payload["evidence_rounds"][0]["captures"], 1)
        self.assertNotIn("shots", payload["evidence_rounds"][0])
        self.assertEqual(self._read_doc()["evidence_rounds"], [{"n": 1, "shots": ["home.png"]}])

    def test_an_out_of_order_action_is_refused_and_names_the_expected_one(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._advance("review_passed", "evt-wrong")
        self.assertEqual(status, 409, payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "invalid_transition")
        self.assertEqual(self._state()[1]["stage"], "created")
        self.assertEqual(self._state()[1]["budget_left"], 12, "a refused action spends nothing")

    def test_an_unknown_action_is_a_request_error_not_a_park(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._advance("ship_it", "evt-1")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "bad_action")
        self.assertEqual(self._state()[1]["stage"], "created")

    def test_advance_object_fields_reject_falsey_non_objects_without_rewriting_state(self):
        self.assertEqual(self._create()[0], 201)
        before = self._doc_path().read_bytes()
        event = 1
        for field in ("data", "expect"):
            for bad in (None, "", False, 0, []):
                with self.subTest(field=field, value=bad):
                    status, payload, _ = self._advance("start_writer", f"evt-bad-{event}", **{field: bad})
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["code"], f"bad_{field}")
                    self.assertEqual(
                        self._doc_path().read_bytes(), before, "a refused advance must not rewrite state"
                    )
                event += 1

        status, payload, _ = self._advance(
            "start_writer", "evt-empty", data={}, expect={}, jobs_spent=0
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["stage"], "writing")

        status, payload, _ = self._advance("writer_done", "evt-omitted")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["stage"], "verifying")

    # -- idempotency --------------------------------------------------------

    def test_replaying_an_event_id_returns_the_receipt_without_rerunning(self):
        self.assertEqual(self._create()[0], 201)
        first = self._start_writer(session="gwslice-" + "1" * 32)[1]
        status, payload, _ = self._start_writer(session="gwslice-" + "1" * 32)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["replayed"])
        self.assertEqual(payload["receipt"], first["receipt"])
        self.assertEqual(payload["stage"], "writing")
        self.assertEqual(payload["budget_left"], first["budget_left"], "a replay spends no budget")
        self.assertEqual(payload["event_count"], 1, "the transition runs once")

    def test_a_successful_event_replays_after_its_recent_receipt_ages_out(self):
        self.assertEqual(self._create()[0], 201)
        first_status, first, _ = self._start_writer(event_id="evt-old")
        self.assertEqual(first_status, 200, first)
        self._age_out_event_receipt("evt-old")
        before = self._doc_path().read_bytes()

        status, payload, _ = self._start_writer(event_id="evt-old")
        self.assertEqual(status, first_status, payload)
        self.assertTrue(payload["replayed"])
        self.assertEqual(payload["receipt"], first["receipt"])
        self.assertEqual(self._doc_path().read_bytes(), before, "a replay must not rewrite state")

    def test_reusing_an_event_id_with_different_input_is_a_conflict(self):
        self.assertEqual(self._create()[0], 201)
        first = self._start_writer(session="gwslice-" + "1" * 32)[1]
        status, payload, _ = self._start_writer(session="gwslice-" + "2" * 32)
        self.assertEqual(status, 409, payload)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["replayed"])
        self.assertEqual(payload["code"], "idempotency_conflict")
        after = self._state()[1]
        self.assertEqual(after["stage"], "writing")
        self.assertEqual(after["writer_session"], "gwslice-" + "1" * 32)
        self.assertEqual(after["budget_left"], first["budget_left"])

    def test_every_input_that_changes_the_outcome_is_part_of_the_event_identity(self):
        # A retry that asks for nine jobs, a longer lease, a different owner,
        # or the lease dropped is a different request. Fingerprinting only
        # action and data answered all four with the original receipt, so the
        # caller was told its new terms had been applied when they had not.
        self.assertEqual(self._create()[0], 201)
        first = self._start_writer()[1]
        for field, value in (
            ("holder", "rewst-run-2"),
            ("jobs_spent", 9),
            ("lease_seconds", 900),
            ("release_lease", True),
        ):
            with self.subTest(field=field):
                changed = {"jobs_spent": 0, field: value}
                status, payload, _ = self._advance("start_writer", "evt-1", **changed)
                self.assertEqual(status, 409, payload)
                self.assertFalse(payload["replayed"])
                self.assertEqual(payload["code"], "idempotency_conflict")
                after = self._state()[1]
                self.assertEqual(after["stage"], "writing")
                self.assertEqual(after["budget_left"], first["budget_left"])
                self.assertEqual(after["lease"]["holder"], "rewst-run-1")

    # -- leases -------------------------------------------------------------

    def test_a_live_lease_keeps_a_second_caller_out(self):
        self.assertEqual(self._create()[0], 201)
        held = self._start_writer(holder="rewst-run-1")[1]
        self.assertEqual(held["lease"]["holder"], "rewst-run-1")
        status, payload, _ = self._advance("writer_done", "evt-2", holder="rewst-run-2")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "lease_held")
        self.assertEqual(self._state()[1]["stage"], "writing", "the loser must not advance the build")

    def test_the_lease_holder_keeps_going_without_reacquiring(self):
        self.assertEqual(self._create()[0], 201)
        self.assertEqual(self._start_writer(holder="rewst-run-1")[0], 200)
        status, payload, _ = self._advance("writer_done", "evt-2", holder="rewst-run-1")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["stage"], "verifying")

    def test_an_expired_lease_is_recoverable_and_keeps_the_verified_boundary(self):
        verified = self._through_verify(holder="rewst-run-1")
        self.assertEqual(verified["stage"], "review")
        boundary = verified["verified_boundary"]
        self.assertIsNotNone(boundary)
        self.assertEqual(boundary["head"], self.head)
        self._expire_lease()
        doc = self._read_doc()
        change_id = server.build_change_id(self.repo, self.head)
        doc["reviews"] = [{"change_id": change_id, "reviewer": "sonnet", "verdict": "PASS"}]
        self._write_doc(doc)
        status, payload, _ = self._gate("evt-4", holder="rewst-run-2")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["stage"], "ready")
        self.assertEqual(payload["lease"]["holder"], "rewst-run-2")
        self.assertEqual(payload["verified_boundary"], boundary, "recovery must not roll the boundary back")

    def test_releasing_the_lease_hands_the_build_to_the_next_caller(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._start_writer(holder="rewst-run-1", release_lease=True)
        self.assertEqual(status, 200, payload)
        self.assertIsNone(payload["lease"])
        status, payload, _ = self._advance("writer_done", "evt-2", holder="rewst-run-2")
        self.assertEqual(status, 200, payload)

    def test_release_lease_accepts_omission_or_booleans_and_rejects_coercion(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._start_writer()
        self.assertEqual(status, 200, payload)
        self.assertIsNotNone(payload["lease"], "omission defaults to keeping the lease")

        status, payload, _ = self._advance("writer_done", "evt-2", release_lease=False)
        self.assertEqual(status, 200, payload)
        self.assertIsNotNone(payload["lease"])

        self._record_fast_pass()
        status, payload, _ = self._gate("evt-3", release_lease=True)
        self.assertEqual(status, 200, payload)
        self.assertIsNone(payload["lease"])

        before = self._doc_path().read_bytes()
        for i, bad in enumerate(("false", "true", 0, 1, None, [], {}), start=4):
            with self.subTest(release_lease=bad):
                status, payload, _ = self._gate(f"evt-{i}", release_lease=bad)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "bad_release_lease")
                self.assertEqual(self._doc_path().read_bytes(), before, "a bad flag must not change state")

    # -- parking ------------------------------------------------------------

    def test_a_moved_head_parks_with_a_named_reason(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._advance("start_writer", "evt-1", expect={"head": "b" * 40})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "head_moved")
        self.assertEqual(payload["stage"], "parked")
        parked = self._state()[1]
        self.assertEqual(parked["stage"], "parked")
        self.assertEqual(parked["park_reason"], "head_moved")
        self.assertIsNone(parked["next_action"])

    def test_a_foreign_worktree_parks_instead_of_guessing(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._advance("start_writer", "evt-1", expect={"worktree": "/tmp/some-other-tree"})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "worktree_mismatch")
        self.assertEqual(self._state()[1]["park_reason"], "worktree_mismatch")

    def test_a_branch_moved_under_the_build_parks_against_the_live_checkout(self):
        # The stored branch is compared to what git reports, not to a copy of
        # the caller's own claim. Comparing belief to belief agrees with
        # itself while the worktree is somewhere else entirely.
        self.assertEqual(self._create()[0], 201)
        self._git("checkout", "main")
        status, payload, _ = self._advance("start_writer", "evt-1")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "branch_mismatch")
        self.assertIn("main", payload["park_detail"])
        self.assertEqual(self._state()[1]["park_reason"], "branch_mismatch")

    def test_retrying_an_automatic_park_replays_it_instead_of_parking_twice(self):
        # An automatic park is an outcome, not an error on the way to one. If
        # it is not recorded under the event id, the caller's retry re-runs the
        # guard and parks again — a second receipt and a second budget charge
        # for one transition the caller only asked for once.
        self.assertEqual(self._create()[0], 201)
        moved = self._commit("writer moved the head")
        self.assertNotEqual(moved, self.head)
        first_status, first, _ = self._advance("start_writer", "evt-1", expect={"head": self.head})
        self.assertEqual(first_status, 409, first)
        self.assertEqual(first["code"], "head_moved")
        self.assertFalse(first["replayed"])
        self.assertEqual(first["receipt"]["jobs_spent"], 0)
        self.assertTrue(first["receipt"]["automatic"])

        status, payload, _ = self._advance("start_writer", "evt-1", expect={"head": self.head})
        self.assertEqual(status, 409, payload)
        self.assertTrue(payload["replayed"])
        self.assertEqual(payload["receipt"], first["receipt"], "the same park, not a second one")
        self.assertEqual(payload["code"], "head_moved")
        after = self._state()[1]
        self.assertEqual(after["stage"], "parked")
        self.assertEqual(after["budget_left"], 12, "parking twice would have charged the budget twice")
        self.assertEqual(len([r for r in after["receipts"] if r]), 1)

    def test_an_automatic_park_replays_after_its_recent_receipt_ages_out(self):
        self.assertEqual(self._create()[0], 201)
        moved = self._commit("writer moved the head")
        self.assertNotEqual(moved, self.head)
        first_status, first, _ = self._advance("start_writer", "evt-park", expect={"head": self.head})
        self.assertEqual(first_status, 409, first)
        self.assertTrue(first["receipt"]["automatic"])
        self._age_out_event_receipt("evt-park")
        before = self._doc_path().read_bytes()

        status, payload, _ = self._advance("start_writer", "evt-park", expect={"head": self.head})
        self.assertEqual(status, first_status, payload)
        self.assertTrue(payload["replayed"])
        self.assertEqual(payload["receipt"], first["receipt"])
        self.assertEqual(payload["code"], first["code"])
        self.assertEqual(self._doc_path().read_bytes(), before, "a park replay must not rewrite state")

    # -- identity is read, never written ------------------------------------

    def test_a_head_in_the_payload_cannot_move_the_builds_identity(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._start_writer(data={"head": "b" * 40})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["head"], self.head, "identity comes from the repository, not the payload")
        self.assertEqual(self._state()[1]["head"], self.head)
        self.assertEqual(payload["receipt"]["head"], self.head)

    def test_only_start_writer_may_bind_the_writer_session(self):
        # The session handle is what resumes the one agent holding this build.
        # A later transition that repointed it would strand the real writer and
        # hand the resume handle to whatever the caller happened to send.
        session = "gwslice-" + "4" * 32
        self.assertEqual(self._create()[0], 201)
        self.assertEqual(self._start_writer(session=session)[0], 200)
        status, payload, _ = self._advance(
            "writer_done", "evt-2", data={"writer_session": "gwslice-" + "5" * 32}
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "writer_session_locked")
        after = self._state()[1]
        self.assertEqual(after["writer_session"], session)
        self.assertEqual(after["stage"], "writing", "a refused rebind advances nothing")

    def test_a_writer_session_that_is_not_a_session_id_is_refused(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._advance("start_writer", "evt-1", data={"writer_session": "the writer agent"})
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "bad_writer_session")
        self.assertEqual(self._state()[1]["stage"], "created")

    def test_overrunning_the_job_budget_parks(self):
        self.assertEqual(self._create(budget={"jobs_max": 1})[0], 201)
        self.assertEqual(self._start_writer()[0], 200)
        status, payload, _ = self._advance("writer_done", "evt-2", jobs_spent=2)
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "budget_exhausted")
        self.assertEqual(self._state()[1]["stage"], "parked")

    def test_a_state_version_we_cannot_read_parks_and_is_left_alone(self):
        self.assertEqual(self._create()[0], 201)
        doc = json.loads(self._doc_path().read_text())
        doc["version"] = server.BUILD_STATE_VERSION + 99
        doc["a_field_from_the_future"] = True
        self._doc_path().write_text(json.dumps(doc, indent=2) + "\n")
        status, payload, _ = self._state()
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "incompatible_state_version")
        self.assertEqual(payload["park_reason"], "state_version")
        self.assertEqual(self._advance("start_writer", "evt-1")[0], 409)
        after = json.loads(self._doc_path().read_text())
        self.assertEqual(after["version"], server.BUILD_STATE_VERSION + 99)
        self.assertTrue(after["a_field_from_the_future"], "a version we cannot read must not be rewritten")

    def test_a_deliberate_park_is_a_transition_like_any_other(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._advance("park", "evt-1", data={"reason": "human_review", "detail": "waiting"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["stage"], "parked")
        self.assertEqual(payload["park_reason"], "human_review")
        self.assertEqual(self._advance("start_writer", "evt-2")[1]["code"], "invalid_transition")

    # -- durability ---------------------------------------------------------

    def test_state_survives_a_service_restart(self):
        self.assertEqual(
            self._create(
                verification={
                    "fast": ["graphwing-compile"],
                    "integration": ["graphwing-unit"],
                    "needs_visual_proof": True,
                }
            )[0],
            201,
        )
        session = "gwslice-" + "3" * 32
        self.assertEqual(
            self._start_writer(
                session=session, data={"stacks": ["riftwing-52", "gw-52"]}
            )[0],
            200,
        )
        self.assertEqual(self._advance("writer_done", "evt-2", jobs_spent=0)[0], 200)
        before = self._state()[1]

        # A genuinely fresh process: no patched globals, no warm module. It
        # reads through buildState, the operation Rewst actually calls, so the
        # proof covers the public answer rather than an internal helper.
        root = Path(server.__file__).resolve().parent
        env = dict(os.environ, GRAPHWING_HOME=str(self.home), GRAPHWING_HERDR="0", PYTHONPATH=str(root))
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, sys; import server; "
                "status, payload = server.build_state_op({'build_id': [sys.argv[1]]}); "
                "print(json.dumps({'status': status, 'payload': payload}))",
                self.BUILD,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        restarted = json.loads(proc.stdout)
        self.assertEqual(restarted["status"], 200, proc.stderr)
        after = restarted["payload"]
        for field in (
            "story",
            "repo",
            "worktree",
            "branch",
            "head",
            "index",
            "ticket",
            "route",
            "verification",
            "writer_session",
            "writer_claim",
            "stacks",
            "evidence_rounds",
            "reviews",
            "budget",
            "budget_left",
            "verified_boundary",
            "receipts",
            "event_count",
            "pr",
            "lease",
            "stage",
            "next_action",
            "created_at",
        ):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after["story"], "issue-52 pre-PR build")
        self.assertEqual(after["repo"], "scratch")
        self.assertEqual(after["branch"], self.BRANCH)
        self.assertEqual(after["head"], self.head, "the head read back is the one git reported")
        self.assertEqual(after["index"], "slices/demo/index.json")
        self.assertEqual(after["ticket"], "slices/demo/01-build-state.md")
        self.assertEqual(
            after["route"], {"class": "visual", "launcher": "hermes", "model": "sol"}
        )
        self.assertTrue(after["verification"]["needs_visual_proof"])
        self.assertEqual(after["writer_session"], session)
        self.assertEqual(after["stacks"], ["riftwing-52", "gw-52"])
        self.assertEqual(after["evidence_rounds"], [])
        self.assertEqual(after["reviews"], [])
        self.assertEqual(after["budget"], {"jobs_max": 12, "jobs_used": 0})
        self.assertEqual(after["budget_left"], 12)
        self.assertEqual(after["event_count"], 2)
        self.assertEqual(
            [r["event_id"] for r in after["receipts"]],
            ["evt-1", "evt-2"],
        )
        self.assertEqual(after["stage"], "verifying")
        self.assertEqual(after["next_action"], "verify_passed")

    def test_writes_are_atomic_and_leave_no_partial_file(self):
        self.assertEqual(self._create()[0], 201)
        self.assertEqual(self._start_writer()[0], 200)
        self.assertEqual(self._advance("writer_done", "evt-2", jobs_spent=0)[0], 200)
        self._record_fast_pass()
        self.assertEqual(self._gate("evt-3")[0], 200)
        leftovers = sorted(p.name for p in self._doc_path().parent.iterdir() if p.name != "build.json")
        self.assertEqual(leftovers, [], "the temp file must be renamed, not left behind")
        self.assertLessEqual(self._doc_path().stat().st_size, server.BUILD_STATE_MAX_BYTES)
        on_disk = json.loads(self._doc_path().read_text())
        self.assertEqual(on_disk["build_id"], self.BUILD)
        self.assertEqual(on_disk["stage"], "review")

    def test_concurrent_writes_get_their_own_temp_file_and_leave_a_valid_state(self):
        # A shared temp name is two writers filling one file: whoever renames
        # second publishes a document half of which is the other one's bytes.
        other = "bld-0000003"
        errors = []
        docs = [
            {
                "version": server.BUILD_STATE_VERSION,
                "build_id": other,
                "stage": "writing",
                "story": f"writer {i}",
                "events": {},
                "event_order": [],
                "event_seen": {},
            }
            for i in range(8)
        ]

        def write(doc):
            errors.append(server.write_build(doc, other))

        threads = [threading.Thread(target=write, args=(doc,)) for doc in docs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([e for e in errors if e], [])
        files = sorted(p.name for p in self._doc_path(other).parent.iterdir())
        self.assertEqual(files, ["build.json"], "every writer must clean up after itself")
        on_disk = json.loads(self._doc_path(other).read_text())
        self.assertEqual(on_disk["build_id"], other)
        self.assertIn(on_disk["story"], [d["story"] for d in docs], "one whole document, not a blend of eight")

    def test_the_state_directory_and_file_are_private_to_the_seat(self):
        # The document carries the worktree path, the writer session handle,
        # and the review record for unshipped work. 0644 under the seat umask
        # puts all of that in front of every local account.
        self.assertEqual(self._create()[0], 201)
        self.assertEqual(self.builds.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self._doc_path().parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self._doc_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self._start_writer()[0], 200)
        self.assertEqual(self._doc_path().stat().st_mode & 0o777, 0o600, "a rewrite must not widen it")

    def test_receipts_age_out_while_every_accepted_event_id_is_remembered(self):
        total = 400
        doc = {
            "version": server.BUILD_STATE_VERSION,
            "build_id": "bld-0000002",
            "stage": "writing",
            "events": {
                f"evt-{i}": {"fingerprint": f"f{i}", "receipt": {"event_id": f"evt-{i}"}, "status": 200}
                for i in range(total)
            },
            "event_order": [f"evt-{i}" for i in range(total)],
        }
        self.assertIsNone(server.write_build(doc))
        on_disk = json.loads(self._doc_path("bld-0000002").read_text())
        self.assertEqual(len(on_disk["event_order"]), server.BUILD_RECEIPTS_KEPT)
        self.assertEqual(on_disk["event_order"][-1], f"evt-{total - 1}", "the newest receipts are the kept ones")
        self.assertEqual(sorted(on_disk["events"]), sorted(on_disk["event_order"]))
        # Losing a receipt loses a report. Losing an event id lets a retry run
        # a second time, so the two bounds are deliberately not the same one.
        self.assertEqual(len(on_disk["event_seen"]), total)
        self.assertEqual(
            on_disk["event_seen"]["evt-0"],
            {"fingerprint": "f0", "status": 200, "receipt": {"event_id": "evt-0"}},
        )
        self.assertEqual(server.build_public(on_disk)["event_count"], total)
        self.assertLessEqual(len(json.dumps(on_disk).encode()), server.BUILD_STATE_MAX_BYTES)

    def test_a_full_event_history_refuses_and_never_frees_an_id_for_reuse(self):
        self.assertEqual(self._create()[0], 201)
        self.assertEqual(self._start_writer()[0], 200)
        doc = json.loads(self._doc_path().read_text())
        doc["event_seen"] = {f"old-{i}": "some-earlier-fingerprint" for i in range(server.BUILD_EVENT_HARD_MAX)}
        self._doc_path().write_text(json.dumps(doc, indent=2) + "\n")
        before = self._doc_path().read_bytes()

        status, payload, _ = self._advance("writer_done", "evt-2")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "event_history_full")
        self.assertEqual(payload["event_count"], server.BUILD_EVENT_HARD_MAX)
        self.assertEqual(payload["event_capacity"], server.BUILD_EVENT_HARD_MAX)
        self.assertEqual(self._doc_path().read_bytes(), before, "a refusal must not rewrite the document")

        # The refusal is the point: accepting evt-2 would mean evicting an id,
        # and an evicted id is one a retry could execute a second time. Even at
        # capacity an id already spent is answered, never re-run.
        status, payload, _ = self._advance("writer_done", "old-0")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "idempotency_conflict")
        self.assertEqual(self._doc_path().read_bytes(), before)
        self.assertEqual(self._state()[1]["stage"], "writing")

    def test_a_build_document_this_service_cannot_read_is_reported_not_replaced(self):
        # Collapsing unreadable into missing is how a truncated build.json
        # becomes a brand new build, and the real one — its lease, its budget,
        # its record of which event ids have already run — is gone.
        self.assertEqual(self._create()[0], 201)
        for broken in ('{"truncated', "[]", json.dumps({"build_id": "bld-0000009", "version": 1})):
            with self.subTest(broken=broken[:24]):
                self._doc_path().write_text(broken)
                for status, payload in (
                    self._create()[:2],
                    self._advance("start_writer", "evt-1")[:2],
                    self._state()[:2],
                ):
                    self.assertEqual(status, 409, payload)
                    self.assertEqual(payload["code"], "state_unreadable")
                    self.assertEqual(payload["park_reason"], "state_unreadable")
                self.assertEqual(self._doc_path().read_text(), broken, "left exactly as it was found")

    def test_a_write_cannot_be_redirected_onto_a_different_build(self):
        doc = {
            "version": server.BUILD_STATE_VERSION,
            "build_id": "bld-0000002",
            "stage": "created",
            "events": {},
            "event_order": [],
            "event_seen": {},
        }
        err = server.write_build(doc, self.BUILD)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "build_id_mismatch")
        self.assertEqual(err["status"], 500)
        self.assertFalse(self._doc_path().exists(), "the named build must not be clobbered")
        self.assertFalse(self._doc_path("bld-0000002").exists(), "and neither must the one in the document")
        escape = server.write_build({"build_id": "../../etc/passwd", "stage": "created"})
        self.assertEqual(escape["code"], "bad_build_id")
        self.assertFalse(self.builds.exists())

    def test_secret_shaped_values_never_reach_the_state_file(self):
        self._through_verify()
        token = "ghp_" + "A" * 32
        status, payload, _ = self._advance(
            "review_passed",
            "evt-4",
            data={
                "review": {
                    "author": "sol",
                    "verdict": "clear",
                    "token": token,
                    "note": f"ran gh with {token}",
                    "hook_secret": "s3cret",
                }
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertNotIn("ghp_", self._doc_path().read_text())

    def test_a_resume_url_is_a_credential_even_though_its_name_looks_harmless(self):
        # kick_url and resume_url are what Rewst hands a build to continue a
        # run. Possessing either one is possessing the right to resume it, and
        # neither name contains a secret-shaped word, so a segment match alone
        # writes both of them to disk in the clear.
        resume = "https://rewst.test/webhooks/resume/9f2c"
        self.assertEqual(
            self._create(
                route={"class": "visual", "launcher": "hermes", "model": "sol", "kick_url": resume}
            )[0],
            201,
        )
        route = self._state()[1]["route"]
        self.assertEqual(route["kick_url"], "[redacted]")
        self.assertEqual(route["launcher"], "hermes", "the routing the build needs must survive")
        self.assertEqual(self._start_writer()[0], 200)
        on_disk = self._doc_path().read_text()
        for leaked in ("rewst.test/webhooks/resume",):
            self.assertNotIn(leaked, on_disk, leaked)

    def test_rewst_capability_urls_redact_by_value_under_generic_keys(self):
        callback = "https://app.rewst.ai/api/callbacks/11111111-1111-1111-1111-111111111111"
        webhook = "https://hooks.rewst.io/webhooks/custom/22222222-2222-2222-2222-222222222222"
        public = "https://docs.rewst.io/guides/webhooks/setup"
        status, payload, _ = self._create(
            route={"launcher": "hermes", "next": callback, "hop": webhook, "docs": public}
        )
        self.assertEqual(status, 201, payload)
        route = payload["route"]
        self.assertEqual(route["next"], "[redacted]")
        self.assertEqual(route["hop"], "[redacted]")
        self.assertEqual(route["docs"], public)
        on_disk = self._doc_path().read_text()
        self.assertNotIn(callback, on_disk)
        self.assertNotIn(webhook, on_disk)
        self.assertIn(public, on_disk)

    def test_an_identity_field_carrying_a_credential_is_refused_not_redacted(self):
        # Two callers whose holder both redacted to [redacted] would compare
        # equal, and the second one would inherit the first one's lease.
        token = "ghp_" + "B" * 32
        status, payload, _ = self._create(story=f"build for {token}")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "secret_in_story")
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._advance("start_writer", "evt-1", holder=token)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "secret_in_holder")
        self.assertEqual(self._state()[1]["stage"], "created")

    def test_the_environment_lease_default_is_clamped_the_way_a_request_is(self):
        # A request asking for 0 or 100000 seconds is a 400. An environment
        # saying the same thing must not become a lease that never expires
        # (locking every future caller out) or one that expires instantly
        # (letting two Rewst runs drive the same build).
        for raw, expected in (
            ("-5", 1),
            ("0", 1),
            ("1", 1),
            ("300", 300),
            ("  600 ", 600),
            (str(server.BUILD_LEASE_MAX_SECONDS + 1), server.BUILD_LEASE_MAX_SECONDS),
            ("100000", server.BUILD_LEASE_MAX_SECONDS),
            ("five minutes", 300),
            ("", 300),
            (None, 300),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(server.build_default_lease_seconds(raw), expected)
        self.assertGreaterEqual(server.BUILD_LEASE_SECONDS, 1)
        self.assertLessEqual(server.BUILD_LEASE_SECONDS, server.BUILD_LEASE_MAX_SECONDS)

    def test_a_requested_lease_outside_the_range_is_a_request_error(self):
        self.assertEqual(self._create()[0], 201)
        for bad in (0, -1, server.BUILD_LEASE_MAX_SECONDS + 1, "soon"):
            with self.subTest(lease_seconds=bad):
                status, payload, _ = self._advance("start_writer", "evt-1", lease_seconds=bad)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "bad_lease_seconds")
        self.assertEqual(self._state()[1]["stage"], "created")

    def test_runtime_state_lives_under_graphwing_home_and_not_in_git(self):
        # The build document is service state, not a product artifact. It goes
        # under $GRAPHWING_HOME and it is ignored in the checkout, so a build
        # in flight can never end up inside the diff it is building.
        root = Path(server.__file__).resolve().parent
        self.assertEqual(str(self.builds), str(self.home / "builds"))
        ignored = [line.strip() for line in (root / ".gitignore").read_text().splitlines()]
        self.assertIn("builds/", ignored)
        self.assertIn("jobs/", ignored)

    # -- planner metadata ---------------------------------------------------

    def test_slice_index_keeps_routing_and_verification_metadata(self):
        # sliceComplete rewrites the index. Rebuilding each ticket from the
        # five normalized fields dropped class/size/ac_count/seams, and the
        # pre-PR build then had nothing left to route on.
        rel = "slices/demo/index.json"
        dest = self.repo / "slices" / "demo"
        dest.mkdir(parents=True)
        (dest / "index.json").write_text(
            json.dumps(
                {
                    "test": "graphwing-unit",
                    "verification": {"fast": ["graphwing-compile"], "needs_visual_proof": False},
                    "tickets": [
                        {
                            "id": "01-build-state",
                            "path": "slices/demo/01-build-state.md",
                            "blocked_by": [],
                            "kind": "build",
                            "status": "open",
                            "class": "sensitive",
                            "size": "L",
                            "ac_count": 8,
                            "seams": 1,
                            "build_id": self.BUILD,
                        },
                        {
                            "id": "02-mechanical-path",
                            "path": "slices/demo/02-mechanical-path.md",
                            "blocked_by": ["01-build-state"],
                            "kind": "build",
                            "status": "open",
                            "class": "mechanical",
                            "size": "M",
                            "ac_count": 8,
                            "seams": 1,
                        },
                    ],
                },
                indent=2,
            )
            + "\n"
        )
        status, payload, _ = server.dispatch(
            "POST",
            "/v1/slice/complete",
            {},
            True,
            json.dumps({"repo": "scratch", "index": rel, "id": "01-build-state"}).encode(),
        )
        self.assertEqual(status, 200, payload)
        idx = json.loads((self.repo / rel).read_text())
        first, second = idx["tickets"]
        self.assertEqual(first["status"], "done")
        self.assertEqual(first["class"], "sensitive")
        self.assertEqual(first["size"], "L")
        self.assertEqual(first["ac_count"], 8)
        self.assertEqual(first["seams"], 1)
        self.assertEqual(first["build_id"], self.BUILD)
        self.assertEqual(second["class"], "mechanical")
        self.assertEqual(idx["verification"]["fast"], ["graphwing-compile"])
        self.assertEqual(idx["test"], "graphwing-unit")

    # -- mechanical ticket 02 ----------------------------------------------

    def test_build_next_derives_the_step_and_replay_never_repeats_completed_work(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        status, first, _ = self._next(stage=["finalize"])
        self.assertEqual(status, 200, first)
        self.assertEqual(first["step"], "preflight", "the caller cannot assert a stage")
        self.assertEqual(self._next(stage=["write"])[1]["step"], "preflight")

        launch = self._launch_writer()
        self.assertEqual(self._next()[1]["step"], "write")
        receipt = self._finish_job(launch)
        completed = self._complete("mechanical-done", launch, receipt)
        self.assertEqual(completed[0], 200, completed[1])
        self.assertEqual(self._next()[1]["step"], "fast_checks")

        doc = self._read_doc()
        change_id = server.build_change_id(self.repo, self.head)
        doc["checks"] = {
            "fast": {
                "change_id": change_id,
                "required": ["fast"],
                "verdicts": {"fast": {"ok": True, "coverage": True}},
                "ok": True,
            }
        }
        self._write_doc(doc)
        self.assertEqual(self._next()[1]["step"], "verify_passed")
        self.assertEqual(self._next()[1]["step"], "verify_passed", "a replay returns the durable next step")

    def test_preflight_fails_closed_for_every_unsafe_launch_condition(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        clean = self._read_doc()
        repos = {"scratch": str(self.repo)}
        catalog = self._recipe_catalog()

        cases = []
        unknown = json.loads(json.dumps(clean))
        unknown["verification"]["fast"] = ["does-not-exist"]
        cases.append(("unknown_recipe", unknown, catalog, {"ok": True, "entries": []}))
        wrong = json.loads(json.dumps(clean))
        wrong_catalog = self._recipe_catalog()
        wrong_catalog["fast"]["unresolvable"] = "pinned to deployed copy"
        cases.append(("recipe_wrong_worktree", wrong, wrong_catalog, {"ok": True, "entries": []}))
        hook = json.loads(json.dumps(clean))
        hook["verification"]["fast"] = ["riftwing-local-gates"]
        cases.append(("hook_not_coverage", hook, catalog, {"ok": True, "entries": []}))
        route = json.loads(json.dumps(clean))
        route["route"].pop("max_turns")
        cases.append(("missing_route_stamp", route, catalog, {"ok": True, "entries": []}))
        trunk = json.loads(json.dumps(clean))
        trunk["branch"] = "main"
        cases.append(("trunk_branch", trunk, catalog, {"ok": True, "entries": []}))
        cases.append(("worktree_conflicted", clean, catalog, {"ok": True, "entries": ["UU server.py"]}))
        cases.append(("worktree_dirty", clean, catalog, {"ok": True, "entries": [" M server.py"]}))

        for expected, doc, recipes, git_state in cases:
            with self.subTest(expected=expected):
                self._write_doc(doc)
                with mock.patch.object(server, "build_recipe_catalog", return_value=recipes), mock.patch.object(
                    server, "git_status", return_value=git_state
                ):
                    status, payload = server.build_preflight(
                        json.dumps({"build_id": self.BUILD}).encode(), repos
                    )
                self.assertEqual(status, 409, payload)
                self.assertIn(expected, [failure["code"] for failure in payload["failures"]])

    def test_declared_and_changed_path_recipes_are_an_add_only_union(self):
        doc = {
            "verification": {
                "fast": ["fast"],
                "path_rules": [
                    {"match": "server.py", "recipes": ["path-check"]},
                    {"match": "*.py", "recipes": ["fast", "path-check"]},
                ],
            }
        }
        required, why = server.build_required_recipes(doc, "fast", ["server.py"])
        self.assertEqual(required, ["fast", "path-check"])
        self.assertIn("declared", why["fast"])
        self.assertEqual(why["path-check"], ["path:server.py", "path:*.py"])

    def test_graphwing_unit_is_rebased_to_the_target_worktree_not_the_deployed_copy(self):
        deployed = {
            "name": "graphwing-unit",
            "argv": ["python3", "test_server.py"],
            "cwd": server.HOME.resolve(),
            "timeout_seconds": 30,
            "coverage": True,
        }
        with mock.patch.object(server, "load_tests", return_value={"graphwing-unit": deployed}), mock.patch.object(
            server, "load_scripts", return_value={}
        ), mock.patch.object(server, "load_rr", return_value={}):
            catalog = server.build_recipe_catalog({"scratch": str(self.repo)}, self.repo)
        self.assertEqual(catalog["graphwing-unit"]["cwd"], self.repo)
        self.assertIsNone(catalog["graphwing-unit"]["unresolvable"])

        seen = []
        with mock.patch.object(
            server,
            "run_cmd",
            side_effect=lambda argv, cwd, timeout: seen.append(Path(cwd))
            or {"ok": True, "returncode": 0, "stdout": "ok", "stderr": ""},
        ):
            verdict = server.build_run_recipe("graphwing-unit", catalog["graphwing-unit"])
        self.assertTrue(verdict["ok"])
        self.assertEqual(seen, [self.repo])

    def test_checks_record_completed_current_change_verdicts_and_never_treat_queue_as_green(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        doc = self._read_doc()
        with mock.patch.object(server, "build_recipe_catalog", return_value=self._recipe_catalog()), mock.patch.object(
            server,
            "build_run_recipe",
            return_value={"name": "fast", "kind": "test", "coverage": True, "cwd": str(self.repo),
                          "ok": True, "returncode": 0, "timed_out": False, "compact": "green"},
        ):
            status, payload = server.build_checks_result(
                doc, "fast", None, None, {"scratch": str(self.repo)}
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["head"], self.head)
        self.assertEqual(payload["change_id"], server.build_change_id(self.repo, self.head))
        self.assertEqual(payload["ran"], ["fast"])
        self.assertEqual(payload["verdicts"][0]["returncode"], 0)

        with mock.patch.object(server.threading.Thread, "start", return_value=None), mock.patch.object(
            server, "JOBS_DIR", self.home / "jobs"
        ):
            queued = self._post_build(
                "checks",
                phase="fast",
                response_webhook_url="https://example.test/resume",
                response_webhook_token="token",
            )
        self.assertEqual(queued[0], 202, queued[1])
        self.assertFalse(queued[1]["ok"])
        self.assertFalse(queued[1]["gate_passed"])
        self.assertEqual(queued[1]["code"], "queued")

    def test_integration_runs_the_exact_staged_candidate_in_a_disposable_runner(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        (self.repo / "ticket.md").unlink()
        (self.repo / "candidate-only.txt").write_text("reviewed candidate\n")
        self._git("add", "candidate-only.txt")
        doc = self._read_doc()
        doc["stage"] = "ready"
        self._write_doc(doc)

        def catalog(_repos, target, _label):
            return self._recipe_catalog(
                integration={
                    "name": "integration",
                    "argv": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; assert Path('candidate-only.txt').read_text() == 'reviewed candidate\\n'",
                    ],
                    "cwd": target,
                    "timeout_seconds": 10,
                    "kind": "test",
                    "coverage": True,
                    "unresolvable": None,
                }
            )

        with mock.patch.object(server, "build_recipe_catalog", side_effect=catalog):
            status, payload = server.build_checks_result(
                doc, "integration", None, "clean-stack", {"scratch": str(self.repo)}
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)
        candidate_tree = self._git("write-tree")
        self.assertEqual(payload["candidate_tree"], candidate_tree)
        self.assertEqual(payload["runner_tree"], candidate_tree)
        self.assertNotEqual(Path(payload["runner"]), self.repo)
        self.assertFalse(Path(payload["runner"]).exists(), "disposable runner is cleaned after the receipt")
        recorded = self._read_doc()["checks"]["integration"]
        self.assertEqual(recorded["candidate_tree"], candidate_tree)
        self.assertEqual(recorded["runner_tree"], candidate_tree)
        self.assertEqual(recorded["base_head"], self.head)
        self.assertEqual(recorded["materialization"], payload["materialization"])

        (self.repo / "candidate-only.txt").write_text("changed after integration\n")
        self._git("add", "candidate-only.txt")
        clean, reason = server.build_checks_clean(
            self._read_doc(), "integration", recorded["change_id"]
        )
        self.assertFalse(clean)
        self.assertIn("does not match staged tree", reason)

    def test_integration_rejects_unstaged_untracked_and_baseline_runner_trees(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        (self.repo / "ticket.md").unlink()
        (self.repo / "candidate.txt").write_text("candidate\n")
        self._git("add", "candidate.txt")
        doc = self._read_doc()

        (self.repo / "README").write_text("unstaged\n")
        unstaged = server.build_checks_result(
            doc, "integration", None, "clean-stack", {"scratch": str(self.repo)}
        )
        self.assertEqual(unstaged[0], 409, unstaged[1])
        self.assertEqual(unstaged[1]["code"], "candidate_not_exact")
        self._git("restore", "README")

        (self.repo / "untracked.txt").write_text("not reviewed\n")
        untracked = server.build_checks_result(
            doc, "integration", None, "clean-stack", {"scratch": str(self.repo)}
        )
        self.assertEqual(untracked[0], 409, untracked[1])
        self.assertEqual(untracked[1]["code"], "candidate_not_exact")
        (self.repo / "untracked.txt").unlink()

        original_verify = server.build_runner_tree
        with mock.patch.object(server, "build_runner_tree", return_value=self.head):
            baseline = server.build_checks_result(
                doc, "integration", None, "clean-stack", {"scratch": str(self.repo)}
            )
        self.assertEqual(baseline[0], 409, baseline[1])
        self.assertEqual(baseline[1]["code"], "runner_tree_mismatch")
        self.assertNotEqual(original_verify(self.repo), self.head)

    def test_candidate_runners_are_isolated_and_cleanup_is_idempotent(self):
        (self.repo / "candidate.txt").write_text("candidate\n")
        self._git("add", "candidate.txt")
        candidate, gap = server.build_materialize_candidate(self.repo, self.head, "change")
        self.assertIsNone(gap)
        self.assertIsNotNone(candidate)
        runners = []
        failures = []
        barrier = threading.Barrier(2)

        def provision(attempt):
            barrier.wait()
            runner, error = server.build_provision_candidate_runner(
                self.repo, self.clean_runner, candidate, attempt
            )
            if error:
                failures.append(error)
            else:
                runners.append(runner)

        threads = [threading.Thread(target=provision, args=(f"attempt-{n}",)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(len({str(path) for path in runners}), 2)
        for runner in runners:
            self.assertEqual((runner / "candidate.txt").read_text(), "candidate\n")
            server.build_cleanup_candidate_runner(self.repo, runner)
            server.build_cleanup_candidate_runner(self.repo, runner)
            self.assertFalse(runner.exists())

    def test_check_gates_reject_stale_missing_red_and_non_clean_integration_results(self):
        change = "a" * 40 + ":current"
        base = {
            "checks": {
                "fast": {
                    "change_id": change,
                    "required": ["fast"],
                    "verdicts": {"fast": {"ok": True, "coverage": True}},
                }
            }
        }
        self.assertFalse(server.build_checks_clean(base, "fast", "b" * 40 + ":new")[0])
        missing = json.loads(json.dumps(base))
        missing["checks"]["fast"]["verdicts"] = {}
        self.assertIn("no verdict", server.build_checks_clean(missing, "fast", change)[1])
        red = json.loads(json.dumps(base))
        red["checks"]["fast"]["verdicts"]["fast"]["ok"] = False
        self.assertIn("failed", server.build_checks_clean(red, "fast", change)[1])
        integration = {
            "stacks": ["writer-stack"],
            "checks": {
                "integration": {
                    "change_id": change,
                    "stack": "preview-clean",
                    "required": ["integration"],
                    "verdicts": {"integration": {"ok": True, "coverage": True}},
                }
            },
        }
        self.assertIn("not in stacks.json", server.build_checks_clean(integration, "integration", change)[1])

    def test_red_commands_are_completed_verdicts_but_cannot_pass_the_gate(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        with mock.patch.object(server, "build_recipe_catalog", return_value=self._recipe_catalog()), mock.patch.object(
            server,
            "build_run_recipe",
            return_value={"name": "fast", "kind": "test", "coverage": True, "cwd": str(self.repo),
                          "ok": False, "returncode": 7, "timed_out": False, "compact": "red"},
        ):
            status, payload = server.build_checks_result(
                self._read_doc(), "fast", None, None, {"scratch": str(self.repo)}
            )
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "check_failed")
        self.assertEqual(payload["verdicts"][0]["returncode"], 7)

    def test_review_is_cross_vendor_current_and_silence_is_not_a_finding(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        doc = self._read_doc()
        doc["stage"] = "review"
        self._write_doc(doc)
        refused = self._post_build("review", reviewer="grok")
        self.assertEqual(refused[0], 409, refused[1])
        self.assertEqual(refused[1]["code"], "reviewer_is_writer")

        with mock.patch.object(
            server,
            "build_review_round_result",
            return_value={"verdict": "PASS", "summary": "clear", "compact": "clear", "no_verdict": False},
        ):
            passed = self._post_build("review", reviewer="sonnet")
        self.assertEqual(passed[0], 200, passed[1])
        self.assertTrue(passed[1]["ok"])
        self.assertEqual(passed[1]["head"], self.head)
        old_change = passed[1]["change_id"]
        (self.repo / "README").write_text("diff changed\n")
        self.assertIsNone(server.build_last_review(self._read_doc(), server.build_change_id(self.repo, self.head)))
        self.assertNotEqual(old_change, server.build_change_id(self.repo, self.head))

        before = len(self._read_doc()["reviews"])
        with mock.patch.object(
            server,
            "build_review_round_result",
            return_value={"verdict": None, "no_verdict": True, "code": "timeout"},
        ):
            silent = self._post_build("review", reviewer="sonnet")
        self.assertEqual(silent[0], 200, silent[1])
        self.assertTrue(silent[1]["no_verdict"])
        self.assertFalse(silent[1]["finding"])
        self.assertEqual(len(self._read_doc()["reviews"]), before)

    def test_review_findings_are_recorded_against_the_current_change(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        doc = self._read_doc()
        doc["stage"] = "review"
        self._write_doc(doc)
        with mock.patch.object(
            server,
            "build_review_round_result",
            return_value={"verdict": "NACK", "summary": "missing seam", "compact": "fix seam", "no_verdict": False},
        ):
            result = self._post_build("review", reviewer="sonnet")
        self.assertEqual(result[0], 200, result[1])
        self.assertFalse(result[1]["ok"])
        self.assertTrue(result[1]["finding"])
        self.assertEqual(self._read_doc()["reviews"][-1]["change_id"], result[1]["change_id"])

    def test_correction_brief_requires_and_resumes_the_writer_session_and_charges_budget(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        no_session = self._post_build("brief", finding="fix the seam")
        self.assertEqual(no_session[0], 409, no_session[1])
        self.assertEqual(no_session[1]["code"], "claim_required")
        doc = self._read_doc()
        doc["writer_session"] = "gwslice-" + "b" * 32
        doc["stage"] = "review"
        change_id = server.build_change_id(self.repo, self.head)
        doc["checks"] = {
            "fast": {
                "change_id": change_id,
                "required": ["fast"],
                "verdicts": {"fast": {"ok": True, "coverage": True}},
            }
        }
        doc["reviews"] = [
            {"change_id": change_id, "reviewer": "sonnet", "verdict": "NACK", "summary": "fix the seam"}
        ]
        before = doc["budget"]["jobs_used"]
        self._write_doc(doc)
        claim = self._claim("claim-correction", kind="correction")
        self.assertEqual(claim[0], 200, claim[1])
        brief = self._post_build("brief", finding="fix the seam")
        self.assertEqual(brief[0], 200, brief[1])
        self.assertTrue(brief[1]["resume"])
        self.assertEqual(brief[1]["hermes_session"], doc["writer_session"])
        self.assertEqual(self._read_doc()["budget"]["jobs_used"], before + 1)

    def test_finalize_requires_current_clean_gates_and_commits_pushes_once(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        (self.repo / "ticket.md").unlink()
        (self.repo / "change.txt").write_text("ready\n")
        self._git("add", "change.txt")
        change_id = server.build_change_id(self.repo, self.head)
        doc = self._read_doc()
        doc["stage"] = "ready"
        doc["verified_boundary"] = {"head": self.head}
        doc["checks"] = {
            phase: {
                "change_id": change_id,
                "stack": "clean-stack" if phase == "integration" else None,
                "runner": str(self.clean_runner) if phase == "integration" else str(self.repo),
                "required": [phase],
                "verdicts": {phase: {"ok": True, "coverage": True}},
            }
            for phase in ("fast", "integration")
        }
        tree = self._git("write-tree")
        doc["checks"]["integration"].update(
            head=self.head,
            runner=str(self.clean_runner / "recorded-attempt"),
            candidate_tree=tree,
            runner_tree=tree,
            base_head=self.head,
            materialization="a" * 40,
        )
        doc["reviews"] = [
            {"change_id": change_id, "reviewer": "sonnet", "verdict": "PASS", "summary": "clear"}
        ]
        self._write_doc(doc)

        original_run_git = server.run_git
        pushes = []

        def git_with_local_push(path, args, **kwargs):
            if args and args[0] == "push":
                pushes.append(list(args))
                return {"ok": True, "returncode": 0, "stdout": "pushed", "stderr": ""}
            return original_run_git(path, args, **kwargs)

        request = {"event_id": "final-once", "holder": "rewst-run", "message": "mechanical change"}
        with mock.patch.object(server, "run_git", side_effect=git_with_local_push):
            first = self._post_build("finalize", **request)
            replay = self._post_build("finalize", **request)
            fresh_replay = self._post_build(
                "finalize", event_id="final-fresh", holder="rewst-run", message="mechanical change"
            )
        self.assertEqual(first[0], 200, first[1])
        self.assertTrue(first[1]["receipt"]["committed"])
        self.assertTrue(first[1]["receipt"]["pushed"])
        self.assertEqual(len(pushes), 1)
        self.assertTrue(replay[1]["replayed"])
        self.assertEqual(replay[1]["receipt"], first[1]["receipt"])
        self.assertEqual(fresh_replay[1]["code"], "already_finalized")
        self.assertEqual(self._git("rev-list", "--count", self.head + "..HEAD"), "1")

    def test_finalize_refuses_missing_stale_dirty_and_wrong_head_evidence(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        (self.repo / "ticket.md").unlink()
        doc = self._read_doc()
        doc["stage"] = "ready"
        self._write_doc(doc)
        missing = self._post_build("finalize", event_id="fin-missing", holder="r", message="x")
        self.assertEqual(missing[0], 409, missing[1])
        self.assertEqual(missing[1]["code"], "not_ready")

        current = server.build_change_id(self.repo, self.head)
        doc = self._read_doc()
        doc["checks"] = {
            phase: {
                "change_id": "stale",
                "stack": "clean-stack" if phase == "integration" else None,
                "runner": str(self.clean_runner) if phase == "integration" else str(self.repo),
                "required": [phase],
                "verdicts": {phase: {"ok": True, "coverage": True}},
            }
            for phase in ("fast", "integration")
        }
        doc["reviews"] = [{"change_id": "stale", "reviewer": "sonnet", "verdict": "PASS"}]
        self._write_doc(doc)
        stale = self._post_build("finalize", event_id="fin-stale", holder="r", message="x")
        self.assertEqual(stale[1]["code"], "not_ready")

        doc = self._read_doc()
        for entry in doc["checks"].values():
            entry["change_id"] = current
        doc["reviews"] = [{"change_id": current, "reviewer": "sonnet", "verdict": "PASS"}]
        doc["verified_boundary"] = {"head": self.head}
        self._write_doc(doc)
        (self.repo / "README").write_text("unstaged conflict-like dirt\n")
        dirty = self._post_build("finalize", event_id="fin-dirty", holder="r", message="x")
        self.assertEqual(dirty[1]["code"], "worktree_not_equal")

        self._git("restore", "README")
        doc = self._read_doc()
        doc["verified_boundary"] = {"head": "f" * 40}
        self._write_doc(doc)
        wrong = self._post_build("finalize", event_id="fin-head", holder="r", message="x")
        self.assertEqual(wrong[0], 409, wrong[1])
        self.assertEqual(wrong[1]["park_reason"], "head_moved_after_verify")

    def test_initial_writer_launch_replay_reserves_one_job_and_session(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        claim = self._claim()[1]["claim"]
        self.assertRegex(claim["writer_session"], r"^gwslice-[0-9a-f]{32}$")
        with mock.patch.object(server, "enqueue_agent") as enqueue:
            first = self._launch()
            replay = self._launch()
        self.assertEqual(first[0], 202, first[1])
        self.assertEqual(replay[0], 202, replay[1])
        self.assertEqual(replay[1]["job_id"], first[1]["job_id"])
        self.assertEqual(replay[1]["hermes_session"], first[1]["hermes_session"])
        self.assertEqual(first[1]["hermes_session"], claim["writer_session"])
        self.assertEqual(enqueue.call_count, 2)
        self.assertEqual(len(list((self.home / "jobs").glob("*/job.json"))), 1)

    def test_writer_launch_recovers_one_job_after_crash_following_launch_intent(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        self.assertEqual(self._claim()[0], 200)
        original_write_job = server.write_job
        with mock.patch.object(server, "write_job", side_effect=OSError("crash")), self.assertRaises(OSError):
            self._launch()
        intent = self._read_doc()["writer_claim"]
        self.assertEqual(intent["launch_state"], "intent")
        self.assertFalse((self.home / "jobs" / intent["job_id"] / "job.json").exists())
        with mock.patch.object(server, "write_job", side_effect=original_write_job), mock.patch.object(
            server, "enqueue_agent"
        ) as enqueue:
            recovered = self._launch()
            replay = self._launch()
        self.assertEqual(recovered[0], 202, recovered[1])
        self.assertEqual(replay[1]["job_id"], recovered[1]["job_id"])
        self.assertEqual(enqueue.call_count, 2)

    def test_writer_launch_replay_dispatches_the_queued_job_after_enqueue_crash(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        self.assertEqual(self._claim()[0], 200)
        with mock.patch.object(server, "enqueue_agent", side_effect=RuntimeError("crash after write")):
            with self.assertRaisesRegex(RuntimeError, "crash after write"):
                self._launch()
        job_id = self._read_doc()["writer_claim"]["job_id"]
        self.assertEqual(server.read_job(job_id)["status"], "queued")

        with mock.patch.object(server, "enqueue_agent") as enqueue:
            replay = self._launch()
        self.assertEqual(replay[0], 202, replay[1])
        self.assertTrue(replay[1]["replayed"])
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[0]["job_id"], job_id)

    def test_concurrent_queued_writer_dispatches_execute_only_one_process(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        self.assertEqual(self._claim()[0], 200)
        with mock.patch.object(server, "enqueue_agent"):
            launch = self._launch()[1]

        entered = threading.Event()
        release = threading.Event()
        spawned = []

        class Proc:
            pid = 12345
            returncode = 0

            def wait(self, timeout=None):
                return 0

        def blocked_spawn(job):
            spawned.append(job["job_id"])
            entered.set()
            release.wait(2)
            return Proc(), None

        with mock.patch.object(server, "spawn_writer", side_effect=blocked_spawn), mock.patch.object(
            server, "deliver_webhook", return_value=None
        ), mock.patch.object(server, "herdr_job_done"):
            first = threading.Thread(target=server.run_agent_job, args=(launch["job_id"],))
            second = threading.Thread(target=server.run_agent_job, args=(launch["job_id"],))
            first.start()
            self.assertTrue(entered.wait(2))
            second.start()
            second.join(2)
            release.set()
            first.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(spawned, [launch["job_id"]])

    def test_writer_completion_is_authoritative_idempotent_and_correction_returns_to_checks(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        self.assertEqual(self._claim()[0], 200)
        with mock.patch.object(server, "enqueue_agent"):
            launch = self._launch()[1]
        receipt = self._finish_job(launch)
        first = self._complete("writer-complete", launch, receipt)
        replay = self._complete("writer-complete", launch, receipt)
        conflict = self._complete("writer-complete", launch, {**receipt, "summary": "different"})
        self.assertEqual(first[0], 200, first[1])
        self.assertEqual(first[1]["stage"], "verifying")
        self.assertEqual(first[1]["writer_session"], launch["hermes_session"])
        self.assertIsNone(first[1]["lease"])
        self.assertTrue(self._read_doc()["writer_claim"]["consumed"])
        self.assertTrue(replay[1]["replayed"])
        self.assertEqual(conflict[0], 409, conflict[1])
        self.assertEqual(conflict[1]["code"], "idempotency_conflict")

        doc = self._read_doc()
        doc["stage"] = "review"
        change_id = server.build_change_id(self.repo, self.head)
        doc["checks"] = {
            "fast": {"change_id": change_id, "required": ["fast"],
                     "verdicts": {"fast": {"ok": True, "coverage": True}}}
        }
        doc["reviews"] = [{"change_id": change_id, "reviewer": "sonnet", "verdict": "NACK"}]
        self._write_doc(doc)
        self.assertEqual(self._claim("claim-correction", kind="correction")[0], 200)
        with mock.patch.object(server, "enqueue_agent"):
            correction = self._launch("claim-correction")[1]
        (self.repo / "README").write_text("corrected\n")
        self._git("add", "README")
        corrected_receipt = self._finish_job(correction)
        completed = self._complete(
            "correction-complete", correction, corrected_receipt, claim_id="claim-correction"
        )
        self.assertEqual(completed[0], 200, completed[1])
        self.assertEqual(completed[1]["stage"], "review", "correction completion asserts no gate")
        self.assertIsNone(completed[1]["lease"])
        self.assertEqual(self._next()[1]["step"], "fast_checks")

    def test_failed_writer_completion_preserves_files_and_records_recoverable_correction(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        self.assertEqual(self._claim()[0], 200)
        with mock.patch.object(server, "enqueue_agent"):
            launch = self._launch()[1]
        (self.repo / "partial.txt").write_text("keep me\n")
        receipt = self._finish_job(launch, status="error", summary="writer failed")
        completed = self._complete("writer-red", launch, receipt)
        self.assertEqual(completed[0], 200, completed[1])
        self.assertEqual(completed[1]["stage"], "writing")
        self.assertEqual(completed[1]["writer_result"]["status"], "error")
        self.assertTrue((self.repo / "partial.txt").is_file())
        self.assertIsNone(completed[1]["lease"])
        self.assertEqual(self._next()[1]["step"], "fix")

    def test_correction_claim_is_stage_gated_locked_idempotent_and_single_spend(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        premature = self._claim("correction-early", kind="correction")
        self.assertEqual(premature[0], 409, premature[1])
        self.assertEqual(premature[1]["code"], "claim_not_due")
        doc = self._read_doc()
        doc["stage"] = "review"
        doc["writer_session"] = "gwslice-" + "d" * 32
        change_id = server.build_change_id(self.repo, self.head)
        doc["checks"] = {
            "fast": {
                "change_id": change_id,
                "required": ["fast"],
                "verdicts": {"fast": {"ok": True, "coverage": True}},
            }
        }
        doc["reviews"] = [{"change_id": change_id, "reviewer": "sonnet", "verdict": "NACK"}]
        self._write_doc(doc)
        first = self._claim("correction-one", kind="correction")
        replay = self._claim("correction-one", kind="correction")
        blocked = self._claim("correction-two", kind="correction")
        self.assertEqual(first[0], 200, first[1])
        self.assertEqual(replay[0], 200, replay[1])
        self.assertTrue(replay[1]["replayed"])
        self.assertEqual(blocked[0], 409, blocked[1])
        self.assertEqual(blocked[1]["code"], "writer_claimed")
        self.assertEqual(first[1]["claim"]["writer_session"], doc["writer_session"])
        self.assertEqual(self._read_doc()["budget"]["jobs_used"], 1)

    def test_active_writer_job_blocks_a_second_claim_after_lease_wall_time(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        self.assertEqual(self._claim()[0], 200)
        with mock.patch.object(server, "enqueue_agent"):
            launch = self._launch()[1]
        doc = self._read_doc()
        doc["lease"]["expires_epoch"] = time.time() - 1
        doc["writer_claim"]["expires_epoch"] = time.time() - 1
        self._write_doc(doc)
        blocked = self._claim("claim-second", holder="rewst-run-2")
        self.assertEqual(blocked[0], 409, blocked[1])
        self.assertEqual(blocked[1]["code"], "writer_claimed")
        self.assertEqual(blocked[1]["claim"]["job_id"], launch["job_id"])

    def test_mechanical_generic_writer_assertions_are_always_refused(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        refused = self._advance("start_writer", "generic-start")
        self.assertEqual(refused[0], 409, refused[1])
        self.assertEqual(refused[1]["code"], "writer_op_required")

        self.assertEqual(self._claim()[0], 200)
        with mock.patch.object(server, "enqueue_agent"):
            launch = self._launch()[1]
        doc = self._read_doc()
        doc["lease"]["expires_epoch"] = time.time() - 1
        doc["writer_claim"]["expires_epoch"] = time.time() - 1
        self._write_doc(doc)
        refused = self._advance("writer_done", "generic-done", holder="another-holder")
        self.assertEqual(refused[0], 409, refused[1])
        self.assertEqual(refused[1]["code"], "writer_op_required")
        self.assertEqual(self._state()[1]["stage"], "writing")
        self.assertEqual(self._read_doc()["writer_claim"]["job_id"], launch["job_id"])

    def test_checks_and_review_gate_recovery_reports_already_applied(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        doc = self._read_doc()
        doc["stage"] = "verifying"
        self._write_doc(doc)
        green = {
            "name": "fast", "kind": "test", "coverage": True, "cwd": str(self.repo),
            "ok": True, "returncode": 0, "timed_out": False, "compact": "green",
        }
        with mock.patch.object(server, "build_recipe_catalog", return_value=self._recipe_catalog()), mock.patch.object(
            server, "build_run_recipe", return_value=green
        ):
            checks = self._post_build("checks", phase="fast")
        self.assertEqual(checks[0], 200, checks[1])
        self.assertEqual(checks[1]["stage"], "review")
        checks_gate = self._gate("stable-event.verify-passed", holder="event:stable-event")
        self.assertEqual(checks_gate[0], 200, checks_gate[1])
        self.assertEqual(checks_gate[1]["code"], "already_applied")

        with mock.patch.object(
            server,
            "build_review_round_result",
            return_value={"verdict": "PASS", "summary": "clear", "compact": "clear", "no_verdict": False},
        ):
            review = self._post_build("review", reviewer="sonnet")
        self.assertEqual(review[0], 200, review[1])
        self.assertEqual(review[1]["stage"], "ready")
        review_gate = self._gate("stable-event.review-passed", holder="event:stable-event")
        self.assertEqual(review_gate[0], 200, review_gate[1])
        self.assertEqual(review_gate[1]["code"], "already_applied")

    def test_generic_advance_cannot_assert_any_authoritative_gate(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        for action in ("verify_passed", "review_passed", "pr_opened"):
            with self.subTest(action=action):
                result = self._advance(action, "inject-" + action)
                self.assertEqual(result[0], 409, result[1])
                self.assertEqual(result[1]["code"], "gate_op_required")
        self.assertEqual(self._state()[1]["stage"], "created")

    def test_review_verdict_parser_requires_a_final_line_and_fails_conflicts_closed(self):
        self.assertEqual(server.build_parse_review_verdict("analysis\nVERDICT: PASS\n")[0], "PASS")
        self.assertIsNone(server.build_parse_review_verdict("VERDICT: PASS\nmore prose")[0])
        self.assertEqual(
            server.build_parse_review_verdict("VERDICT: PASS\nVERDICT: NACK\n")[0], "NACK"
        )
        self.assertIsNone(
            server.build_parse_review_verdict('{"status":"ok","receipt":{"verdict":"PASS"}}')[0]
        )

    def test_writer_vendor_aliases_are_complete_and_unknown_fails_closed(self):
        expected = {
            "sonnet": "anthropic",
            "opus": "anthropic",
            "fable": "anthropic",
            "claude-sonnet-5": "anthropic",
            "grok-4.6": "xai",
            "gpt-5.6-sol": "openai",
        }
        for model, vendor in expected.items():
            with self.subTest(model=model):
                self.assertEqual(server.build_writer_vendor({"route": {"model": model}}), vendor)
        unknown = {"route": {"model": "mystery-9000"}}
        self.assertEqual(server.build_writer_vendor(unknown), server.BUILD_VENDOR_UNKNOWN)
        self.assertIn("no vendor", server.build_review_authority_gap(unknown, "sonnet"))

    def test_push_retry_uses_the_journalled_sha_without_a_second_commit(self):
        self._ready_finalize_fixture()
        original = server.run_git
        commits = []
        pushes = []

        def fail_then_push(path, args, **kwargs):
            if args[0] == "commit":
                commits.append(list(args))
            if args[0] == "ls-remote":
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if args[0] == "push":
                pushes.append(list(args))
                if len(pushes) == 1:
                    return {"ok": False, "returncode": 1, "stdout": "", "stderr": "offline", "error": "offline"}
                return {"ok": True, "returncode": 0, "stdout": "pushed", "stderr": ""}
            return original(path, args, **kwargs)

        event_id = "push-retry"
        first_run_id = "rewst-run-one"
        retry_run_id = "rewst-run-two"
        holder = lambda _run_id: f"event:{event_id}"
        request = {"event_id": event_id, "holder": holder(first_run_id), "message": "retry me"}
        with mock.patch.object(server, "run_git", side_effect=fail_then_push):
            failed = self._post_build("finalize", **request)
            pending_sha = self._read_doc()["pending_finalize"]["sha"]
            retried = self._post_build(
                "finalize", **{**request, "holder": holder(retry_run_id)}
            )
        self.assertEqual(failed[1]["code"], "push_failed")
        self.assertEqual(failed[1]["sha"], pending_sha)
        self.assertEqual(retried[0], 200, retried[1])
        self.assertEqual(retried[1]["receipt"]["sha"], pending_sha)
        self.assertEqual(len(commits), 1)
        self.assertEqual(len(pushes), 2)
        self.assertNotEqual(self._state()[1]["stage"], "parked")

    def test_finalize_state_write_failure_before_commit_retries_exactly_once(self):
        self._ready_finalize_fixture()
        original_git = server.run_git
        original_write = server.write_build
        commits = []
        pushes = []

        def git_with_remote(path, args, **kwargs):
            if args[0] == "commit":
                commits.append(list(args))
            if args[0] == "push":
                pushes.append(list(args))
                return {"ok": True, "returncode": 0, "stdout": "pushed", "stderr": ""}
            return original_git(path, args, **kwargs)

        failed_write = {
            "ok": False, "error": "injected crash before commit",
            "code": "state_write_failed", "status": 500,
        }
        request = {"event_id": "crash-before-commit", "holder": "run", "message": "one commit"}
        with mock.patch.object(server, "run_git", side_effect=git_with_remote), mock.patch.object(
            server, "write_build", return_value=failed_write
        ):
            failed = self._post_build("finalize", **request)
        self.assertEqual(failed[1]["code"], "state_write_failed")
        self.assertEqual(commits, [])
        self.assertEqual(self._git("rev-parse", "HEAD"), self.head)
        with mock.patch.object(server, "run_git", side_effect=git_with_remote), mock.patch.object(
            server, "write_build", side_effect=original_write
        ):
            retried = self._post_build("finalize", **request)
        self.assertEqual(retried[0], 200, retried[1])
        self.assertEqual(len(commits), 1)
        self.assertEqual(len(pushes), 1)

    def _assert_finalize_recovers_state_write(self, crash_write):
        self._ready_finalize_fixture()
        original_git = server.run_git
        original_write = server.write_build
        writes = 0
        commits = []
        pushes = []
        remote_heads = {}

        def fail_selected_write(doc, expected_id=None):
            nonlocal writes
            writes += 1
            if writes == crash_write:
                return {
                    "ok": False, "error": "injected crash",
                    "code": "state_write_failed", "status": 500,
                }
            return original_write(doc, expected_id)

        def git_with_remote(path, args, **kwargs):
            if args[0] == "commit":
                commits.append(list(args))
            if args[0] == "ls-remote":
                sha = remote_heads.get(args[-1], "")
                stdout = f"{sha}\t{args[-1]}\n" if sha else ""
                return {"ok": True, "returncode": 0, "stdout": stdout, "stderr": ""}
            if args[0] == "push":
                pushes.append(list(args))
                sha, ref = args[2].split(":", 1)
                remote_heads[ref] = sha
                return {"ok": True, "returncode": 0, "stdout": "pushed", "stderr": ""}
            return original_git(path, args, **kwargs)

        request = {
            "event_id": f"recover-write-{crash_write}", "holder": "run",
            "message": f"recover write {crash_write}",
        }
        with mock.patch.object(server, "run_git", side_effect=git_with_remote), mock.patch.object(
            server, "write_build", side_effect=fail_selected_write
        ):
            failed = self._post_build("finalize", **request)
        self.assertEqual(failed[1]["code"], "state_write_failed")
        expected_state = "prepared" if crash_write == 2 else "committed"
        durable_intent = self._read_doc()["pending_finalize"]
        self.assertEqual(durable_intent["state"], expected_state)
        if crash_write == 3:
            for field in ("sha", "tree", "parent", "message"):
                self.assertTrue(durable_intent.get(field), field)
        takeover = self._post_build(
            "finalize", event_id=f"takeover-write-{crash_write}", holder="run",
            message=request["message"],
        )
        self.assertEqual(takeover[0], 409, takeover[1])
        self.assertEqual(takeover[1]["code"], "finalize_in_progress")
        committed_sha = self._git("rev-parse", "HEAD")
        with mock.patch.object(server, "run_git", side_effect=git_with_remote), mock.patch.object(
            server, "write_build", side_effect=original_write
        ):
            retried = self._post_build("finalize", **request)
            replay = self._post_build("finalize", **request)
        self.assertEqual(retried[0], 200, retried[1])
        self.assertEqual(retried[1]["receipt"]["sha"], committed_sha)
        self.assertEqual(replay[1]["receipt"], retried[1]["receipt"])
        self.assertEqual(len(commits), 1)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(
            pushes[0][2],
            f"{committed_sha}:refs/heads/{self.BRANCH}",
        )
        self.assertFalse(any(arg.startswith("--force") for arg in pushes[0]))

    def test_finalize_state_write_failure_after_commit_adopts_without_recommit(self):
        self._assert_finalize_recovers_state_write(2)

    def test_finalize_state_write_failure_after_push_verifies_remote_without_repush(self):
        self._assert_finalize_recovers_state_write(3)

    def test_prepared_finalize_rejects_takeover_and_conflicting_same_event(self):
        request = self._prepare_finalize_intent()
        takeover = self._post_build(
            "finalize", event_id="other-finalize", holder="run", message=request["message"]
        )
        conflict = self._post_build("finalize", **{**request, "message": "different message"})
        self.assertEqual(takeover[0], 409, takeover[1])
        self.assertEqual(takeover[1]["code"], "finalize_in_progress")
        self.assertEqual(conflict[0], 409, conflict[1])
        self.assertEqual(conflict[1]["code"], "idempotency_conflict")
        self.assertEqual(self._git("rev-parse", "HEAD"), self.head)

    def test_prepared_finalize_rejects_a_conflicting_candidate_tree_without_committing(self):
        request = self._prepare_finalize_intent(event_id="tree-conflict")
        (self.repo / "change.txt").write_text("changed after prepare\n")
        self._git("add", "change.txt")
        refused = self._post_build("finalize", **request)
        self.assertEqual(refused[0], 409, refused[1])
        self.assertEqual(refused[1]["code"], "finalize_adoption_mismatch")
        self.assertEqual(self._git("rev-parse", "HEAD"), self.head)

    def _assert_finalize_adoption_rejected(self, mismatch):
        request = self._prepare_finalize_intent(event_id=f"adopt-{mismatch}", message="expected message")
        doc = self._read_doc()
        intent = doc["pending_finalize"]
        self._git("commit", "-m", "wrong message" if mismatch == "message" else request["message"])
        if mismatch == "parent":
            intent["expected_parent"] = "f" * 40
        elif mismatch == "tree":
            intent["candidate_tree"] = "f" * 40
        elif mismatch == "branch":
            self._git("checkout", "-b", "feature/wrong-branch")
        elif mismatch == "author":
            intent["authoring_boundary"]["author"] = "someone else <else@example.test>"
        self._write_doc(doc)
        refused = self._post_build("finalize", **request)
        self.assertEqual(refused[0], 409, refused[1])
        self.assertEqual(refused[1]["code"], "finalize_adoption_mismatch")

    def test_prepared_finalize_adoption_rejects_wrong_parent(self):
        self._assert_finalize_adoption_rejected("parent")

    def test_prepared_finalize_adoption_rejects_wrong_tree(self):
        self._assert_finalize_adoption_rejected("tree")

    def test_prepared_finalize_adoption_rejects_wrong_message(self):
        self._assert_finalize_adoption_rejected("message")

    def test_prepared_finalize_adoption_rejects_wrong_branch(self):
        self._assert_finalize_adoption_rejected("branch")

    def test_prepared_finalize_adoption_rejects_wrong_author_boundary(self):
        self._assert_finalize_adoption_rejected("author")

    def test_finalize_rejects_unstaged_tracked_and_untracked_content(self):
        self._ready_finalize_fixture()
        for event_id, make_dirt in (
            ("dirty-tracked", lambda: (self.repo / "README").write_text("unstaged\n")),
            ("dirty-untracked", lambda: (self.repo / "loose.txt").write_text("untracked\n")),
        ):
            with self.subTest(event_id=event_id):
                make_dirt()
                change_id = server.build_change_id(self.repo, self.head)
                doc = self._read_doc()
                for entry in doc["checks"].values():
                    entry["change_id"] = change_id
                doc["reviews"] = [
                    {"change_id": change_id, "reviewer": "sonnet", "verdict": "PASS"}
                ]
                self._write_doc(doc)
                result = self._post_build("finalize", event_id=event_id, holder="run", message="no")
                self.assertEqual(result[0], 409, result[1])
                self.assertEqual(result[1]["code"], "worktree_not_equal")
                self._git("restore", "README")
                (self.repo / "loose.txt").unlink(missing_ok=True)

    def test_clean_stack_role_runner_and_cwd_are_authoritative(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        (self.repo / "ticket.md").unlink()
        (self.repo / "candidate.txt").write_text("candidate\n")
        self._git("add", "candidate.txt")
        doc = self._read_doc()
        healthy_but_arbitrary = server.build_checks_result(
            doc, "integration", None, "looks-clean", {"scratch": str(self.repo)}
        )
        self.assertEqual(healthy_but_arbitrary[0], 409)
        self.assertEqual(healthy_but_arbitrary[1]["code"], "not_a_clean_stack")

        seen = []
        def catalog(_repos, target, _label):
            recipes = self._recipe_catalog()
            recipes["integration"]["cwd"] = target
            return recipes

        with mock.patch.object(server, "build_recipe_catalog", side_effect=catalog), mock.patch.object(
            server,
            "build_run_recipe",
            side_effect=lambda name, spec: seen.append(Path(spec["cwd"]))
            or {"name": name, "ok": True, "coverage": True},
        ):
            result = server.build_checks_result(
                doc, "integration", None, "clean-stack", {"scratch": str(self.repo)}
            )
        self.assertEqual(result[0], 200, result[1])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].parent, self.clean_runner)
        self.assertNotIn(self.repo, seen)

    def test_path_rule_phase_and_preflight_validation_cover_every_reachable_recipe(self):
        verification = {
            "fast": ["fast"],
            "integration": ["integration"],
            "path_rules": [
                {"match": "*.py", "phase": "integration", "recipes": ["path-check"]}
            ],
        }
        self.assertEqual(self._mechanical_create(verification=verification)[0], 201)
        doc = self._read_doc()
        self.assertEqual(server.build_required_recipes(doc, "fast", ["server.py"])[0], ["fast"])
        self.assertEqual(
            server.build_required_recipes(doc, "integration", ["server.py"])[0],
            ["integration", "path-check"],
        )
        for code, mutate in (
            ("unknown_recipe", lambda catalog: catalog.pop("path-check")),
            ("recipe_wrong_worktree", lambda catalog: catalog["path-check"].update(unresolvable="wrong tree")),
            ("hook_not_coverage", lambda catalog: [catalog[name].update(coverage=False) for name in ("integration", "path-check")]),
        ):
            with self.subTest(code=code):
                catalog = self._recipe_catalog()
                mutate(catalog)
                with mock.patch.object(server, "build_recipe_catalog", return_value=catalog):
                    failures = server.build_preflight_recipes(doc, {"scratch": str(self.repo)})
                self.assertIn(code, [failure["code"] for failure in failures])

    def test_missing_jobs_max_fails_closed_before_any_writer_claim(self):
        self.assertEqual(self._mechanical_create(budget={})[0], 201)
        claim = self._claim()
        self.assertEqual(claim[0], 409, claim[1])
        self.assertEqual(claim[1]["code"], "no_job_budget")
        self.assertEqual(self._read_doc()["budget"]["jobs_used"], 0)

    def test_pre_pr_graph_is_acyclic_fail_closed_and_has_one_shipping_path(self):
        graph_path = Path(server.__file__).resolve().parent / "graphs" / "pre-pr-build.json"
        graph = json.loads(graph_path.read_text())
        self.assertEqual(graph["slug"], "graphwing-pre-pr-build")
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        incoming = {name: [] for name in nodes}
        outgoing = {name: [] for name in nodes}
        for edge in edges:
            incoming[edge["target"]].append(edge["source"])
            outgoing[edge["source"]].append(edge["target"])
        for node_id, sources in incoming.items():
            if len(sources) > 1:
                self.assertEqual(nodes[node_id]["type"], "logic.join.any", node_id)

        indegree = {name: len(sources) for name, sources in incoming.items()}
        queue = [name for name, degree in indegree.items() if degree == 0]
        visited = []
        while queue:
            name = queue.pop()
            visited.append(name)
            for target in outgoing[name]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        self.assertEqual(len(visited), len(nodes), "published graph must not contain a cycle")
        for wait_id, filter_id in (("wait_fast", "if_fast"), ("wait_review", "if_review"), ("wait_int", "if_int")):
            self.assertIn(wait_id, incoming[filter_id], f"{filter_id} must inspect the wait receipt")
        terminals = [node for node in nodes.values() if not outgoing[node["id"]]]
        terminal_labels = {node["label"] for node in terminals}
        self.assertGreaterEqual(len(terminal_labels), 12)
        self.assertTrue(any("missing" in label.lower() for label in terminal_labels))
        self.assertTrue(any("clean stack" in label.lower() for label in terminal_labels))
        self.assertFalse(any("kick" in json.dumps(node["config"]).lower() for node in nodes.values()))
        action_nodes = [node for node in nodes.values() if node["type"].startswith("action.")]
        shipping = [node["id"] for node in action_nodes if "/v1/build/finalize" in node["type"]]
        self.assertEqual(shipping, ["finalize"])
        self.assertFalse(any("/v1/git/" in node["type"] or "/v1/gh/" in node["type"] for node in action_nodes))
        self.assertFalse(any("/v1/agent/run" in node["type"] for node in action_nodes))
        self.assertEqual(
            len([node for node in action_nodes if "/v1/build/writer/launch" in node["type"]]), 2
        )
        self.assertEqual(
            len([node for node in action_nodes if "/v1/build/writer/complete" in node["type"]]), 2
        )
        forbidden = [
            node["id"] for node in action_nodes
            if "/v1/build/advance" in node["type"]
            and node.get("config", {}).get("action") in ("verify_passed", "review_passed")
        ]
        self.assertEqual(forbidden, [])
        holders = [
            node.get("config", {}).get("holder")
            for node in action_nodes
            if "holder" in node.get("config", {})
        ]
        self.assertTrue(holders)
        self.assertEqual(set(holders), {"event:{{ CTX.INPUT.event_id }}"})
        self.assertEqual(graph["tags"], ["graphwing-supervised"])
        edge_targets = {
            (edge["source"], edge["sourceHandle"]): edge["target"] for edge in edges
        }
        self.assertEqual(edge_targets[("join_start", "out")], "register")
        self.assertEqual(edge_targets[("register", "success")], "next")
        self.assertEqual(edge_targets[("register", "failure")], "registration_refused")
        register = nodes["register"]
        self.assertEqual(register["config"]["source_run_id"], "{{ CTX.WORKFLOW.runId }}")
        for field in (
            "repo", "writer_prompt", "writer_prompt_ref", "fast_recipe",
            "integration_recipe", "reviewer", "class_stamp", "route_stamp",
        ):
            self.assertEqual(
                register["config"]["continuation"][field],
                f"{{{{ CTX.INPUT.{field} }}}}",
            )
        self.assertEqual(edge_targets[("if_fast", "pass")], "fast_green")
        self.assertEqual(edge_targets[("if_review", "pass")], "review_green")
        self.assertEqual(edge_targets[("switch_step", "case-4")], "join_verify_passed")
        self.assertEqual(edge_targets[("switch_step", "case-6")], "join_review_passed")

    def test_bounded_mechanical_canary_reaches_finalize_with_deterministic_mocks(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        recipes = self._recipe_catalog()
        repos = {"scratch": str(self.repo)}
        with mock.patch.object(server, "build_recipe_catalog", return_value=recipes), mock.patch.object(
            server, "git_status", return_value={"ok": True, "entries": []}
        ):
            self.assertEqual(self._post_build("preflight")[0], 200)
        launch = self._launch_writer()
        (self.repo / "canary.txt").write_text("canary\n")
        self._git("add", "canary.txt")
        (self.repo / "ticket.md").unlink()
        receipt = self._finish_job(launch)
        completed = self._complete("canary-written", launch, receipt)
        self.assertEqual(completed[0], 200, completed[1])
        green = {"kind": "test", "coverage": True, "cwd": str(self.repo), "ok": True,
                 "returncode": 0, "timed_out": False, "compact": "ok"}
        with mock.patch.object(server, "build_recipe_catalog", return_value=recipes), mock.patch.object(
            server, "build_run_recipe", side_effect=lambda name, spec: {**green, "name": name}
        ):
            self.assertTrue(self._post_build("checks", phase="fast")[1]["ok"])
        self.assertEqual(self._state()[1]["stage"], "review")
        with mock.patch.object(
            server,
            "build_review_round_result",
            return_value={"verdict": "PASS", "summary": "clear", "compact": "clear", "no_verdict": False},
        ):
            self.assertTrue(self._post_build("review", reviewer="sonnet")[1]["ok"])
        self.assertEqual(self._state()[1]["stage"], "ready")
        with mock.patch.object(server, "build_recipe_catalog", return_value=recipes), mock.patch.object(
            server, "build_run_recipe", side_effect=lambda name, spec: {**green, "name": name}
        ), mock.patch.object(server, "stack_status", return_value={"ok": True, "healthy": True}):
            self.assertTrue(self._post_build("checks", phase="integration", stack="clean-stack")[1]["ok"])
        self.assertEqual(self._next()[1]["step"], "finalize")

    # -- sensitive ticket 05 -----------------------------------------------

    def test_sensitive_generic_advance_cannot_assert_writer_or_gate_state(self):
        self.assertEqual(self._sensitive_create()[0], 201)
        for action, code in (
            ("start_writer", "writer_op_required"),
            ("writer_done", "writer_op_required"),
            ("verify_passed", "gate_op_required"),
            ("review_passed", "gate_op_required"),
            ("pr_opened", "gate_op_required"),
        ):
            with self.subTest(action=action):
                result = self._advance(action, "sensitive-inject-" + action)
                self.assertEqual(result[0], 409, result[1])
                self.assertEqual(result[1]["code"], code)
        self.assertEqual(self._state()[1]["stage"], "created")

    def test_historical_mechanical_open_surface_delegates_to_the_shared_path(self):
        self.assertEqual(self._sensitive_create()[0], 201)
        expected = server.build_open_path(self.BUILD)
        actual = server.build_open_mechanical(self.BUILD)
        self.assertEqual(actual, expected)
        self.assertEqual(server.build_class(actual[0]), server.BUILD_SENSITIVE_CLASS)

    def test_sensitive_reviews_are_server_routed_and_both_gate_the_same_change(self):
        self.assertEqual(self._sensitive_create()[0], 201)
        change_id = self._record_fast_pass()
        doc = self._read_doc()
        doc["stage"] = "review"
        self._write_doc(doc)

        status, due, _ = self._next()
        self.assertEqual(status, 200, due)
        self.assertEqual(due["review_role"], "behavior")
        self.assertEqual(due["review_reviewer"], "terra")
        with mock.patch.object(
            server,
            "build_review_round_result",
            return_value={"verdict": "PASS", "summary": "clear", "compact": "clear", "no_verdict": False},
        ):
            first = self._post_build("review")
        self.assertEqual(first[0], 200, first[1])
        self.assertEqual(first[1]["role"], "behavior")
        self.assertEqual(first[1]["reviewer"], "terra")
        self.assertEqual(self._read_doc()["stage"], "review")

        due = self._next()[1]
        self.assertEqual(due["review_role"], "security")
        self.assertEqual(due["review_reviewer"], "grok")
        with mock.patch.object(
            server,
            "build_review_round_result",
            return_value={"verdict": "PASS", "summary": "clear", "compact": "clear", "no_verdict": False},
        ):
            second = self._post_build("review")
        self.assertEqual(second[0], 200, second[1])
        self.assertEqual(second[1]["role"], "security")
        self.assertEqual(second[1]["reviewer"], "grok")
        self.assertEqual(second[1]["gate"], "ready")
        recorded = self._read_doc()["reviews"]
        self.assertEqual([entry["role"] for entry in recorded], ["behavior", "security"])
        self.assertEqual({entry["change_id"] for entry in recorded}, {change_id})
        self.assertEqual({entry["evidence_id"] for entry in recorded}, {"none"})

    def test_sensitive_preflight_rejects_any_route_that_is_not_opus_terra_grok(self):
        cases = (
            ("model", "grok-4.6", "written by Opus"),
            ("reviewer1", "sonnet", "behavior-and-test review is 'terra'"),
            ("reviewer2", "terra", "security-and-tenant review is 'grok'"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                if self._doc_path().exists():
                    shutil.rmtree(self._doc_path().parent)
                self.assertEqual(self._sensitive_create(route=self._sensitive_route(**{field: value}))[0], 201)
                with mock.patch.object(
                    server, "build_recipe_catalog", return_value=self._recipe_catalog()
                ), mock.patch.object(server, "git_status", return_value={"ok": True, "entries": []}):
                    status, payload, _ = self._post_build("preflight")
                self.assertEqual(status, 409, payload)
                failures = [item for item in payload["failures"] if item["code"] == "invalid_sensitive_route"]
                self.assertTrue(failures, payload)
                self.assertIn(message, failures[0]["detail"])

    def test_sensitive_review_prompts_are_focused_and_do_not_share_hidden_reasoning(self):
        self.assertEqual(self._sensitive_create()[0], 201)
        doc = self._read_doc()
        doc["reviews"] = [{"compact": "SECRET PRIOR REVIEW REASONING"}]
        behavior = server.build_review_prompt(doc, server.BUILD_REVIEW_BEHAVIOR_ROLE)
        security = server.build_review_prompt(doc, server.BUILD_REVIEW_SECURITY_ROLE)
        for lens in ("PROMISED BEHAVIOR", "SCOPE", "RED EVIDENCE", "PRODUCTION PATH", "INTEGRATION SEAM", "HONEST FAILURE"):
            self.assertIn(lens, behavior)
        for lens in ("TENANT SCOPE", "PERMISSIONS", "FORGED INPUT", "LEAKAGE", "IDEMPOTENCY", "DATA LOSS"):
            self.assertIn(lens, security)
        self.assertNotIn("SECRET PRIOR REVIEW REASONING", behavior)
        self.assertNotIn("SECRET PRIOR REVIEW REASONING", security)

    def test_sensitive_visible_reviews_bind_to_the_same_approved_evidence(self):
        self.assertEqual(
            self._sensitive_create(
                verification={
                    "fast": ["fast"],
                    "integration": ["integration"],
                    "needs_visual_proof": True,
                    "human_visual_review": True,
                }
            )[0],
            201,
        )
        change_id = server.build_change_id(self.repo, self.head)
        doc = self._read_doc()
        doc["evidence_rounds"] = [
            {
                "round": 1,
                "change_id": change_id,
                "shots": [{"name": "tenant-list"}],
                "human_review": {"verdict": "approved"},
            },
            {
                "round": 2,
                "change_id": change_id,
                "shots": [{"name": "tenant-detail"}],
            },
        ]
        old_evidence_id = server.build_evidence_identity(
            {**doc, "evidence_rounds": doc["evidence_rounds"][:1]}, change_id
        )
        doc["reviews"] = [
            {"change_id": change_id, "reviewer": "terra", "role": "behavior", "verdict": "PASS", "evidence_id": old_evidence_id},
            {"change_id": change_id, "reviewer": "grok", "role": "security", "verdict": "PASS", "evidence_id": old_evidence_id},
        ]
        visual = server.build_visual_gaps(doc, change_id)
        self.assertEqual(len(visual), 1)
        self.assertIn("latest", visual[0])
        stale = server.build_review_gaps(doc, change_id)
        self.assertEqual(len(stale), 2)
        self.assertTrue(all("no longer" in gap for gap in stale))

        doc["evidence_rounds"][-1]["human_review"] = {"verdict": "approved"}
        evidence_id = server.build_evidence_identity(doc, change_id)
        for review in doc["reviews"]:
            review["evidence_id"] = evidence_id
        self.assertEqual(server.build_visual_gaps(doc, change_id), [])
        self.assertEqual(server.build_review_gaps(doc, change_id), [])
        self.assertEqual({review["evidence_id"] for review in doc["reviews"]}, {evidence_id})

    def test_sensitive_review_receipts_require_the_current_evidence_identity(self):
        self.assertEqual(self._sensitive_create()[0], 201)
        change_id = self._record_fast_pass()
        doc = self._read_doc()
        live = server.build_evidence_identity(doc, change_id)
        valid = {
            "behavior": {"change_id": change_id, "reviewer": "terra", "role": "behavior", "verdict": "PASS", "evidence_id": live},
            "security": {"change_id": change_id, "reviewer": "grok", "role": "security", "verdict": "PASS", "evidence_id": live},
        }
        for verdict in ("PASS", "NACK"):
            for bad_evidence in (None, "different-evidence"):
                with self.subTest(role="behavior", verdict=verdict, evidence_id=bad_evidence):
                    behavior = {**valid["behavior"], "verdict": verdict, "evidence_id": bad_evidence}
                    if bad_evidence is None:
                        behavior.pop("evidence_id")
                    candidate = {**doc, "reviews": [behavior, valid["security"]]}
                    self.assertEqual(server.build_next_review_role(candidate, change_id)[0], "behavior")
                    gaps = server.build_finalize_gaps(candidate, change_id)
                    self.assertTrue(any("behavior-and-test" in gap and "evidence" in gap for gap in gaps), gaps)
                with self.subTest(role="security", verdict=verdict, evidence_id=bad_evidence):
                    security = {**valid["security"], "verdict": verdict, "evidence_id": bad_evidence}
                    if bad_evidence is None:
                        security.pop("evidence_id")
                    candidate = {**doc, "reviews": [valid["behavior"], security]}
                    self.assertEqual(server.build_next_review_role(candidate, change_id)[0], "security")
                    gaps = server.build_finalize_gaps(candidate, change_id)
                    self.assertTrue(any("security-and-tenant" in gap and "evidence" in gap for gap in gaps), gaps)

    def test_ticket_02_mechanical_review_receipts_keep_legacy_evidence_compatibility(self):
        self.assertEqual(self._mechanical_create()[0], 201)
        change_id = self._record_fast_pass()
        doc = self._read_doc()
        doc["reviews"] = [
            {"change_id": change_id, "reviewer": "sonnet", "verdict": "PASS"}
        ]
        self.assertEqual(server.build_review_gaps(doc, change_id), [])
        self.assertEqual(server.build_next_review_role(doc, change_id), (None, ""))

    def test_sensitive_catalog_and_graph_expose_the_server_selected_review(self):
        spec = json.loads(server.openapi_bytes())
        next_props = spec["components"]["schemas"]["BuildNext"]["properties"]
        review_props = spec["components"]["schemas"]["BuildReview"]["properties"]
        for field in ("review_role", "review_reviewer", "review_roles", "reviews_outstanding", "visual_outstanding"):
            self.assertIn(field, next_props)
        for field in ("role", "evidence_id"):
            self.assertIn(field, review_props)

        graph = json.loads(
            (Path(server.__file__).resolve().parent / "graphs" / "pre-pr-build.json").read_text()
        )
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        self.assertEqual(nodes["review"]["config"]["reviewer"], "{{ TASKS.next.data.review_reviewer }}")
        cases = nodes["switch_step"]["config"]["cases"]
        evidence_index = next(
            index for index, case in enumerate(cases)
            if case["rules"][0]["value"] == "evidence"
        )
        edge = next(
            edge for edge in graph["spec"]["edges"]
            if edge["source"] == "switch_step" and edge["sourceHandle"] == f"case-{evidence_index}"
        )
        self.assertEqual(edge["target"], "visual_evidence_required")

    # -- catalog ------------------------------------------------------------

    def test_openapi_declares_the_build_operations(self):
        spec = json.loads(server.openapi_bytes())
        for route, method, op_id in (
            ("/v1/build/create", "post", "buildCreate"),
            ("/v1/build/advance", "post", "buildAdvance"),
            ("/v1/build/state", "get", "buildState"),
        ):
            self.assertIn(route, spec["paths"])
            self.assertEqual(spec["paths"][route][method]["operationId"], op_id)
        schemas = spec["components"]["schemas"]
        self.assertEqual(
            schemas["BuildCreateRequest"]["required"],
            ["build_id", "story", "repo", "branch"],
        )
        create_properties = schemas["BuildCreateRequest"]["properties"]
        for field in ("index", "ticket"):
            with self.subTest(field=field):
                self.assertEqual(create_properties[field]["minLength"], 1)
                self.assertEqual(create_properties[field]["maxLength"], 256)
        self.assertEqual(
            sorted(schemas["BuildAdvanceRequest"]["required"]),
            ["action", "build_id", "event_id", "holder"],
        )
        advance_properties = schemas["BuildAdvanceRequest"]["properties"]
        self.assertEqual(
            advance_properties["expect"]["properties"]["branch"]["type"],
            "string",
        )
        data_description = advance_properties["data"]["description"]
        self.assertIn(
            "Branch and head identity come from the live Git read",
            data_description,
        )
        self.assertIn("a data.head is ignored", data_description)
        # The spec and the state machine drift apart silently otherwise: Rewst
        # would send an action the server has never heard of.
        self.assertEqual(
            set(advance_properties["action"]["enum"]),
            set(server.BUILD_TRANSITIONS) | {"park"},
        )
        stages = {"parked"}
        for frm, to in server.BUILD_TRANSITIONS.values():
            stages.update((frm, to))
        self.assertEqual(set(schemas["BuildState"]["properties"]["stage"]["enum"]), stages)


class CompletionSupervisorTests(unittest.TestCase):
    """Every terminal Rewst run for a supervised build has to land somewhere.

    The failure this class exists to prevent is a build that quietly stops:
    Rewst says `completed`, nobody checks whether Graphwing recorded anything,
    and the branch sits there with a running preview stack and no operator who
    knows. So the tests are mostly about the answers that are *not* success —
    a completion with no receipt, a claim with no job, a retry that has already
    been spent, a second delivery of the same run.
    """

    BUILD = "bld-sup-0001"
    BRANCH = "feature/issue-52-supervisor"
    RUN = "rewst-run-00000001"
    EVENT = "evt-writer-1"
    LISTENER = "graphwing-build-completion-supervisor@7"
    WORKFLOW = "wf-pre-pr-build"
    VERSION = "v11"
    DEADLINE = "2099-01-01T00:00:00Z"
    PLAN_PANE = "pane-plan-issue-52"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        # Watch workers are daemon threads and can outlive one test by a few
        # polling ticks. Give each fixture a distinct durable identity so a
        # prior worker cannot observe or settle the next test's build.
        self.BUILD = f"bld-sup-{root.name}"
        self.home = root / "gwhome"
        self.builds = self.home / "builds"
        self.supervisor = self.home / "supervisor"
        self.jobs = self.home / "jobs"
        self.jobs.mkdir(parents=True)
        self.repo = root / "repo"
        self.repo.mkdir()
        for args in (
            ["git", "init", "-b", "main", str(self.repo)],
            ["git", "-C", str(self.repo), "config", "user.email", "gw@test"],
            ["git", "-C", str(self.repo), "config", "user.name", "graphwing-test"],
            ["git", "-C", str(self.repo), "config", "commit.gpgsign", "false"],
        ):
            subprocess.run(args, check=True, capture_output=True)
        (self.repo / "README").write_text("hi\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "README"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-b", self.BRANCH], check=True, capture_output=True
        )
        for attr, value in (
            ("BUILDS_DIR", self.builds),
            ("SUPERVISOR_DIR", self.supervisor),
            ("JOBS_DIR", self.jobs),
        ):
            patch = mock.patch.object(server, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        rp = mock.patch.object(server, "load_repos", return_value={"scratch": str(self.repo)})
        rp.start()
        self.addCleanup(rp.stop)
        pane = mock.patch.object(
            server,
            "herdr_pane_known",
            side_effect=lambda pane_id: (
                (True, "")
                if pane_id == self.PLAN_PANE
                else (False, "unknown_plan_pane")
            ),
        )
        pane.start()
        self.addCleanup(pane.stop)
        # Most tests exercise durable watch decisions, not daemon scheduling.
        # Keep a worker from one test from consuming another test's global
        # mocks; scheduler-specific tests install their own mock or restore
        # this saved implementation explicitly.
        self._real_schedule_watch = server.supervisor_schedule_watch
        scheduler = mock.patch.object(server, "supervisor_schedule_watch", return_value=True)
        scheduler.start()
        self.addCleanup(scheduler.stop)
        self._open_build()

    # -- helpers ------------------------------------------------------------

    def _open_build(self, build_id=None):
        body = {
            "build_id": build_id or self.BUILD,
            "story": "issue-52 completion supervisor",
            "repo": "scratch",
            "branch": self.BRANCH,
            "stacks": ["riftwing-52"],
            "budget": {"jobs_max": 12},
        }
        status, payload, _ = server.dispatch("POST", "/v1/build/create", {}, True, json.dumps(body).encode())
        self.assertEqual(status, 201, payload)
        # These supervisor tests predate the ticket-02 mechanical operations
        # and need a legal buildAdvance receipt as their fixture. Keep that
        # legacy/non-path setup explicit: the shared mechanical-and-sensitive
        # path itself is covered by BuildStateTests and may only move through
        # its authoritative claim, writer, check, review, gate, and finalize
        # operations.
        doc, read_err = server.load_build(build_id or self.BUILD)
        self.assertIsNone(read_err)
        self.assertIsNotNone(doc)
        assert doc is not None
        doc["route"] = {"class": "visual"}
        self.assertIsNone(server.write_build(doc, build_id or self.BUILD))
        return payload

    def _advance(self, action, event_id, build_id=None, holder="rewst-run-1"):
        body = {
            "build_id": build_id or self.BUILD,
            "event_id": event_id,
            "action": action,
            "holder": holder,
        }
        return server.dispatch("POST", "/v1/build/advance", {}, True, json.dumps(body).encode())

    def _register_body(self, **over):
        body = {
            "source_run_id": self.RUN,
            "build_id": self.BUILD,
            "event_id": self.EVENT,
            "workflow_id": self.WORKFLOW,
            "workflow_version": self.VERSION,
            "expected_stage": "created",
            "deadline": self.DEADLINE,
            "plan_pane": self.PLAN_PANE,
            "continuation": {
                "repo": "scratch",
                "writer_prompt": "Implement issue 52 ticket 06b only.",
                "writer_prompt_ref": "slices/issue-52-pre-pr-build/06b-completion-supervisor.md",
                "fast_recipe": "fast-checks",
                "integration_recipe": "integration-checks",
                "reviewer": "terra",
                "class_stamp": "mechanical",
                "route_stamp": "writer-check-review",
            },
        }
        body.update(over)
        return body

    def _register(self, **over):
        body = self._register_body(**over)
        return server.dispatch("POST", "/v1/supervisor/register", {}, True, json.dumps(body).encode())

    def _completion(self, status="failed", run=None, **over):
        """A trigger.workflowCompleted payload, shaped the way the graph sends it."""
        body = {
            "listener_id": self.LISTENER,
            "source_workflow": {
                "id": self.WORKFLOW,
                "slug": "graphwing-pre-pr-build",
                "name": "graphwing-pre-pr-build",
                "tags": ["graphwing-supervised"],
            },
            "source_run": {
                "id": run or self.RUN,
                "status": status,
                "execution_mode": "live",
                "started_at": "2026-08-25T10:00:00Z",
                "finished_at": "2026-08-25T10:04:00Z",
            },
            "output": {"build_id": self.BUILD, "event_id": self.EVENT},
            "error": status != "completed",
        }
        for key, value in over.items():
            if isinstance(value, dict) and isinstance(body.get(key), dict):
                body[key] = {**body[key], **value}
            else:
                body[key] = value
        return body

    def _reconcile(self, status="failed", run=None, **over):
        body = self._completion(status=status, run=run, **over)
        return server.dispatch("POST", "/v1/supervisor/reconcile", {}, True, json.dumps(body).encode())

    def _bind(self, job_id, run=None):
        return server.dispatch(
            "POST",
            "/v1/supervisor/bind",
            {},
            True,
            json.dumps({"source_run_id": run or self.RUN, "job_id": job_id}).encode(),
        )

    def _watch(self, run=None, **over):
        body = {"source_run_id": run or self.RUN}
        body.update(over)
        return server.dispatch(
            "POST", "/v1/supervisor/watch", {}, True, json.dumps(body).encode()
        )

    def _report(self, **qs):
        return server.dispatch(
            "GET", "/v1/supervisor/report", {k: [v] for k, v in qs.items()}, True, b""
        )

    def _write_job(self, job_id, status):
        path = self.jobs / job_id / "job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"job_id": job_id, "status": status, "kind": "agent"}) + "\n")
        return job_id

    def _next_run(self, n):
        return f"rewst-run-{n:08d}"

    def _fresh_delivery(self, n, status="failed", job_id=None, **over):
        """A retry is a new Rewst run against the same build event id.

        Nothing redelivers the first run: Rewst re-runs the workflow, which
        gets its own run id and its own launch registration. That is exactly
        why the retry counter cannot live on the launch record.
        """
        run = self._next_run(n)
        reg = self._register(source_run_id=run, **({"job_id": job_id} if job_id else {}))
        self.assertEqual(reg[0], 201, reg[1])
        return self._reconcile(status=status, run=run, **over)

    def _state(self, build_id=None):
        status, payload, _ = server.dispatch(
            "GET", "/v1/build/state", {"build_id": [build_id or self.BUILD]}, True, b""
        )
        self.assertIn(status, (200, 409), payload)
        return payload

    def _supervisor_doc(self, build_id=None):
        path = self.supervisor / "builds" / (build_id or self.BUILD) / "supervisor.json"
        return json.loads(path.read_text())

    # -- launch registration ------------------------------------------------

    def test_registration_maps_the_source_run_to_the_whole_launch(self):
        status, payload, _ = self._register()
        self.assertEqual(status, 201, payload)
        self.assertTrue(payload["registered"])
        self.assertTrue(payload["supervised"])
        self.assertEqual(payload["source_run_id"], self.RUN)
        self.assertEqual(payload["build_id"], self.BUILD)
        self.assertEqual(payload["event_id"], self.EVENT)
        self.assertEqual(payload["workflow_id"], self.WORKFLOW)
        self.assertEqual(payload["workflow_version"], self.VERSION)
        self.assertEqual(payload["expected_stage"], "created")
        self.assertEqual(payload["deadline"], self.DEADLINE)
        self.assertEqual(payload["plan_pane"], self.PLAN_PANE)
        self.assertNotIn("continuation", payload, "the source spec is returned only on continuation")
        saved = self._supervisor_doc()["runs"][self.RUN]["continuation"]
        self.assertEqual(saved["repo"], "scratch")
        self.assertEqual(saved["fast_recipe"], "fast-checks")
        self.assertEqual(saved["integration_recipe"], "integration-checks")
        self.assertEqual(saved["reviewer"], "terra")
        self.assertTrue((self.supervisor / "builds" / self.BUILD / "supervisor.json").is_file())

    def test_registration_rejects_secret_or_capability_continuation_values(self):
        for field, value in (
            ("writer_prompt", "use token ghp_" + "a" * 36),
            ("writer_prompt_ref", "https://engine.rewst.io/webhooks/custom/" + "a" * 32),
        ):
            with self.subTest(field=field):
                body = self._register_body(source_run_id=self._next_run(700))
                body["continuation"][field] = value
                status, payload, _ = server.dispatch(
                    "POST", "/v1/supervisor/register", {}, True, json.dumps(body).encode()
                )
                self.assertEqual(status, 400, payload)
                self.assertTrue(
                    payload["code"] == "bad_continuation" or payload["code"].startswith("secret_in_"),
                    payload,
                )

    def test_a_local_job_binding_is_narrow_idempotent_and_immutable(self):
        self.assertEqual(self._register()[0], 201)
        first_job = "a" * 32
        status, payload, _ = self._bind(first_job)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["bound"])
        self.assertEqual(payload["job_id"], first_job)
        status, replay, _ = self._bind(first_job)
        self.assertEqual(status, 200, replay)
        self.assertFalse(replay["bound"])
        status, conflict, _ = self._bind("b" * 32)
        self.assertEqual(status, 409, conflict)
        self.assertEqual(conflict["code"], "job_binding_conflict")
        self.assertEqual(self._supervisor_doc()["runs"][self.RUN]["job_id"], first_job)

    def test_a_local_job_binding_requires_the_registered_source_run(self):
        status, payload, _ = self._bind("a" * 32)
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["code"], "unknown_source_run")

    def test_async_job_ops_bind_before_enqueue_and_returning_202(self):
        runs = (self.RUN, self._next_run(901), self._next_run(902))
        for run in runs:
            self.assertEqual(self._register(source_run_id=run)[0], 201)
        observed = []

        def assert_bound(job):
            launch = self._supervisor_doc()["runs"][job["supervised_source_run_id"]]
            self.assertEqual(launch["job_id"], job["job_id"])
            observed.append(job["kind"])

        profile = [{
            "id": server.HOME_PROFILE,
            "runnable": True,
            "hermes_home": str(server.HOME),
        }]
        with mock.patch.object(server, "load_profiles", return_value=profile), mock.patch.object(
            server, "HERMES_BIN", self.repo / "README"
        ), mock.patch.object(server, "enqueue_agent", side_effect=assert_bound):
            status, agent = server.agent_run(
                json.dumps({"cwd": "scratch", "prompt": "one job", "supervised_source_run_id": runs[0]}).encode(),
                {"scratch": str(self.repo)},
            )
        self.assertEqual(status, 202, agent)

        tests = {"fast": {"argv": ["true"], "cwd": self.repo, "timeout_seconds": 30, "async": True}}
        with mock.patch.object(server, "load_tests", return_value=tests), mock.patch.object(
            server, "enqueue_script", side_effect=assert_bound
        ):
            status, check = server.test_run(
                json.dumps({"name": "fast", "supervised_source_run_id": runs[1]}).encode(),
                {"scratch": str(self.repo)},
            )
        self.assertEqual(status, 202, check)

        with mock.patch.object(server, "enqueue_review", side_effect=assert_bound):
            status, review = server.review_run(
                json.dumps({
                    "repo": "scratch", "reviewer": "terra", "prompt": "review only",
                    "response_webhook_url": "https://example.invalid/resume",
                    "supervised_source_run_id": runs[2],
                }).encode(),
                {"scratch": str(self.repo)},
            )
        self.assertEqual(status, 202, review)
        self.assertEqual(observed, ["agent", "test", "review"])

    def test_registration_requires_an_exact_saved_planning_pane(self):
        body = self._register_body()
        body.pop("plan_pane")
        status, payload, _ = server.dispatch(
            "POST", "/v1/supervisor/register", {}, True, json.dumps(body).encode()
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "missing_plan_pane")
        self.assertFalse((self.supervisor / "builds" / self.BUILD / "supervisor.json").exists())

    def test_registration_rejects_an_unknown_planning_pane_without_fallback(self):
        status, payload, _ = self._register(plan_pane="pane-not-saved")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "unknown_plan_pane")
        self.assertEqual(payload["plan_pane"], "pane-not-saved")
        self.assertFalse((self.supervisor / "builds" / self.BUILD / "supervisor.json").exists())

    def test_registering_the_same_source_run_twice_is_idempotent(self):
        first = self._register()[1]
        status, payload, _ = self._register()
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["registered"])
        self.assertEqual(payload["event_id"], first["event_id"])
        self.assertEqual(len(self._supervisor_doc()["runs"]), 1)

    def test_registering_the_same_source_run_with_new_input_is_a_conflict(self):
        self.assertEqual(self._register()[0], 201)
        status, payload, _ = self._register(event_id="evt-writer-2")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "registration_conflict")
        self.assertEqual(self._supervisor_doc()["runs"][self.RUN]["event_id"], self.EVENT)

    def test_registering_one_source_run_against_two_builds_is_a_conflict(self):
        self.assertEqual(self._register()[0], 201)
        self._open_build("bld-sup-0002")
        status, payload, _ = self._register(build_id="bld-sup-0002")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "registration_conflict")

    def test_registration_requires_a_build_that_exists(self):
        status, payload, _ = self._register(build_id="bld-sup-9999")
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["code"], "unknown_build")

    def test_registration_refuses_a_stage_the_build_machine_does_not_have(self):
        status, payload, _ = self._register(expected_stage="daydreaming")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "bad_expected_stage")

    def test_an_interrupted_registration_is_repaired_by_the_identical_retry(self):
        # Two files back one registration and they cannot be one write, so the
        # index write can fail after the launch record lands. Nothing else ever
        # recreates that entry: the run would look registered and reconcile as
        # unsupervised forever. The idempotent retry is the repair.
        real_write = server.supervisor_write_json
        index_path = server.supervisor_run_index_path(self.RUN)

        def fail_the_index(path, doc):
            if Path(path) == Path(index_path):
                return {"ok": False, "error": "disk went away", "code": "supervisor_write_failed", "status": 500}
            return real_write(path, doc)

        with mock.patch.object(server, "supervisor_write_json", side_effect=fail_the_index):
            status, payload, _ = self._register()
        self.assertEqual(status, 500, payload)
        self.assertEqual(payload["code"], "supervisor_write_failed")
        self.assertFalse(index_path.exists())
        # The launch record is there, so this is the interrupted state, not a
        # registration that never happened.
        self.assertIn(self.RUN, self._supervisor_doc()["runs"])
        self.assertEqual(self._reconcile(status="failed")[0], 404, "unsupervised until repaired")

        status, payload, _ = self._register()
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["registered"])
        self.assertTrue(payload["index_repaired"])
        self.assertTrue(index_path.is_file())
        self.assertEqual(len(self._supervisor_doc()["runs"]), 1, "the repair adds no second launch")

        status, payload, _ = self._reconcile(status="failed")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["build_id"], self.BUILD)
        self.assertEqual(payload["action"], "retry_event")

    def test_a_repair_retry_never_repoints_a_run_at_another_build(self):
        # The repair rewrites an index entry, so it is the one place a
        # conflicting registration could quietly steal a run. It stays a 409.
        self.assertEqual(self._register()[0], 201)
        self._open_build("bld-sup-0003")
        status, payload, _ = self._register(build_id="bld-sup-0003")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "registration_conflict")
        mapped, err = server.supervisor_run_lookup(self.RUN)
        self.assertIsNone(err)
        self.assertEqual(mapped, self.BUILD)

    def test_registration_refuses_a_deadline_that_is_not_an_instant(self):
        # A duration would restart on every retried registration, so the
        # window it was meant to bound would never close.
        status, payload, _ = self._register(deadline="600")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "bad_deadline")

    # -- reconciliation: the resolved paths ---------------------------------

    def test_a_run_that_recorded_its_terminal_receipt_repeats_no_work(self):
        job = self._write_job("1" * 32, "running")
        self.assertEqual(self._register()[0], 201)
        self.assertEqual(self._advance("start_writer", self.EVENT)[0], 200)
        self.assertEqual(self._bind(job)[0], 200)
        self._write_job(job, "completed")
        status, payload, _ = self._reconcile(status="completed")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "receipt")
        self.assertEqual(payload["resolution"], "resolved")
        self.assertFalse(payload["workflow_fault"])
        self.assertEqual(payload["receipt"]["action"], "start_writer")
        self.assertEqual(payload["build_stage"], "writing")

    def test_a_failed_run_that_already_recorded_its_receipt_is_still_resolved(self):
        # The transition ran and then the graph fell over on the way out. The
        # receipt is the proof, not the status Rewst reported.
        job = self._write_job("2" * 32, "running")
        self.assertEqual(self._register()[0], 201)
        self.assertEqual(self._advance("start_writer", self.EVENT)[0], 200)
        self.assertEqual(self._bind(job)[0], 200)
        self._write_job(job, "completed")
        payload = self._reconcile(status="failed")[1]
        self.assertEqual(payload["action"], "receipt")
        self.assertFalse(payload["parked"])

    def test_completed_without_a_terminal_receipt_is_a_workflow_fault(self):
        # Rewst labels a graph `completed` even when it skipped the path it was
        # published to take, so `completed` alone is never success here.
        self.assertEqual(self._register()[0], 201)
        status, payload, _ = self._reconcile(status="completed")
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["parked"])
        self.assertTrue(payload["workflow_fault"])
        self.assertEqual(payload["park_reason"], "workflow_fault")
        self.assertEqual(payload["resolution"], "parked")
        self.assertIn("completed without recording event", payload["next_operator_action"])
        self.assertEqual(self._state()["stage"], "parked")
        self.assertEqual(self._state()["park_reason"], "workflow_fault")

    # -- reconciliation: the recovery ladder --------------------------------

    def test_an_unclaimed_event_retries_the_same_event_id(self):
        self.assertEqual(self._register()[0], 201)
        status, payload, _ = self._reconcile(status="failed")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["action"], "retry_event")
        self.assertEqual(payload["resolution"], "retry")
        self.assertEqual(payload["local_job_status"], "unclaimed")
        self.assertEqual(payload["event_id"], self.EVENT)
        self.assertEqual(payload["attempts"], 1)
        self.assertIn(f"event_id={self.EVENT}", payload["next_operator_action"])
        self.assertEqual(self._state()["stage"], "created", "a retry must not park the build")

    def test_an_active_local_job_is_watched_rather_than_relaunched(self):
        job = self._write_job("a" * 32, "running")
        self.assertEqual(self._register(job_id=job)[0], 201)
        status, payload, _ = self._reconcile(status="failed")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["action"], "watch")
        self.assertEqual(payload["resolution"], "watching")
        self.assertEqual(payload["local_job_status"], "active")
        self.assertEqual(payload["attempts"], 0, "watching must not spend a retry")
        self.assertEqual(self._supervisor_doc().get("circuits") or {}, {})

    def test_a_start_claim_receipt_does_not_hide_the_bound_active_job(self):
        job = self._write_job("9" * 32, "running")
        self.assertEqual(self._register()[0], 201)
        self.assertEqual(self._advance("start_writer", self.EVENT)[0], 200)
        self.assertEqual(self._bind(job)[0], 200)
        payload = self._reconcile(status="failed")[1]
        self.assertEqual(payload["action"], "watch")
        self.assertEqual(payload["local_job_status"], "active")
        self.assertIsNone(payload["receipt"])

    def test_an_unbound_start_claim_receipt_is_never_terminal_success(self):
        self.assertEqual(self._register()[0], 201)
        self.assertEqual(self._advance("start_writer", self.EVENT)[0], 200)
        payload = self._reconcile(status="failed")[1]
        self.assertEqual(payload["action"], "park")
        self.assertEqual(payload["park_reason"], "state_trace_disagreement")
        self.assertIsNone(payload["receipt"])

    def test_registration_bind_failure_watch_relaunch_is_reachable(self):
        job = self._write_job("8" * 32, "running")
        self.assertEqual(self._register()[0], 201)
        self.assertEqual(self._bind(job)[0], 200)
        self.assertEqual(self._reconcile(status="failed")[1]["action"], "watch")
        (self.jobs / job / "job.json").unlink()
        run = self._next_run(801)
        self.assertEqual(self._register(source_run_id=run)[0], 201)
        self.assertEqual(self._bind(job, run=run)[0], 200)
        payload = self._reconcile(status="failed", run=run)[1]
        self.assertEqual(payload["action"], "relaunch")
        self.assertEqual(payload["continuation"]["writer_prompt"], "Implement issue 52 ticket 06b only.")

    def test_a_claimed_job_that_never_started_is_relaunched_once(self):
        job = "b" * 32
        self.assertEqual(self._register(job_id=job)[0], 201)
        first = self._reconcile(status="failed")[1]
        self.assertEqual(first["action"], "relaunch")
        self.assertEqual(first["local_job_status"], "missing")
        self.assertIn("never started", first["next_operator_action"])
        # A second run for the same event, still with no job on disk, is not a
        # second relaunch: the seat is not coming back on its own.
        second = self._fresh_delivery(2, job_id=job)[1]
        self.assertEqual(second["action"], "park")
        self.assertEqual(second["park_reason"], "relaunch_exhausted")

    def test_a_finished_job_with_no_receipt_parks_on_the_disagreement(self):
        job = self._write_job("c" * 32, "completed")
        self.assertEqual(self._register(job_id=job)[0], 201)
        status, payload, _ = self._reconcile(status="failed")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["action"], "park")
        self.assertEqual(payload["park_reason"], "state_trace_disagreement")
        self.assertTrue(payload["files_intact"])
        self.assertEqual(payload["report"]["stacks"], ["riftwing-52"])

    def test_a_build_already_past_the_expected_stage_parks(self):
        self.assertEqual(self._register(expected_stage="created")[0], 201)
        # Something else advanced the build, and it did it without ever
        # recording this run's event id.
        self.assertEqual(self._advance("start_writer", "evt-other")[0], 200)
        payload = self._reconcile(status="failed")[1]
        self.assertEqual(payload["action"], "park")
        self.assertEqual(payload["park_reason"], "state_trace_disagreement")

    def test_declared_output_naming_another_build_parks_instead_of_repointing(self):
        self.assertEqual(self._register()[0], 201)
        payload = self._reconcile(status="failed", output={"build_id": "bld-sup-0002"})[1]
        self.assertEqual(payload["action"], "park")
        self.assertEqual(payload["park_reason"], "state_trace_disagreement")
        self.assertEqual(payload["build_id"], self.BUILD, "the registry, not the output, names the build")

    def test_a_run_that_failed_before_declaring_any_output_is_still_supervised(self):
        # The whole reason registration happens at launch: this run never got
        # far enough to say which build it was working on.
        self.assertEqual(self._register()[0], 201)
        body = self._completion(status="failed")
        body.pop("output")
        status, payload, _ = server.dispatch(
            "POST", "/v1/supervisor/reconcile", {}, True, json.dumps(body).encode()
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["build_id"], self.BUILD)
        self.assertEqual(payload["event_id"], self.EVENT)
        self.assertEqual(payload["action"], "retry_event")
        self.assertEqual(
            payload["continuation"],
            {
                **self._register_body()["continuation"],
                "workflow_slug": server.SUPERVISOR_SOURCE_SLUG,
                "build_id": self.BUILD,
                "event_id": self.EVENT,
                "expected_stage": "created",
                "deadline": self.DEADLINE,
                "plan_pane": self.PLAN_PANE,
                "reason": "retry_event",
                "attempt": 1,
                "bounded": True,
            },
        )

    def test_an_empty_declared_output_is_absent_not_a_disagreement(self):
        # A Rewst template renders a missing value as "", not as no key.
        self.assertEqual(self._register()[0], 201)
        payload = self._reconcile(status="failed", output={"build_id": "", "event_id": ""})[1]
        self.assertEqual(payload["action"], "retry_event")

    def test_real_retry_continuation_matches_the_strict_response_schema_and_graph_fields(self):
        self.assertEqual(self._register()[0], 201)
        payload = self._reconcile(status="failed")[1]
        spec = json.loads(server.openapi_bytes())
        schema = spec["components"]["schemas"]["SupervisorContinuationResponse"]
        continuation = payload["continuation"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(continuation), set(schema["required"]))
        graph = json.loads(
            (Path(__file__).resolve().parent / "graphs" / "build-completion-supervisor.json").read_text()
        )
        node = next(n for n in graph["spec"]["nodes"] if n["id"] == "continue_source")
        consumed = {
            value.removeprefix("{{ TASKS.reconcile.data.continuation.").removesuffix(" }}")
            for value in node["config"]["input"].values()
        }
        self.assertLessEqual(consumed, set(continuation))
        for field in (
            "workflow_slug", "build_id", "event_id", "expected_stage", "deadline",
            "plan_pane", "reason", "attempt", "bounded",
        ):
            self.assertIn(field, continuation)

    def test_a_deadline_that_has_passed_parks_rather_than_retrying(self):
        self.assertEqual(self._register(deadline="2020-01-01T00:00:00Z")[0], 201)
        payload = self._reconcile(status="failed")[1]
        self.assertEqual(payload["action"], "park")
        self.assertEqual(payload["park_reason"], "deadline_exceeded")

    def test_an_already_parked_build_keeps_its_original_park_reason(self):
        self.assertEqual(self._register()[0], 201)
        parked = self._advance("park", "evt-park")
        self.assertEqual(parked[0], 200, parked[1])
        payload = self._reconcile(status="failed")[1]
        self.assertEqual(payload["park_reason"], "build_parked")
        self.assertEqual(self._state()["park_reason"], "requested")

    # -- retry policy and the circuit ---------------------------------------

    def test_a_timeout_is_transient_and_retries_within_its_bound(self):
        for n, expected in ((1, "retry_event"), (2, "retry_event"), (3, "retry_event")):
            payload = self._fresh_delivery(n, status="timed_out")[1]
            self.assertEqual(payload["action"], expected, f"delivery {n}")
            self.assertEqual(payload["retry_budget"], server.SUPERVISOR_RETRY_BUDGET["transient"])
            self.assertEqual(payload["attempts"], n)
        spent = self._fresh_delivery(4, status="timed_out")[1]
        self.assertEqual(spent["action"], "park")
        self.assertEqual(spent["park_reason"], "retry_exhausted")

    def test_a_cancel_is_deterministic_and_never_retries_unchanged(self):
        self.assertEqual(self._register()[0], 201)
        payload = self._reconcile(status="canceled")[1]
        self.assertEqual(payload["action"], "park")
        self.assertEqual(payload["park_reason"], "deterministic_failure")
        self.assertEqual(payload["retry_budget"], 0)
        self.assertEqual(payload["attempts"], 0)
        self.assertIn("Do not re-run", payload["next_operator_action"])

    def test_a_caller_cannot_declare_its_own_retry_class(self):
        # The whole retry ladder hangs off this field, so a listener that could
        # set it could vote itself three more runs of a graph that is broken --
        # or, the other way, park a build on the first transient blip.
        self.assertEqual(self._register()[0], 201)
        status, payload, _ = self._reconcile(
            status="timed_out", failure={"class": "deterministic", "node": "agent1"}
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "bad_failure")
        self.assertIn("source_run.status", payload["error"])

    def test_the_retry_class_comes_from_the_terminal_status_alone(self):
        # Same body, different status, different budget: the status is the only
        # thing that moved, so the status is the only thing deciding.
        for n, (status, expected_class) in enumerate(
            (("timed_out", "transient"), ("canceled", "deterministic"), ("failed", "unknown")), start=1
        ):
            with self.subTest(status=status):
                self._open_build(f"bld-class-{n}")
                run = self._next_run(100 + n)
                reg = self._register(source_run_id=run, build_id=f"bld-class-{n}")
                self.assertEqual(reg[0], 201, reg[1])
                payload = self._reconcile(
                    status=status,
                    run=run,
                    output={"build_id": f"bld-class-{n}"},
                    failure={"node": "agent1", "code": "bad_schema"},
                )[1]
                self.assertEqual(payload["report"]["failure_class"], expected_class)
                self.assertEqual(
                    payload["retry_budget"], server.SUPERVISOR_RETRY_BUDGET[expected_class]
                )

    def test_a_deterministic_status_does_not_retry_and_names_the_node(self):
        self.assertEqual(self._register()[0], 201)
        payload = self._reconcile(status="canceled", failure={"node": "agent1", "code": "bad_schema"})[1]
        self.assertEqual(payload["action"], "park")
        self.assertEqual(payload["park_reason"], "deterministic_failure")
        self.assertIn("agent1", payload["next_operator_action"])

    def test_a_completion_that_names_no_node_reports_a_stable_label(self):
        # Not a blank and not a fresh value per delivery: an operator line that
        # says nothing is as unhelpful as a circuit that never trips.
        self.assertEqual(self._register()[0], 201)
        payload = self._reconcile(status="canceled")[1]
        self.assertEqual(payload["report"]["failed_node"], server.SUPERVISOR_UNKNOWN_NODE)
        self.assertIn(server.SUPERVISOR_UNKNOWN_NODE, payload["next_operator_action"])

    def test_an_unknown_failure_gets_exactly_one_retry_before_parking(self):
        first = self._fresh_delivery(1, status="failed")[1]
        self.assertEqual(first["action"], "retry_event")
        self.assertEqual(first["retry_budget"], server.SUPERVISOR_RETRY_BUDGET["unknown"])
        second = self._fresh_delivery(2, status="failed")[1]
        self.assertEqual(second["action"], "park")
        self.assertEqual(second["park_reason"], "retry_exhausted")

    def test_the_same_canonical_failure_giving_up_twice_opens_the_circuit(self):
        failure = {"node": "commit", "code": "bad_config"}
        first = self._fresh_delivery(1, status="canceled", failure=failure)[1]
        self.assertEqual(first["action"], "park")
        self.assertEqual(first["circuit"]["count"], 1)
        self.assertFalse(first["circuit"]["open"])
        second = self._fresh_delivery(2, status="canceled", failure=failure)[1]
        self.assertEqual(second["action"], "circuit_open")
        self.assertEqual(second["park_reason"], "circuit_open")
        self.assertTrue(second["circuit"]["open"])
        self.assertEqual(second["circuit"]["count"], server.SUPERVISOR_CIRCUIT_THRESHOLD)
        self.assertIn("stop retrying", second["next_operator_action"])
        self.assertEqual(
            second["circuit"]["signature"],
            f"{self.WORKFLOW}@{self.VERSION}#commit:bad_config",
        )

    def test_different_authoritative_failure_details_use_separate_circuits(self):
        first = self._fresh_delivery(1, status="canceled", failure={"node": "commit"})[1]
        self.assertEqual(first["circuit"]["count"], 1)
        second = self._fresh_delivery(
            2, status="canceled", failure={"node": "push", "code": "other_thing"}
        )[1]
        self.assertEqual(second["action"], "park")
        self.assertFalse(second["circuit"]["open"])
        self.assertNotEqual(second["circuit"]["signature"], first["circuit"]["signature"])
        self.assertEqual(len(self._supervisor_doc()["circuits"]), 2)

    def test_missing_failure_details_have_one_stable_status_and_presence_signature(self):
        first = self._fresh_delivery(1, status="canceled", error=False)[1]
        second = self._fresh_delivery(2, status="canceled", error=False)[1]
        self.assertEqual(second["action"], "circuit_open")
        self.assertEqual(first["circuit"]["signature"], second["circuit"]["signature"])
        self.assertEqual(
            first["circuit"]["signature"],
            f"{self.WORKFLOW}@{self.VERSION}#unknown:canceled:no_error",
        )

    def test_unknown_failure_identity_does_not_collide_across_versions_or_status_classes(self):
        first = self._fresh_delivery(1, status="canceled", error=True)[1]
        run = self._next_run(2)
        self.assertEqual(self._register(source_run_id=run, workflow_version="v12")[0], 201)
        second = self._reconcile(status="canceled", run=run, error=True)[1]
        third = self._fresh_delivery(3, status="completed", error=True)[1]
        signatures = {
            first["circuit"]["signature"],
            second["circuit"]["signature"],
            third["circuit"]["signature"],
        }
        self.assertEqual(len(signatures), 3)

    def test_circuit_map_uses_a_deterministic_overflow_identity_at_its_cap(self):
        with mock.patch.object(server, "SUPERVISOR_CIRCUITS_MAX", 3):
            outcomes = [
                self._fresh_delivery(
                    n,
                    status="canceled",
                    failure={"node": f"node{n}", "code": f"code_{n}"},
                )[1]
                for n in range(1, 5)
            ]
        circuits = self._supervisor_doc()["circuits"]
        self.assertLessEqual(len(circuits), 3)
        self.assertEqual(outcomes[2]["circuit"]["signature"], outcomes[3]["circuit"]["signature"])
        self.assertIn("#overflow:canceled:error", outcomes[2]["circuit"]["signature"])

    def test_watching_a_live_job_never_counts_against_the_circuit(self):
        job = self._write_job("d" * 32, "queued")
        for n in (1, 2, 3):
            payload = self._fresh_delivery(n, job_id=job)[1]
            self.assertEqual(payload["action"], "watch", f"delivery {n}")
            self.assertIsNone(payload["circuit"])

    # -- duplicate delivery -------------------------------------------------

    def test_a_duplicate_delivery_replays_the_first_answer(self):
        self.assertEqual(self._register()[0], 201)
        first = self._reconcile(status="failed")[1]
        self.assertEqual(first["attempts"], 1)
        status, second, _ = self._reconcile(status="failed")
        self.assertEqual(status, 200, second)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["action"], "ignored")
        self.assertIsNone(second["continuation"], "the same completion cannot launch again")
        self.assertEqual(second["attempts"], 1, "a redelivery must not spend a second retry")
        self.assertEqual(len(self._supervisor_doc()["reports"]), 1)

    def test_a_duplicate_delivery_with_different_input_is_a_conflict(self):
        self.assertEqual(self._register()[0], 201)
        self.assertEqual(self._reconcile(status="failed")[0], 200)
        status, payload, _ = self._reconcile(status="timed_out")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "idempotency_conflict")

    def test_concurrent_reconcile_spends_one_retry_and_records_one_side_effect(self):
        self.assertEqual(self._register()[0], 201)
        barrier = threading.Barrier(3)
        results = []

        def reconcile():
            barrier.wait()
            results.append(self._reconcile(status="failed")[:2])

        threads = [threading.Thread(target=reconcile) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(results), 2)
        self.assertEqual({payload["action"] for _, payload in results}, {"retry_event", "ignored"})
        doc = self._supervisor_doc()
        self.assertEqual(doc["events"][self.EVENT]["attempts"], 1)
        self.assertEqual(len(doc["reports"]), 1)

    def test_a_second_listener_version_replays_rather_than_re_deciding(self):
        # Republishing the listener is routine, and Rewst's outbox can deliver
        # the same completion to the new version. The dedupe key is the run
        # plus the listener *slug* precisely so that costs nothing: keying on
        # the version would spend a second retry, write a second park, and tick
        # the circuit again, all for one source run.
        self.assertEqual(self._register()[0], 201)
        first = self._reconcile(status="failed")[1]
        self.assertEqual(first["listener_slug"], server.SUPERVISOR_LISTENER_SLUG)
        status, payload, _ = self._reconcile(
            status="failed", listener_id="graphwing-build-completion-supervisor@8"
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["replayed"])
        self.assertEqual(payload["action"], "ignored")
        self.assertIsNone(payload["continuation"])
        self.assertEqual(payload["attempts"], first["attempts"])
        self.assertEqual(
            payload["listener_id"], self.LISTENER, "the replay is the first answer, unedited"
        )
        doc = self._supervisor_doc()
        self.assertEqual(len(doc["reports"]), 1)
        self.assertEqual(len(doc["deliveries"]), 1)
        self.assertEqual(doc["events"][self.EVENT]["attempts"], 1)

    def test_a_republished_listener_cannot_park_a_build_a_second_time(self):
        self.assertEqual(self._register()[0], 201)
        first = self._reconcile(status="canceled")[1]
        self.assertEqual(first["park_reason"], "deterministic_failure")
        self.assertEqual(first["circuit"]["count"], 1)
        payload = self._reconcile(
            status="canceled", listener_id="graphwing-build-completion-supervisor@9"
        )[1]
        self.assertTrue(payload["replayed"])
        self.assertEqual(payload["circuit"]["count"], 1)
        self.assertFalse(payload["circuit"]["open"], "a republish must not trip the breaker")
        self.assertEqual(self._supervisor_doc()["circuits"][first["circuit"]["signature"]]["count"], 1)

    # -- the crash window between the build write and the supervisor write ---

    def test_a_park_whose_delivery_write_was_lost_replays_on_redelivery(self):
        self.assertEqual(self._register()[0], 201)
        # The park reaches the build; the supervisor write that would record
        # the delivery does not.
        with mock.patch.object(
            server,
            "supervisor_save",
            return_value={"ok": False, "error": "disk went away", "code": "supervisor_write_failed", "status": 500},
        ):
            status, payload, _ = self._reconcile(status="canceled")
        self.assertEqual(status, 500, payload)
        self.assertEqual(self._state()["stage"], "parked", "the park itself did land")
        self.assertEqual(self._state()["park_reason"], "deterministic_failure")
        doc = self._supervisor_doc()
        self.assertEqual(doc.get("deliveries") or {}, {}, "no delivery was recorded")
        self.assertEqual(doc.get("circuits") or {}, {}, "no give-up was recorded")

        # The outbox redelivers. Without the receipt check this walks the
        # ladder again and charges the circuit for one give-up twice.
        status, payload, _ = self._reconcile(status="canceled")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["parked"])
        self.assertTrue(payload["park_replayed"])
        self.assertEqual(payload["park_reason"], "deterministic_failure")
        self.assertIsNone(payload["circuit"], "the replay must not tick the breaker")
        self.assertEqual(payload["attempts"], 0, "the replay must not spend a retry")
        self.assertEqual(self._state()["park_reason"], "deterministic_failure")
        self.assertEqual(self._supervisor_doc().get("circuits") or {}, {})

    def test_a_lost_park_write_is_reported_rather_than_recorded_as_parked(self):
        # The other half of the same window: if the *build* write fails, the
        # delivery must not be recorded as a park either, or every later
        # redelivery replays "parked" for a build that never was.
        self.assertEqual(self._register()[0], 201)
        with mock.patch.object(
            server,
            "write_build",
            return_value={"ok": False, "error": "disk went away", "code": "state_write_failed", "status": 500},
        ):
            status, payload, _ = self._reconcile(status="canceled")
        self.assertEqual(status, 500, payload)
        self.assertEqual(self._state()["stage"], "created")
        self.assertEqual(self._supervisor_doc().get("deliveries") or {}, {})

    def test_a_redelivery_after_a_lost_write_does_not_relaunch_twice(self):
        # The relaunch budget is spent in supervisor state, so a lost write
        # leaves it unspent -- and the redelivery has to reach the same answer
        # rather than a second, uncounted relaunch.
        job = "f" * 32
        self.assertEqual(self._register(job_id=job)[0], 201)
        with mock.patch.object(
            server,
            "supervisor_save",
            return_value={"ok": False, "error": "disk went away", "code": "supervisor_write_failed", "status": 500},
        ):
            self.assertEqual(self._reconcile(status="failed")[0], 500)
        self.assertEqual(self._supervisor_doc().get("events") or {}, {}, "nothing was spent")
        payload = self._reconcile(status="failed")[1]
        self.assertEqual(payload["action"], "relaunch")
        self.assertEqual(self._supervisor_doc()["events"][self.EVENT]["relaunches"], 1)
        # And the relaunch is still bounded at one across the whole event.
        second = self._fresh_delivery(2, job_id=job)[1]
        self.assertEqual(second["park_reason"], "relaunch_exhausted")

    # -- refusals -----------------------------------------------------------

    def test_a_run_with_no_launch_record_is_not_supervised(self):
        status, payload, _ = self._reconcile(status="failed")
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["code"], "unknown_source_run")

    def test_the_supervisor_cannot_listen_to_itself(self):
        self.assertEqual(self._register()[0], 201)
        status, payload, _ = self._reconcile(
            status="failed", source_workflow={"slug": server.SUPERVISOR_LISTENER_SLUG}
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "self_listener")

    def test_a_supervised_tag_naming_the_listener_is_also_refused(self):
        self.assertEqual(self._register()[0], 201)
        status, payload, _ = self._reconcile(
            status="failed", source_workflow={"tags": [server.SUPERVISOR_LISTENER_SLUG]}
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "self_listener")

    def test_tags_accept_multiple_empty_and_the_exact_maximum(self):
        for n, tags in enumerate(
            (
                [],
                ["graphwing-supervised", "pre-pr", "issue-52"],
                [f"tag-{i}" for i in range(server.SUPERVISOR_TAGS_MAX)],
            ),
            start=1,
        ):
            with self.subTest(count=len(tags)):
                run = self._next_run(800 + n)
                self.assertEqual(self._register(source_run_id=run)[0], 201)
                status, payload, _ = self._reconcile(
                    status="failed", run=run, source_workflow={"tags": tags}
                )
                self.assertEqual(status, 200, payload)

    def test_tags_reject_arbitrary_encoded_strings_malformed_json_and_non_strings(self):
        for n, tags in enumerate(
            ("graphwing-supervised", "['graphwing-supervised']", "{bad", '["ok", 7]'),
            start=1,
        ):
            with self.subTest(tags=tags):
                run = self._next_run(810 + n)
                self.assertEqual(self._register(source_run_id=run)[0], 201)
                status, payload, _ = self._reconcile(
                    status="failed", run=run, source_workflow={"tags": tags}
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "bad_tags")

    def test_tags_reject_non_string_elements_and_more_than_the_maximum(self):
        for n, tags in enumerate(
            (["ok", 7], ["ok"] * (server.SUPERVISOR_TAGS_MAX + 1)), start=1
        ):
            with self.subTest(case=n):
                run = self._next_run(820 + n)
                self.assertEqual(self._register(source_run_id=run)[0], 201)
                status, payload, _ = self._reconcile(
                    status="failed", run=run, source_workflow={"tags": tags}
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "bad_tags")

    def test_a_completion_from_a_different_workflow_parks(self):
        self.assertEqual(self._register()[0], 201)
        payload = self._reconcile(status="failed", source_workflow={"id": "wf-something-else"})[1]
        self.assertEqual(payload["action"], "park")
        self.assertEqual(payload["park_reason"], "state_trace_disagreement")

    def test_a_non_terminal_status_is_refused(self):
        self.assertEqual(self._register()[0], 201)
        status, payload, _ = self._reconcile(status="running")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "bad_source_status")

    def test_a_preview_run_is_not_supervised_work(self):
        self.assertEqual(self._register()[0], 201)
        payload = self._reconcile(status="failed", source_run={"execution_mode": "preview"})[1]
        self.assertEqual(payload["action"], "ignored")
        self.assertEqual(payload["resolution"], "ignored")
        self.assertEqual(self._state()["stage"], "created")

    def test_raw_error_text_is_refused_rather_than_redacted(self):
        self.assertEqual(self._register()[0], 201)
        status, payload, _ = self._reconcile(
            status="failed",
            error={"present": True, "message": "Traceback: KeyError 'build_id'"},
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "raw_error_forbidden")
        self.assertFalse((self.supervisor / "builds" / self.BUILD / "supervisor.json").read_text().count("Traceback"))

    def test_a_trace_body_smuggled_through_output_is_refused(self):
        self.assertEqual(self._register()[0], 201)
        status, payload, _ = self._reconcile(
            status="failed", output={"stack": "at handler (server.py:12)"}
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "raw_error_forbidden")

    def test_error_presence_survives_a_jinja_render(self):
        # A Rewst node config is a string template, so the flag arrives as the
        # word, not as a JSON boolean.
        self.assertEqual(self._register()[0], 201)
        payload = self._reconcile(status="failed", error="true")[1]
        self.assertTrue(payload["report"]["error_present"])

    def test_a_free_text_failure_code_is_refused(self):
        self.assertEqual(self._register()[0], 201)
        status, payload, _ = self._reconcile(
            status="failed", failure={"code": "connection reset by peer at 10.0.0.1"}
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "bad_failure_code")

    def test_no_credential_or_callback_url_reaches_supervisor_state(self):
        self.assertEqual(self._register()[0], 201)
        secret = "https://engine.rewst.io/webhooks/custom/9f2c41ab-77d0-4a1e-9d0e-5c1f0b2a7e33"
        payload = self._reconcile(status="failed", output={"kick_url": secret, "token": "ghp_" + "a" * 32})[1]
        self.assertEqual(payload["action"], "retry_event")
        raw = (self.supervisor / "builds" / self.BUILD / "supervisor.json").read_text()
        self.assertNotIn(secret, raw)
        self.assertNotIn("ghp_", raw)

    # -- the planning-session report ----------------------------------------

    def test_every_park_reports_what_the_operator_has_to_do_next(self):
        self.assertEqual(self._register(job_id=self._write_job("e" * 32, "failed"))[0], 201)
        payload = self._reconcile(status="failed")[1]
        report = payload["report"]
        for field in (
            "source_run_id",
            "workflow_version",
            "failed_node",
            "build_stage",
            "local_job_status",
            "action",
            "next_operator_action",
        ):
            self.assertIn(field, report, field)
        self.assertEqual(report["source_run_id"], self.RUN)
        self.assertEqual(report["workflow_version"], self.VERSION)
        self.assertEqual(report["build_stage"], "parked")
        self.assertEqual(report["local_job_status"], "failed")
        self.assertEqual(report["action"], "park")
        self.assertTrue(report["next_operator_action"])
        self.assertTrue(report["files_intact"])

    def test_the_saved_report_survives_the_run_that_produced_it(self):
        self.assertEqual(self._register()[0], 201)
        self.assertEqual(self._reconcile(status="failed")[0], 200)
        status, payload, _ = self._report(build_id=self.BUILD)
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(payload["reports"]), 1)
        self.assertEqual(payload["reports"][0]["action"], "retry_event")
        self.assertEqual(payload["circuit_threshold"], server.SUPERVISOR_CIRCUIT_THRESHOLD)
        self.assertEqual(payload["run_count"], 1)

    def test_a_report_can_be_found_by_source_run_id_alone(self):
        self.assertEqual(self._register()[0], 201)
        self.assertEqual(self._reconcile(status="failed")[0], 200)
        self.assertEqual(self._fresh_delivery(2)[0], 200)
        status, payload, _ = self._report(source_run_id=self.RUN)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["build_id"], self.BUILD)
        self.assertEqual([r["source_run_id"] for r in payload["reports"]], [self.RUN])

    def test_a_report_for_an_unregistered_run_is_a_404(self):
        status, payload, _ = self._report(source_run_id="rewst-run-99999999")
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["code"], "unknown_source_run")

    def test_the_report_endpoint_needs_a_selector(self):
        status, payload, _ = self._report()
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "missing_selector")

    # -- restart ------------------------------------------------------------

    def test_supervision_survives_a_service_restart(self):
        self.assertEqual(self._register()[0], 201)
        first = self._reconcile(status="failed")[1]
        # Nothing in memory: every later answer comes off disk.
        status, payload, _ = self._report(build_id=self.BUILD)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["reports"][0]["next_operator_action"], first["next_operator_action"])
        self.assertEqual(payload["events"][self.EVENT]["attempts"], 1)

    # -- unauthenticated ----------------------------------------------------

    def test_the_supervisor_operations_require_the_api_key(self):
        for method, path, body in (
            ("POST", "/v1/supervisor/register", json.dumps(self._register_body()).encode()),
            ("POST", "/v1/supervisor/bind", json.dumps({"source_run_id": self.RUN, "job_id": "a" * 32}).encode()),
            ("POST", "/v1/supervisor/reconcile", json.dumps(self._completion()).encode()),
            ("POST", "/v1/supervisor/watch", json.dumps({"source_run_id": self.RUN}).encode()),
            ("GET", "/v1/supervisor/report", b""),
        ):
            with self.subTest(path=path):
                status, payload, _ = server.dispatch(method, path, {}, False, body)
                self.assertEqual(status, 401, payload)
                self.assertEqual(payload["code"], "unauthorized")

    def test_watch_request_rejects_fields_outside_the_openapi_contract(self):
        for extra in (
            {"response_webhook_url": "https://example.invalid/resume"},
            {"response_webhook_token": "callback-token"},
            {"deadline": "2099-01-01T00:00:00Z"},
        ):
            with self.subTest(extra=extra):
                status, payload, _ = self._watch(**extra)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "bad_watch_request")

    def test_watch_start_and_replay_always_call_the_deduping_scheduler(self):
        job = self._write_job("1" * 32, "running")
        self.assertEqual(self._register(job_id=job)[0], 201)
        with mock.patch.object(server, "supervisor_schedule_watch") as schedule:
            status, first, _ = self._watch()
            replay_status, replay, _ = self._watch()
        self.assertEqual(status, 202, first)
        self.assertTrue(first["started"])
        self.assertEqual(replay_status, 202, replay)
        self.assertFalse(replay["started"])
        self.assertEqual(replay["watch_id"], first["watch_id"])
        self.assertEqual(schedule.call_count, 2)
        saved = self._supervisor_doc()["watches"]
        self.assertEqual(len(saved), 1)
        raw = json.dumps(saved).lower()
        for forbidden in ("response_webhook", "resumeurl", "resumetoken", "callback", "token"):
            self.assertNotIn(forbidden, raw)

    def test_process_restart_recovers_a_watching_record(self):
        job = self._write_job("2" * 32, "running")
        self.assertEqual(self._register(job_id=job)[0], 201)
        with mock.patch.object(server, "supervisor_schedule_watch"):
            self.assertEqual(self._watch()[0], 202)
        with mock.patch.object(server, "supervisor_schedule_watch") as schedule:
            server.supervisor_recover_watches()
        schedule.assert_called_once()
        args = schedule.call_args.args
        self.assertEqual(args[0], self.BUILD)
        self.assertIn(self.RUN, args[1])

    def test_reconcile_makes_watch_durable_before_the_graph_watch_handoff(self):
        job = self._write_job("2" * 32, "running")
        self.assertEqual(self._register(job_id=job)[0], 201)
        with mock.patch.object(server, "supervisor_schedule_watch") as schedule:
            status, payload, _ = self._reconcile(status="failed")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["action"], "watch")
        key = f"{self.RUN}|{self.EVENT}"
        saved = self._supervisor_doc()["watches"][key]
        self.assertEqual(saved["state"], "watching")
        self.assertEqual(saved["job_id"], job)
        schedule.assert_called_once_with(self.BUILD, key)
        raw = json.dumps(saved).lower()
        for forbidden in ("response_webhook", "resumeurl", "resumetoken", "callback", "token"):
            self.assertNotIn(forbidden, raw)
        with mock.patch.object(server, "supervisor_schedule_watch") as acknowledge_schedule:
            ack_status, acknowledgement, _ = self._watch()
        self.assertEqual(ack_status, 202, acknowledgement)
        self.assertFalse(acknowledgement["started"])
        self.assertEqual(acknowledgement["watch_id"], saved["watch_id"])
        acknowledge_schedule.assert_called_once_with(self.BUILD, key)

        # The reconcile response was lost before POST /watch. A fresh process
        # recovers the durable row and settles it without that graph hop.
        self.assertEqual(self._advance("start_writer", self.EVENT)[0], 200)
        self._write_job(job, "completed")
        with mock.patch.object(
            server, "supervisor_schedule_watch", side_effect=server.supervisor_watch_worker
        ) as recovered:
            self.assertEqual(server.supervisor_recover_watches(), 1)
        recovered.assert_called_once_with(self.BUILD, key)
        settled = self._supervisor_doc()["watches"][key]
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(settled["action"], "receipt")

    def test_replayed_watch_delivery_repairs_a_missing_durable_row(self):
        job = self._write_job("8" * 32, "running")
        self.assertEqual(self._register(job_id=job)[0], 201)
        with mock.patch.object(server, "supervisor_schedule_watch"):
            first = self._reconcile(status="failed")[1]
        self.assertEqual(first["action"], "watch")
        key = f"{self.RUN}|{self.EVENT}"
        with server.SUPERVISOR_LOCK:
            doc, err = server.supervisor_load(self.BUILD)
            self.assertIsNone(err)
            doc["watches"].pop(key)
            doc["watch_order"].remove(key)
            self.assertIsNone(server.supervisor_save(doc))

        with mock.patch.object(server, "supervisor_schedule_watch") as schedule:
            status, replay, _ = self._reconcile(status="failed")
        self.assertEqual(status, 200, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["action"], "ignored")
        repaired = self._supervisor_doc()["watches"][key]
        self.assertEqual(repaired["state"], "watching")
        self.assertEqual(repaired["job_id"], job)
        schedule.assert_called_once_with(self.BUILD, key)

    def test_concurrent_reconcile_and_watch_ack_schedule_one_watcher(self):
        job = self._write_job("9" * 32, "running")
        self.assertEqual(self._register(job_id=job)[0], 201)
        barrier = threading.Barrier(3)
        results = []

        def reconcile():
            barrier.wait()
            results.append(self._reconcile(status="failed")[:2])

        def acknowledge():
            barrier.wait()
            results.append(self._watch()[:2])

        workers = [threading.Thread(target=reconcile), threading.Thread(target=acknowledge)]
        key = f"{self.RUN}|{self.EVENT}"
        identity = (self.BUILD, key)
        with server.SUPERVISOR_WATCHER_LOCK:
            server.SUPERVISOR_ACTIVE_WATCHERS.discard(identity)
        try:
            with mock.patch.object(
                server, "supervisor_schedule_watch", side_effect=self._real_schedule_watch
            ), mock.patch.object(server.threading, "Thread") as watcher_thread:
                watcher_thread.return_value.start.return_value = None
                for worker in workers:
                    worker.start()
                barrier.wait()
                for worker in workers:
                    worker.join(timeout=5)
            self.assertEqual(len(results), 2)
            self.assertEqual({status for status, _ in results}, {200, 202})
            self.assertEqual(watcher_thread.call_count, 1)
            self.assertEqual(len(self._supervisor_doc()["watches"]), 1)
        finally:
            with server.SUPERVISOR_WATCHER_LOCK:
                server.SUPERVISOR_ACTIVE_WATCHERS.discard(identity)

    def test_reconcile_persists_pending_delivery_before_send_and_recovery_retries_it(self):
        self.assertEqual(self._register()[0], 201)
        with mock.patch.object(server, "supervisor_attempt_delivery") as attempt:
            self.assertEqual(self._reconcile(status="failed")[0], 200)
        report = self._supervisor_doc()["reports"][0]
        self.assertEqual(report["delivery"]["state"], "pending")
        self.assertRegex(report["delivery"]["delivery_id"], r"^[0-9a-f]{64}$")
        attempt.assert_called_once_with(self.BUILD, report["delivery"]["delivery_id"])
        delivered = {"ok": True, "attempted": True, "code": None, "pane_id": self.PLAN_PANE}
        with mock.patch.object(server, "herdr_plan_deliver", return_value=delivered) as send:
            server.supervisor_recover_deliveries()
        send.assert_called_once()
        saved = self._supervisor_doc()["reports"][0]["delivery"]
        self.assertEqual(saved["state"], "delivered")

    def test_delivery_crash_after_send_remains_pending_and_recovery_is_at_least_once(self):
        self.assertEqual(self._register()[0], 201)
        with mock.patch.object(server, "supervisor_attempt_delivery"):
            self.assertEqual(self._reconcile(status="failed")[0], 200)
        delivery_id = self._supervisor_doc()["reports"][0]["delivery"]["delivery_id"]
        receipt = {"ok": True, "attempted": True, "code": None, "pane_id": self.PLAN_PANE}
        with mock.patch.object(server, "herdr_plan_deliver", return_value=receipt), mock.patch.object(
            server, "supervisor_save", return_value={"status": 500, "code": "supervisor_write_failed"}
        ):
            server.supervisor_attempt_delivery(self.BUILD, delivery_id)
        self.assertEqual(self._supervisor_doc()["reports"][0]["delivery"]["state"], "pending")
        with mock.patch.object(server, "herdr_plan_deliver", return_value=receipt) as resend:
            server.supervisor_recover_deliveries()
        resend.assert_called_once()
        saved = self._supervisor_doc()["reports"][0]["delivery"]
        self.assertEqual(saved["state"], "delivered")
        self.assertTrue(saved["at_least_once"])

    def test_terminal_job_with_expected_build_receipt_resolves_watch(self):
        job = self._write_job("3" * 32, "running")
        self.assertEqual(self._register(job_id=job)[0], 201)
        with mock.patch.object(server, "supervisor_schedule_watch"):
            watch = self._watch()[1]
        self.assertEqual(self._advance("start_writer", self.EVENT)[0], 200)
        self._write_job(job, "completed")
        server.supervisor_watch_worker(self.BUILD, f"{self.RUN}|{self.EVENT}")
        saved = self._supervisor_doc()["watches"][f"{self.RUN}|{self.EVENT}"]
        self.assertEqual(saved["state"], "settled")
        self.assertEqual(saved["action"], "receipt")
        self.assertEqual(saved["watch_id"], watch["watch_id"])
        self.assertEqual(saved["build_receipt"]["action"], "start_writer")
        self.assertEqual(self._state()["stage"], "writing")

    def test_failed_job_without_receipt_parks_before_delivery(self):
        job = self._write_job("4" * 32, "running")
        self.assertEqual(self._register(job_id=job)[0], 201)
        with mock.patch.object(server, "supervisor_schedule_watch"):
            self.assertEqual(self._watch()[0], 202)
        self._write_job(job, "failed")
        with mock.patch.object(server, "SUPERVISOR_WATCH_SETTLE_SECONDS", 0):
            server.supervisor_watch_worker(self.BUILD, f"{self.RUN}|{self.EVENT}")
        self.assertEqual(self._state()["stage"], "parked")
        self.assertEqual(self._state()["park_reason"], "state_trace_disagreement")
        saved = self._supervisor_doc()["watches"][f"{self.RUN}|{self.EVENT}"]
        self.assertEqual(saved["action"], "park")
        self.assertEqual(saved["park_reason"], "state_trace_disagreement")
        self.assertEqual(saved["state"], "settled")
        self.assertIn("delivery", saved)

    def test_expired_watch_parks_fail_closed(self):
        job = self._write_job("5" * 32, "running")
        self.assertEqual(self._register(job_id=job, deadline="2020-01-01T00:00:00Z")[0], 201)
        with mock.patch.object(server, "supervisor_schedule_watch"):
            self.assertEqual(self._watch()[0], 202)
        server.supervisor_watch_worker(self.BUILD, f"{self.RUN}|{self.EVENT}")
        self.assertEqual(self._state()["stage"], "parked")
        saved = self._supervisor_doc()["watches"][f"{self.RUN}|{self.EVENT}"]
        self.assertEqual(saved["action"], "park")
        self.assertEqual(saved["park_reason"], "deadline_exceeded")

    def test_delivery_failure_is_saved_after_the_watch_parks(self):
        job = self._write_job("6" * 32, "failed")
        self.assertEqual(self._register(job_id=job)[0], 201)
        with mock.patch.object(server, "supervisor_schedule_watch"):
            self.assertEqual(self._watch()[0], 202)
        failed = {"ok": False, "attempted": True, "code": "herdr_send_failed", "pane_id": self.PLAN_PANE}
        with mock.patch.object(server, "SUPERVISOR_WATCH_SETTLE_SECONDS", 0), mock.patch.object(
            server, "herdr_plan_deliver", return_value=failed
        ):
            server.supervisor_watch_worker(self.BUILD, f"{self.RUN}|{self.EVENT}")
        self.assertEqual(self._state()["stage"], "parked", "delivery cannot undo settlement")
        saved = self._supervisor_doc()["watches"][f"{self.RUN}|{self.EVENT}"]
        self.assertFalse(saved["delivery"]["ok"])
        self.assertEqual(saved["delivery"]["state"], "pending")
        self.assertEqual(saved["delivery"]["receipt"], failed)

    def test_watch_park_write_failure_stays_watching_and_schedules_in_process_retry(self):
        job = self._write_job("a" * 32, "failed")
        self.assertEqual(self._register(job_id=job)[0], 201)
        with mock.patch.object(server, "supervisor_schedule_watch"):
            self.assertEqual(self._watch()[0], 202)
        failed = {"ok": False, "error": "disk", "code": "state_write_failed", "status": 500}
        with mock.patch.object(server, "SUPERVISOR_WATCH_SETTLE_SECONDS", 0), mock.patch.object(
            server, "write_build", return_value=failed
        ), mock.patch.object(server, "supervisor_schedule_watch_retry") as retry:
            server.supervisor_watch_worker(self.BUILD, f"{self.RUN}|{self.EVENT}")
        saved = self._supervisor_doc()["watches"][f"{self.RUN}|{self.EVENT}"]
        self.assertEqual(saved["state"], "watching")
        self.assertEqual(saved["settlement_error"]["code"], "state_write_failed")
        retry.assert_called_once_with(self.BUILD, f"{self.RUN}|{self.EVENT}")

    def test_watch_worker_exception_stays_watching_and_schedules_in_process_retry(self):
        job = self._write_job("b" * 32, "running")
        self.assertEqual(self._register(job_id=job)[0], 201)
        with mock.patch.object(server, "supervisor_schedule_watch"):
            self.assertEqual(self._watch()[0], 202)
        with mock.patch.object(server, "supervisor_watch_settle", side_effect=RuntimeError("boom")), mock.patch.object(
            server, "supervisor_schedule_watch_retry"
        ) as retry:
            server.supervisor_watch_worker(self.BUILD, f"{self.RUN}|{self.EVENT}")
        saved = self._supervisor_doc()["watches"][f"{self.RUN}|{self.EVENT}"]
        self.assertEqual(saved["state"], "watching")
        self.assertEqual(saved["worker_error"]["code"], "watch_worker_failed")
        retry.assert_called_once_with(self.BUILD, f"{self.RUN}|{self.EVENT}")

    def test_running_to_terminal_race_cannot_strand_the_build(self):
        job = self._write_job("7" * 32, "running")
        self.assertEqual(self._register(job_id=job)[0], 201)
        with mock.patch.object(server, "supervisor_schedule_watch"):
            self.assertEqual(self._watch()[0], 202)
        receipt = {"action": "start_writer", "event_id": self.EVENT}
        with mock.patch.object(
            server,
            "supervisor_job_snapshot",
            side_effect=[
                ("active", {"job_id": job, "status": "running"}),
                ("completed", {"job_id": job, "status": "completed"}),
                ("completed", {"job_id": job, "status": "completed"}),
            ],
        ), mock.patch.object(server, "supervisor_event_receipt", return_value=("writing", receipt)), mock.patch.object(
            server.time, "time", side_effect=[0.0, 10_000_000_000.0]
        ):
            server.supervisor_watch_worker(self.BUILD, f"{self.RUN}|{self.EVENT}")
        saved = self._supervisor_doc()["watches"][f"{self.RUN}|{self.EVENT}"]
        self.assertEqual(saved["action"], "receipt")
        self.assertEqual(saved["state"], "settled")

    # -- catalog and graph --------------------------------------------------

    def test_openapi_declares_the_supervisor_operations(self):
        spec = json.loads(server.openapi_bytes())
        for route, method, op_id in (
            ("/v1/supervisor/register", "post", "supervisorRegister"),
            ("/v1/supervisor/bind", "post", "supervisorBind"),
            ("/v1/supervisor/reconcile", "post", "supervisorReconcile"),
            ("/v1/supervisor/watch", "post", "supervisorWatch"),
            ("/v1/supervisor/report", "get", "supervisorReport"),
        ):
            self.assertIn(route, spec["paths"])
            self.assertEqual(spec["paths"][route][method]["operationId"], op_id)
        schemas = spec["components"]["schemas"]
        self.assertEqual(
            sorted(schemas["SupervisorRegisterRequest"]["required"]),
            [
                "build_id",
                "continuation",
                "deadline",
                "event_id",
                "expected_stage",
                "plan_pane",
                "source_run_id",
                "workflow_id",
                "workflow_version",
            ],
        )
        self.assertEqual(
            set(schemas["SupervisorRegisterRequest"]["properties"]["expected_stage"]["enum"]),
            set(server.BUILD_STAGE_ORDER),
        )
        reconcile = schemas["SupervisorReconcileRequest"]["properties"]
        self.assertEqual(
            set(reconcile["source_run"]["properties"]["status"]["enum"]),
            set(server.SUPERVISOR_TERMINAL_STATUSES),
        )
        self.assertEqual(
            set(reconcile["source_run"]["properties"]["execution_mode"]["enum"]),
            set(server.SUPERVISOR_EXECUTION_MODES),
        )
        # No caller-settable retry class in the contract. Authenticated,
        # bounded node/code metadata may refine circuit identity, never budget.
        self.assertEqual(set(reconcile["failure"]["properties"]), {"node", "code"})
        tags = reconcile["source_workflow"]["properties"]["tags"]
        self.assertEqual(tags["type"], "array")
        self.assertEqual(tags["maxItems"], server.SUPERVISOR_TAGS_MAX)
        self.assertEqual(
            set(schemas["SupervisorPlanReport"]["properties"]["failure_class"]["enum"]),
            set(server.SUPERVISOR_FAILURE_CLASSES),
        )
        watch_request = schemas["SupervisorWatchRequest"]
        self.assertFalse(watch_request["additionalProperties"])
        self.assertEqual(watch_request["required"], ["source_run_id"])
        self.assertEqual(set(watch_request["properties"]), {"source_run_id"})
        # The strict contract has to admit exactly what a Jinja render of a
        # boolean produces, and nothing wider.
        error_forms = reconcile["error"]["oneOf"]
        rendered = next(o for o in error_forms if o.get("type") == "string")
        self.assertEqual(set(rendered["enum"]), {"true", "false", ""})
        # An error object that allows extra keys is an error object someone
        # will put a stack trace in.
        error_object = next(option for option in error_forms if option["type"] == "object")
        self.assertFalse(error_object["additionalProperties"])
        self.assertFalse(reconcile["failure"]["additionalProperties"])
        actions = set(schemas["SupervisorReconcile"]["properties"]["action"]["enum"])
        self.assertEqual(
            actions,
            {"receipt", "watch", "relaunch", "retry_event", "park", "circuit_open", "ignored"},
        )
        self.assertIn("supervisor", {tag["name"] for tag in spec["tags"]})

    def test_every_graphwing_action_in_the_new_graphs_exists_in_openapi(self):
        spec = json.loads(server.openapi_bytes())
        root = Path(__file__).resolve().parent / "graphs"
        for name in ("pre-pr-build", "build-completion-supervisor"):
            graph = json.loads((root / f"{name}.json").read_text())
            for node in graph["spec"]["nodes"]:
                match = re.fullmatch(r"action\.graphwing\.([A-Z]+):(.+)", node["type"])
                if not match:
                    continue
                method, route = match.groups()
                self.assertIn(route, spec["paths"], f"{name}:{node['id']} route")
                self.assertIn(method.lower(), spec["paths"][route], f"{name}:{node['id']} method")

    def test_the_listener_graph_is_a_completion_trigger_that_cannot_self_kick(self):
        graph = json.loads(
            (Path(__file__).resolve().parent / "graphs" / "build-completion-supervisor.json").read_text()
        )
        self.assertEqual(graph["slug"], server.SUPERVISOR_LISTENER_SLUG)
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        trigger = nodes["trigger"]
        self.assertEqual(trigger["type"], "trigger.workflowCompleted")
        config = trigger["config"]
        self.assertEqual(set(config["statuses"]), set(server.SUPERVISOR_TERMINAL_STATUSES))
        self.assertEqual(config["executionModes"], ["live"])
        self.assertEqual(config["contextMode"], "output")
        self.assertEqual(config["sourceWorkflowTags"], [server.SUPERVISOR_TAG])
        self.assertEqual(config["sourceWorkflowIds"], ["$GRAPHWING_SOURCE_WORKFLOW_ID"])
        self.assertTrue(config["excludeSelf"])
        node_types = [node["type"].lower() for node in graph["spec"]["nodes"]]
        # The server owns durable watch settlement; the listener never holds an
        # expiring callback capability or an idle wait node.
        waits = [node for node in nodes.values() if node["type"].startswith("action.wait")]
        self.assertEqual(waits, [])
        raw_graph = json.dumps(graph).lower()
        for forbidden in ("response_webhook", "resumeurl", "resumetoken", "callback"):
            self.assertNotIn(forbidden, raw_graph)
        for banned in ("trigger.webhook", "trigger.manual", "rewst/fire", "agent/run", "git/"):
            self.assertFalse(any(banned in node_type for node_type in node_types), banned)
        self.assertFalse(
            any(node["type"].startswith("trigger.") for nid, node in nodes.items() if nid != "trigger"),
            "the listener has exactly one trigger",
        )
        outgoing = {}
        for edge in graph["spec"]["edges"]:
            outgoing.setdefault(edge["source"], set()).add(edge["target"])
        reachable = set()
        frontier = list(outgoing.get("watch_start", ()))
        while frontier:
            nid = frontier.pop()
            if nid in reachable:
                continue
            reachable.add(nid)
            frontier.extend(outgoing.get(nid, ()))
        self.assertNotIn("trigger", reachable, "the watch cannot cycle to the completion trigger")
        self.assertNotIn("watch_start", reachable, "the watch cannot cycle to itself")

    def test_the_listener_graph_routes_every_reconciled_action(self):
        graph = json.loads(
            (Path(__file__).resolve().parent / "graphs" / "build-completion-supervisor.json").read_text()
        )
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        self.assertEqual(nodes["reconcile"]["type"], "action.graphwing.POST:/v1/supervisor/reconcile")
        self.assertEqual(nodes["report"]["type"], "action.graphwing.GET:/v1/supervisor/report")
        # The listener sends identity and presence, never a key or a token.
        raw = json.dumps(nodes["reconcile"]["config"])
        for banned in ("api_key", "X-Graphwing-Key", "mcp", "token", "kick_url", "GRAPHWING_KEY"):
            self.assertNotIn(banned, raw, banned)
        for field in ("sourceWorkflow.id", "sourceWorkflow.slug", "sourceRun.id", "sourceRun.status"):
            self.assertIn(field, raw, field)
        payload = nodes["completion_payload"]
        self.assertEqual(payload["type"], "transforms.objectBuilder")
        mappings = {m["output"]: m["expression"] for m in payload["config"]["mappings"]}
        self.assertEqual(
            mappings["source_workflow"],
            {"kind": "getField", "path": "TRIGGER.workflowCompleted.sourceWorkflow"},
        )
        self.assertEqual(
            nodes["reconcile"]["config"]["source_workflow"]["tags"],
            "{{ TASKS.completion_payload.source_workflow.tags | tojson }}",
        )
        switch = nodes["switch_action"]
        labels = [case["label"] for case in switch["config"]["cases"]]
        labels.append(switch["config"]["default"]["label"])
        actions = set(
            json.loads(server.openapi_bytes())["components"]["schemas"]["SupervisorReconcile"][
                "properties"
            ]["action"]["enum"]
        )
        self.assertEqual(set(labels), actions, "every action the op can return needs an edge")
        edges = {edge["id"]: edge for edge in graph["spec"]["edges"]}
        self.assertEqual(edges["e_trigger_payload"]["target"], "completion_payload")
        self.assertEqual(edges["e_payload_reconcile"]["source"], "completion_payload")
        self.assertEqual(edges["e_payload_reconcile"]["target"], "reconcile")
        self.assertEqual(edges["e_reconcile_failure"]["target"], "not_supervised")
        self.assertEqual(edges["e_circuit_join"]["target"], "join_parked")
        self.assertEqual(edges["e_parked_join"]["target"], "join_parked")
        self.assertEqual(nodes["join_parked"]["type"], "logic.join.any")
        targets = {edge["target"] for edge in graph["spec"]["edges"]}
        for case_index, label in enumerate(switch["config"]["cases"]):
            handle = f"case-{case_index}"
            self.assertTrue(
                any(
                    edge["source"] == "switch_action" and edge["sourceHandle"] == handle
                    for edge in graph["spec"]["edges"]
                ),
                f"{label['label']} has no edge",
            )
        self.assertIn("report", targets)

    def test_retry_event_continues_with_the_registered_writer_check_and_reviewer_input(self):
        graph = json.loads(
            (Path(__file__).resolve().parent / "graphs" / "build-completion-supervisor.json").read_text()
        )
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        continuation = nodes["continue_source"]
        self.assertEqual(continuation["config"]["workflowId"], "$GRAPHWING_SOURCE_WORKFLOW_ID")
        self.assertTrue(continuation["config"]["await"])
        self.assertEqual(continuation["config"]["maxInvocations"], 1)
        inputs = continuation["config"]["input"]
        for field in (
            "build_id", "event_id", "plan_pane", "expected_stage", "deadline", "repo",
            "writer_prompt", "writer_prompt_ref", "fast_recipe", "integration_recipe", "reviewer",
            "class_stamp", "route_stamp",
        ):
            self.assertEqual(inputs[field], f"{{{{ TASKS.reconcile.data.continuation.{field} }}}}")
        continued = nodes["continued"]["config"]["mappings"]
        mapped = {entry["output"]: entry["expression"].get("path") for entry in continued}
        self.assertEqual(mapped["source_receipt"], "TASKS.continue_source.data.receipt")
        self.assertEqual(mapped["source_report"], "TASKS.continue_source.data.report")

    def test_source_graph_binds_async_job_acknowledgements_before_callbacks_finish(self):
        graph = json.loads(
            (Path(__file__).resolve().parent / "graphs" / "pre-pr-build.json").read_text()
        )
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        for producer, binder in (
            ("agent", "bind_writer_initial"),
            ("agent_fix", "bind_writer_fix"),
            ("checks_fast", "bind_fast"),
            ("checks_int", "bind_integration"),
            ("review", "bind_review"),
        ):
            self.assertEqual(nodes[binder]["type"], "action.graphwing.POST:/v1/supervisor/bind")
            self.assertEqual(nodes[binder]["config"]["source_run_id"], "{{ CTX.WORKFLOW.runId }}")
            self.assertEqual(nodes[binder]["config"]["job_id"], f"{{{{ TASKS.{producer}.data.job_id }}}}")
            self.assertTrue(
                any(
                    edge["source"] == producer
                    and edge["sourceHandle"] == "success"
                    and edge["target"] == binder
                    for edge in edges
                )
            )
            self.assertTrue(
                any(
                    edge["source"] == binder
                    and edge["sourceHandle"] == "failure"
                    and edge["target"] == "join_bind_failed"
                    for edge in edges
                )
            )
        self.assertEqual(nodes["join_bind_failed"]["type"], "logic.join.any")
        self.assertTrue(
            any(
                edge["source"] == "join_bind_failed"
                and edge["target"] == "bind_failed"
                for edge in edges
            )
        )

    # -- the body the published listener actually sends ----------------------

    @staticmethod
    def _render_rewst_config(config, context):
        """Render a Rewst node config the way the engine does.

        Every node config value is a string template, so whatever a `{{ ... }}`
        resolves to is stringified on the way out: a list becomes its repr, a
        boolean becomes "true"/"false", a missing value becomes "". This is the
        difference between a payload that validates in a hand-written test and
        one that validates in production.
        """
        def resolve(path):
            value = context
            for part in path.split("."):
                if not isinstance(value, dict) or part not in value:
                    return ""
                value = value[part]
            if isinstance(value, bool):
                return "true" if value else "false"
            return "" if value is None else str(value)

        def render(node):
            if isinstance(node, dict):
                return {k: render(v) for k, v in node.items()}
            if isinstance(node, list):
                return [render(v) for v in node]
            if isinstance(node, str):
                return re.sub(r"\{\{\s*(.+?)\s*\}\}", lambda m: resolve(m.group(1)), node)
            return node

        return render(config)

    def _graph_reconcile_body(self, tags=None, **run_over):
        graph = json.loads(
            (Path(__file__).resolve().parent / "graphs" / "build-completion-supervisor.json").read_text()
        )
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        config = nodes["reconcile"]["config"]
        source_run = {
            "id": self.RUN,
            "status": "failed",
            "executionMode": "live",
            "startedAt": "2026-08-25T10:00:00Z",
            "finishedAt": "2026-08-25T10:04:00Z",
        }
        source_run.update(run_over)
        context = {
            "CTX": {"WORKFLOW": {"version": "7"}},
            "TRIGGER": {
                "workflowCompleted": {
                    "sourceWorkflow": {
                        "id": self.WORKFLOW,
                        "slug": "graphwing-pre-pr-build",
                        "name": "graphwing-pre-pr-build",
                        "tags": ["graphwing-supervised"] if tags is None else tags,
                    },
                    "sourceRun": source_run,
                    "output": {"build_id": self.BUILD, "event_id": self.EVENT},
                    "error": {"present": source_run["status"] != "completed"},
                }
            },
        }
        transform = nodes["completion_payload"]["config"]
        transformed = {}
        for mapping in transform["mappings"]:
            path = mapping["expression"]["path"]
            value = context
            for part in path.split("."):
                value = value[part]
            transformed[mapping["output"]] = value
        self.assertIsInstance(transformed["source_workflow"]["tags"], list)
        context["TASKS"] = {"completion_payload": transformed}

        def render_action(node):
            if isinstance(node, dict):
                return {k: render_action(v) for k, v in node.items()}
            if isinstance(node, list):
                return [render_action(v) for v in node]
            if not isinstance(node, str):
                return node
            match = re.fullmatch(r"\{\{\s*(.+?)\s*\}\}", node)
            if match and match.group(1).endswith(" | tojson"):
                path = match.group(1)[: -len(" | tojson")]
                value = context
                for part in path.split("."):
                    value = value[part]
                return json.dumps(value, separators=(",", ":"))
            return self._render_rewst_config(node, context)

        body = render_action(config)
        # Node plumbing, not payload.
        for key in ("integrationInstanceId", "timeout"):
            body.pop(key, None)
        return body

    def test_the_body_the_published_listener_renders_is_accepted(self):
        self.assertEqual(self._register()[0], 201)
        body = self._graph_reconcile_body()
        self.assertEqual(body["listener_id"], self.LISTENER)
        self.assertEqual(json.loads(body["source_workflow"]["tags"]), ["graphwing-supervised"])
        self.assertEqual(body["error"], "true", "a rendered boolean is the word, not the type")
        status, payload, _ = server.dispatch(
            "POST", "/v1/supervisor/reconcile", {}, True, json.dumps(body).encode()
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["action"], "retry_event")
        self.assertTrue(payload["report"]["error_present"])
        self.assertEqual(payload["listener_slug"], server.SUPERVISOR_LISTENER_SLUG)

    def test_a_clean_completion_renders_error_presence_as_false(self):
        # The other render of the same field. "" and "false" both mean absent,
        # and neither may be read as a message.
        self.assertEqual(self._register()[0], 201)
        body = self._graph_reconcile_body(status="completed")
        self.assertEqual(body["error"], "false")
        status, payload, _ = server.dispatch(
            "POST", "/v1/supervisor/reconcile", {}, True, json.dumps(body).encode()
        )
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["report"]["error_present"])
        self.assertTrue(payload["workflow_fault"])

    def test_graph_transform_delivers_multiple_empty_self_and_maximum_tags(self):
        cases = (
            ([], 200, None),
            (["graphwing-supervised", "pre-pr"], 200, None),
            ([server.SUPERVISOR_LISTENER_SLUG], 409, "self_listener"),
            ([f"tag-{i}" for i in range(server.SUPERVISOR_TAGS_MAX)], 200, None),
        )
        for n, (tags, expected_status, expected_code) in enumerate(cases, start=1):
            with self.subTest(tags=len(tags), expected=expected_status):
                run = self._next_run(900 + n)
                self.assertEqual(self._register(source_run_id=run)[0], 201)
                body = self._graph_reconcile_body(tags=tags, id=run)
                self.assertEqual(json.loads(body["source_workflow"]["tags"]), tags)
                status, payload, _ = server.dispatch(
                    "POST", "/v1/supervisor/reconcile", {}, True, json.dumps(body).encode()
                )
                self.assertEqual(status, expected_status, payload)
                if expected_code:
                    self.assertEqual(payload["code"], expected_code)

    def test_the_catalog_documents_the_listener(self):
        readme = (Path(__file__).resolve().parent / "graphs" / "README.md").read_text()
        self.assertIn(server.SUPERVISOR_LISTENER_SLUG, readme)
        publish = (Path(__file__).resolve().parent / "scripts" / "publish_graphs.py").read_text()
        self.assertIn("build-completion-supervisor", publish)


class VisualEvidenceTests(unittest.TestCase):
    """The visual round is real-page proof, not a screenshot-shaped receipt."""

    BUILD = BuildStateTests.BUILD
    BRANCH = BuildStateTests.BRANCH
    _git = BuildStateTests._git
    _create_body = BuildStateTests._create_body
    _create = BuildStateTests._create
    _post_build = BuildStateTests._post_build
    _read_doc = BuildStateTests._read_doc
    _write_doc = BuildStateTests._write_doc
    _doc_path = BuildStateTests._doc_path

    def setUp(self):
        BuildStateTests.setUp(self)
        stacks = {
            "clean-stack": {
                "name": "clean-stack", "role": "clean", "cwd": self.clean_runner,
                "runner": "git-worktree",
            },
            "preview-52": {
                "name": "preview-52", "role": "preview", "cwd": self.repo,
                "base_url": "http://127.0.0.1:4173",
            },
        }
        patcher = mock.patch.object(server, "build_stack_catalog", return_value=stacks)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _contract(self, human_review=True):
        return {
            "route": "/settings/billing",
            "scenario": "billing-ready",
            "setup": ["Sign in through the normal test login"],
            "navigation": ["Open Settings", "Choose Billing"],
            "expected": ["Current plan: Pro", "Save changes"],
            "checklist": ["Plan and save action are visible"],
            "viewports": [{"name": "desktop", "width": 1280, "height": 720}],
            "human_review": human_review,
            "preview_stack": "preview-52",
            "browser_recipe": "visual-browser",
            "tenant": "Acme test",
            "planning_pane": "w1:p3" if human_review else None,
            "allow_login_redirect": False,
        }

    def _visual_create(self, human_review=True):
        (self.repo / "ticket.md").write_text("# Visual ticket\n\nShow the current plan and save action.\n")
        verification = {
            "fast": ["fast"], "integration": ["integration"],
            "needs_visual_proof": True, "human_visual_review": human_review,
            "visual": self._contract(human_review),
        }
        status, payload, _ = self._create(
            story="https://github.com/tfournet/graphwing/issues/52",
            ticket="ticket.md",
            route={
                "class": "visual", "launcher": "hermes", "model": "grok-4.6",
                "max_turns": 12, "run_budget_seconds": 900, "reviewer1": "sonnet",
            },
            verification=verification,
            stacks=["preview-52"],
        )
        self.assertEqual(status, 201, payload)
        doc = self._read_doc()
        doc["stage"] = "evidence"
        self._write_doc(doc)
        return self._read_doc()

    def test_preflight_requires_complete_contract_and_explicit_human_boolean(self):
        contract = self._contract()
        contract.pop("human_review")
        parsed, error = server.parse_build_visual(contract)
        self.assertIsNone(parsed)
        self.assertEqual(error["code"], "missing_human_review")
        for missing in ("navigation", "expected", "checklist", "viewports"):
            with self.subTest(missing=missing):
                incomplete = self._contract()
                incomplete.pop(missing)
                self.assertEqual(server.parse_build_visual(incomplete)[1]["code"], "bad_visual")

    def test_preview_identity_url_and_instructions_are_stable_on_resume(self):
        doc = self._visual_create()
        recipe = {"visual-browser": {"name": "visual-browser", "cwd": self.repo,
                  "argv": ["browser"], "timeout_seconds": 30, "unresolvable": None}}
        healthy = {"ok": True, "healthy": True}
        probe = {"ok": True, "status": 200}
        with mock.patch.object(server, "build_visual_recipe_catalog", return_value=recipe), \
             mock.patch.object(server, "stack_ensure_started", return_value={"ok": True, "resumed": True}), \
             mock.patch.object(server, "stack_status", return_value=healthy), \
             mock.patch.object(server, "probe_loopback", return_value=probe):
            first = self._post_build("visual/preview", event_id="preview-one", holder="event:round-1")
            replay = self._post_build("visual/preview", event_id="preview-one", holder="event:round-1")
        self.assertEqual(first[0], 200, first[1])
        self.assertEqual(first[1]["preview"]["url"], "http://127.0.0.1:4173/settings/billing")
        self.assertEqual(first[1]["preview"]["preview_id"], server.build_preview_identity(doc, "preview-52"))
        self.assertEqual(first[1]["instructions"]["navigation"], self._contract()["navigation"])
        self.assertTrue(replay[1]["replayed"])

    def test_real_page_observation_records_visible_a11y_console_and_request_results(self):
        contract = self._contract()
        preview = {"url": "http://127.0.0.1:4173/settings/billing", "preview_id": "preview-id",
                   "http_status": 200}
        observation = {
            "mode": "browser", "synthetic": False, "url": preview["url"],
            "route": contract["route"], "tenant": contract["tenant"],
            "served_by": preview["preview_id"], "http_status": 200,
            "visible": ["Current plan: Pro", "Save changes"],
            "accessibility": {"ok": False, "violations": ["missing label"]},
            "console": {"ok": False, "errors": ["hydration failed"]},
            "requests": {"ok": False, "failures": ["GET /api/plan 500"]},
            "images": [{"file": "candidate.png", "viewport": "desktop", "source": "browser"}],
        }
        self.assertEqual(server.build_visual_observation_gaps(observation, contract, preview), [])
        for mutation in (
            {"mode": "dom"}, {"synthetic": True}, {"url": "http://127.0.0.1:9/fake"},
            {"accessibility": {}}, {"console": {}}, {"requests": {}},
        ):
            with self.subTest(mutation=mutation):
                self.assertTrue(server.build_visual_observation_gaps({**observation, **mutation}, contract, preview))

    def test_review_identity_prompt_and_image_inspection_cover_every_visual_lens(self):
        doc = {
            "route": {"model": "grok-4.6"}, "verification": {"visual": self._contract()},
            "worktree": str(self.repo), "base_head": self.head, "ticket": "ticket.md",
        }
        capture = {
            "role": "candidate", "viewport": "desktop", "width": 1280,
            "route": "/wrong", "tenant": "Other tenant", "obscured": True,
            "text": ["Current plan"], "path": "/tmp/candidate.png",
        }
        reasons = server.build_visual_image_reasons(capture, self._contract())
        self.assertTrue(any("obscured" in reason for reason in reasons))
        self.assertTrue(any("not '/settings/billing'" in reason for reason in reasons))
        self.assertTrue(any("visible tenant" in reason for reason in reasons))
        self.assertTrue(any("Save changes" in reason for reason in reasons))
        self.assertIn("own vendor", server.build_review_authority_gap(doc, "grok"))
        rnd = {"preview": {"url": "http://127.0.0.1:4173/settings/billing"},
               "observations": {"candidate": {"accessibility": {"ok": True}}}, "captures": []}
        prompt = server.build_visual_review_prompt(doc, rnd)
        for phrase in ("Ticket:", "Diff for this round:", "Browser observations:", "Screenshots", "ACCESSIBILITY"):
            self.assertIn(phrase, prompt)

    def test_recapture_and_superseding_rounds_keep_before_candidate_history(self):
        doc = {"verification": {"visual": self._contract()}, "evidence_rounds": []}
        first = server.build_visual_round_start(doc, {"head": "a" * 40, "change_id": "change-1"})
        self.assertEqual(server.build_visual_required_roles(first), ["before", "candidate"])
        first["status"] = "published"
        second = server.build_visual_round_start(doc, {"head": "b" * 40, "change_id": "change-2"})
        self.assertEqual(second["supersedes"], 1)
        self.assertEqual(server.build_visual_required_roles(second), ["candidate"])
        second["preview"] = {"preview_id": "stable-preview", "url": "http://127.0.0.1:4173/settings/billing"}
        second["captures"] = [{"role": "candidate", "viewport": "desktop"}]
        second["inspection"] = {"change_id": "change-2", "ok": False,
                                "recapture": ["candidate/desktop"], "error": "obscured"}
        second["inspection_queue"] = [{"role": "candidate", "only": ["desktop"]}]
        with mock.patch.object(server, "build_visual_contract_gaps", return_value=[]):
            self.assertEqual(server.build_visual_step({**doc, "stage": "evidence", "budget": {"jobs_max": 9, "jobs_used": 0}}, "change-2")[0], "scenario")
        self.assertEqual(len(doc["evidence_rounds"]), 2)

    def test_simultaneous_before_and_candidate_failures_queue_both_roles_once(self):
        failures = [
            {"role": "before", "viewport": "desktop"},
            {"role": "candidate", "viewport": "desktop"},
        ]
        queue = server.build_visual_recapture_queue(failures, {})
        self.assertEqual(queue, [
            {"role": "before", "only": ["desktop"]},
            {"role": "candidate", "only": ["desktop"]},
        ])
        rnd = {
            "round": 1, "status": "open", "change_id": "change-1",
            "preview": {"url": "http://127.0.0.1:4173/settings/billing"},
            "captures": [
                {"role": "before", "viewport": "desktop"},
                {"role": "candidate", "viewport": "desktop"},
            ],
            "inspection": {"change_id": "change-1", "ok": False},
            "inspection_queue": queue,
            "recapture_attempts": {},
        }
        doc = {
            "stage": "evidence", "budget": {"jobs_max": 9, "jobs_used": 0},
            "verification": {"visual": self._contract()}, "evidence_rounds": [rnd],
        }
        with mock.patch.object(server, "build_visual_contract_gaps", return_value=[]):
            self.assertEqual(server.build_visual_step(doc, "change-1")[0], "scenario")
        self.assertEqual(server.build_visual_recapture_target(rnd, "before"), ["desktop"])
        rnd["recapture_attempts"]["before"] = 1
        rnd["inspection_queue"] = queue[1:]
        self.assertIsNone(server.build_visual_recapture_target(rnd, "before"))
        self.assertEqual(server.build_visual_recapture_target(rnd, "candidate"), ["desktop"])
        rnd["recapture_attempts"]["candidate"] = 1
        rnd["inspection_queue"] = []
        rnd["inspection"] = None
        with mock.patch.object(server, "build_visual_contract_gaps", return_value=[]):
            self.assertEqual(server.build_visual_step(doc, "change-1")[0], "inspect")
        rnd["inspection"] = {"change_id": "change-1", "ok": False,
                             "recapture": ["before/desktop", "candidate/desktop"]}
        with mock.patch.object(server, "build_visual_contract_gaps", return_value=[]):
            self.assertEqual(server.build_visual_step(doc, "change-1")[0], "parked")

    def test_artifact_grant_is_private_expiring_single_use_while_record_is_durable(self):
        meta = {"artifact_id": "a" * 32, "media_type": "image/png", "grant": None}
        token, grant = server.artifact_mint_grant(meta, 60)
        self.assertNotIn(token, json.dumps(grant))
        self.assertRegex(grant["digest"], r"^[0-9a-f]{64}$")
        self.assertGreater(grant["expires_epoch"], time.time())
        self.assertIsNone(grant["consumed_at"])

    def test_artifact_grant_consume_is_atomic_and_symlink_bytes_are_rejected(self):
        artifact_id = "c" * 32
        with tempfile.TemporaryDirectory() as td, mock.patch.object(server, "BUILDS_DIR", Path(td) / "builds"):
            path = server.artifact_dir(artifact_id)
            path.mkdir(parents=True)
            token, grant = server.artifact_mint_grant({"artifact_id": artifact_id}, 60)
            server.artifact_write({"artifact_id": artifact_id, "media_type": "image/png", "grant": grant})
            (path / "image.png").write_bytes(b"real")
            barrier = threading.Barrier(8)
            results = []

            def fetch():
                barrier.wait()
                results.append(server.artifact_fetch(artifact_id, {server.BUILD_ARTIFACT_TOKEN_HEADER: token})[0])

            threads = [threading.Thread(target=fetch) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(results.count(200), 1, results)
            self.assertEqual(results.count(410), 7, results)

            second_id = "d" * 32
            second = server.artifact_dir(second_id)
            second.mkdir(parents=True)
            token2, grant2 = server.artifact_mint_grant({"artifact_id": second_id}, 60)
            server.artifact_write({"artifact_id": second_id, "media_type": "image/png", "grant": grant2})
            (second / "target.png").write_bytes(b"secret")
            (second / "image.png").symlink_to(second / "target.png")
            self.assertEqual(
                server.artifact_fetch(second_id, {server.BUILD_ARTIFACT_TOKEN_HEADER: token2})[1]["code"],
                "artifact_gone",
            )

    def test_handoff_grant_state_never_remints_after_spent_expired_or_missing(self):
        states = (
            ({"digest": "a" * 64, "consumed_at": "2026-08-25T12:00:00Z",
              "expires_epoch": time.time() + 60}, "grant_spent"),
            ({"digest": "b" * 64, "consumed_at": None,
              "expires_epoch": time.time() - 1}, "grant_expired"),
            (None, "grant_missing"),
        )
        for number, (grant, expected) in enumerate(states):
            with self.subTest(expected=expected):
                artifact_id = f"{number + 1:032x}"
                path = server.artifact_dir(artifact_id)
                path.mkdir(parents=True, exist_ok=True)
                meta = {"artifact_id": artifact_id, "number": 1, "role": "candidate",
                        "viewport": "desktop", "media_type": "image/png", "digest": "d" * 64,
                        "grant": grant}
                server.artifact_write(meta)
                outstanding, code = server.artifact_grant_replay_state([artifact_id])
                self.assertEqual(outstanding, [])
                self.assertEqual(code, expected)

    def test_visual_route_is_strict_and_comment_fields_are_markdown_safe(self):
        for route in (
            "https://evil.test/x", "//evil.test/x", "/@evil.test", "/white space", "/line\nbreak",
            "/x?token=abc", "/x#frag", "/x\\y", "/ghp_abcdefghijklmnopqrstuvwxyz",
        ):
            with self.subTest(route=route):
                contract = self._contract()
                contract["route"] = route
                self.assertIsNotNone(server.parse_build_visual(contract)[1])
        safe = server.github_markdown_text("@octocat [x](evil)\nnext")
        self.assertNotIn("@octocat", safe)
        self.assertNotIn("\n", safe)
        self.assertIn("\\[x\\]", safe)
        self.assertTrue(server.durable_github_url("https://github.com/tfournet/graphwing/assets/1"))
        self.assertFalse(server.durable_github_url("http://github.com/tfournet/graphwing/assets/1"))
        self.assertFalse(server.durable_github_url("https://evil.test/asset"))
        self.assertFalse(server.durable_github_url("https://github.com/x?token=secret"))

    def test_durable_github_urls_reject_malformed_authorities(self):
        for url in (
            "https://github.com/o/r/assets/1",
            "https://github.com:443/o/r/assets/1",
            "https://raw.githubusercontent.com/o/r/main/proof.png",
            "https://user-images.githubusercontent.com/1/proof.png",
        ):
            with self.subTest(valid=url):
                self.assertTrue(server.durable_github_url(url))
        for url in (
            "https://github.com:443>/assets/1",
            "https://github.com%3e/assets/1",
            "https://github.com\\@evil.test/assets/1",
            "https://github.com\t.evil.test/assets/1",
            "https://user@github.com/assets/1",
            "https://[::1]/assets/1",
            "https://github.com:444/assets/1",
            "https://github.com:notaport/assets/1",
            "https://github.com./assets/1",
            "https://github.com.evil.test/assets/1",
            "https://githubusercontent.com/assets/1",
        ):
            with self.subTest(invalid=url):
                self.assertFalse(server.durable_github_url(url))
        self.assertTrue(server.durable_upload_ref("github-asset-123"))
        self.assertTrue(server.durable_upload_ref("https://github.com:443/o/r/assets/1"))
        self.assertFalse(server.durable_upload_ref("https://github.com:443>/assets/1"))
        self.assertFalse(server.durable_upload_ref("https%3a//github.com:443>/assets/1"))

    def test_unvalidated_authorities_never_break_out_of_markdown(self):
        attacks = (
            "https://github.com:443>/assets/1",
            "https://github.com%3e/assets/1",
            "https://github.com\\@evil.test/assets/1",
            "https://user@github.com/assets/1",
            "https://[::1]:443>/assets/1",
            "http://127.0.0.1:4173>/proof",
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                rendered = server.github_markdown_url(attack)
                self.assertFalse(rendered.startswith("<"), rendered)
                self.assertNotIn("<https://", rendered)
                self.assertNotIn(":443>", rendered)
        self.assertFalse(server.loopback_http_url("http://127.0.0.1:4173>/proof"))

    def test_validated_urls_are_rendered_as_markdown_safe_autolinks(self):
        attacks = (
            ")![x](", "@octocat", "<b>html</b>", "user@example.test",
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                rendered = server.github_markdown_url("https://github.com/o/r/assets/" + attack)
                self.assertTrue(rendered.startswith("<https://github.com/"), rendered)
                self.assertTrue(rendered.endswith(">"), rendered)
                self.assertNotIn("\n", rendered)
                self.assertNotIn(")![x](", rendered)
                self.assertNotIn("@octocat", rendered)
                self.assertNotIn("<b>", rendered)
        for invalid in (
            "https://github.com/o/r/assets/line\nbreak",
            "https://github.com/o/r/assets/1?query=x",
            "https://github.com/o/r/assets/1#fragment",
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(server.github_markdown_url(invalid).startswith("<"))
        doc = {"build_id": self.BUILD, "branch": self.BRANCH,
               "verification": {"visual": self._contract()}}
        rnd = {
            "round": 1, "head": self.head, "human_review": True,
            "preview": {"preview_id": "p", "url": "http://127.0.0.1:4173/a)![x](b"},
            "review": {"reviewer": "sonnet", "summary": "ok", "findings": False},
        }
        comment = server.build_evidence_comment(doc, rnd, [{
            "number": 1, "role": "candidate", "viewport": "desktop", "label": "x",
            "artifact_id": "a" * 32, "url": "https://github.com/o/r/assets/a)![x](b",
        }])
        self.assertNotIn(")![x](", comment)
        self.assertIn("<http://127.0.0.1:4173/a%29%21%5Bx%5D%28b>", comment)

    def test_probe_loopback_does_not_follow_redirects_and_only_allows_safe_login(self):
        class Redirects(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/ok":
                    self.send_response(204)
                elif self.path == "/login-redirect":
                    self.send_response(302)
                    self.send_header("Location", "/login")
                elif self.path == "/decoy":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:9/login")
                else:
                    self.send_response(404)
                self.end_headers()

            def log_message(self, _format, *_args):
                return

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Redirects)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_port}"
        self.assertEqual(server.probe_loopback(base + "/ok")["status"], 204)
        refused = server.probe_loopback(base + "/login-redirect")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["status"], 302)
        allowed = server.probe_loopback(base + "/login-redirect", allow_login=True)
        self.assertTrue(allowed["ok"])
        self.assertEqual(allowed["status"], 302)
        self.assertEqual(allowed["final_location"], base + "/login")
        self.assertFalse(server.probe_loopback(base + "/decoy", allow_login=True)["ok"])
        self.assertFalse(server.probe_loopback(base + "/missing", allow_login=True)["ok"])

    def test_recipe_observation_cannot_override_graphwing_authority(self):
        contract = self._contract()
        preview = {
            "url": "http://127.0.0.1:4173/settings/billing", "preview_id": "preview-id",
            "route": contract["route"], "tenant": contract["tenant"], "http_status": 200,
        }
        base = {
            "mode": "browser", "synthetic": False, "url": preview["url"], "route": contract["route"],
            "tenant": contract["tenant"], "served_by": preview["preview_id"], "http_status": 200,
            "visible": [], "accessibility": {"ok": True}, "console": {"ok": True},
            "requests": {"ok": True},
            "images": [{"file": "candidate.png", "viewport": "desktop", "source": "browser"}],
        }
        for field, value in (
            ("mode", "dom"), ("synthetic", True), ("route", "/forged"), ("tenant", "Other"),
            ("served_by", "forged"), ("http_status", 404),
            ("final_location", "http://127.0.0.1:9/login"),
        ):
            with self.subTest(field=field):
                self.assertTrue(server.build_visual_observation_gaps({**base, field: value}, contract, preview))

    def test_visual_browser_catalog_is_graphwing_owned_and_integrity_pinned(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            driver = home / "bin" / "visual-browser"
            driver.parent.mkdir()
            driver.write_text("#!/bin/sh\nexit 0\n")
            digest = __import__("hashlib").sha256(driver.read_bytes()).hexdigest()
            catalog_path = home / "visual-browsers.json"
            catalog_path.write_text(json.dumps({"browsers": [{
                "name": "visual-browser", "argv": ["./bin/visual-browser"],
                "integrity_path": "bin/visual-browser", "sha256": digest,
                "timeout_seconds": 30,
            }]}))
            with mock.patch.object(server, "HOME", home), \
                 mock.patch.object(server, "VISUAL_BROWSERS_PATH", catalog_path):
                catalog = server.build_visual_recipe_catalog()
                self.assertEqual(catalog["visual-browser"]["cwd"], home)
                self.assertEqual(catalog["visual-browser"]["integrity_sha256"], digest)
                self.assertIsNone(catalog["visual-browser"]["unresolvable"])
                catalog_path.write_text(catalog_path.read_text().replace(digest, "0" * 64))
                self.assertEqual(
                    server.build_visual_recipe_catalog()["visual-browser"]["unresolvable"],
                    "browser driver integrity does not match its installed pin",
                )

    def test_stopped_preview_starts_once_and_healthy_replay_attaches(self):
        entry = {"name": "preview-52", "cwd": self.repo, "compose_file": "compose.yml"}
        with mock.patch.object(server, "stack_status", side_effect=[{"healthy": False}, {"healthy": True}]), \
             mock.patch.object(server, "run_cmd", return_value={"ok": True}) as run:
            self.assertEqual(server.stack_ensure_started(entry), {"ok": True, "resumed": False, "error": None})
            self.assertEqual(server.stack_ensure_started(entry), {"ok": True, "resumed": True})
        self.assertEqual(run.call_count, 1)
        self.assertTrue(server.loopback_http_url("http://[::1]:4173"))

    def test_round_history_is_complete_beyond_public_summary_bound(self):
        doc = {"verification": {"visual": self._contract()}, "evidence_rounds": []}
        for number in range(40):
            rnd = server.build_visual_round_start(
                doc, {"head": f"{number:040x}", "change_id": f"change-{number}"}
            )
            rnd["status"] = "published"
            rnd["preview"] = {
                "preview_id": f"preview-{number}", "url": f"http://127.0.0.1:{4100 + number}/proof",
                "healthy": True, "browser_argv": ["browser", "--token=secret"],
            }
            rnd["captures"] = [
                {"role": "candidate", "viewport": "desktop", "verified": True,
                 "path": f"/tmp/round-{number}.png", "text": ["unredacted page text"]},
                {"role": "before", "viewport": "desktop", "verified": False,
                 "path": f"/tmp/before-{number}.png"},
            ]
            rnd["observations"] = {"candidate": {"cookies": ["session=secret"], "raw": "page text"}}
            rnd["inspection"] = {"ok": False, "recapture": ["candidate/desktop"]}
            rnd["review"] = {"verdict": "NACK", "findings": True, "text": "raw review transcript"}
            rnd["published"] = {
                "commented_at": f"2026-08-25T12:{number:02d}:00Z",
                "comment_url": f"https://github.com/tfournet/graphwing/issues/52#issuecomment-{number}",
                "grant": "secret-grant", "comment": "unredacted comment text",
            }
            rnd["prompted_at"] = f"2026-08-25T13:{number:02d}:00Z"
        self.assertEqual(len(doc["evidence_rounds"]), 40)
        self.assertEqual(doc["evidence_rounds"][-1]["supersedes"], 39)
        public = server.build_public(doc)
        self.assertEqual(len(doc["evidence_rounds"]), 40)
        self.assertEqual(len(public["evidence_rounds"]), server.BUILD_EVIDENCE_ROUNDS_KEPT)
        self.assertEqual(public["evidence_rounds"][0]["round"], 9)
        self.assertEqual(public["evidence_rounds"][-1]["round"], 40)
        self.assertEqual(public["visual_round"], public["evidence_rounds"][-1])
        latest = public["visual_round"]
        self.assertEqual(latest["preview"], {
            "preview_id": "preview-39", "url": "http://127.0.0.1:4139/proof", "healthy": True,
        })
        self.assertEqual(latest["captures"], 2)
        self.assertEqual(latest["verified"], 1)
        self.assertEqual(latest["recapture"], ["candidate/desktop"])
        self.assertEqual(latest["published_at"], "2026-08-25T12:39:00Z")
        public_text = json.dumps(public["evidence_rounds"])
        for secret in ("/tmp/", "unredacted", "secret", "browser_argv", "cookies", "observations"):
            self.assertNotIn(secret, public_text)

    def test_nack_and_no_verdict_cannot_handoff(self):
        for review in ({"verdict": "NACK"}, {"verdict": None, "no_verdict": True}):
            rnd = {
                "inspection": {"ok": True}, "review": review,
                "captures": [{"verified": True, "role": "candidate"}],
            }
            self.assertEqual(server.build_visual_handoff_gap(rnd), "review_not_passed")

    def test_comment_url_must_match_issue_and_round(self):
        issue = {"owner": "tfournet", "repo": "graphwing", "number": 52}
        good = "https://github.com/tfournet/graphwing/issues/52#issuecomment-123"
        self.assertIsNone(server.github_comment_url_gap(good, issue))
        self.assertIsNotNone(server.github_comment_url_gap(
            "https://github.com/tfournet/graphwing/issues/51#issuecomment-123", issue
        ))
        for url in (
            "https://github.com:443>/tfournet/graphwing/issues/52#issuecomment-123",
            "https://github.com%3e/tfournet/graphwing/issues/52#issuecomment-123",
            "https://user@github.com/tfournet/graphwing/issues/52#issuecomment-123",
            "https://[::1]/tfournet/graphwing/issues/52#issuecomment-123",
        ):
            with self.subTest(url=url):
                self.assertIsNotNone(server.github_comment_url_gap(url, issue))

    def test_github_issue_comment_payload_uses_durable_artifact_ids_and_no_shortcut(self):
        doc = {
            "build_id": self.BUILD, "branch": self.BRANCH,
            "verification": {"visual": self._contract()},
        }
        rnd = {
            "round": 2, "head": self.head, "supersedes": 1, "human_review": True,
            "preview": {"preview_id": "preview-id", "url": "http://127.0.0.1:4173/settings/billing"},
            "review": {"reviewer": "sonnet", "findings": False, "summary": "acknowledged"},
            "observations": {"candidate": {"visible": ["Current plan: Pro"],
                "accessibility": {"ok": True}, "console": {"ok": True}, "requests": {"ok": True}}},
        }
        uploads = [{"number": 1, "role": "candidate", "viewport": "desktop", "label": "billing",
                    "artifact_id": "b" * 32, "url": "https://github.com/tfournet/graphwing/assets/1"}]
        comment = server.build_evidence_comment(doc, rnd, uploads)
        for expected in ("round 2", "round 1", "b" * 32, uploads[0]["url"], "accessibility", "pre-commit"):
            self.assertIn(expected.lower(), comment.lower())
        self.assertNotIn("shortcut", comment.lower())
        self.assertEqual(server.build_issue_target({"story": "https://github.com/tfournet/graphwing/issues/52"})[0]["number"], 52)

    def test_exact_planning_pane_prompt_is_precommit_and_preserves_stack(self):
        doc = {"build_id": self.BUILD, "verification": {"visual": self._contract()}}
        rnd = {
            "round": 1, "preview": {"url": "http://127.0.0.1:4173/settings/billing"},
            "published": {"issue": {"url": "https://github.com/tfournet/graphwing/issues/52"},
                          "uploads": [{"number": 1, "role": "candidate", "viewport": "desktop", "url": "https://example.test/1"}]},
            "review": {"reviewer": "sonnet", "findings": False, "summary": "acknowledged"},
        }
        prompt = server.build_visual_planning_prompt(doc, rnd)
        self.assertEqual(self._contract()["planning_pane"], "w1:p3")
        for phrase in ("Open:", "How to view it:", "Proof checklist:", "Screenshots:",
                       "Nothing is committed", "preview stack stays up", "approve, change"):
            self.assertIn(phrase, prompt)

    def test_openapi_graph_catalog_install_and_ticket_are_complete(self):
        root = Path(server.__file__).resolve().parent
        spec = json.loads((root / "openapi.json").read_text())
        operations = {
            "/v1/build/visual/next": ("get", "buildVisualNext"),
            "/v1/build/visual/preflight": ("post", "buildVisualPreflight"),
            "/v1/build/visual/preview": ("post", "buildVisualPreview"),
            "/v1/build/visual/scenario": ("post", "buildVisualScenario"),
            "/v1/build/visual/inspect": ("post", "buildVisualInspect"),
            "/v1/build/visual/review": ("post", "buildVisualReview"),
            "/v1/build/visual/handoff": ("post", "buildVisualHandoff"),
            "/v1/build/visual/publish": ("post", "buildVisualPublish"),
            "/v1/build/visual/publish-result": ("post", "buildVisualPublishResult"),
            "/v1/build/visual/prompt": ("post", "buildVisualPrompt"),
            "/v1/artifact/{artifact_id}": ("get", "artifactFetch"),
        }
        for route, (method, op_id) in operations.items():
            with self.subTest(route=route):
                self.assertEqual(spec["paths"][route][method]["operationId"], op_id)
                schema = spec["paths"][route][method].get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
                if schema and "$ref" in schema:
                    self.assertFalse(spec["components"]["schemas"][schema["$ref"].split("/")[-1]].get("additionalProperties", True))
        graph = json.loads((root / "graphs" / "visual-evidence.json").read_text())
        self.assertEqual(graph["slug"], "graphwing-visual-evidence")
        text = json.dumps(graph).lower()
        self.assertIn("github", text)
        self.assertNotIn("shortcut", text)
        self.assertNotIn("synthetic", text)
        self.assertIn("pre-commit", text)
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        self.assertEqual(nodes["recapture_before"]["config"]["role"], "before")
        self.assertIn("recapture_by_role.before", nodes["recapture_before"]["config"]["only"])
        self.assertEqual(nodes["recapture_candidate"]["config"]["role"], "candidate")
        self.assertIn("recapture_by_role.candidate", nodes["recapture_candidate"]["config"]["only"])
        self.assertIn("publish-result", nodes["publish_result"]["type"])
        ticket = (root / "slices" / "issue-52-pre-pr-build" / "03-visual-evidence.md").read_text().lower()
        self.assertNotIn("shortcut", ticket)
        self.assertIn("github issue #52", ticket)
        self.assertIn("visual-evidence", (root / "scripts" / "publish_graphs.py").read_text())
        self.assertIn("graphs", (root / "install.py").read_text())

    def test_visual_graph_is_acyclic_bounded_and_has_deterministic_canary_fixture(self):
        graph = json.loads((Path(server.__file__).resolve().parent / "graphs" / "visual-evidence.json").read_text())
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        incoming = {name: [] for name in nodes}
        outgoing = {name: [] for name in nodes}
        for edge in edges:
            incoming[edge["target"]].append(edge["source"])
            outgoing[edge["source"]].append(edge["target"])
        for node_id, sources in incoming.items():
            if len(sources) > 1:
                self.assertEqual(nodes[node_id]["type"], "logic.join.any", node_id)
        indegree = {name: len(sources) for name, sources in incoming.items()}
        queue = [name for name, degree in indegree.items() if degree == 0]
        seen = []
        while queue:
            name = queue.pop()
            seen.append(name)
            for target in outgoing[name]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        self.assertEqual(len(seen), len(nodes), "visual graph must contain no cycle or unbounded wait")
        types = [node["type"] for node in nodes.values()]
        self.assertFalse(any("/v1/build/finalize" in node_type for node_type in types))
        self.assertFalse(any("/v1/git/commit" in node_type or "/v1/git/push" in node_type for node_type in types))
        self.assertTrue(any("github" in node_type.lower() and "comment" in node_type.lower() for node_type in types))
        self.assertIn("canary", graph)
        self.assertEqual(graph["canary"]["issue"], "https://github.com/tfournet/graphwing/issues/52")


class VisualIterationTests(VisualEvidenceTests):
    """One design turn resumes the writer and returns proof to planning."""

    def _iteration_ready(self, turns=0):
        self._visual_create()
        doc = self._read_doc()
        doc["writer_session"] = "gwslice-" + "a" * 32
        change_id = server.build_change_id(self.repo, self.head)
        doc["evidence_rounds"] = [{
            "round": 1,
            "status": "published",
            "head": self.head,
            "change_id": change_id,
            "human_review": True,
            "preview": {
                "preview_id": "preview-one",
                "stack": "preview-52",
                "url": "http://127.0.0.1:4173/settings/billing",
            },
            "inspection": {"ok": True},
            "review": {"reviewer": "sonnet", "findings": False, "summary": "clear"},
            "published": {
                "comment_url": "https://github.com/tfournet/graphwing/issues/52#issuecomment-123",
                "uploads": [{
                    "number": 1,
                    "role": "candidate",
                    "viewport": "desktop",
                    "artifact_id": "b" * 32,
                    "url": "https://github.com/tfournet/graphwing/assets/1",
                }],
            },
        }]
        doc["iteration"] = {
            "state": "iterating",
            "turns": [
                {
                    "turn": number,
                    "source": "human",
                    "status": "closed",
                    "notes": f"prior {number}",
                    "notes_digest": str(number),
                    "decision": "reframe",
                }
                for number in range(1, turns + 1)
            ],
            "decisions": [],
            "splits": [],
            "returns": [],
        }
        self._write_doc(doc)
        return change_id

    def _iteration_post(self, operation, event_id, **body):
        body.update(event_id=event_id, holder="planning-run")
        return self._post_build(f"iteration/{operation}", **body)

    def _claim(self, event_id="claim-initial", holder="rewst-run-1", kind="initial", **over):
        return BuildStateTests._claim(self, event_id, holder, kind, **over)

    def test_exact_feedback_replays_and_conflicting_reuse_changes_no_files(self):
        self._iteration_ready()
        notes = "Make the Save button 2px shorter.  Keep  this spacing.\n"
        before = self._git("status", "--porcelain=v1")
        first = self._iteration_post("feedback", "feedback-1", notes=notes, round=1)
        self.assertEqual(first[0], 200, first[1])
        self.assertEqual(first[1]["notes"], notes)
        self.assertEqual(self._read_doc()["iteration"]["turns"][-1]["notes"], notes)
        replay = self._iteration_post("feedback", "feedback-1", notes=notes, round=1)
        self.assertEqual(replay[0], 200, replay[1])
        self.assertTrue(replay[1]["replayed"])
        conflict = self._iteration_post(
            "feedback", "feedback-1", notes="Different words", round=1
        )
        self.assertEqual(conflict[0], 409, conflict[1])
        self.assertEqual(conflict[1]["code"], "idempotency_conflict")
        self.assertEqual(self._git("status", "--porcelain=v1"), before)

    def test_stale_and_second_feedback_are_refused_without_a_writer_claim(self):
        self._iteration_ready()
        stale = self._iteration_post("feedback", "feedback-old", notes="Old", round=2)
        self.assertEqual(stale[0], 409, stale[1])
        self.assertEqual(stale[1]["code"], "stale_feedback")
        self.assertTrue(stale[1]["files_unchanged"])
        self.assertEqual(
            self._iteration_post("feedback", "feedback-open", notes="First", round=1)[0],
            200,
        )
        duplicate = self._iteration_post(
            "feedback", "feedback-duplicate", notes="First", round=1
        )
        conflict = self._iteration_post(
            "feedback", "feedback-conflict", notes="Second", round=1
        )
        self.assertEqual(duplicate[1]["code"], "duplicate_feedback")
        self.assertEqual(conflict[1]["code"], "feedback_conflict")
        self.assertIsNone(self._read_doc().get("writer_claim"))

    def test_design_claim_and_brief_resume_the_same_writer_without_strikes(self):
        self._iteration_ready()
        notes = "Tighten the card and keep the current labels."
        self.assertEqual(
            self._iteration_post("feedback", "feedback-resume", notes=notes, round=1)[0],
            200,
        )
        session = self._read_doc()["writer_session"]
        claim = self._claim("claim-design", holder="planning-run", kind="design")
        self.assertEqual(claim[0], 200, claim[1])
        self.assertEqual(claim[1]["claim"]["writer_session"], session)
        brief = self._post_build("brief")
        self.assertEqual(brief[0], 200, brief[1])
        self.assertTrue(brief[1]["resume"])
        self.assertEqual(brief[1]["hermes_session"], session)
        self.assertIn(notes, brief[1]["prompt"])
        self.assertEqual(claim[1]["receipt"]["strikes"], {"test": 0, "reviewer": 0, "retry": 0})
        self.assertTrue(claim[1]["receipt"]["strike_free"])

    def test_turn_requires_checks_round_review_screenshots_comment_and_exact_pane_report(self):
        change_id = self._iteration_ready()
        doc = self._read_doc()
        turn = {
            "turn": 1,
            "source": "human",
            "status": "written",
            "notes": "Reduce the card padding",
            "notes_digest": "digest",
            "writer_session": doc["writer_session"],
            "writer_result": {"summary": "Reduced card padding"},
            "change_id": change_id,
        }
        doc["iteration"]["turns"] = [turn]
        self._write_doc(doc)
        self.assertEqual(server.build_iteration_step(doc, change_id)[0], "checks")
        doc["checks"] = {"fast": {
            "ok": True,
            "change_id": change_id,
            "required": ["changed-area", "typecheck"],
            "changed_paths": ["ui/card.tsx"],
            "verdicts": {
                "changed-area": {"ok": True, "coverage": True},
                "typecheck": {"ok": True, "coverage": True},
            },
        }}
        rnd = doc["evidence_rounds"][-1]
        rnd["status"] = "superseded"
        self.assertEqual(server.build_iteration_step(doc, change_id)[0], "round")
        rnd["change_id"] = change_id
        rnd["status"] = "published"
        report = server.build_iteration_report_text(doc, turn, rnd)
        for text in (
            "Open: <http://127.0.0.1:4173/settings/billing>",
            "How to get there:",
            "Reduce the card padding",
            "changed\\-area, typecheck",
            "Automated UI review",
            "Screenshots:",
            "Evidence comment:",
        ):
            self.assertIn(text, report)
        self.assertEqual(self._contract()["planning_pane"], "w1:p3")

    def test_generic_fast_receipt_cannot_advance_a_design_candidate(self):
        change_id = self._iteration_ready()
        doc = self._read_doc()
        doc["iteration"]["turns"] = [{
            "turn": 1, "source": "human", "status": "written", "notes": "Adjust spacing",
            "writer_session": doc["writer_session"], "change_id": change_id,
        }]
        # This is deliberately green but has only generic/unit proof.  It must
        # not stand in for the two design-turn proofs, regardless of a happy
        # generic fast receipt.
        doc["checks"] = {"fast": {
            "ok": True, "change_id": change_id, "required": ["unit"],
            "verdicts": {"unit": {"ok": True, "coverage": True}},
        }}
        doc["evidence_rounds"][-1]["prompted_at"] = server.utcnow()
        self._write_doc(doc)
        step, reason = server.build_iteration_step(doc, change_id)
        self.assertEqual(step, "checks")
        self.assertIn("changed-area tests and typecheck", reason)
        refused_report = self._iteration_post("report", "generic-report")
        self.assertEqual(refused_report[0], 409, refused_report[1])
        self.assertEqual(refused_report[1]["code"], "report_not_due")
        doc = self._read_doc()
        doc["iteration"]["turns"][-1]["status"] = "reported"
        self._write_doc(doc)
        refused_ready = self._iteration_post("decision", "generic-ready", decision="design_ready")
        self.assertEqual(refused_ready[0], 409, refused_ready[1])
        self.assertEqual(refused_ready[1]["code"], "not_design_ready")
        self.assertIn("changed-area tests and typecheck", refused_ready[1]["error"])

    def test_later_round_refuses_before_capture_and_graph_bypasses_it(self):
        change_id = self._iteration_ready()
        doc = self._read_doc()
        second = server.build_visual_round_start(doc, {"head": self.head, "change_id": change_id})
        self.assertEqual(second["round"], 2)
        self.assertEqual(second["supersedes"], 1)
        with mock.patch.object(server, "run_cmd") as browser:
            status, payload = server.build_visual_scenario_result(
                {"doc": doc, "change_id": change_id}, "before", None, {"scratch": str(self.repo)}
            )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "role_not_required")
        browser.assert_not_called()
        self.assertEqual(second["captures"], [])
        graph = json.loads((Path(server.__file__).resolve().parent / "graphs" / "visual-evidence.json").read_text())
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        self.assertEqual(nodes["before_required"]["config"]["rules"][0]["value"], 1)
        targets = {(edge["source"], edge["sourceHandle"]): edge["target"] for edge in graph["spec"]["edges"]}
        self.assertEqual(targets[("before_required", "pass")], "wait_before")
        self.assertEqual(targets[("before_required", "fail")], "join_candidate_capture")
        self.assertEqual(targets[("before_ok", "pass")], "join_candidate_capture")
        self.assertEqual(targets[("join_candidate_capture", "out")], "wait_candidate")

    def test_feedback_graph_refuses_missing_round_before_feedback_api(self):
        graph = json.loads((Path(server.__file__).resolve().parent / "graphs" / "visual-iteration.json").read_text())
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        self.assertEqual(nodes["feedback_round_required"]["type"], "logic.filter")
        rule = nodes["feedback_round_required"]["config"]["rules"][0]
        self.assertEqual((rule["path"], rule["op"], rule["value"]), ("CTX.INPUT.round", "greater_than", 0))
        targets = {(edge["source"], edge["sourceHandle"]): edge["target"] for edge in graph["spec"]["edges"]}
        self.assertEqual(targets[("input_switch", "case-feedback")], "feedback_round_required")
        self.assertEqual(targets[("feedback_round_required", "pass")], "feedback")
        self.assertEqual(targets[("feedback_round_required", "fail")], "join_fail")
        # The API remains authoritative as well: no round reaches neither a
        # writer claim nor a feedback turn.
        self._iteration_ready()
        result = self._iteration_post("feedback", "missing-round", notes="No identity")
        self.assertEqual(result[0], 400, result[1])
        self.assertEqual(result[1]["code"], "missing_round")
        self.assertIsNone(self._read_doc().get("writer_claim"))

    def test_return_reframe_or_split_cannot_resurrect_pre_return_candidate(self):
        for decision in ("reframe", "split"):
            with self.subTest(decision=decision):
                self.tearDown()
                self.setUp()
                change_id = self._iteration_ready()
                doc = self._read_doc()
                doc["checks"] = {"fast": {
                    "ok": True, "change_id": change_id,
                    "required": ["changed-area", "typecheck"],
                    "verdicts": {
                        "changed-area": {"ok": True, "coverage": True},
                        "typecheck": {"ok": True, "coverage": True},
                    },
                }}
                doc["evidence_rounds"][-1]["prompted_at"] = server.utcnow()
                self._write_doc(doc)
                self.assertEqual(
                    self._iteration_post("decision", "ready-before-return", decision="design_ready")[0], 200
                )
                returned = self._iteration_post(
                    "return", "return-before-reframe", finding="Visible final-gate defect", gate="final-review"
                )
                self.assertEqual(returned[0], 200, returned[1])
                body = {"decision": decision}
                if decision == "split":
                    body["note"] = "Follow up in a separate issue"
                reframed = self._iteration_post(f"decision", f"{decision}-after-return", **body)
                self.assertEqual(reframed[0], 200, reframed[1])
                refused = self._iteration_post("decision", f"ready-after-{decision}", decision="design_ready")
                self.assertEqual(refused[0], 409, refused[1])
                self.assertEqual(refused[1]["code"], "return_requires_new_candidate")
                persisted = self._read_doc()
                self.assertEqual(persisted["iteration"]["returned_candidate_change_id"], change_id)
                self.assertIsNone(persisted["iteration"]["returned_candidate_writer_change_id"])
                gaps = server.build_visual_human_path_gaps(persisted, change_id)
                self.assertTrue(gaps)
                self.assertIn("new writer-produced candidate", gaps[0])

    def test_turn_20_is_one_checkpoint_and_turn_21_has_no_ceiling(self):
        change_id = self._iteration_ready(turns=19)
        opened = self._iteration_post("feedback", "feedback-20", notes="One more turn", round=1)
        self.assertEqual(opened[0], 200, opened[1])
        self.assertEqual(opened[1]["turn"], 20)
        self.assertTrue(opened[1]["checkpoint"])
        self.assertEqual(server.build_iteration_step(self._read_doc(), change_id)[0], "checkpoint")
        continued = self._iteration_post("decision", "continue-20", decision="continue")
        self.assertEqual(continued[0], 200, continued[1])
        doc = self._read_doc()
        self.assertFalse(server.build_iteration_checkpoint_due(doc))
        doc["iteration"]["turns"][-1]["status"] = "closed"
        doc["evidence_rounds"][-1].pop("feedback", None)
        self._write_doc(doc)
        turn_21 = self._iteration_post("feedback", "feedback-21", notes="Past twenty", round=1)
        self.assertEqual(turn_21[0], 200, turn_21[1])
        self.assertEqual(turn_21[1]["turn"], 21)
        self.assertFalse(turn_21[1]["checkpoint"])
        public = server.build_iteration_public(self._read_doc())
        self.assertIsNone(public["turn_ceiling"])

    def test_decisions_have_distinct_states_and_split_history(self):
        expected = {
            "reframe": "reframed",
            "split": "split",
            "park": "parked",
            "stop": "stopped",
        }
        for decision, state in expected.items():
            with self.subTest(decision=decision):
                self.tearDown()
                self.setUp()
                self._iteration_ready()
                self.assertEqual(
                    self._iteration_post(
                        "feedback", f"feedback-{decision}", notes="Change it", round=1
                    )[0],
                    200,
                )
                body = {"decision": decision}
                if decision == "split":
                    body["note"] = "Move the table to issue #53"
                result = self._iteration_post("decision", f"decision-{decision}", **body)
                self.assertEqual(result[0], 409 if decision in ("park", "stop") else 200, result[1])
                doc = self._read_doc()
                self.assertEqual(doc["iteration"]["state"], state)
                self.assertEqual(doc["iteration"]["decisions"][-1]["decision"], decision)
                if decision == "split":
                    self.assertEqual(doc["iteration"]["splits"][-1]["note"], body["note"])

    def test_design_ready_and_final_gate_return_are_durable_and_block_commit(self):
        change_id = self._iteration_ready()
        doc = self._read_doc()
        doc["checks"] = {"fast": {
            "ok": True,
            "change_id": change_id,
            "required": ["changed-area", "typecheck"],
            "verdicts": {
                "changed-area": {"ok": True, "coverage": True},
                "typecheck": {"ok": True, "coverage": True},
            },
        }}
        doc["evidence_rounds"][-1]["prompted_at"] = server.utcnow()
        self._write_doc(doc)
        ready = self._iteration_post("decision", "design-ready", decision="design_ready")
        self.assertEqual(ready[0], 200, ready[1])
        self.assertEqual(ready[1]["state"], "design_ready")
        self.assertEqual(ready[1]["gate"], "review")
        returned = self._iteration_post(
            "return",
            "visible-return",
            finding="The final gate needs a visible focus-ring fix",
            gate="final-review",
        )
        self.assertEqual(returned[0], 200, returned[1])
        self.assertEqual(returned[1]["to"], "evidence")
        self.assertIn("human path", returned[1]["commit_blocked_until"])
        doc = self._read_doc()
        self.assertEqual(doc["iteration"]["returns"][-1]["gate"], "final-review")
        self.assertIsNone(doc["iteration"]["design_ready"])
        self.assertTrue(server.build_visual_human_path_gaps(doc, change_id))

    def test_iteration_composes_with_sensitive_route_and_keeps_stack_history(self):
        self._iteration_ready()
        doc = self._read_doc()
        self.assertEqual(doc["route"]["class"], "visual")
        doc["route"]["class"] = "sensitive"
        doc["route"]["model"] = "opus"
        doc["stacks"] = ["preview-52"]
        self._write_doc(doc)
        result = self._iteration_post("feedback", "sensitive-feedback", notes="Adjust contrast", round=1)
        self.assertEqual(result[0], 200, result[1])
        self.assertEqual(self._read_doc()["route"]["class"], "sensitive")
        self.assertEqual(self._read_doc()["stacks"], ["preview-52"])
        self.assertEqual(len(self._read_doc()["evidence_rounds"]), 1)

    def test_openapi_graph_publisher_install_catalog_readme_and_ticket_are_complete(self):
        root = Path(server.__file__).resolve().parent
        spec = json.loads((root / "openapi.json").read_text())
        operations = {
            "/v1/build/iteration/next": ("get", "buildIterationNext", None),
            "/v1/build/iteration/feedback": ("post", "buildIterationFeedback", "BuildIterationFeedbackRequest"),
            "/v1/build/iteration/report": ("post", "buildIterationReport", "BuildIterationEventRequest"),
            "/v1/build/iteration/decision": ("post", "buildIterationDecision", "BuildIterationDecisionRequest"),
            "/v1/build/iteration/return": ("post", "buildIterationReturn", "BuildIterationReturnRequest"),
        }
        for route, (method, operation_id, schema_name) in operations.items():
            op = spec["paths"][route][method]
            self.assertEqual(op["operationId"], operation_id)
            if schema_name:
                ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
                self.assertEqual(ref, f"#/components/schemas/{schema_name}")
                self.assertFalse(spec["components"]["schemas"][schema_name]["additionalProperties"])
        graph = json.loads((root / "graphs" / "visual-iteration.json").read_text())
        self.assertEqual(graph["slug"], "graphwing-visual-iteration")
        graph_text = json.dumps(graph).lower()
        for text in ("github", "changed-area", "typecheck", "planning", "design_ready", "final_gates"):
            self.assertIn(text, graph_text)
        self.assertNotIn("shortcut", graph_text)
        self.assertNotIn("strike", graph_text)
        ticket = (root / "slices" / "issue-52-pre-pr-build" / "04-visual-iteration.md").read_text().lower()
        self.assertIn("github issue #52", ticket)
        self.assertIn("ticket-03", ticket)
        self.assertNotIn("shortcut", ticket)
        self.assertIn("visual-iteration", (root / "scripts" / "publish_graphs.py").read_text())
        self.assertIn("graphs/visual-iteration.json", (root / "install.py").read_text())
        self.assertIn("graphwing-visual-iteration", (root / "README.md").read_text())
        self.assertIn("visual_iteration", (root / "server.py").read_text())

    def test_visual_iteration_graph_is_acyclic_bounded_per_turn_and_has_canary(self):
        graph = json.loads(
            (Path(server.__file__).resolve().parent / "graphs" / "visual-iteration.json").read_text()
        )
        nodes = {node["id"]: node for node in graph["spec"]["nodes"]}
        edges = graph["spec"]["edges"]
        incoming = {name: [] for name in nodes}
        outgoing = {name: [] for name in nodes}
        for edge in edges:
            incoming[edge["target"]].append(edge["source"])
            outgoing[edge["source"]].append(edge["target"])
        indegree = {name: len(sources) for name, sources in incoming.items()}
        queue = [name for name, degree in indegree.items() if degree == 0]
        seen = []
        while queue:
            name = queue.pop()
            seen.append(name)
            for target in outgoing[name]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        self.assertEqual(len(seen), len(nodes))
        for node_id, sources in incoming.items():
            if len(sources) > 1:
                self.assertEqual(nodes[node_id]["type"], "logic.join.any", node_id)
        text = json.dumps(graph)
        self.assertNotIn("/v1/git/commit", text)
        self.assertNotIn("/v1/git/push", text)
        self.assertNotIn("/v1/build/finalize", text)
        self.assertEqual(graph["canary"]["issue"], "https://github.com/tfournet/graphwing/issues/52")
        self.assertEqual(graph["canary"]["turn"], 21)


if __name__ == "__main__":
    unittest.main()
