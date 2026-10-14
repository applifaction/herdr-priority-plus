<div align="center">

# Herdr Priority+

**Read a result without losing its place in your queue.**

[![Herdr 0.9.1+](https://img.shields.io/badge/Herdr-0.9.1%2B-blue)](https://herdr.dev)
![Linux](https://img.shields.io/badge/platform-Linux-blue)
![Python standard library](https://img.shields.io/badge/runtime-Python_standard_library-green)

[Quick start](#quick-start) · [Usage](#usage) · [How it works](#how-it-works) · [Development](#development)

</div>

A small [Herdr](https://herdr.dev) plugin that keeps **viewed completions above running agents** until the next work cycle. Opening a finished session no longer means losing track of it at the bottom of your queue.

Built for multi-session Pi workflows, Priority+ uses Herdr's lifecycle and display metadata for normal ranking. It additionally inspects a bounded tail of reported Pi session files to recover unanswered assistant completions after startup and to detect an unresolved `interview` tool call. Inside every priority tier, it groups sessions by the visible **Spaces order** and then sorts them alphabetically by tab name within each Space. It adds conditional badges for **pending interviews**, **subagent work**, and **asynchronous peer or resource-queue waits**, and provides a toggleable Agents view. Native status icons stay unchanged.

With Herdr's symbol indicators enabled:

```text
agents                  Priority+

◐ Working, waiting for interview input
  ⚠ Interview Pending
× Blocked
✓ Finished, not yet viewed
○ Reviewed result, still prioritized
◐ Working
○ Waiting for another Pi session
  ⏳ Warte auf history & release
○ Parent idle while a child runs
  ⏳ Subagent working
○ Ordinary idle
○ Inactive in a LATER workspace
○ Inactive in a WAIT workspace
```

The additional badges read **`⚠ Interview Pending`**, **`⏳ Subagent working`**, and **`⏳ Warte auf <Tab/Session>`**. Pending completions remain prioritized without an extra text row. Plugin action titles are currently German.

## What changes

| Priority | Session state | Behavior |
| --- | --- | --- |
| 1 | Working with an unresolved Pi `interview` call | Shows `⚠ Interview Pending` and moves to the first tier. |
| 2 | Blocked | Stays above ordinary completions and active work. |
| 3 | Completed, not viewed outside `LATER…`/`WAIT…` | Keeps Herdr's native done indicator near the top and groups it by Spaces order. |
| 4 | Completed, viewed, no new run | Stays above ordinary working agents without an extra badge and groups it by Spaces order. |
| 5 | Working | Clears the previous completion marker. |
| 6 | Waiting on another Pi session or a durable resource-queue predecessor | Shows `⏳ Warte auf <Tab/Session>` and stays below every directly working session. |
| 6 | Active pi-subagents child | Shows `⏳ Subagent working` below directly working agents. |
| 7 | Ordinary idle | Remains below active work. |
| 8 | Inactive in `LATER…` | Stays below ordinary idle. |
| 9 | Inactive in `WAIT…` | Stays below LATER sessions. |
| 10 | Unknown outside parked workspaces | Remains last. |

Inside each priority tier, sessions are grouped by the current top-left Spaces order: all matching sessions from the first Space appear before matching sessions from the second Space, and so on. The tier hierarchy itself stays unchanged—blocked remains above completed, completed above working, and so forth. Within one Space, every session sorts case-insensitively by its displayed tab name. Herdr's seen state and the most recent state change are only fallback tie-breakers for equal or unavailable tab names.

Clicking a session or typing an unsent draft does **not** clear the completion marker. Starting a new parent work cycle does. A background child temporarily gets the subagent rank; any underlying completion priority returns when the child finishes.

Workspace labels beginning with the case-sensitive prefix **`LATER`** or **`WAIT`** act as parking signals. Inactive sessions in either kind of workspace move to the parked tiers, including fresh completions. Ordinary idle sessions remain above parked sessions; LATER sessions remain above WAIT sessions. Blocked, directly working, and active-subagent sessions keep their activity ranks. Priority+ preserves pending history, so moving a completed session out of a parked workspace restores its pending-completion rank.

Priority+ never rearranges the Spaces themselves. Their current visible order—including any drag-and-drop arrangement—is the source of truth for every Agents tier. `LATER…` and `WAIT…` labels change the inactive session rank, not the Space position; alphabetical tab-name sorting applies equally to normal and parked Spaces.

> [!IMPORTANT]
> The pending-completion rank is a workflow reminder, not content analysis. It means **Herdr observed a completion—or Priority+ recovered a final Pi assistant response—and no new work cycle has started**. An error or abort that settles to idle can count as completion; an autonomous new run clears the marker too.

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
    { equals = "⚠ Interview Pending", fg = "#cb4b16", bold = true },
    { equals = "⏳ Subagent working", fg = "#268bd2", bold = true },
    { contains = "⏳ Warte auf ", fg = "#268bd2", bold = true },
    { contains = "", hide = true },
  ] }],
]
```

The first three rules show Priority+'s activity badges; the final rule hides every other status label. When there is no badge, the entire extra row disappears. Do not create a duplicate TOML section. If you use `rows_by_agent` overrides, append the row to the relevant overrides too.

This affects the **expanded desktop sidebar**. Collapsed and mobile layouts keep their native compact appearance.

> [!NOTE]
> **Upgrading:** Version 0.12.1 recovers unanswered idle Pi completions after watcher restarts or upgrades, so invoke `enable` once to restart the watcher. Version 0.12.0 moves peer and resource-queue waits below every directly working session. Version 0.11.0 added durable resource-queue waits and kept them out of the reply-required completion tier. Version 0.10.0 added pending-interview detection. Add the orange `⚠ Interview Pending` rule shown above, then invoke `enable` once so Priority+ restarts its watcher. Version 0.9.0 added `parallel_wait_target`; keep the dynamic `contains = "⏳ Warte auf "` rule as well. Version 0.8.0 introduced alphabetical tab sorting in every Space. If upgrading from 0.6.1 or earlier, version 0.7.0 also removed the `◉ Awaiting reply` presentation; remove that retired rule. Do not run `cleanup` for these upgrades.

### 3. Activate Priority+

```sh
herdr server reload-config
herdr plugin action invoke local.priority-plus.enable
herdr plugin log list --plugin local.priority-plus
```

Check that the `enable` action succeeded. Plugin actions run asynchronously; invoking one does not mean it has finished. No Herdr or Pi restart is required.

> [!NOTE]
> Existing unseen `done` sessions are adopted. For an idle Pi session with a reported JSONL path, Priority+ also recovers a final assistant text response on its first observation; historical idle sessions without a usable Pi transcript are not guessed to be unanswered.

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

Short-lived hooks reconcile Herdr's agent, workspace, and tab snapshots and track completion by terminal and agent/session identity. A `pp_rank` metadata token controls the priority section. Every session receives a zero-padded `pp_workspace_sort` position derived from the authoritative visible workspace list, so each rank independently follows the Spaces order. When the tab join is available, every session also receives a case-folded, Herdr-bounded `pp_session_sort` token from its displayed tab name. Herdr's **client-local `seen`** and the state-change sequence remain fallback tie-breakers. Pending completions do not receive an additional display label.

On the first compatible observation of an idle Pi session, Priority+ checks whether the reported JSONL session's active head is a non-empty assistant text response. If so, it restores the pending-completion rank even when watcher state was missing after a restart or upgrade. The completed transcript classification is persisted, so an unchanged idle session is not scanned repeatedly.

While a Pi agent remains `working`, Priority+ checks the bounded tail of its reported JSONL session path for an unresolved `interview` tool call on the active branch. That pane moves above blocked sessions and shows `⚠ Interview Pending`. The watcher notices both the call and its matching tool result without waiting for a lifecycle transition; after the result arrives, the ordinary working rank and label return.

When the Pi Parallel Session Watch extension publishes `parallel_wait_target`, Priority+ places that pane below every directly working session and shows `⏳ Warte auf <target>`. Durable resource-queue waits receive the same treatment when the extension publishes both `parallel_wait_resource_ticket` and `parallel_wait_predecessor`. This keeps a settled Pi turn that is still waiting out of both the higher reply-required completion tier and the directly working tier. The extension retains ownership of all wait tokens; Priority+ only projects them. Removing the relevant tokens restores the underlying lifecycle/completion rank and clears the dynamic label.

When [pi-subagents](https://github.com/nicobailon/pi-subagents) publishes its `summary` token, Priority+ gives that pane a stable `⏳ Subagent working` label and places it below directly working agents. If that best-effort metadata is missing, Priority+ falls back to live, same-user pi-subagents status artifacts and matches their parent session path to Herdr's agent-session identity. The original completion state remains intact underneath. Because Herdr deliberately excludes high-volume `pane.updated` events from manifest hooks, startup and low-volume hooks supervise one detached per-server socket watcher. It selects peer-wait, subagent-summary, and legacy-label cleanup edges, recovers `events_lost`, and reconciles when the active-session projection changes; unrelated pane updates are ignored.

Priority+ joins each agent's `workspace_id` and `tab_id` with Herdr's live workspace and tab lists. Labels beginning with `LATER` or `WAIT` assign distinct inactive ranks: ordinary idle → LATER → WAIT. Every rank uses the visible workspace position as its first secondary key and the case-folded tab label as its next key, so normal and parked sessions sort alphabetically inside the same Space. Blocked, working, and active-subagent behavior remains visible at the normal activity ranks. An active subagent keeps its badge across idle, done, working, and blocked parent states and always uses the common subagent rank. Pending interviews use the highest rank; blocked sessions without active children remain next. Create, rename, drag-and-drop, and reorder hooks only refresh metadata from the authoritative snapshot; they never call a workspace mutation API.

Transcript inspection is limited to completion recovery and interview detection: it reads at most the last 1 MiB of same-user, regular Pi JSONL session files reported by Herdr, uses only message roles and structural content types from the active branch/head, caches or persists completed projections, and records no message content. All other behavior remains lifecycle/metadata based. The watcher primarily uses Herdr's event stream and performs a bounded two-second heartbeat, with a full reconciliation at most every 30 seconds when nothing changes. It exits when the server socket closes; `cleanup` stops it explicitly. Per-socket locks and atomic private files serialize updates. Requests have bounded timeouts and verify the server's process identity. Subagent status artifacts must be regular same-user files, stay below 1 MiB, name a live parent PID, and report a queued/running state. The plugin never submits prompts, changes focus, renames sessions, or fabricates native agent states. Stored metadata may include session IDs or paths.

### Limits and coexistence

- **Transcript-based detection requires a reported Pi JSONL session path.** Pending-interview detection applies only while Herdr reports `working`; completion recovery applies to an unclassified idle Pi session. Unusually large final JSONL records beyond the bounded 1 MiB tail may not be detected.
- **Restarts reconstruct Pi completion tracking when a usable transcript is available** and preserve your sorting preference. Historical idle runs from other agents or missing/unusable transcripts cannot be reconstructed; hook updates can briefly lag.
- **Subagent detection prefers pi-subagents metadata.** The standard `⏳ N subagent(s)` summary token is primary; live same-user status artifacts are the fallback when the root Pi session fails to publish or refresh it.
- **Parking matching is case-sensitive and prefix-based.** `LATER`, `LATER/MP`, `WAIT`, and `WAITING` match; lowercase variants do not.
- **Space order is user-controlled.** Priority+ mirrors the current visible order inside every Agents tier and never moves Spaces itself. Drag-and-drop changes therefore become the new ordering source immediately, including for LATER, WAIT, and linked-worktree Spaces.
- **Session-name sorting is case-insensitive, not locale-aware.** Every tier uses Unicode case folding on displayed tab labels within a Space; Space ordering itself is never alphabetical unless the user arranged it that way.
- **Single-server support.** Combined multi-machine views are unsupported. Simultaneous clients have not been end-to-end validated.
- **Priority+ token keys are reserved:** Herdr stores token keys per pane, not per reporter. Other plugins must not use `pp_rank`, `pp_workspace_sort`, or `pp_session_sort`. Version 0.7.0 also clears the retired `pp_context_suppressed` token during migration. `parallel_wait_target`, `parallel_wait_resource_ticket`, and `parallel_wait_predecessor` are intentionally foreign metadata owned by the Pi watcher extension; Priority+ reads but never clears them.
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

Coverage includes transitions, startup/upgrade recovery of idle Pi completions, user-controlled drag-and-drop Space ordering across every rank, alphabetical tab-name sorting across normal and parked sessions, LATER/WAIT parking and rename restoration, metadata-only and status-artifact subagent detection, watcher filtering/ticks/event-loss recovery, identity changes, moves, restarts, concurrent persistence, foreign metadata/views, and cleanup. The real TUI test checks **unread → clicked/read → draft typed → new work**, cross-Space ordering, persisted Space moves, LATER/WAIT hierarchy, the subagent row, and both sorting toggles.
