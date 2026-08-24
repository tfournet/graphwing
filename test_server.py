#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
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
        self.assertEqual(spec["info"]["version"], "0.5.2")
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
                server.herdr_job_done(job)
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
        self.assertEqual(head["config"]["path"], "{{ TASKS.frontier.data.path }}")
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
        self.assertEqual(payload["reviewer1"], "sol")
        status, payload, _ = server.dispatch(
            "POST", "/v1/slice/route", {}, True, b'{"class":"visual","size":"M"}'
        )
        self.assertEqual(payload["launcher"], "claude")
        self.assertEqual(payload["reviewer1"], "sol")
        status, payload, _ = server.dispatch(
            "POST", "/v1/slice/route", {}, True, b'{"class":"sensitive","size":"M"}'
        )
        self.assertEqual(payload["reviewer1"], "sol")
        self.assertEqual(payload["reviewer2"], "opus")
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


if __name__ == "__main__":
    unittest.main()
