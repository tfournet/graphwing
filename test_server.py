#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
import threading
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
        rp = mock.patch.object(server, "load_repos", return_value={"scratch": str(self.repo)})
        rp.start()
        self.addCleanup(rp.stop)

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
            "route": {"launcher": "hermes", "model": "sol"},
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
        for i, action in enumerate(("start_writer", "writer_done", "verify_passed"), start=1):
            status, payload, _ = self._advance(action, f"evt-{i}", holder=holder)
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
        steps = [
            ("start_writer", "writing", {"writer_session": "gwslice-" + "0" * 32}),
            ("writer_done", "verifying", {}),
            # needs_visual_proof is false, so evidence is skipped outright.
            ("verify_passed", "review", {}),
            ("review_passed", "ready", {"review": {"author": "sol", "verdict": "clear"}}),
            ("pr_opened", "pr_opened", {"pr": {"number": 7, "url": "https://example.test/pull/7"}}),
        ]
        for i, (action, stage, data) in enumerate(steps, start=1):
            status, payload, _ = self._advance(action, f"evt-{i}", data=data)
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["stage"], stage, action)
            self.assertEqual(payload["receipt"]["to"], stage)
        self.assertIsNone(payload["next_action"])
        self.assertIsNone(payload["lease"], "a finished build must not hold a lease")
        self.assertEqual(payload["writer_session"], "gwslice-" + "0" * 32)
        self.assertEqual(payload["pr"]["number"], 7)
        self.assertEqual(payload["budget_left"], 7)

    def test_evidence_is_a_real_stage_when_visual_proof_is_declared(self):
        self.assertEqual(
            self._create(verification={"fast": [], "integration": [], "needs_visual_proof": True})[0], 201
        )
        for i, action in enumerate(("start_writer", "writer_done", "verify_passed"), start=1):
            status, payload, _ = self._advance(action, f"evt-{i}")
        self.assertEqual(payload["stage"], "evidence", payload)
        self.assertEqual(payload["next_action"], "evidence_captured")
        status, payload, _ = self._advance(
            "evidence_captured", "evt-4", data={"round": {"n": 1, "shots": ["home.png"]}}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["stage"], "review")
        self.assertEqual(payload["evidence_rounds"], [{"n": 1, "shots": ["home.png"]}])

    def test_an_out_of_order_action_is_refused_and_names_the_expected_one(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._advance("review_passed", "evt-wrong")
        self.assertEqual(status, 409, payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "invalid_transition")
        self.assertEqual(payload["expected_action"], "start_writer")
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

        status, payload, _ = self._advance("start_writer", "evt-empty", data={}, expect={})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["stage"], "writing")

        status, payload, _ = self._advance("writer_done", "evt-omitted")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["stage"], "verifying")

    # -- idempotency --------------------------------------------------------

    def test_replaying_an_event_id_returns_the_receipt_without_rerunning(self):
        self.assertEqual(self._create()[0], 201)
        first = self._advance("start_writer", "evt-1", data={"writer_session": "gwslice-" + "1" * 32})[1]
        status, payload, _ = self._advance("start_writer", "evt-1", data={"writer_session": "gwslice-" + "1" * 32})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["replayed"])
        self.assertEqual(payload["receipt"], first["receipt"])
        self.assertEqual(payload["stage"], "writing")
        self.assertEqual(payload["budget_left"], first["budget_left"], "a replay spends no budget")
        self.assertEqual(payload["event_count"], 1, "one id accepted, however many times it arrives")

    def test_a_successful_event_replays_after_its_recent_receipt_ages_out(self):
        self.assertEqual(self._create()[0], 201)
        first_status, first, _ = self._advance("start_writer", "evt-old")
        self.assertEqual(first_status, 200, first)
        self._age_out_event_receipt("evt-old")
        before = self._doc_path().read_bytes()

        status, payload, _ = self._advance("start_writer", "evt-old")
        self.assertEqual(status, first_status, payload)
        self.assertTrue(payload["replayed"])
        self.assertEqual(payload["receipt"], first["receipt"])
        self.assertEqual(self._doc_path().read_bytes(), before, "a replay must not rewrite state")

    def test_reusing_an_event_id_with_different_input_is_a_conflict(self):
        self.assertEqual(self._create()[0], 201)
        first = self._advance("start_writer", "evt-1", data={"writer_session": "gwslice-" + "1" * 32})[1]
        status, payload, _ = self._advance("start_writer", "evt-1", data={"writer_session": "gwslice-" + "2" * 32})
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
        first = self._advance("start_writer", "evt-1")[1]
        for field, value in (
            ("holder", "rewst-run-2"),
            ("jobs_spent", 9),
            ("lease_seconds", 900),
            ("release_lease", True),
        ):
            with self.subTest(field=field):
                status, payload, _ = self._advance("start_writer", "evt-1", **{field: value})
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
        held = self._advance("start_writer", "evt-1", holder="rewst-run-1")[1]
        self.assertEqual(held["lease"]["holder"], "rewst-run-1")
        status, payload, _ = self._advance("writer_done", "evt-2", holder="rewst-run-2")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "lease_held")
        self.assertEqual(self._state()[1]["stage"], "writing", "the loser must not advance the build")

    def test_the_lease_holder_keeps_going_without_reacquiring(self):
        self.assertEqual(self._create()[0], 201)
        self.assertEqual(self._advance("start_writer", "evt-1", holder="rewst-run-1")[0], 200)
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
        status, payload, _ = self._advance("review_passed", "evt-4", holder="rewst-run-2")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["stage"], "ready")
        self.assertEqual(payload["lease"]["holder"], "rewst-run-2")
        self.assertEqual(payload["verified_boundary"], boundary, "recovery must not roll the boundary back")

    def test_releasing_the_lease_hands_the_build_to_the_next_caller(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._advance("start_writer", "evt-1", holder="rewst-run-1", release_lease=True)
        self.assertEqual(status, 200, payload)
        self.assertIsNone(payload["lease"])
        status, payload, _ = self._advance("writer_done", "evt-2", holder="rewst-run-2")
        self.assertEqual(status, 200, payload)

    def test_release_lease_accepts_omission_or_booleans_and_rejects_coercion(self):
        self.assertEqual(self._create()[0], 201)
        status, payload, _ = self._advance("start_writer", "evt-1")
        self.assertEqual(status, 200, payload)
        self.assertIsNotNone(payload["lease"], "omission defaults to keeping the lease")

        status, payload, _ = self._advance("writer_done", "evt-2", release_lease=False)
        self.assertEqual(status, 200, payload)
        self.assertIsNotNone(payload["lease"])

        status, payload, _ = self._advance("verify_passed", "evt-3", release_lease=True)
        self.assertEqual(status, 200, payload)
        self.assertIsNone(payload["lease"])

        before = self._doc_path().read_bytes()
        for i, bad in enumerate(("false", "true", 0, 1, None, [], {}), start=4):
            with self.subTest(release_lease=bad):
                status, payload, _ = self._advance("review_passed", f"evt-{i}", release_lease=bad)
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
        status, payload, _ = self._advance("start_writer", "evt-1", data={"head": "b" * 40})
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
        self.assertEqual(self._advance("start_writer", "evt-1", data={"writer_session": session})[0], 200)
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
        self.assertEqual(self._advance("start_writer", "evt-1")[0], 200)
        status, payload, _ = self._advance("writer_done", "evt-2")
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
        for i, (action, data) in enumerate(
            (
                ("start_writer", {"writer_session": session, "stacks": ["riftwing-52", "gw-52"]}),
                ("writer_done", {}),
                ("verify_passed", {}),
                ("evidence_captured", {"round": {"n": 1, "shots": ["home.png"]}}),
                ("review_passed", {"review": {"author": "sol", "verdict": "clear"}}),
                ("pr_opened", {"pr": {"number": 7, "url": "https://example.test/pull/7"}}),
            ),
            start=1,
        ):
            self.assertEqual(self._advance(action, f"evt-{i}", data=data)[0], 200, action)
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
        self.assertEqual(after["route"], {"launcher": "hermes", "model": "sol"})
        self.assertTrue(after["verification"]["needs_visual_proof"])
        self.assertEqual(after["writer_session"], session)
        self.assertEqual(after["stacks"], ["riftwing-52", "gw-52"])
        self.assertEqual(after["evidence_rounds"], [{"n": 1, "shots": ["home.png"]}])
        self.assertEqual(after["reviews"], [{"author": "sol", "verdict": "clear"}])
        self.assertEqual(after["verified_boundary"]["head"], self.head)
        self.assertEqual(after["budget"], {"jobs_max": 12, "jobs_used": 6})
        self.assertEqual(after["budget_left"], 6)
        self.assertEqual(after["event_count"], 6)
        self.assertEqual([r["event_id"] for r in after["receipts"]], [f"evt-{i}" for i in range(1, 7)])
        self.assertEqual(after["pr"], {"number": 7, "url": "https://example.test/pull/7"})
        self.assertIsNone(after["lease"], "a finished build holds no lease across a restart either")
        self.assertEqual(after["stage"], "pr_opened")
        self.assertIsNone(after["next_action"])

    def test_writes_are_atomic_and_leave_no_partial_file(self):
        self.assertEqual(self._create()[0], 201)
        for i, action in enumerate(("start_writer", "writer_done", "verify_passed"), start=1):
            self.assertEqual(self._advance(action, f"evt-{i}")[0], 200, action)
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
        self.assertEqual(self._advance("start_writer", "evt-1")[0], 200)
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
        self.assertEqual(self._advance("start_writer", "evt-1")[0], 200)
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
        review = payload["reviews"][-1]
        self.assertEqual(review["token"], "[redacted]")
        self.assertEqual(review["hook_secret"], "[redacted]")
        self.assertEqual(review["note"], "[redacted]", "a token pasted into prose is still a token")
        # `author` contains no credential; a substring match on "auth" would
        # have eaten the one field that says who reviewed.
        self.assertEqual(review["author"], "sol")
        self.assertEqual(review["verdict"], "clear")
        self.assertNotIn("ghp_", self._doc_path().read_text())

    def test_a_resume_url_is_a_credential_even_though_its_name_looks_harmless(self):
        # kick_url and resume_url are what Rewst hands a build to continue a
        # run. Possessing either one is possessing the right to resume it, and
        # neither name contains a secret-shaped word, so a segment match alone
        # writes both of them to disk in the clear.
        resume = "https://rewst.test/webhooks/resume/9f2c"
        self.assertEqual(
            self._create(route={"launcher": "hermes", "model": "sol", "kick_url": resume})[0], 201
        )
        route = self._state()[1]["route"]
        self.assertEqual(route["kick_url"], "[redacted]")
        self.assertEqual(route["launcher"], "hermes", "the routing the build needs must survive")
        for i, action in enumerate(("start_writer", "writer_done", "verify_passed"), start=1):
            self.assertEqual(self._advance(action, f"evt-{i}")[0], 200, action)
        status, payload, _ = self._advance(
            "review_passed",
            "evt-4",
            data={
                "review": {
                    "author": "sol",
                    "verdict": "clear",
                    "resume_url": resume,
                    "callbackUrl": resume,
                    "webhook_url": resume,
                    "clientSecret": "cs-abcdefgh",
                    "refresh_token": "rt-abcdefgh",
                    "AWS_SECRET_ACCESS_KEY": "wJalr",
                    "signed_link": "https://example.test/artifact?signature=abcdef",
                    "mirror": "https://deploy:hunter2@example.test/repo.git",
                    "docs_url": "https://example.test/docs?utm_source=graphwing&page=2",
                    "commit": self.head,
                }
            },
        )
        self.assertEqual(status, 200, payload)
        review = payload["reviews"][-1]
        for field in (
            "resume_url",
            "callbackUrl",
            "webhook_url",
            "clientSecret",
            "refresh_token",
            "AWS_SECRET_ACCESS_KEY",
            "signed_link",
            "mirror",
        ):
            self.assertEqual(review[field], "[redacted]", field)
        # A public URL and a 40-hex object id are the two things redaction most
        # easily eats. Losing the head would take the identity guard with it.
        self.assertEqual(review["docs_url"], "https://example.test/docs?utm_source=graphwing&page=2")
        self.assertEqual(review["commit"], self.head)
        self.assertEqual(review["author"], "sol")
        on_disk = self._doc_path().read_text()
        for leaked in ("rewst.test/webhooks/resume", "hunter2", "cs-abcdefgh", "rt-abcdefgh"):
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


if __name__ == "__main__":
    unittest.main()
