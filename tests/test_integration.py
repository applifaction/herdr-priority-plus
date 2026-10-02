"""Opt-in real Herdr 0.9.1 tests; never use an inherited socket or HOME."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import priority_plus as pp

ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get("PP_HERDR_BIN", "herdr")
ARTIFACTS = ROOT / "test-artifacts"


@unittest.skipUnless(os.environ.get("PP_INTEGRATION") == "1", "set PP_INTEGRATION=1 for isolated real-binary tests")
class RealHerdrTests(unittest.TestCase):
    def test_manifest_lifecycle_and_actions(self):
        ARTIFACTS.mkdir(exist_ok=True)
        evidence = []
        with tempfile.TemporaryDirectory(prefix="pp-herdr-") as temporary:
            base = Path(temporary)
            env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
            for name, subdir in [("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                 ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
                                 ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "runtime")]:
                path = base / subdir
                path.mkdir(mode=0o700)
                env[name] = str(path)
            config = base / "config.toml"
            config.write_text('[update]\nversion_check = false\nmanifest_check = false\n'
                              '[ui.sidebar.agents]\nrows = [\n'
                              '  ["state_icon", "agent", "workspace"],\n'
                              '  [{ token = "state_text", rules = [{ equals = "⏳ Subagent working" }, '
                              '{ equals = "◉ Awaiting reply" }, { contains = "", hide = true }] }],\n]\n')
            env.update(HERDR_SOCKET_PATH=str(base / "api.sock"),
                       HERDR_CLIENT_SOCKET_PATH=str(base / "client.sock"),
                       HERDR_CONFIG_PATH=str(config), SHELL="/bin/sh", TERM="xterm-256color")
            version = subprocess.run([BINARY, "--version"], env=env, cwd=base,
                                     capture_output=True, text=True, check=True, timeout=10)
            self.assertIn("0.9.1", version.stdout)
            evidence.append(version.stdout.strip())
            # Offline link, so the actual manifest startup hook is exercised.
            linked = subprocess.run([BINARY, "plugin", "link", str(ROOT)], env=env, cwd=base,
                                    capture_output=True, text=True, check=True, timeout=15)
            (ARTIFACTS / "isolated-link.log").write_text(linked.stdout + linked.stderr)
            process = None
            client = None
            with (ARTIFACTS / "isolated-server.log").open("w") as log:
                try:
                    process = subprocess.Popen([BINARY, "server"], env=env, cwd=base,
                                               stdout=log, stderr=log, start_new_session=True)
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        self.assertIsNone(process.poll(), "isolated server exited; see isolated-server.log")
                        try:
                            client = pp.Client(env["HERDR_SOCKET_PATH"])
                            client.call("ping")
                            break
                        except (OSError, RuntimeError):
                            if client:
                                client.close()
                                client = None
                            time.sleep(0.05)
                    self.assertIsNotNone(client, "isolated server not ready")

                    def call(method, **params):
                        return client.call(method, params)

                    def wait(predicate, description):
                        deadline = time.monotonic() + 12
                        while time.monotonic() < deadline:
                            value = predicate()
                            if value:
                                return value
                            time.sleep(0.05)
                        self.fail("Timed out: " + description)

                    def logs():
                        return call("plugin.log.list", plugin_id=pp.PLUGIN)["logs"]

                    def settled():
                        return all(entry["status"] != "running" for entry in logs())

                    def action(name):
                        before = {entry["log_id"] for entry in logs()}
                        call("plugin.action.invoke", action_id=f"{pp.PLUGIN}.{name}")
                        wait(lambda: any(entry["log_id"] not in before and
                                         entry.get("action_id") == name and
                                         entry["status"] == "succeeded" for entry in logs()), name)
                        wait(settled, "hooks settled after " + name)

                    def fixture(label, focus=False):
                        result = call("workspace.create", cwd=str(base), label=label, focus=focus)
                        return result["root_pane"]["pane_id"]

                    def report(pane, state):
                        call("pane.report_agent", pane_id=pane, source="custom:pp-fixture", agent="pi", state=state)

                    def get(pane):
                        return call("pane.get", pane_id=pane)["pane"]

                    def expect_rank(pane, rank):
                        return wait(lambda: get(pane).get("tokens", {}).get(pp.TOKEN) == rank,
                                    f"{pane} rank={rank}")

                    wait(lambda: any(entry.get("event") == "startup" and entry["status"] == "succeeded"
                                     for entry in logs()), "real startup hook")
                    evidence.append("actual manifest startup succeeded")
                    focused = fixture("focused", True)
                    background = fixture("background")
                    blocked = fixture("blocked")
                    plain = fixture("plain-idle")
                    working = fixture("working")
                    subagent = fixture("subagent")
                    waiting = fixture("WAIT/fixture")
                    report(plain, "idle")
                    expect_rank(plain, "1")
                    self.assertNotEqual(get(plain).get("state_labels", {}).get("idle"), pp.LABEL)
                    report(working, "working")
                    expect_rank(working, "3")
                    report(blocked, "blocked")
                    expect_rank(blocked, "5")
                    for pane in (focused, background):
                        call("pane.report_metadata", pane_id=pane, source="test:foreign",
                             title="foreign title", state_labels={"idle": "foreign idle", "working": "foreign work"},
                             tokens={"foreign": "keep"})
                        report(pane, "working")
                        expect_rank(pane, "3")
                        report(pane, "idle")
                        expect_rank(pane, "4")
                        wait(lambda: get(pane).get("state_labels", {}).get("idle") == pp.LABEL, "pending badge")
                        self.assertEqual(get(pane)["tokens"]["foreign"], "keep")
                        self.assertEqual(get(pane)["title"], "foreign title")
                        self.assertEqual(get(pane)["state_labels"]["working"], "foreign work")
                    self.assertEqual(get(background)["agent_status"], "done")
                    evidence.append("background done and focused completion both rank 4; blocked=5, working=3, plain idle=1")
                    report(subagent, "working")
                    expect_rank(subagent, "3")
                    report(subagent, "idle")
                    expect_rank(subagent, "4")
                    subagent_summary = "⏳ 1 subagent (worker)"
                    # A summary-token-only pane.updated event must be sufficient;
                    # it does not produce a low-volume agent lifecycle hook.
                    call("pane.report_metadata", pane_id=subagent, source="pi-subagents:herdr",
                         tokens={"summary": subagent_summary})
                    expect_rank(subagent, "2")
                    wait(lambda: get(subagent).get("state_labels", {}).get("idle") == pp.SUBAGENT_LABEL,
                         "stable subagent label after metadata-only update")
                    # A pi-subagents refresh temporarily becomes the latest label;
                    # the resulting status-label change must make Priority+ win again.
                    call("pane.report_metadata", pane_id=subagent, source="pi-subagents:herdr",
                         state_labels={state: subagent_summary for state in pp.SUBAGENT_STATES},
                         tokens={"summary": subagent_summary})
                    wait(lambda: get(subagent).get("state_labels", {}).get("idle") == pp.SUBAGENT_LABEL,
                         "subagent label after refresh")
                    call("pane.report_metadata", pane_id=subagent, source="pi-subagents:herdr",
                         clear_state_labels=True, tokens={"summary": None})
                    expect_rank(subagent, "4")
                    wait(lambda: get(subagent).get("state_labels", {}).get("idle") == pp.LABEL,
                         "awaiting label restored after subagent")
                    evidence.append("active subagent rank 2 sits below working rank 3 and restores pending rank 4")
                    report(waiting, "working")
                    expect_rank(waiting, "3")
                    report(waiting, "idle")
                    expect_rank(waiting, "1")
                    call("pane.focus", pane_id=waiting)
                    wait(lambda: get(waiting)["agent_status"] == "idle", "review WAIT result")
                    expect_rank(waiting, "1")
                    wait(lambda: get(waiting).get("state_labels", {}).get("idle") != pp.LABEL,
                         "WAIT suppresses awaiting label")
                    waiting_workspace = get(waiting)["workspace_id"]
                    call("workspace.rename", workspace_id=waiting_workspace, label="resumed")
                    expect_rank(waiting, "4")
                    wait(lambda: get(waiting).get("state_labels", {}).get("idle") == pp.LABEL,
                         "leaving WAIT restores awaiting label")
                    call("workspace.rename", workspace_id=waiting_workspace, label="WAIT/fixture")
                    expect_rank(waiting, "1")
                    wait(lambda: get(waiting).get("state_labels", {}).get("idle") != pp.LABEL,
                         "returning to WAIT clears awaiting label")
                    evidence.append("WAIT workspace keeps pending history but presents reviewed sessions as ordinary idle")
                    call("pane.focus", pane_id=background)
                    wait(lambda: get(background)["agent_status"] == "idle", "focus native review")
                    call("pane.send_text", pane_id=background, text="draft-not-submitted")
                    time.sleep(0.15)
                    self.assertEqual(get(background)["tokens"][pp.TOKEN], "4")
                    self.assertEqual(get(background)["state_labels"]["idle"], pp.LABEL)
                    evidence.append("pane.focus and draft typing preserve pending label/rank")
                    terminal = get(background)["terminal_id"]
                    moved = call("pane.move", pane_id=background,
                                 destination={"type": "new_workspace", "label": "moved"}, focus=True)["move_result"]
                    self.assertTrue(moved["changed"])
                    background = moved["pane"]["pane_id"]
                    expect_rank(background, "4")
                    self.assertEqual(get(background)["terminal_id"], terminal)
                    self.assertEqual(get(background)["state_labels"]["idle"], pp.LABEL)
                    evidence.append("pane move preserves completion by terminal identity")
                    # Other plugins' views are not reclaimed by sync or cleared by disable.
                    call("agent.view.set", source="test:foreign", label="foreign", sort=[])
                    report(working, "blocked")
                    expect_rank(working, "5")
                    action("disable")
                    view = call("agent.view.clear", source=pp.SOURCE)
                    self.assertTrue(view["active"])
                    self.assertEqual(view["source"], "test:foreign")
                    action("enable")
                    view = call("agent.view.clear", source="test:not-owner")
                    self.assertEqual(view["source"], pp.SOURCE)
                    self.assertEqual(view["label"], "Priority+")
                    action("toggle")
                    self.assertFalse(call("agent.view.clear", source="test:not-owner")["active"])
                    action("toggle")
                    evidence.append("disable is source-scoped; events preserve foreign view; enable/toggle install Priority+")
                    report(background, "working")
                    expect_rank(background, "3")
                    wait(lambda: get(background).get("state_labels", {}).get("idle") == "foreign idle",
                         "new work clears only our label")
                    self.assertEqual(get(background)["tokens"]["foreign"], "keep")
                    evidence.append("new work clears owned label and reveals foreign idle label")
                    wait(settled, "convergence")
                    before = len(logs())
                    time.sleep(0.3)
                    self.assertEqual(len(logs()), before, "self-triggered hooks did not converge")
                    failed = [entry for entry in logs() if entry["status"] == "failed"]
                    self.assertEqual(failed, [])
                    evidence.append("all manifest hooks converged without failures")
                    action("cleanup")
                    for pane in (focused, background, blocked, plain, working, subagent, waiting):
                        self.assertNotIn(pp.TOKEN, get(pane).get("tokens", {}))
                        self.assertNotEqual(get(pane).get("state_labels", {}).get("idle"), pp.LABEL)
                    self.assertEqual(get(focused)["state_labels"]["idle"], "foreign idle")
                    report(blocked, "working")
                    wait(settled, "queued hooks after cleanup")
                    time.sleep(0.15)
                    self.assertNotIn(pp.TOKEN, get(blocked).get("tokens", {}))
                    self.assertFalse(call("agent.view.clear", source="test:not-owner")["active"])
                    action("enable")
                    expect_rank(blocked, "3")
                    evidence.append("cleanup removes metadata and quiesces hooks; enable resumes")
                    (ARTIFACTS / "isolated-plugin-logs-before-restart.json").write_text(json.dumps({"logs": logs()}, indent=2))
                    state_files = list(base.rglob("state.json"))
                    self.assertTrue(state_files)
                    saved = json.loads(state_files[0].read_text())
                    self.assertNotIn('"seen"', json.dumps(saved))
                    self.assertNotIn('"focused"', json.dumps(saved))
                    self.assertNotIn("draft-not-submitted", json.dumps(saved))
                    evidence.append("private persisted state contains no seen/focus/draft contents")
                    action("disable")
                    old_boot = client.boot
                    client.call("server.stop")
                    client.close()
                    client = None
                    process.wait(timeout=10)
                    process = subprocess.Popen([BINARY, "server"], env=env, cwd=base,
                                               stdout=log, stderr=log, start_new_session=True)
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        self.assertIsNone(process.poll(), "isolated restart exited")
                        try:
                            client = pp.Client(env["HERDR_SOCKET_PATH"])
                            client.call("ping")
                            break
                        except (OSError, RuntimeError):
                            if client:
                                client.close()
                                client = None
                            time.sleep(0.05)
                    self.assertIsNotNone(client, "isolated restart not ready")
                    self.assertNotEqual(client.boot, old_boot)
                    wait(lambda: any(entry.get("event") == "startup" and entry["status"] == "succeeded"
                                     for entry in logs()), "restart startup")
                    self.assertFalse(call("agent.view.clear", source="test:not-owner")["active"])
                    saved = json.loads(state_files[0].read_text())
                    self.assertFalse(saved["enabled"])
                    self.assertEqual(saved["boot"], client.boot)
                    action("enable")
                    self.assertEqual(call("agent.view.clear", source="test:not-owner")["source"], pp.SOURCE)
                    evidence.append("owned server restart resets boot observations and restores disabled choice")
                finally:
                    if client:
                        # Every connection is pinned to our temporary socket and boot.
                        try:
                            snapshot = client.call("plugin.log.list", {"plugin_id": pp.PLUGIN})
                            (ARTIFACTS / "isolated-plugin-logs.json").write_text(json.dumps(snapshot, indent=2))
                        except (OSError, RuntimeError, ValueError, KeyError):
                            pass
                        try:
                            client.call("server.stop")
                        except (OSError, RuntimeError, ValueError, KeyError):
                            pass
                        finally:
                            client.close()
                    if process:
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.terminate()  # Only our Popen child, never a discovered PID.
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=5)
                        evidence.append(f"owned isolated server stopped (exit {process.returncode})")
                    (ARTIFACTS / "integration-summary.json").write_text(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    unittest.main()
