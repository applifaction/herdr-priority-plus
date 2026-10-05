<div align="center">

# Herdr Priority+

**Read a result without losing its place in your queue.**

[![Herdr 0.9.1+](https://img.shields.io/badge/Herdr-0.9.1%2B-blue)](https://herdr.dev)
![Linux](https://img.shields.io/badge/platform-Linux-blue)
![Python standard library](https://img.shields.io/badge/runtime-Python_standard_library-green)

[Quick start](#quick-start) · [Usage](#usage) · [How it works](#how-it-works) · [Development](#development)

</div>

A small [Herdr](https://herdr.dev) plugin that keeps **viewed completions above running agents** until the next work cycle. Opening a finished session no longer means losing track of it at the bottom of your queue.

Built for multi-session Pi workflows, Priority+ uses Herdr's lifecycle and display metadata—not transcript scanning. It adds conditional **awaiting-reply** and **subagent-working** badges plus a toggleable Agents view. Native status icons stay unchanged.

With Herdr's symbol indicators enabled:

```text
agents                  Priority+

× Blocked
✓ Finished, not yet viewed
○ Reviewed result
  ◉ Awaiting reply
◐ Working
○ Parent idle while a child runs
  ⏳ Subagent working
○ Ordinary idle
○ Inactive in a LATER workspace
○ Inactive in a WAIT workspace
```

The badges read **`◉ Awaiting reply`** and **`⏳ Subagent working`**. Plugin action titles are currently German.

## What changes

| Priority | Session state | Behavior |
| --- | --- | --- |
| 1 | Blocked | Stays at the top. |
| 2 | Completed, not viewed outside `LATER…`/`WAIT…` | Keeps Herdr's native done indicator near the top. |
| 3 | Completed, viewed, no new run | Shows the extra badge above working agents. |
| 4 | Working | Clears the previous completion marker. |
| 5 | Active pi-subagents child | Shows `⏳ Subagent working` below directly working agents. |
| 6 | Ordinary idle | Remains below active work. |
| 7 | Inactive in `LATER…` | Stays below ordinary idle without a reply badge. |
| 8 | Inactive in `WAIT…` | Stays below LATER sessions without a reply badge. |
| 9 | Unknown outside parked workspaces | Remains last. |

Clicking a session or typing an unsent draft does **not** clear the completion marker. Starting a new parent work cycle does. A background child temporarily gets the subagent rank; any underlying completion marker returns when the child finishes.

Workspace labels beginning with the case-sensitive prefix **`LATER`** or **`WAIT`** act as parking signals. Inactive sessions in either kind of workspace lose the `Awaiting reply` badge, including fresh completions. Ordinary idle sessions remain above parked sessions; LATER sessions remain above WAIT sessions. Blocked, directly working, and active-subagent sessions keep their activity ranks. Priority+ preserves pending history, so moving a completed session out of a parked workspace restores its rank and badge.

While sorting is enabled, Priority+ also keeps the canonical workspace list in three groups: ordinary workspaces first in their existing order, followed by `LATER…` and `WAIT…`, each sorted case-insensitively by workspace name. In the Agents view, sessions inside each parked group are sorted by workspace name and then by session/tab name.

> [!IMPORTANT]
> “Awaiting reply” is a workflow reminder, not content analysis. It means **Herdr observed a completion and no new work cycle has started**. An error or abort that settles to idle can count as completion; an autonomous new run clears the marker too.

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
    { equals = "◉ Awaiting reply", fg = "#b58900", bold = true },
    { contains = "", hide = true },
  ] }],
]
```

The first two rules show the two Priority+ badges; the third hides every other status label. When there is no badge, the entire extra row disappears. Do not create a duplicate TOML section. If you use `rows_by_agent` overrides, append the row to the relevant overrides too.

This affects the **expanded desktop sidebar**. Collapsed and mobile layouts keep their native compact appearance.

> [!NOTE]
> **Upgrading:** from 0.1.0, change the old `◉ Antwort offen` value to `◉ Awaiting reply`. From 0.1.1, add the `⏳ Subagent working` rule shown above. Version 0.1.3 starts an event watcher so subagent-only metadata changes apply immediately. Version 0.2.0 adds `LATER…` parking and canonical normal → LATER → WAIT workspace ordering. Version 0.2.1 separates the inactive Agents tiers into ordinary → LATER → WAIT. Version 0.3.0 alphabetizes parked workspaces and their sessions. Invoke `enable` once after updating a linked checkout; existing pending badges refresh without losing their completion state and the workspace groups reconcile. Do not run `cleanup` for these upgrades.

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

**Disable turns off sorting enforcement only.** Completion tracking and badges remain active. Herdr returns to its configured Agents sort policy when this plugin owns the active view, and Priority+ stops correcting future workspace moves. Because workspace ordering is a canonical Herdr mutation, disabling does not restore the order that existed before Priority+ ran.

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

Cleanup also stops the event watcher and pauses tracking so queued hooks cannot recreate the metadata or restart the watcher. It does not reconstruct an earlier workspace order. To use the plugin again, invoke `enable`. Herdr retains plugin-owned state after unlinking. Simply disabling or unlinking the plugin does not perform this metadata cleanup.

## How it works

Short-lived hooks reconcile Herdr's agent, workspace, and tab snapshots and track completion by terminal and agent/session identity. A `pp_rank` metadata token controls the priority section. Inactive parked sessions additionally receive case-folded `pp_workspace_sort` and, when the tab join is available, `pp_session_sort` tokens derived from the displayed workspace and session/tab names. Active tiers do not receive these alphabetical keys. Herdr's **client-local `seen`** then separates otherwise equal unread and reviewed results. An `idle` display label supplies the reply badge only after review.

When [pi-subagents](https://github.com/nicobailon/pi-subagents) publishes its `summary` token, Priority+ gives that pane a stable `⏳ Subagent working` label and places it below directly working agents. The original completion state remains intact underneath. Because Herdr deliberately excludes high-volume `pane.updated` events from manifest hooks, startup and low-volume hooks supervise one detached per-server socket watcher. It selects only subagent-summary edges and reconciles current snapshots; ordinary pane updates are ignored.

Priority+ joins each agent's `workspace_id` and `tab_id` with Herdr's live workspace and tab lists. Labels beginning with `LATER` or `WAIT` suppress the awaiting-reply presentation and assign distinct inactive ranks: ordinary idle → LATER → WAIT. Within the inactive LATER and WAIT sections, the view sorts case-insensitively by workspace label and then by session/tab label. Blocked, working, and active-subagent behavior remains visible at the normal activity ranks and keeps its existing secondary ordering. When sorting is enabled, the authoritative workspace snapshot drives one idempotent `workspace.move_block` only when necessary, preserving normal order while alphabetizing the LATER and WAIT groups. Create, workspace/tab rename, manual-move, and reorder hooks converge without repeatedly moving an already-correct list.

There is **no polling or transcript scanning**. The watcher uses Herdr's event stream and exits when the server socket closes; `cleanup` stops it explicitly. Per-socket locks and atomic private files serialize updates. Requests have bounded timeouts and verify the server's process identity. The plugin never submits prompts, changes focus, renames sessions, or fabricates native agent states. Stored metadata may include session IDs or paths.

### Limits and coexistence

- **Restarts reset completion tracking**, but preserve your sorting preference. Missed historical runs cannot be reconstructed; hook updates can briefly lag.
- **Subagent detection follows pi-subagents metadata.** It requires the root interactive Pi session to publish the standard `⏳ N subagent(s)` summary token through its Herdr bridge.
- **Parking matching is case-sensitive and prefix-based.** `LATER`, `LATER/MP`, `WAIT`, and `WAITING` match; lowercase variants do not.
- **Workspace grouping changes canonical order.** While enabled, Priority+ corrects manual moves back to normal → alphabetic LATER → alphabetic WAIT. Disable or cleanup stops future enforcement but does not restore a previous order; linked-worktree workspaces are sorted by their own displayed labels like every other workspace.
- **Alphabetical sorting is case-insensitive, not locale-aware.** It uses Unicode case folding on the displayed workspace and session/tab labels.
- **Single-server support.** Combined multi-machine views are unsupported. Simultaneous clients have not been end-to-end validated.
- **Priority+ token keys are reserved:** Herdr stores token keys per pane, not per reporter. Other plugins must not use `pp_rank`, `pp_workspace_sort`, or `pp_session_sort`.
- **Views are shared:** enable/startup can replace another plugin's view. Ordinary hooks never reclaim it; clearing affects only Priority+'s view.
- **Other idle labels may override the badge.** Cleanup removes only this plugin's label contribution and leaves unrelated tokens alone.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No badge | Check the conditional row and agent-specific overrides, reload config, then finish/view a new run in the expanded sidebar. |
| Normal sort header | Invoke `enable` and check its log. Linking does not run startup hooks in an already-running server. |
| Native icon becomes a circle | Expected: the badge is an extra row, not an icon replacement. |
| Badges remain after disabling sorting | Expected; `cleanup` removes them and pauses tracking. |
| `Awaiting reply` remains in a parked workspace | Confirm the workspace label begins with uppercase `LATER` or `WAIT`, then invoke `enable` and inspect plugin logs. |
| Workspace groups are not corrected | Confirm Priority+ sorting is enabled; disabled/cleaned hooks intentionally do not reorder workspaces. |

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

Coverage includes transitions, LATER/WAIT parking and rename restoration, alphabetic parked workspace/session ordering, metadata-only subagent start/refresh/completion, watcher filtering, identity changes, moves, restarts, concurrent persistence, foreign metadata/views, and cleanup. The real TUI test checks **unread → clicked/read → draft typed → new work**, LATER/WAIT hierarchy and alphabetization, canonical workspace order, the subagent row, and both sorting toggles.
