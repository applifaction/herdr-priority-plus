import copy
import json
import multiprocessing
import os
from pathlib import Path
import socket
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch

import priority_plus as pp


def agent(status="idle", seq=1, **fields):
    return {"terminal_id": "term-1", "pane_id": "w1:p1", "agent": "pi",
            "agent_status": status, "state_change_seq": seq, "tokens": {}, **fields}


class FakeClient:
    def __init__(self, agents):
        self.agents = agents
        self.calls = []
        self.fail = None
        self.panes = []

    def call(self, method, params=None):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "agent.list":
            return {"agents": copy.deepcopy(self.agents)}
        if method == "pane.list":
            return {"panes": self.panes}
        if self.fail:
            raise pp.ApiError(self.fail)
        if method == "pane.report_metadata":
            for a in self.agents:
                if a["pane_id"] == params["pane_id"]:
                    for key, value in params.get("tokens", {}).items():
                        if value is None:
                            a["tokens"].pop(key, None)
                        else:
                            a["tokens"][key] = value
        return {}


def increment(directory, sock):
    for _ in range(12):
        with pp.state_lock(directory, sock) as path:
            state = json.loads(path.read_text()) if path.exists() else {"count": 0}
            state["count"] += 1
            pp.save_state(path, state)


class LifecycleTests(unittest.TestCase):
    def test_bootstrap_and_all_ranks(self):
        expected = {"blocked": "4", "done": "3", "working": "2", "idle": "1", "unknown": "0"}
        for status, rank in expected.items():
            with self.subTest(status=status):
                record = pp.observe(agent(status))
                self.assertEqual(pp.rank(record), rank)
                self.assertEqual(record["pending"], status == "done")

    def test_transitions_and_review(self):
        record = None
        for status, seq, pending in [("idle", 1, False), ("working", 2, False),
                                     ("blocked", 3, False), ("idle", 4, True),
                                     ("done", 4, True), ("idle", 4, True),
                                     ("working", 5, False), ("done", 6, True),
                                     ("unknown", 7, False)]:
            record = pp.observe(agent(status, seq), record)
            self.assertEqual(record["pending"], pending, (status, seq))
        self.assertEqual(pp.rank(pp.observe(agent("idle", 2), pp.observe(agent("working", 1)))), "3")

    def test_skipped_work_and_metadata_only(self):
        first = pp.observe(agent("idle", 1))
        self.assertFalse(pp.observe(agent("idle", 1, revision=900), first)["pending"])
        self.assertTrue(pp.observe(agent("idle", 3), first)["pending"])

    def test_focus_drafts_and_moves_are_not_identity_or_seen(self):
        first = pp.observe(agent("done", 3))
        moved = pp.observe(agent("idle", 3, pane_id="w9:p2", focused=True,
                                 workspace_id="w9", revision=100, seen=True,
                                 title="draft or metadata"), first)
        self.assertTrue(moved["pending"])
        self.assertNotIn("seen", moved)
        self.assertNotIn("focused", moved)
        self.assertNotIn("title", moved)
        self.assertEqual(pp.rank(moved), pp.rank(first))

    def test_rollback_agent_and_session_reset(self):
        first = pp.observe(agent("done", 10))
        for fields in ({"seq": 1}, {"agent": "codex"},
                       {"agent_session": {"source": "herdr:pi", "agent": "pi", "kind": "id", "value": "new"}}):
            args = {"seq": 10, **fields}
            with self.subTest(fields=fields):
                self.assertFalse(pp.observe(agent("idle", **args), first)["pending"])
        session = {"source": "herdr:pi", "agent": "pi", "kind": "id", "value": "old"}
        first = pp.observe(agent("done", 10, agent_session=session))
        self.assertFalse(pp.observe(agent("idle", 10), first)["pending"])

    def test_ranking_expression(self):
        self.assertNotIn("filter", pp.VIEW)
        self.assertEqual(pp.VIEW["sort"], [
            {"field": {"token": "pp_rank"}, "order": "desc"},
            {"field": "seen", "order": "asc"},
            {"field": "state_change_seq", "order": "desc"}])
        rows = [("blocked", False), ("done", False), ("idle", True), ("working", True)]
        facts = [(pp.rank(pp.observe(agent(status), pp.observe(agent("done"))) ), seen)
                 for status, seen in rows]
        self.assertEqual(sorted(facts, key=lambda r: (-int(r[0]), r[1])), facts)


class PersistenceTests(unittest.TestCase):
    def test_concurrent_writes_private_atomic_and_socket_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            workers = [multiprocessing.Process(target=increment, args=(directory, "/tmp/pp-test-socket"))
                       for _ in range(8)]
            for process in workers:
                process.start()
            for process in workers:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            with pp.state_lock(directory, "/tmp/pp-test-socket") as path:
                self.assertEqual(json.loads(path.read_text())["count"], 96)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertFalse(list(path.parent.glob(".state-*")))
            with pp.state_lock(directory, "/tmp/pp-other-socket") as path:
                self.assertFalse(path.exists())

    def test_boot_reset_retains_choice_and_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = pp.load_state(path, "boot-1")
            state["enabled"] = False
            state["agents"]["t1"] = pp.observe(agent("done"))
            pp.save_state(path, state)
            newer = pp.load_state(path, "boot-2")
            self.assertEqual(newer["agents"], {})
            self.assertFalse(newer["enabled"])
            path.write_text('{"version": 99}')
            with self.assertRaises(RuntimeError):
                pp.load_state(path, "boot-2")
            path.write_text("broken")
            with self.assertRaises(ValueError):
                pp.load_state(path, "boot-2")

    def test_lock_wait_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("priority_plus.fcntl.flock", side_effect=BlockingIOError), \
                    patch("priority_plus.time.monotonic", side_effect=[0, 21]):
                with self.assertRaises(TimeoutError):
                    with pp.state_lock(directory, "/tmp/pp-lock-timeout"):
                        self.fail("lock unexpectedly acquired")

    def test_failed_replace_leaves_previous_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            pp.save_state(path, {"old": True})
            with patch("priority_plus.os.replace", side_effect=OSError("test")):
                with self.assertRaises(OSError):
                    pp.save_state(path, {"new": True})
            self.assertEqual(json.loads(path.read_text()), {"old": True})
            self.assertEqual(len(list(path.parent.iterdir())), 1)


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.state = pp.load_state(self.path, "boot")
        self.client = FakeClient([agent("working")])

    def sync(self):
        pp.reconcile(self.client, self.state, self.path)

    def writes(self):
        return [p for m, p in self.client.calls if m == "pane.report_metadata"]

    def test_idempotence_all_writes_owned_and_foreign_metadata_untouched(self):
        self.client.agents[0]["tokens"] = {"foreign": "keep"}
        self.sync()
        self.client.calls.clear()
        self.sync()
        self.assertEqual(self.writes(), [])
        self.client.agents[0].update(agent_status="idle", state_change_seq=2)
        self.sync()
        self.assertEqual(self.writes()[0]["state_labels"], {"idle": pp.LABEL})
        self.client.calls.clear()
        self.client.agents[0]["state_labels"] = {"idle": "foreign later label"}
        self.sync()
        self.assertEqual(self.writes(), [])  # Don't fight another label owner.
        self.client.agents[0].update(agent_status="working", state_change_seq=3)
        self.sync()
        for fields in self.writes():
            self.assertEqual(fields["source"], pp.SOURCE)
            self.assertTrue(fields["clear_state_labels"])
            self.assertEqual(set(fields["tokens"]), {pp.TOKEN})
        self.assertEqual(self.client.agents[0]["tokens"]["foreign"], "keep")

    def test_disappearance_clear_surviving_terminal_but_not_replacement(self):
        self.sync()
        self.client.panes = [agent(pane_id="w2:p9"), agent(terminal_id="replacement", pane_id="w1:p1")]
        self.client.agents = []
        self.client.calls.clear()
        self.sync()
        self.assertEqual([p["pane_id"] for p in self.writes()], ["w2:p9"])
        self.assertEqual(self.state["agents"], {})

    def test_api_failure_keeps_observation_for_retry(self):
        self.sync()
        self.client.agents[0].update(agent_status="idle", state_change_seq=2)
        self.client.fail = "invalid_metadata"
        with self.assertRaises(pp.ApiError):
            self.sync()
        self.state = pp.load_state(self.path, "boot")
        self.assertTrue(self.state["agents"]["term-1"]["pending"])
        self.assertFalse(self.state["agents"]["term-1"]["applied"])
        self.client.fail = None
        self.sync()
        self.assertTrue(self.state["agents"]["term-1"]["applied"])

    def test_pane_closed_during_write_is_tolerated(self):
        self.client.fail = "pane_not_found"
        self.sync()
        self.assertIsNone(self.state["agents"]["term-1"]["applied"])

    def test_cleanup_source_scoped_and_quiescent_then_enable(self):
        self.client.boot = "boot"
        self.client.close = lambda: None
        self.client.panes = [agent(tokens={pp.TOKEN: "3", "other": "keep"})]
        with patch("priority_plus.Client", return_value=self.client):
            pp.run("cleanup", self.temp.name, "/tmp/pp-action-test")
            self.assertIn(("agent.view.clear", {"source": pp.SOURCE}), self.client.calls)
            fields = self.writes()[-1]
            self.assertEqual(fields["tokens"], {pp.TOKEN: None})
            self.assertEqual(fields["source"], pp.SOURCE)
            self.client.calls.clear()
            pp.run("sync", self.temp.name, "/tmp/pp-action-test")
            self.assertEqual(self.client.calls, [])
            pp.run("enable", self.temp.name, "/tmp/pp-action-test")
            self.assertIn(("agent.view.set", pp.VIEW), self.client.calls)
            self.client.calls.clear()
            pp.run("sync", self.temp.name, "/tmp/pp-action-test")
            self.assertFalse(any(m.startswith("agent.view") for m, _ in self.client.calls))
            pp.run("toggle", self.temp.name, "/tmp/pp-action-test")
            self.assertEqual(self.client.calls[-1], ("agent.view.clear", {"source": pp.SOURCE}))


class SocketTests(unittest.TestCase):
    def test_real_socket_peer_identity_and_malformed_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/api.sock"
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen()
            self.addCleanup(server.close)

            def serve():
                connection, _ = server.accept()
                with connection:
                    connection.recv(8192)
                    connection.sendall(b'{"id":"wrong","result":{}}\n')

            thread = threading.Thread(target=serve)
            thread.start()
            client = pp.Client(path)
            try:
                self.assertIn(f":{os.getpid()}:", client.boot)
                with self.assertRaisesRegex(RuntimeError, "response id"):
                    client.call("agent.list")
            finally:
                client.close()
                thread.join(5)

    def test_socket_timeout_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            server = socket.socket(socket.AF_UNIX)
            path = directory + "/api.sock"
            server.bind(path)
            server.listen()
            self.addCleanup(server.close)
            client = pp.Client(path)
            connection, _ = server.accept()
            self.addCleanup(connection.close)
            try:
                client.sock.settimeout(0.05)
                with self.assertRaises(TimeoutError):
                    client.call("agent.list")
            finally:
                client.close()

    def test_eof_and_api_error_are_not_silent(self):
        from io import BytesIO
        client = object.__new__(pp.Client)
        client.serial = 0
        from unittest.mock import Mock
        client.sock = Mock()
        client.reader = BytesIO(b"")
        with self.assertRaisesRegex(RuntimeError, "response missing"):
            client.request("agent.list", {})
        client.reader = BytesIO(b'{"id":"2","error":{"code":"invalid_metadata"}}\n')
        with self.assertRaises(pp.ApiError):
            client.request("agent.list", {})

    def test_manifest_is_one_shot_and_no_focus_or_output_hooks(self):
        root = Path(__file__).resolve().parents[1]
        manifest = tomllib.loads((root / "herdr-plugin.toml").read_text())
        self.assertEqual(manifest["id"], pp.PLUGIN)
        self.assertEqual(manifest["platforms"], ["linux"])
        self.assertEqual(manifest["startup"][0]["command"], ["python3", "priority_plus.py", "startup"])
        self.assertEqual({a["id"] for a in manifest["actions"]}, {"enable", "disable", "toggle", "cleanup"})
        hooks = {e["on"] for e in manifest["events"]}
        self.assertIn("pane.agent_status_changed", hooks)
        self.assertTrue(hooks.isdisjoint({"pane.updated", "pane.focused", "pane.output"}))
        self.assertTrue(all(e["command"] == ["python3", "priority_plus.py", "sync"]
                            for e in manifest["events"]))

    def test_missing_environment_fails_without_default_socket(self):
        with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["priority_plus.py", "sync"]):
            self.assertEqual(pp.main(), 1)


if __name__ == "__main__":
    unittest.main()
