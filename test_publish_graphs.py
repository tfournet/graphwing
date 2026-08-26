#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parent / "scripts" / "publish_graphs.py"
SPEC = importlib.util.spec_from_file_location("publish_graphs", MODULE_PATH)
publish_graphs = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(publish_graphs)


class PublishGraphsTests(unittest.TestCase):
    def test_all_publishes_source_before_listener_and_substitutes_source_id(self):
        install = {"instance_id": "instance-1"}
        published_order = []
        loaded = []

        def fake_load(stem, instance, hook_secret="", status_repo="", source_workflow_id=""):
            loaded.append((stem, source_workflow_id))
            return {"name": stem, "slug": stem, "description": stem, "spec": {"source": source_workflow_id}}

        def fake_upsert(_mcp, name, _slug, _description, _spec, _tags):
            published_order.append(name)
            return f"wf-{name}", f"ver-{name}", name

        with (
            mock.patch.object(publish_graphs, "load_graph", side_effect=fake_load),
            mock.patch.object(publish_graphs, "upsert_workflow", side_effect=fake_upsert),
            mock.patch.object(publish_graphs, "verify_workflow_parity", return_value={"readback": True}),
        ):
            published = publish_graphs.publish_selected(
                "mcp", install, publish_graphs.resolve_stems("all"), "instance-1", "", ""
            )

        self.assertLess(published_order.index("pre-pr-build"), published_order.index("build-completion-supervisor"))
        listener_load = next(row for row in loaded if row[0] == "build-completion-supervisor")
        self.assertEqual(listener_load[1], "wf-pre-pr-build")
        self.assertEqual(published["pre-pr-build"]["workflow_id"], "wf-pre-pr-build")

    def test_listener_only_requires_an_already_published_source_id(self):
        with self.assertRaisesRegex(SystemExit, "pre-pr-build.*workflow_id"):
            publish_graphs.publish_selected(
                "mcp", {"instance_id": "instance-1"}, ["build-completion-supervisor"], "instance-1", "", ""
            )

    def test_listener_only_substitutes_the_persisted_source_id(self):
        install = {"pre_pr_build": {"workflow_id": "wf-source-existing"}}
        seen = {}

        def fake_load(stem, instance, hook_secret="", status_repo="", source_workflow_id=""):
            seen["source"] = source_workflow_id
            return {"name": stem, "slug": stem, "description": stem, "spec": {}}

        with (
            mock.patch.object(publish_graphs, "load_graph", side_effect=fake_load),
            mock.patch.object(
                publish_graphs,
                "upsert_workflow",
                return_value=("wf-listener", "ver-listener", "build-completion-supervisor"),
            ),
            mock.patch.object(publish_graphs, "verify_workflow_parity", return_value={"readback": True}),
        ):
            publish_graphs.publish_selected(
                "mcp", install, ["build-completion-supervisor"], "instance-1", "", ""
            )
        self.assertEqual(seen["source"], "wf-source-existing")

    def test_source_and_listener_ids_are_persisted_intentionally(self):
        install = {"org_id": "org-1", "instance_id": "instance-1"}
        published = {
            "pre-pr-build": {"workflow_id": "wf-source", "workflow_version_id": "v1", "slug": "source"},
            "build-completion-supervisor": {
                "workflow_id": "wf-listener",
                "workflow_version_id": "v2",
                "slug": "listener",
            },
        }
        publish_graphs.persist_published(install, published)
        self.assertEqual(install["pre_pr_build"]["workflow_id"], "wf-source")
        self.assertEqual(install["build_completion_supervisor"]["workflow_id"], "wf-listener")

    def test_graph_substitution_removes_the_source_placeholder(self):
        graph = publish_graphs.load_graph(
            "build-completion-supervisor", "instance-1", source_workflow_id="wf-source"
        )
        raw = json.dumps(graph)
        self.assertNotIn("$GRAPHWING_SOURCE_WORKFLOW_ID", raw)
        self.assertIn("wf-source", raw)

    def test_publish_passes_source_and_listener_top_level_tags(self):
        seen = {}

        def fake_upsert(_mcp, name, _slug, _description, _spec, tags):
            seen[name] = tags
            return f"wf-{name}", f"ver-{name}", name

        with mock.patch.object(publish_graphs, "upsert_workflow", side_effect=fake_upsert), \
             mock.patch.object(publish_graphs, "verify_workflow_parity", return_value={"readback": True}):
            publish_graphs.publish_selected(
                "mcp", {}, ["pre-pr-build", "build-completion-supervisor"], "instance-1", "", ""
            )
        self.assertEqual(seen["graphwing-pre-pr-build"], ["graphwing-supervised"])
        self.assertEqual(seen["graphwing-build-completion-supervisor"], [])

    def test_upsert_includes_tags_on_create_and_version_patch(self):
        calls = []

        def fake_api(_mcp, method, path, body=None, timeout=120):
            calls.append((method, path, body))
            if method == "GET":
                return 404, {}
            if method == "POST" and path == "/workflows":
                return 201, {"id": "wf-1", "slug": "source", "currentVersion": {"id": "v1"}}
            return 200, {}

        with mock.patch.object(publish_graphs, "api", side_effect=fake_api):
            publish_graphs.upsert_workflow("mcp", "source", "source", "desc", {"nodes": []}, ["tag-a"])
        self.assertIn(
            ("POST", "/workflows", {"name": "source", "description": "desc", "tags": ["tag-a"]}), calls
        )
        self.assertIn(
            ("PATCH", "/workflows/wf-1/versions/v1", {"spec": {"nodes": []}, "tags": ["tag-a"]}), calls
        )

    def test_normalized_live_readback_is_required_and_never_uses_submitted_bytes(self):
        expected = {"nodes": [{"id": "a", "config": {"x": 1, "y": 2}}]}
        reordered = {"nodes": [{"config": {"y": 2, "x": 1}, "id": "a"}]}
        receipt = publish_graphs.require_catalog_parity(expected, reordered, "graph")
        self.assertEqual(receipt["live_source"], "fresh_post_publish_api_read")
        changed = {"nodes": [{"id": "a", "config": {"x": 99, "y": 2}}]}
        with self.assertRaisesRegex(SystemExit, "mismatch; no parity receipt"):
            publish_graphs.require_catalog_parity(expected, changed, "graph")
        with self.assertRaisesRegex(SystemExit, "unreadable; no parity receipt"):
            publish_graphs.require_catalog_parity(expected, None, "graph")

    def test_parity_reads_real_api_envelopes_and_ignores_only_volatile_metadata(self):
        expected = {"nodes": [{"id": "a", "config": {"x": 1}}]}
        live = {"data": {"workflow": {"currentVersion": {"spec": {
            "nodes": [{"id": "a", "config": {"x": 1}}], "updatedAt": "volatile",
        }}}}}
        self.assertEqual(publish_graphs.workflow_readback_spec(live)["nodes"], expected["nodes"])
        self.assertEqual(publish_graphs.catalog_hash(expected), publish_graphs.catalog_hash(publish_graphs.workflow_readback_spec(live)))
        altered = {"nodes": [{"id": "a", "config": {"x": 2}}], "updatedAt": "volatile"}
        with self.assertRaises(SystemExit):
            publish_graphs.require_catalog_parity(expected, altered, "graph")


if __name__ == "__main__":
    unittest.main()
