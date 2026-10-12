import copy
import json
import multiprocessing
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import tomllib
import unittest
from unittest.mock import patch

import priority_plus as pp


def agent(status="idle", seq=1, **fields):
    return {"terminal_id": "term-1", "pane_id": "w1:p1", "workspace_id": "w1", "agent": "pi",
            "agent_status": status, "state_change_seq": seq, "tokens": {}, **fields}


class FakeClient:
    def __init__(self, agents):
        self.agents = agents
        self.calls = []
        self.fail = None
        self.panes = []
        self.tabs = []
        self.workspaces = [{"workspace_id": "w1", "label": "main"}]

    def call(self, method, params=None):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "agent.list":
            return {"agents": copy.deepcopy(self.agents)}
        if method == "pane.list":
            return {"panes": self.panes}
        if method == "tab.list":
            return {"tabs": copy.deepcopy(self.tabs)}
        if method == "workspace.list":
            return {"workspaces": copy.deepcopy(self.workspaces)}
        if self.fail:
            raise pp.ApiError(self.fail)
        if method == "workspace.move_block":
            moved = set(params["workspace_ids"])
            by_id = {workspace["workspace_id"]: workspace for workspace in self.workspaces}
            self.workspaces = [workspace for workspace in self.workspaces
                               if workspace["workspace_id"] not in moved]
            self.workspaces.extend(by_id[workspace_id] for workspace_id in params["workspace_ids"])
            return {"workspaces": copy.deepcopy(self.workspaces)}
        if method == "pane.report_metadata":
            for a in self.agents:
                if a["pane_id"] == params["pane_id"]:
                    for key, value in params.get("tokens", {}).items():
                        if value is None:
                            a["tokens"].pop(key, None)
                        else:
                            a["tokens"][key] = value
                    if params.get("clear_state_labels"):
                        a.pop("state_labels", None)
                    if "state_labels" in params:
                        a["state_labels"] = copy.deepcopy(params["state_labels"])
        return {}


def increment(directory, sock):
    for _ in range(12):
        with pp.state_lock(directory, sock) as path:
            state = json.loads(path.read_text()) if path.exists() else {"count": 0}
            state["count"] += 1
            pp.save_state(path, state)


class LifecycleTests(unittest.TestCase):
    def test_only_explicit_activity_badges_are_active(self):
        self.assertFalse(hasattr(pp, "LABEL"))
        self.assertEqual(pp.LEGACY_PENDING_LABEL, "◉ Awaiting reply")
        self.assertEqual(pp.SUBAGENT_LABEL, "⏳ Subagent working")
        self.assertEqual(pp.PARALLEL_WAIT_LABEL_PREFIX, "⏳ Warte auf ")

    def test_bootstrap_and_all_ranks(self):
        expected = {"blocked": "7", "done": "6", "working": "5", "idle": "3", "unknown": "0"}
        for status, expected_rank in expected.items():
            with self.subTest(status=status):
                record = pp.observe(agent(status))
                self.assertEqual(pp.rank(record), expected_rank)
                self.assertEqual(record["pending"], status == "done")
                self.assertFalse(record["subagent"])
        record = pp.observe(agent("idle", tokens={"summary": "⏳ 2 subagents (worker, reviewer)"}))
        self.assertTrue(record["subagent"])
        self.assertEqual(pp.rank(record), "4")
        blocked_with_child = pp.observe(agent(
            "blocked", tokens={"summary": "⏳ 1 subagent (worker)"}))
        self.assertEqual(pp.rank(blocked_with_child), pp.RANK_SUBAGENT)
        parallel_wait = pp.observe(agent(
            "idle", tokens={pp.PARALLEL_WAIT_TARGET_TOKEN: "history & release"}))
        self.assertEqual(parallel_wait["parallel_wait"], "history & release")
        self.assertEqual(pp.rank(parallel_wait), pp.RANK_WORKING)

    def test_transitions_and_review(self):
        record = None
        for status, seq, pending in [("idle", 1, False), ("working", 2, False),
                                     ("blocked", 3, False), ("idle", 4, True),
                                     ("done", 4, True), ("idle", 4, True),
                                     ("working", 5, False), ("done", 6, True),
                                     ("unknown", 7, False)]:
            record = pp.observe(agent(status, seq), record)
            self.assertEqual(record["pending"], pending, (status, seq))
        self.assertEqual(pp.rank(pp.observe(agent("idle", 2), pp.observe(agent("working", 1)))), "6")

    def test_skipped_work_and_metadata_only(self):
        first = pp.observe(agent("idle", 1))
        self.assertFalse(pp.observe(agent("idle", 1, revision=900), first)["pending"])
        self.assertTrue(pp.observe(agent("idle", 3), first)["pending"])

    def test_parked_workspace_detection_is_case_sensitive_prefix(self):
        for label in ("WAIT", "WAIT/MP", "WAITING", "WAIT-list"):
            with self.subTest(label=label):
                self.assertTrue(pp.is_wait_workspace(label))
                self.assertTrue(pp.is_parked_workspace(label))
                self.assertFalse(pp.is_later_workspace(label))
        for label in ("LATER", "LATER/MP", "LATERAL", "LATER-list"):
            with self.subTest(label=label):
                self.assertTrue(pp.is_later_workspace(label))
                self.assertTrue(pp.is_parked_workspace(label))
                self.assertFalse(pp.is_wait_workspace(label))
        for label in (None, "", "wait/MP", "later/MP", " LATER/MP", "AWAIT/MP"):
            with self.subTest(label=label):
                self.assertFalse(pp.is_wait_workspace(label))
                self.assertFalse(pp.is_later_workspace(label))
                self.assertFalse(pp.is_parked_workspace(label))

    def test_wait_and_later_workspaces_keep_active_states_visible(self):
        for status in ("working", "blocked"):
            with self.subTest(status=status):
                normal = pp.observe(agent(status))
                later = pp.observe(agent(status), parking="later")
                waiting = pp.observe(agent(status), parking="wait")
                self.assertEqual(pp.rank(normal), pp.rank(later))
                self.assertEqual(pp.rank(normal), pp.rank(waiting))
        active = pp.observe(agent("done", tokens={"summary": "⏳ 1 subagent"}), parking="later")
        self.assertEqual(pp.rank(active), "4")

    def test_inactive_agent_order_is_normal_then_later_then_wait(self):
        normal = pp.observe(agent("idle"))
        later = pp.observe(agent("idle"), parking="later")
        waiting = pp.observe(agent("idle"), parking="wait")
        self.assertEqual([pp.rank(normal), pp.rank(later), pp.rank(waiting)], ["3", "2", "1"])

    def test_subagent_summary_detection_and_pending_preservation(self):
        for summary in ("⏳ 1 subagent", "⏳ 2 subagents (worker, reviewer)"):
            with self.subTest(summary=summary):
                self.assertTrue(pp.has_active_subagents(agent(tokens={"summary": summary})))
        for summary in (None, "", "⏳ 0 subagents", "1 subagent", "⏳ subagent", "unrelated"):
            with self.subTest(summary=summary):
                tokens = {} if summary is None else {"summary": summary}
                self.assertFalse(pp.has_active_subagents(agent(tokens=tokens)))

        completed = pp.observe(agent("done", 4))
        delegated = pp.observe(agent("idle", 4, tokens={"summary": "⏳ 1 subagent (worker)"}), completed)
        self.assertTrue(delegated["pending"])
        self.assertTrue(delegated["subagent"])
        self.assertEqual(pp.rank(delegated), "4")
        resumed = pp.observe(agent("idle", 4), delegated)
        self.assertTrue(resumed["pending"])
        self.assertFalse(resumed["subagent"])
        self.assertEqual(pp.rank(resumed), "6")

        plain = pp.observe(agent("idle", 1))
        delegated = pp.observe(agent("idle", 1, tokens={"summary": "⏳ 1 subagent"}), plain)
        self.assertFalse(pp.observe(agent("idle", 1), delegated)["pending"])

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

    def test_workspace_and_metadata_sort_keys_are_lexically_safe(self):
        self.assertEqual(pp.workspace_order_key(0), "00000000000000000000")
        self.assertLess(pp.workspace_order_key(2), pp.workspace_order_key(10))
        self.assertEqual(pp.metadata_sort_key("Ä" * 100), ("ä" * 80))
        self.assertEqual(len(pp.metadata_sort_key("A" * 100)), pp.METADATA_TOKEN_CHAR_LIMIT)

    def test_ranking_expression(self):
        self.assertNotIn("filter", pp.VIEW)
        self.assertEqual(pp.VIEW["sort"], [
            {"field": {"token": "pp_rank"}, "order": "desc"},
            {"field": {"token": "pp_workspace_sort"}, "order": "asc"},
            {"field": {"token": "pp_session_sort"}, "order": "asc"},
            {"field": "seen", "order": "asc"},
            {"field": "state_change_seq", "order": "desc"}])
        rows = [("blocked", False), ("done", False), ("idle", True), ("working", True)]
        facts = [(pp.rank(pp.observe(agent(status), pp.observe(agent("done")))), seen)
                 for status, seen in rows]
        facts.append((pp.rank(pp.observe(agent("idle", tokens={"summary": "⏳ 1 subagent"}))), True))
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


class WatcherTests(unittest.TestCase):
    def pane_event(self, **pane):
        return {"event": "pane_updated", "data": {"type": "pane_updated", "pane": pane}}

    def test_legacy_pending_badge_requires_reconciliation_until_removed(self):
        legacy = self.pane_event(
            pane_id="w1:p1",
            tokens={pp.TOKEN: pp.RANK_PENDING},
            state_labels={"idle": pp.LEGACY_PENDING_LABEL},
        )
        self.assertTrue(pp.needs_reconcile_event(legacy))

        cleaned = self.pane_event(
            pane_id="w1:p1",
            tokens={pp.TOKEN: pp.RANK_PENDING},
        )
        self.assertFalse(pp.needs_reconcile_event(cleaned))

    def test_parallel_wait_metadata_edges_require_reconciliation(self):
        wait_label = pp.parallel_wait_label("history & release")
        active = self.pane_event(
            pane_id="w1:p1",
            tokens={pp.PARALLEL_WAIT_TARGET_TOKEN: "history & release", pp.TOKEN: "3"},
        )
        self.assertTrue(pp.needs_reconcile_event(active))

        converged = self.pane_event(
            pane_id="w1:p1",
            tokens={pp.PARALLEL_WAIT_TARGET_TOKEN: "history & release",
                    pp.TOKEN: pp.RANK_WORKING},
            state_labels={state: wait_label for state in pp.SUBAGENT_STATES},
        )
        self.assertFalse(pp.needs_reconcile_event(converged))

        removed = self.pane_event(
            pane_id="w1:p1",
            tokens={pp.TOKEN: pp.RANK_WORKING},
            state_labels={"idle": wait_label},
        )
        self.assertTrue(pp.needs_reconcile_event(removed))

    def test_only_subagent_metadata_edges_require_reconciliation(self):
        active = self.pane_event(
            pane_id="w1:p1",
            tokens={"summary": "⏳ 1 subagent (worker)", pp.TOKEN: "6"},
            state_labels={"idle": "⏳ 1 subagent (worker)"},
        )
        self.assertTrue(pp.needs_reconcile_event(active))

        converged = self.pane_event(
            pane_id="w1:p1",
            tokens={"summary": "⏳ 1 subagent (worker)", pp.TOKEN: "4"},
            state_labels={state: pp.SUBAGENT_LABEL for state in pp.SUBAGENT_STATES},
        )
        self.assertFalse(pp.needs_reconcile_event(converged))

        completed = self.pane_event(pane_id="w1:p1", tokens={pp.TOKEN: "4"})
        self.assertTrue(pp.needs_reconcile_event(completed))
        self.assertFalse(pp.needs_reconcile_event(
            self.pane_event(pane_id="w1:p1", tokens={pp.TOKEN: "6"})))
        self.assertFalse(pp.needs_reconcile_event(
            {"event": "pane_closed", "data": {"type": "pane_closed", "pane_id": "w1:p1"}}))
        self.assertFalse(pp.needs_reconcile_event({"event": "pane_updated", "data": {}}))

    def test_active_status_files_are_a_summary_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "async-subagent-runs"
            active = root / "active"
            complete = root / "complete"
            active.mkdir(parents=True)
            complete.mkdir()
            session = "/sessions/parent.jsonl"
            now = time.time() * 1000
            (active / "status.json").write_text(json.dumps({
                "sessionId": session,
                "state": "running",
                "pid": os.getpid(),
                "startedAt": now,
                "lastUpdate": now,
                "steps": [{"status": "running"}],
            }))
            (complete / "status.json").write_text(json.dumps({
                "sessionId": "/sessions/complete.jsonl",
                "state": "complete",
                "pid": os.getpid(),
                "startedAt": now,
                "lastUpdate": now,
                "steps": [{"status": "running"}],
            }))
            sessions = pp.active_subagent_sessions(root, now_ms=now)
            self.assertEqual(sessions, {session})
            record = pp.observe(agent(
                "idle", agent_session={"source": "herdr:pi", "agent": "pi",
                                        "kind": "path", "value": session}),
                active_sessions=sessions)
            self.assertTrue(record["subagent"])
            self.assertEqual(pp.rank(record), pp.RANK_SUBAGENT)

            stale = root / "stale"
            stale.mkdir()
            (stale / "status.json").write_text(json.dumps({
                "sessionId": "/sessions/stale.jsonl",
                "state": "running",
                "pid": os.getpid(),
                "startedAt": now,
                "lastUpdate": now - (pp.SUBAGENT_STATUS_STALE_SECONDS + 1) * 1000,
                "steps": [{"status": "running"}],
            }))
            self.assertEqual(pp.active_subagent_sessions(root, now_ms=now), {session})

    def test_socket_watcher_subscribes_and_reconciles_metadata_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/api.sock"
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen()
            requests = []

            def serve():
                connection, _ = server.accept()
                with connection:
                    request = b""
                    while not request.endswith(b"\n"):
                        request += connection.recv(8192)
                    requests.append(json.loads(request))
                    event = self.pane_event(
                        pane_id="w1:p1",
                        tokens={"summary": "⏳ 1 subagent", pp.TOKEN: "6"},
                    )
                    connection.sendall(
                        (json.dumps({"id": pp.WATCH_REQUEST_ID,
                                     "result": {"type": "subscription_started"}}) + "\n").encode()
                        + (json.dumps(event) + "\n").encode())

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            reconciles = []
            try:
                pp.watch_events(path, lambda: reconciles.append(True))
            finally:
                server.close()
                thread.join(5)
            self.assertEqual(requests[0]["method"], "events.subscribe")
            self.assertEqual(requests[0]["params"], {"subscriptions": [{"type": "pane.updated"}]})
            self.assertEqual(reconciles, [True])

    def test_socket_watcher_ticks_without_events_and_resubscribes_after_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/api.sock"
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen()

            def serve_tick():
                connection, _ = server.accept()
                with connection:
                    request = b""
                    while not request.endswith(b"\n"):
                        request += connection.recv(8192)
                    connection.sendall((json.dumps({
                        "id": pp.WATCH_REQUEST_ID,
                        "result": {"type": "subscription_started"},
                    }) + "\n").encode())
                    event = (json.dumps(self.pane_event(
                        pane_id="w1:p1", tokens={pp.TOKEN: pp.RANK_PENDING})) + "\n").encode()
                    deadline = time.monotonic() + 0.2
                    while time.monotonic() < deadline:
                        try:
                            connection.sendall(event)
                        except (BrokenPipeError, ConnectionResetError):
                            break
                        time.sleep(0.005)

            thread = threading.Thread(target=serve_tick, daemon=True)
            thread.start()
            ticks = []
            try:
                reconnect = pp.watch_events(
                    path, lambda: True, on_tick=lambda: ticks.append(True) or False,
                    tick_seconds=0.05)
            finally:
                server.close()
                thread.join(5)
            self.assertFalse(reconnect)
            self.assertEqual(ticks, [True])

        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/api.sock"
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen()

            def serve_loss():
                connection, _ = server.accept()
                with connection:
                    request = b""
                    while not request.endswith(b"\n"):
                        request += connection.recv(8192)
                    connection.sendall(
                        (json.dumps({"id": pp.WATCH_REQUEST_ID,
                                     "result": {"type": "subscription_started"}}) + "\n").encode()
                        + (json.dumps({"event": "events_lost",
                                      "data": {"type": "events_lost"}}) + "\n").encode())

            thread = threading.Thread(target=serve_loss, daemon=True)
            thread.start()
            try:
                reconnect = pp.watch_events(path, lambda: True)
            finally:
                server.close()
                thread.join(5)
            self.assertTrue(reconnect)

    def test_stale_watcher_is_replaced_after_plugin_upgrade(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("priority_plus.read_watcher_registration",
                      side_effect=[(123, "old"), (456, pp.WATCHER_VERSION)]), \
                patch("priority_plus.stop_watcher") as stop, \
                patch("priority_plus.subprocess.Popen") as spawn:
            spawn.return_value.poll.return_value = None
            pp.ensure_watcher(directory, "/tmp/pp-upgrade-socket")
            stop.assert_called_once_with(directory, "/tmp/pp-upgrade-socket")
            spawn.assert_called_once()

    def test_current_watcher_is_reused(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("priority_plus.read_watcher_registration",
                      return_value=(123, pp.WATCHER_VERSION)), \
                patch("priority_plus.stop_watcher") as stop, \
                patch("priority_plus.subprocess.Popen") as spawn:
            pp.ensure_watcher(directory, "/tmp/pp-current-watcher-socket")
            stop.assert_not_called()
            spawn.assert_not_called()

    def test_cleaned_state_prevents_or_terminates_watcher(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = directory + "/api.sock"
            with pp.state_lock(directory, socket_path) as path:
                state = pp.load_state(path, "boot")
                state["cleaned"] = True
                pp.save_state(path, state)
            with patch("priority_plus.subprocess.Popen") as spawn:
                pp.ensure_watcher(directory, socket_path)
                spawn.assert_not_called()

            server = socket.socket(socket.AF_UNIX)
            server.bind(socket_path)
            server.listen()

            def serve():
                connection, _ = server.accept()
                with connection:
                    request = b""
                    while not request.endswith(b"\n"):
                        request += connection.recv(8192)
                    connection.sendall((json.dumps({
                        "id": pp.WATCH_REQUEST_ID,
                        "result": {"type": "subscription_started"},
                    }) + "\n").encode())

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                pp.watch_events(socket_path, lambda: self.fail("event callback ran"),
                                on_ready=lambda: False)
            finally:
                server.close()
                thread.join(5)


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
        self.assertEqual(self.writes()[0]["tokens"], {pp.TOKEN: pp.RANK_PENDING})
        self.assertNotIn("state_labels", self.writes()[0])
        self.client.calls.clear()
        self.client.agents[0]["state_labels"] = {"idle": "foreign later label"}
        self.sync()
        self.assertEqual(self.writes(), [])  # Don't fight another label owner.
        self.client.agents[0].update(agent_status="working", state_change_seq=3)
        self.sync()
        for fields in self.writes():
            self.assertEqual(fields["source"], pp.SOURCE)
            self.assertNotIn("clear_state_labels", fields)
            self.assertEqual(set(fields["tokens"]), {pp.TOKEN})
        self.assertEqual(self.client.agents[0]["tokens"]["foreign"], "keep")

    def test_active_subagent_ranks_below_working_and_reasserts_its_label(self):
        completed = pp.observe(agent("done", 1))
        self.state["agents"]["term-1"] = completed
        self.client.agents[0] = agent(
            "idle", 1,
            tokens={"summary": "⏳ 1 subagent (worker)", pp.TOKEN: "6"},
            state_labels={"idle": "⏳ 1 subagent (worker)"},
        )
        self.sync()
        self.assertEqual(self.client.agents[0]["tokens"][pp.TOKEN], "4")
        self.assertEqual(self.client.agents[0]["state_labels"], {
            "blocked": pp.SUBAGENT_LABEL,
            "idle": pp.SUBAGENT_LABEL,
            "done": pp.SUBAGENT_LABEL,
            "working": pp.SUBAGENT_LABEL,
        })
        self.assertTrue(self.state["agents"]["term-1"]["pending"])
        self.client.calls.clear()
        self.sync()
        self.assertEqual(self.writes(), [])

        # The pi-subagents bridge refreshes its own dynamic label. Priority+
        # reasserts the stable user-facing label without touching its token.
        self.client.agents[0]["state_labels"] = {"idle": "⏳ 1 subagent (worker)"}
        self.sync()
        self.assertEqual(self.writes()[-1]["state_labels"]["idle"], pp.SUBAGENT_LABEL)
        self.client.agents[0]["tokens"].pop("summary")
        self.client.calls.clear()
        self.sync()
        self.assertEqual(self.client.agents[0]["tokens"][pp.TOKEN], "6")
        self.assertNotIn("state_labels", self.client.agents[0])

    def test_parallel_wait_uses_working_rank_and_dynamic_label_then_cleans_up(self):
        self.client.agents[0] = agent(
            "idle", 1,
            tokens={pp.PARALLEL_WAIT_TARGET_TOKEN: "history & release",
                    pp.TOKEN: pp.RANK_IDLE},
        )
        self.sync()
        wait_label = pp.parallel_wait_label("history & release")
        current = self.client.agents[0]
        self.assertEqual(current["tokens"][pp.TOKEN], pp.RANK_WORKING)
        self.assertEqual(current["tokens"][pp.PARALLEL_WAIT_TARGET_TOKEN], "history & release")
        self.assertEqual(current["state_labels"], {
            state: wait_label for state in pp.SUBAGENT_STATES
        })
        self.assertEqual(self.state["agents"]["term-1"]["parallel_wait"], "history & release")
        self.client.calls.clear()
        self.sync()
        self.assertEqual(self.writes(), [])

        current["tokens"].pop(pp.PARALLEL_WAIT_TARGET_TOKEN)
        self.client.calls.clear()
        self.sync()
        self.assertEqual(current["tokens"][pp.TOKEN], pp.RANK_IDLE)
        self.assertNotIn("state_labels", current)

    def test_upgrade_removes_legacy_pending_presentation_and_token(self):
        completed = pp.observe(agent("done", 2))
        completed["applied"] = pp.LEGACY_PENDING_LABEL
        self.state["agents"]["term-1"] = completed
        self.client.agents[0] = agent(
            "idle", 2,
            tokens={pp.TOKEN: pp.RANK_PENDING, pp.LEGACY_CONTEXT_SUPPRESSION_TOKEN: "1"},
            state_labels={"idle": pp.LEGACY_PENDING_LABEL},
        )

        self.sync()
        record = self.state["agents"]["term-1"]
        self.assertTrue(record["pending"])
        self.assertFalse(record["applied"])
        self.assertEqual(self.client.agents[0]["tokens"][pp.TOKEN], pp.RANK_PENDING)
        self.assertNotIn(pp.LEGACY_CONTEXT_SUPPRESSION_TOKEN, self.client.agents[0]["tokens"])
        self.assertNotIn("state_labels", self.client.agents[0])

    def test_wait_and_later_suppress_and_restore_pending_presentation(self):
        for parked_label in ("WAIT/MP", "LATER/MP"):
            with self.subTest(parked_label=parked_label):
                completed = pp.observe(agent("done", 2))
                # Recover even if an older run left its pending-label contribution visible.
                completed["applied"] = False
                self.state["agents"]["term-1"] = completed
                self.client.workspaces = [{"workspace_id": "parked", "label": parked_label}]
                self.client.agents[0] = agent(
                    "idle", 2, workspace_id="parked", tokens={pp.TOKEN: "6"},
                    state_labels={"idle": pp.LEGACY_PENDING_LABEL},
                )
                self.sync()
                record = self.state["agents"]["term-1"]
                self.assertTrue(record["pending"])
                self.assertTrue(record["waiting"])
                expected_rank = "2" if parked_label.startswith("LATER") else "1"
                self.assertEqual(record["parking"], "later" if expected_rank == "2" else "wait")
                self.assertEqual(self.client.agents[0]["tokens"][pp.TOKEN], expected_rank)
                self.assertNotIn("state_labels", self.client.agents[0])
                self.client.calls.clear()
                self.sync()
                self.assertEqual(self.writes(), [])

                self.client.workspaces[0]["label"] = "MP MAIN"
                self.client.calls.clear()
                self.sync()
                self.assertFalse(self.state["agents"]["term-1"]["waiting"])
                self.assertIsNone(self.state["agents"]["term-1"]["parking"])
                self.assertEqual(self.client.agents[0]["tokens"][pp.TOKEN], "6")
                self.assertNotIn("state_labels", self.client.agents[0])

    def test_all_ranks_follow_workspace_order_and_tab_name(self):
        self.client.workspaces = [
            {"workspace_id": "z-first", "label": "Zulu first"},
            {"workspace_id": "a-second", "label": "Alpha second"},
        ]
        self.client.agents = [
            agent("done", terminal_id="first-unread", pane_id="z-first:p1",
                  workspace_id="z-first", tab_id="z-first:t1"),
            agent("idle", terminal_id="first-reviewed", pane_id="z-first:p2",
                  workspace_id="z-first", tab_id="z-first:t2"),
            agent("done", terminal_id="second-unread", pane_id="a-second:p1",
                  workspace_id="a-second", tab_id="a-second:t1",
                  tokens={"pp_workspace_sort": "stale", "pp_session_sort": "stale"}),
            agent("working", terminal_id="second-working", pane_id="a-second:p2",
                  workspace_id="a-second", tab_id="a-second:t2",
                  tokens={"pp_workspace_sort": "stale", "pp_session_sort": "stale"}),
            agent("idle", terminal_id="second-idle", pane_id="a-second:p3",
                  workspace_id="a-second", tab_id="a-second:t3",
                  tokens={"pp_workspace_sort": "stale", "pp_session_sort": "stale"}),
            agent("blocked", terminal_id="second-blocked", pane_id="a-second:p4",
                  workspace_id="a-second", tab_id="a-second:t4"),
            agent("idle", terminal_id="second-subagent", pane_id="a-second:p5",
                  workspace_id="a-second", tab_id="a-second:t5",
                  tokens={"summary": "⏳ 1 subagent"}),
            agent("unknown", terminal_id="second-unknown", pane_id="a-second:p6",
                  workspace_id="a-second", tab_id="a-second:t6"),
        ]
        self.client.tabs = [
            {"tab_id": "z-first:t1", "workspace_id": "z-first", "label": "Zulu result"},
            {"tab_id": "z-first:t2", "workspace_id": "z-first", "label": "alpha result"},
            {"tab_id": "a-second:t1", "workspace_id": "a-second", "label": "Pending"},
            {"tab_id": "a-second:t2", "workspace_id": "a-second", "label": "Working"},
            {"tab_id": "a-second:t3", "workspace_id": "a-second", "label": "Idle"},
            {"tab_id": "a-second:t4", "workspace_id": "a-second", "label": "Blocked"},
            {"tab_id": "a-second:t5", "workspace_id": "a-second", "label": "Subagent"},
            {"tab_id": "a-second:t6", "workspace_id": "a-second", "label": "Unknown"},
        ]
        self.state["agents"]["first-reviewed"] = pp.observe(agent(
            "done", terminal_id="first-reviewed", pane_id="z-first:p2",
            workspace_id="z-first", tab_id="z-first:t2"))

        self.sync()
        by_terminal = {entry["terminal_id"]: entry for entry in self.client.agents}
        first_key = pp.workspace_order_key(0)
        second_key = pp.workspace_order_key(1)
        self.assertEqual(by_terminal["first-unread"]["tokens"], {
            pp.TOKEN: pp.RANK_PENDING, pp.WORKSPACE_TOKEN: first_key,
            pp.SESSION_TOKEN: "zulu result",
        })
        self.assertEqual(by_terminal["first-reviewed"]["tokens"], {
            pp.TOKEN: pp.RANK_PENDING, pp.WORKSPACE_TOKEN: first_key,
            pp.SESSION_TOKEN: "alpha result",
        })
        self.assertEqual(by_terminal["second-unread"]["tokens"], {
            pp.TOKEN: pp.RANK_PENDING, pp.WORKSPACE_TOKEN: second_key,
            pp.SESSION_TOKEN: "pending",
        })
        self.assertEqual(by_terminal["second-working"]["tokens"], {
            pp.TOKEN: pp.RANK_WORKING, pp.WORKSPACE_TOKEN: second_key,
            pp.SESSION_TOKEN: "working",
        })
        self.assertEqual(by_terminal["second-idle"]["tokens"], {
            pp.TOKEN: pp.RANK_IDLE, pp.WORKSPACE_TOKEN: second_key,
            pp.SESSION_TOKEN: "idle",
        })
        self.assertEqual(by_terminal["second-blocked"]["tokens"], {
            pp.TOKEN: pp.RANK_BLOCKED, pp.WORKSPACE_TOKEN: second_key,
            pp.SESSION_TOKEN: "blocked",
        })
        self.assertEqual(by_terminal["second-subagent"]["tokens"], {
            "summary": "⏳ 1 subagent", pp.TOKEN: pp.RANK_SUBAGENT,
            pp.WORKSPACE_TOKEN: second_key, pp.SESSION_TOKEN: "subagent",
        })
        self.assertEqual(by_terminal["second-unknown"]["tokens"], {
            pp.TOKEN: pp.RANK_UNKNOWN, pp.WORKSPACE_TOKEN: second_key,
            pp.SESSION_TOKEN: "unknown",
        })

        self.client.workspaces.reverse()
        self.sync()
        self.assertEqual(by_terminal["first-unread"]["tokens"][pp.WORKSPACE_TOKEN], second_key)
        self.assertEqual(by_terminal["first-reviewed"]["tokens"][pp.WORKSPACE_TOKEN], second_key)
        for terminal in ("second-unread", "second-working", "second-idle",
                         "second-blocked", "second-subagent", "second-unknown"):
            self.assertEqual(by_terminal[terminal]["tokens"][pp.WORKSPACE_TOKEN], first_key)

    def test_all_agents_get_visible_workspace_and_alphabetical_session_tokens(self):
        self.client.agents = [
            agent("idle", terminal_id="later-z", pane_id="later-z:p1",
                  workspace_id="later-z", tab_id="later-z:t1"),
            agent("idle", terminal_id="later-a-beta", pane_id="later-a:p2",
                  workspace_id="later-a", tab_id="later-a:t2"),
            agent("idle", terminal_id="later-a-alpha", pane_id="later-a:p1",
                  workspace_id="later-a", tab_id="later-a:t1"),
            agent("idle", terminal_id="normal", pane_id="normal:p1",
                  workspace_id="normal", tab_id="normal:t1",
                  tokens={pp.TOKEN: "2", "pp_workspace_sort": "stale", "pp_session_sort": "stale"}),
            agent("working", terminal_id="later-working", pane_id="later-a:p3",
                  workspace_id="later-a", tab_id="later-a:t3",
                  tokens={"pp_workspace_sort": "stale", "pp_session_sort": "stale"}),
            agent("blocked", terminal_id="later-blocked", pane_id="later-a:p4",
                  workspace_id="later-a", tab_id="later-a:t4",
                  tokens={"pp_workspace_sort": "stale", "pp_session_sort": "stale"}),
            agent("idle", terminal_id="later-subagent", pane_id="later-a:p5",
                  workspace_id="later-a", tab_id="later-a:t5",
                  tokens={"summary": "⏳ 1 subagent", "pp_workspace_sort": "stale",
                          "pp_session_sort": "stale"}),
            agent("idle", terminal_id="later-missing-tab", pane_id="later-a:p6",
                  workspace_id="later-a", tab_id="later-a:missing"),
        ]
        self.client.workspaces = [
            {"workspace_id": "later-z", "label": "LATER/Zebra"},
            {"workspace_id": "normal", "label": "Main"},
            {"workspace_id": "later-a", "label": "LATER/alpha"},
        ]
        self.client.tabs = [
            {"tab_id": "later-z:t1", "workspace_id": "later-z", "label": "Middle"},
            {"tab_id": "later-a:t2", "workspace_id": "later-a", "label": "Zulu session"},
            {"tab_id": "later-a:t1", "workspace_id": "later-a", "label": "alpha session"},
            {"tab_id": "normal:t1", "workspace_id": "normal", "label": "Normal"},
            {"tab_id": "later-a:t3", "workspace_id": "later-a", "label": "Working"},
            {"tab_id": "later-a:t4", "workspace_id": "later-a", "label": "Blocked"},
            {"tab_id": "later-a:t5", "workspace_id": "later-a", "label": "Subagent"},
        ]
        self.sync()
        by_terminal = {entry["terminal_id"]: entry for entry in self.client.agents}
        later_z_key = pp.workspace_order_key(0)
        normal_key = pp.workspace_order_key(1)
        later_a_key = pp.workspace_order_key(2)
        self.assertEqual(by_terminal["later-z"]["tokens"], {
            pp.TOKEN: "2", "pp_workspace_sort": later_z_key, "pp_session_sort": "middle",
        })
        self.assertEqual(by_terminal["later-a-beta"]["tokens"], {
            pp.TOKEN: "2", "pp_workspace_sort": later_a_key, "pp_session_sort": "zulu session",
        })
        self.assertEqual(by_terminal["later-a-alpha"]["tokens"], {
            pp.TOKEN: "2", "pp_workspace_sort": later_a_key, "pp_session_sort": "alpha session",
        })
        self.assertEqual(by_terminal["normal"]["tokens"], {
            pp.TOKEN: "3", pp.WORKSPACE_TOKEN: normal_key, pp.SESSION_TOKEN: "normal",
        })
        self.assertEqual(by_terminal["later-working"]["tokens"], {
            pp.TOKEN: "5", pp.WORKSPACE_TOKEN: later_a_key, pp.SESSION_TOKEN: "working",
        })
        self.assertEqual(by_terminal["later-blocked"]["tokens"], {
            pp.TOKEN: "7", pp.WORKSPACE_TOKEN: later_a_key, pp.SESSION_TOKEN: "blocked",
        })
        self.assertEqual(by_terminal["later-subagent"]["tokens"], {
            "summary": "⏳ 1 subagent", pp.TOKEN: "4", pp.WORKSPACE_TOKEN: later_a_key,
            pp.SESSION_TOKEN: "subagent",
        })
        self.assertEqual(by_terminal["later-missing-tab"]["tokens"], {
            pp.TOKEN: "2", "pp_workspace_sort": later_a_key,
        })

    def test_user_workspace_order_is_preserved_while_enabled(self):
        self.client.workspaces = [
            {"workspace_id": "wait", "label": "WAIT/one"},
            {"workspace_id": "normal", "label": "one"},
            {"workspace_id": "later", "label": "LATER/one"},
        ]
        self.client.agents = [
            agent("idle", terminal_id="wait", pane_id="wait:p1", workspace_id="wait"),
            agent("idle", terminal_id="normal", pane_id="normal:p1", workspace_id="normal"),
            agent("idle", terminal_id="later", pane_id="later:p1", workspace_id="later"),
        ]
        original = copy.deepcopy(self.client.workspaces)
        self.sync()
        self.assertEqual(self.client.workspaces, original)
        self.assertFalse(any(method == "workspace.move_block"
                             for method, _ in self.client.calls))
        by_terminal = {entry["terminal_id"]: entry for entry in self.client.agents}
        for position, terminal in enumerate(("wait", "normal", "later")):
            self.assertEqual(by_terminal[terminal]["tokens"][pp.WORKSPACE_TOKEN],
                             pp.workspace_order_key(position))

        self.client.workspaces = [original[2], original[0], original[1]]
        self.client.calls.clear()
        self.sync()
        self.assertEqual([workspace["workspace_id"] for workspace in self.client.workspaces],
                         ["later", "wait", "normal"])
        self.assertFalse(any(method == "workspace.move_block"
                             for method, _ in self.client.calls))
        for position, terminal in enumerate(("later", "wait", "normal")):
            self.assertEqual(by_terminal[terminal]["tokens"][pp.WORKSPACE_TOKEN],
                             pp.workspace_order_key(position))

    def test_upgrade_clears_applied_pending_labels_without_losing_completion(self):
        self.sync()
        self.client.agents[0].update(agent_status="idle", state_change_seq=2)
        self.sync()
        for old_applied in (True, "◉ Antwort offen", pp.LEGACY_PENDING_LABEL):
            with self.subTest(old_applied=old_applied):
                self.state["agents"]["term-1"]["applied"] = old_applied
                self.client.agents[0]["state_labels"] = {"idle": pp.LEGACY_PENDING_LABEL}
                pp.save_state(self.path, self.state)
                self.state = pp.load_state(self.path, "boot")
                self.client.calls.clear()
                self.sync()
                self.assertEqual(self.writes(), [{
                    "pane_id": "w1:p1", "source": pp.SOURCE,
                    "clear_state_labels": True,
                }])
                record = self.state["agents"]["term-1"]
                self.assertTrue(record["pending"])
                self.assertEqual(record["seq"], 2)
                self.assertFalse(record["applied"])
                self.assertEqual(self.client.agents[0]["tokens"][pp.TOKEN], "6")
                self.client.calls.clear()
                self.sync()
                self.assertEqual(self.writes(), [])

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
        self.assertFalse(self.state["agents"]["term-1"]["applied"])

    def test_pane_closed_during_write_is_tolerated(self):
        self.client.fail = "pane_not_found"
        self.sync()
        self.assertIsNone(self.state["agents"]["term-1"]["applied"])

    def test_cleanup_source_scoped_and_quiescent_then_enable(self):
        self.client.boot = "boot"
        self.client.close = lambda: None
        self.client.panes = [agent(tokens={
            pp.TOKEN: "3", "pp_workspace_sort": "stale", "pp_session_sort": "stale",
            "other": "keep",
        })]
        self.client.workspaces = [
            {"workspace_id": "wait", "label": "WAIT/one"},
            {"workspace_id": "normal", "label": "one"},
            {"workspace_id": "later", "label": "LATER/one"},
        ]
        with patch("priority_plus.Client", return_value=self.client):
            pp.run("cleanup", self.temp.name, "/tmp/pp-action-test")
            self.assertIn(("agent.view.clear", {"source": pp.SOURCE}), self.client.calls)
            self.assertFalse(any(method == "workspace.move_block" for method, _ in self.client.calls))
            fields = self.writes()[-1]
            self.assertEqual(fields["tokens"], {
                pp.TOKEN: None, "pp_workspace_sort": None, "pp_session_sort": None,
            })
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

    def test_manifest_uses_low_volume_watchdog_hooks(self):
        root = Path(__file__).resolve().parents[1]
        manifest = tomllib.loads((root / "herdr-plugin.toml").read_text())
        self.assertEqual(manifest["id"], pp.PLUGIN)
        self.assertEqual(manifest["platforms"], ["linux"])
        self.assertEqual(manifest["startup"][0]["command"], ["python3", "priority_plus.py", "startup"])
        self.assertEqual({a["id"] for a in manifest["actions"]}, {"enable", "disable", "toggle", "cleanup"})
        self.assertEqual(manifest["version"], "0.9.0")
        hooks = {e["on"] for e in manifest["events"]}
        self.assertIn("pane.agent_status_changed", hooks)
        self.assertIn("tab.moved", hooks)
        self.assertIn("tab.renamed", hooks)
        self.assertTrue({"workspace.created", "workspace.renamed", "workspace.moved",
                         "workspace.reordered", "workspace.closed"}.issubset(hooks))
        self.assertTrue(hooks.isdisjoint({"pane.updated", "pane.focused", "pane.output"}))
        self.assertTrue(all(e["command"] == ["python3", "priority_plus.py", "sync"]
                            for e in manifest["events"]))

    def test_cleaned_hooks_do_not_respawn_watcher_and_cleanup_stops_it(self):
        environment = {"HERDR_PLUGIN_STATE_DIR": "/state", "HERDR_SOCKET_PATH": "/socket"}
        with (patch.dict(os.environ, environment, clear=True),
              patch("priority_plus.run", return_value=False),
              patch("priority_plus.ensure_watcher") as ensure,
              patch("priority_plus.stop_watcher") as stop,
              patch("sys.argv", ["priority_plus.py", "sync"])):
            self.assertEqual(pp.main(), 0)
            ensure.assert_not_called()
            stop.assert_not_called()
        with (patch.dict(os.environ, environment, clear=True),
              patch("priority_plus.run", return_value=False),
              patch("priority_plus.ensure_watcher") as ensure,
              patch("priority_plus.stop_watcher") as stop,
              patch("sys.argv", ["priority_plus.py", "cleanup"])):
            self.assertEqual(pp.main(), 0)
            ensure.assert_not_called()
            stop.assert_called_once_with("/state", "/socket")

    def test_missing_environment_fails_without_default_socket(self):
        with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["priority_plus.py", "sync"]):
            self.assertEqual(pp.main(), 1)


if __name__ == "__main__":
    unittest.main()
