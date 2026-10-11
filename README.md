<div align="center">

# Herdr Priority+

**Read a result without losing its place in your queue.**

[![Herdr 0.9.1+](https://img.shields.io/badge/Herdr-0.9.1%2B-blue)](https://herdr.dev)
![Linux](https://img.shields.io/badge/platform-Linux-blue)
![Python standard library](https://img.shields.io/badge/runtime-Python_standard_library-green)

[Quick start](#quick-start) · [Usage](#usage) · [How it works](#how-it-works) · [Development](#development)

</div>

A small [Herdr](https://herdr.dev) plugin that keeps **viewed completions above running agents** until the next work cycle. Opening a finished session no longer means losing track of it at the bottom of your queue.

Built for multi-session Pi workflows, Priority+ uses Herdr's lifecycle and display metadata—not transcript scanning. Inside every priority tier, it groups sessions by the visible **Spaces order**, adds a conditional **subagent-working** badge, and provides a toggleable Agents view. Native status icons stay unchanged.

With Herdr's symbol indicators enabled:

```text
agents                  Priority+

× Blocked
✓ Finished, not yet viewed
○ Reviewed result, still prioritized
◐ Working
○ Parent idle while a child runs
  ⏳ Subagent working
○ Ordinary idle
○ Inactive in a LATER workspace
○ Inactive in a WAIT workspace
```

The only additional badge reads **`⏳ Subagent working`**. Pending completions remain prioritized without an extra text row. Plugin action titles are currently German.

## What changes

| Priority | Session state | Behavior |
| --- | --- | --- |
| 1 | Blocked | Stays at the top. |
| 2 | Completed, not viewed outside `LATER…`/`WAIT…` | Keeps Herdr's native done indicator near the top and groups it by Spaces order. |
| 3 | Completed, viewed, no new run | Stays above working agents without an extra badge and groups by Spaces order. |
| 4 | Working | Clears the previous completion marker. |
| 5 | Active pi-subagents child | Shows `⏳ Subagent working` below directly working agents. |
| 6 | Ordinary idle | Remains below active work. |
| 7 | Inactive in `LATER…` | Stays below ordinary idle. |
| 8 | Inactive in `WAIT…` | Stays below LATER sessions. |
| 9 | Unknown outside parked workspaces | Remains last. |

Inside each priority tier, sessions are grouped by the current top-left Spaces order: all matching sessions from the first Space appear before matching sessions from the second Space, and so on. The tier hierarchy itself stays unchanged—blocked remains above completed, completed above working, and so forth. Inside one Space, completed sessions keep unseen results ahead of reviewed completions, followed by the most recent state change. In LATER and WAIT, session/tab names remain the final alphabetical tie-breaker.

Clicking a session or typing an unsent draft does **not** clear the completion marker. Starting a new parent work cycle does. A background child temporarily gets the subagent rank; any underlying completion priority returns when the child finishes.

Workspace labels beginning with the case-sensitive prefix **`LATER`** or **`WAIT`** act as parking signals. Inactive sessions in either kind of workspace move to the parked tiers, including fresh completions. Ordinary idle sessions remain above parked sessions; LATER sessions remain above WAIT sessions. Blocked, directly working, and active-subagent sessions keep their activity ranks. Priority+ preserves pending history, so moving a completed session out of a parked workspace restores its pending-completion rank.

Priority+ never rearranges the Spaces themselves. Their current visible order—including any drag-and-drop arrangement—is the source of truth for every Agents tier. `LATER…` and `WAIT…` labels change the inactive session rank, not the Space position; inside each parked Space, sessions additionally sort by session/tab name.

> [!IMPORTANT]
> The pending-completion rank is a workflow reminder, not content analysis. It means **Herdr observed a completion and no new work cycle has started**. An error or abort that settles to idle can count as completion; an autonomous new run clears the marker too.

## Quick start

### Requirements

**Linux**, **Herdr 0.9.1+**, and `python3` on `PATH`; agents must report their lifecycle states. Use **Python 3.11+** for development/tests. Verified with **Herdr 0.9.1 and Python 3.12**.

No third-party runtime dependencies, build steps, core patches, or changes to the official Pi integration. macOS and Windows are not supported.

### 1. Link the plugin

Clone this repository, enter its root directory, and run:

```sh
herdr plugin link "$PWD"
```

The plugin ID is **`local.priority-plus`**, regardless of the repository name.

Alternatively, use `herdr plugin install OWNER/herdr-priority-plus`, replacing `OWNER` with the repository owner. Use either a linked checkout or a GitHub-managed installation, not both.

### 2. Add the badge row

Back up your Herdr configuration—normally `~/.config/herdr/config.toml`. Append the conditional `state_text` row to your existing `[ui.sidebar.agents].rows`:

```toml
[ui.sidebar.agents]
rows = [
  # Keep your existing rows here. This first row is an example.
  ["state_icon", "machine", "workspace", "tab"],
  [{ token = "state_text", rules = [
    { equals = "⏳ Subagent working", fg = "#268bd2", bold = true },
    { contains = "", hide = true },
  ] }],
]
```

The first rule shows the Priority+ subagent badge; the second hides every other status label. When there is no badge, the entire extra row disappears. Do not create a duplicate TOML section. If you use `rows_by_agent` overrides, append the row to the relevant overrides too.

This affects the **expanded desktop sidebar**. Collapsed and mobile layouts keep their native compact appearance.

> [!NOTE]
> **Upgrading:** Version 0.7.0 removes the `◉ Awaiting reply` presentation while retaining pending-completion ranking. Remove its `state_text` rule from your Herdr configuration as shown above, then invoke `enable` once. Priority+ clears legacy labels and suppression metadata without losing completion state. Do not run `cleanup` for this upgrade.

### 3. Activate Priority+

```sh
herdr server reload-config
herdr plugin action invoke local.priority-plus.enable
herdr plugin log list --plugin local.priority-plus
```

Check that the `enable` action succeeded. Plugin actions run asynchronously; invoking one does not mean it has finished. No Herdr or Pi restart is required.

> [!NOTE]
> Tracking starts when the plugin runs. Existing unseen `done` sessions are adopted, but already-viewed historical `idle` sessions are not guessed to be unanswered.

## Usage

### Toggle sorting

```sh
herdr plugin action invoke local.priority-plus.toggle
```

Or choose explicitly:

```sh
herdr plugin action invoke local.priority-plus.enable
herdr plugin action invoke local.priority-plus.disable
```

**Disable turns off the custom Agents sort view only.** Completion tracking and badges remain active. Herdr returns to its configured Agents sort policy when this plugin owns the active view. Priority+ never changes the Space order, whether enabled or disabled.

Priority+ is a custom view, not a new entry in Herdr's native sort selector. While active, its label appears in the Agents header and replaces the normal sort toggle. Use the plugin action or a shortcut to switch back.

For keyboard access, add an unused binding to your config and reload it:

```toml
[[keys.command]]
key = "prefix+alt+p"
type = "plugin_action"
command = "local.priority-plus.toggle"
description = "Toggle Priority+ sorting"
```

Press your Herdr prefix, then **Alt+P**. The sorting preference is remembered per server/socket and reapplied at server startup. Other sessions already running need their own `enable` invocation.

### Remove the plugin

First clear the plugin's view and metadata:

```sh
herdr plugin action invoke local.priority-plus.cleanup
herdr plugin log list --plugin local.priority-plus
```

**Wait for `cleanup` to succeed**, then remove the installation:

```sh
# Locally linked checkout:
herdr plugin unlink local.priority-plus

# For a GitHub-managed installation, use this instead:
# herdr plugin uninstall local.priority-plus
```

Remove the badge row and optional shortcut from your config, then run `herdr server reload-config`.

Cleanup also stops the event watcher and pauses tracking so queued hooks cannot recreate the metadata or restart the watcher. It leaves the user-controlled Space order untouched. To use the plugin again, invoke `enable`. Herdr retains plugin-owned state after unlinking. Simply disabling or unlinking the plugin does not perform this metadata cleanup.

## How it works

Short-lived hooks reconcile Herdr's agent, workspace, and tab snapshots and track completion by terminal and agent/session identity. A `pp_rank` metadata token controls the priority section. Every session receives a zero-padded `pp_workspace_sort` position derived from the authoritative visible workspace list, so each rank independently follows the Spaces order. Inactive parked sessions additionally receive a case-folded, Herdr-bounded `pp_session_sort` token when the tab join is available. Herdr's **client-local `seen`** then separates unread and reviewed results inside the same Space. Pending completions do not receive an additional display label.

When [pi-subagents](https://github.com/nicobailon/pi-subagents) publishes its `summary` token, Priority+ gives that pane a stable `⏳ Subagent working` label and places it below directly working agents. If that best-effort metadata is missing, Priority+ falls back to live, same-user pi-subagents status artifacts and matches their parent session path to Herdr's agent-session identity. The original completion state remains intact underneath. Because Herdr deliberately excludes high-volume `pane.updated` events from manifest hooks, startup and low-volume hooks supervise one detached per-server socket watcher. It selects subagent-summary and legacy-label cleanup edges, recovers `events_lost`, and reconciles when the active-session projection changes; unrelated pane updates are ignored.

Priority+ joins each agent's `workspace_id` and `tab_id` with Herdr's live workspace and tab lists. Labels beginning with `LATER` or `WAIT` assign distinct inactive ranks: ordinary idle → LATER → WAIT. Every rank uses the visible workspace position as its first secondary key; inactive LATER and WAIT sections then sort sessions inside the same workspace case-insensitively by session/tab label. Blocked, working, and active-subagent behavior remains visible at the normal activity ranks. An active subagent keeps its badge across idle, done, working, and blocked parent states and always uses the common subagent rank. Only blocked sessions without active children retain the highest rank. Create, rename, drag-and-drop, and reorder hooks only refresh metadata from the authoritative snapshot; they never call a workspace mutation API.

There is **no transcript scanning**. The watcher primarily uses Herdr's event stream and performs a bounded two-second status-artifact heartbeat, with a full reconciliation at most every 30 seconds when nothing changes. It exits when the server socket closes; `cleanup` stops it explicitly. Per-socket locks and atomic private files serialize updates. Requests have bounded timeouts and verify the server's process identity. Status artifacts must be regular same-user files, stay below 1 MiB, name a live parent PID, and report a queued/running state. The plugin never submits prompts, changes focus, renames sessions, or fabricates native agent states. Stored metadata may include session IDs or paths.

### Limits and coexistence

- **Restarts reset completion tracking**, but preserve your sorting preference. Missed historical runs cannot be reconstructed; hook updates can briefly lag.
- **Subagent detection prefers pi-subagents metadata.** The standard `⏳ N subagent(s)` summary token is primary; live same-user status artifacts are the fallback when the root Pi session fails to publish or refresh it.
- **Parking matching is case-sensitive and prefix-based.** `LATER`, `LATER/MP`, `WAIT`, and `WAITING` match; lowercase variants do not.
- **Space order is user-controlled.** Priority+ mirrors the current visible order inside every Agents tier and never moves Spaces itself. Drag-and-drop changes therefore become the new ordering source immediately, including for LATER, WAIT, and linked-worktree Spaces.
- **Parked session-name sorting is case-insensitive, not locale-aware.** It uses Unicode case folding on displayed session/tab labels; Space ordering itself is never alphabetical unless the user arranged it that way.
- **Single-server support.** Combined multi-machine views are unsupported. Simultaneous clients have not been end-to-end validated.
- **Priority+ token keys are reserved:** Herdr stores token keys per pane, not per reporter. Other plugins must not use `pp_rank`, `pp_workspace_sort`, or `pp_session_sort`. Version 0.7.0 also clears the retired `pp_context_suppressed` token during migration.
- **Views are shared:** enable/startup can replace another plugin's view. Ordinary hooks never reclaim it; clearing affects only Priority+'s view.
- **Other idle labels may override the badge.** Cleanup removes only this plugin's label contribution and leaves unrelated tokens alone.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No badge | Check the conditional row and agent-specific overrides, reload config, then finish/view a new run in the expanded sidebar. |
| Normal sort header | Invoke `enable` and check its log. Linking does not run startup hooks in an already-running server. |
| Native icon becomes a circle | Expected: the badge is an extra row, not an icon replacement. |
| Badges remain after disabling sorting | Expected; `cleanup` removes them and pauses tracking. |
| Legacy `Awaiting reply` text remains | Remove its `state_text` rule from the Herdr config, reload config, then invoke `enable` once. |
| Agents rows do not follow a new Space order | Invoke `enable`, then inspect plugin logs for successful `workspace.moved`/`workspace.reordered` hooks. Priority+ should never move the Spaces back. |

```sh
herdr plugin list --plugin local.priority-plus --json
herdr plugin log list --plugin local.priority-plus --limit 10
```

Review diagnostic logs before sharing them: Herdr invocation context may include local paths.

## Development

| File | Purpose |
| --- | --- |
| `herdr-plugin.toml` | Manifest, actions, and hooks. |
| `priority_plus.py` | Tracking, socket API/event watcher, persistence, and view updates. |
| `tests/test_priority_plus.py` | Unit tests. |
| `tests/test_integration.py` | Isolated real-server tests. |
| `tests/tui_smoke.py` | Real PTY client test. |

Run the standard-library tests:

```sh
python3 -m unittest discover -s tests -v
```

The real-server test is opt-in and currently targets **Herdr 0.9.1**. It uses `herdr` from `PATH`; set `PP_HERDR_BIN` to an absolute executable path to override it:

```sh
PP_INTEGRATION=1 python3 -m unittest discover -s tests -v
```

For the optional TUI test, [uv](https://docs.astral.sh/uv/) provides an isolated environment with `pyte`:

```sh
mkdir -p test-artifacts
uv run --with pyte python tests/tui_smoke.py \
  --plugin . --output test-artifacts/tui-evidence.json
```

Tests use temporary HOME/XDG directories and sockets, discard inherited Herdr connection variables, and stop only their own servers—**never your live sessions**. Evidence under `test-artifacts/` is Git-ignored.

Coverage includes transitions, user-controlled drag-and-drop Space ordering across every rank, LATER/WAIT parking and rename restoration, parked session ordering, metadata-only and status-artifact subagent detection, watcher filtering/ticks/event-loss recovery, identity changes, moves, restarts, concurrent persistence, foreign metadata/views, and cleanup. The real TUI test checks **unread → clicked/read → draft typed → new work**, cross-Space ordering, persisted Space moves, LATER/WAIT hierarchy, the subagent row, and both sorting toggles.
