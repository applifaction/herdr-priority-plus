<div align="center">

# Herdr Priority+

**Read a result without losing its place in your queue.**

[![Herdr 0.9.1+](https://img.shields.io/badge/Herdr-0.9.1%2B-blue)](https://herdr.dev)
![Linux](https://img.shields.io/badge/platform-Linux-blue)
![Python standard library](https://img.shields.io/badge/runtime-Python_standard_library-green)

[Quick start](#quick-start) · [Usage](#usage) · [How it works](#how-it-works) · [Development](#development)

</div>

A small [Herdr](https://herdr.dev) plugin that keeps **viewed completions above running agents** until the next work cycle. Opening a finished session no longer means losing track of it at the bottom of your queue.

Built for multi-session Pi workflows, Priority+ uses Herdr's agent lifecycle events—not a Pi extension. It adds a conditional **awaiting-reply badge** and a toggleable Agents view. Native status icons stay unchanged.

With Herdr's symbol indicators enabled:

```text
agents                  Priority+

× Blocked
✓ Finished, not yet viewed
○ Reviewed result
  ◉ Awaiting reply
◐ Working
○ Idle
```

The badge reads **`◉ Awaiting reply`**. Plugin action titles are currently German.

## What changes

| Priority | Session state | Behavior |
| --- | --- | --- |
| 1 | Blocked | Stays at the top. |
| 2 | Completed, not viewed | Keeps Herdr's native done indicator. |
| 3 | Completed, viewed, no new run | Shows the extra badge above working agents. |
| 4 | Working | Clears the previous completion marker. |
| 5 | Ordinary idle | Remains below active work. |
| 6 | Unknown | Remains last. |

Clicking a session or typing an unsent draft does **not** clear the marker. Starting a new work cycle does.

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
    { equals = "◉ Awaiting reply", fg = "#b58900", bold = true },
    { contains = "", hide = true },
  ] }],
]
```

The first rule shows the badge; the second hides every other status label. When there is no badge, the entire extra row disappears. Do not create a duplicate TOML section. If you use `rows_by_agent` overrides, append the row to the relevant overrides too.

This affects the **expanded desktop sidebar**. Collapsed and mobile layouts keep their native compact appearance.

> [!NOTE]
> **Upgrading from 0.1.0:** change the old `◉ Antwort offen` value in your sidebar rule to `◉ Awaiting reply`. Existing pending badges refresh on the next hook or `enable` action without losing their completion state. Do not run `cleanup` for this upgrade.

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

**Disable turns off only the custom sorting.** Completion tracking and badges remain active. Herdr returns to its configured sort policy when this plugin owns the active view.

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

Cleanup also pauses tracking so queued hooks cannot recreate the metadata. To use the plugin again, invoke `enable`. Herdr retains plugin-owned state after unlinking. Simply disabling or unlinking the plugin does not perform this metadata cleanup.

## How it works

Short-lived hooks reconcile Herdr's agent snapshots and track completion by terminal and agent/session identity. A `pp_rank` metadata token groups pending completions above working agents; Herdr's **client-local `seen`** then separates unread from reviewed results. An `idle` display label supplies the badge only after review.

There is **no polling daemon or transcript scanning**. Per-socket locks and atomic private files serialize updates. Requests have bounded timeouts and verify the server's process identity. The plugin never submits prompts, changes focus, renames sessions, or fabricates native agent states. Stored metadata may include session IDs or paths.

### Limits and coexistence

- **Restarts reset completion tracking**, but preserve your sorting preference. Missed historical runs cannot be reconstructed; hook updates can briefly lag.
- **Single-server support.** Combined multi-machine views are unsupported. Simultaneous clients have not been end-to-end validated.
- **`pp_rank` is reserved:** Herdr stores token keys per pane, not per reporter. Other plugins must not use this key.
- **Views are shared:** enable/startup can replace another plugin's view. Ordinary hooks never reclaim it; clearing affects only Priority+'s view.
- **Other idle labels may override the badge.** Cleanup removes only this plugin's label contribution and leaves unrelated tokens alone.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No badge | Check the conditional row and agent-specific overrides, reload config, then finish/view a new run in the expanded sidebar. |
| Normal sort header | Invoke `enable` and check its log. Linking does not run startup hooks in an already-running server. |
| Native icon becomes a circle | Expected: the badge is an extra row, not an icon replacement. |
| Badges remain after disabling sorting | Expected; `cleanup` removes them and pauses tracking. |

```sh
herdr plugin list --plugin local.priority-plus --json
herdr plugin log list --plugin local.priority-plus --limit 10
```

Review diagnostic logs before sharing them: Herdr invocation context may include local paths.

## Development

| File | Purpose |
| --- | --- |
| `herdr-plugin.toml` | Manifest, actions, and hooks. |
| `priority_plus.py` | Tracking, socket API, persistence, and view updates. |
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

Coverage includes transitions, identity changes, moves, restarts, concurrent persistence, foreign metadata/views, and cleanup. The real TUI test checks **unread → clicked/read → draft typed → new work**, plus both sorting toggles.
