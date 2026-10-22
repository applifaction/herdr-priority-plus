#!/usr/bin/env python3
"""One-shot Priority+ reconciliation. Linux, Python standard library only."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

PLUGIN = "local.priority-plus"
SOURCE = "plugin:" + PLUGIN
TOKEN = "pp_rank"
WORKSPACE_TOKEN = "pp_workspace_sort"
SESSION_TOKEN = "pp_session_sort"
AGENT_DISPLAY_TOKEN = "pp_agent_display"
REPLY_DISPLAY_TOKEN = "pp_reply_display"
STATUS_DISPLAY_TOKEN = "pp_status_display"
INTERNET_WAIT_TOKEN = "pp_internet_wait"
FETCH_RECOVERY_TOKEN = "pica_fetch_recovery"
LEGACY_CONTEXT_SUPPRESSION_TOKEN = "pp_context_suppressed"
PARALLEL_WAIT_TARGET_TOKEN = "parallel_wait_target"
RESOURCE_WAIT_TICKET_TOKEN = "parallel_wait_resource_ticket"
RESOURCE_WAIT_PREDECESSOR_TOKEN = "parallel_wait_predecessor"
COORDINATOR_WAIT_OWNER_TOKEN = "coordinator_wait_owner"
COORDINATOR_ACCEPTED_COMPLETION_TOKEN = "coordinator_accepted_completion"
COORDINATOR_ACCEPTED_COMPLETION_OUTCOME_TOKEN = "coordinator_accepted_completion_outcome"
COORDINATOR_MANAGED_WAIT_TOKEN = "coordinator_managed_wait"
COORDINATOR_MANAGED_WAIT_TARGET_PREFIX = "target:"
COORDINATOR_USER_REVIEW_TOKEN = "coordinator_user_review"
COORDINATOR_RECOVERY_TOKEN = "coordinator_recovery"
COORDINATOR_RECOVERY_REASON_TOKEN = "coordinator_recovery_reason"
OWNED_TOKENS = (TOKEN, WORKSPACE_TOKEN, SESSION_TOKEN, AGENT_DISPLAY_TOKEN,
                REPLY_DISPLAY_TOKEN, STATUS_DISPLAY_TOKEN, INTERNET_WAIT_TOKEN,
                LEGACY_CONTEXT_SUPPRESSION_TOKEN)
RANK_USER_REVIEW = "9"
RANK_RECOVERY_REQUIRED = RANK_USER_REVIEW
RANK_INTERVIEW = "8"
RANK_BLOCKED = "7"
RANK_PENDING = "6"
RANK_WORKING = "5"
RANK_INTERNET_WAIT = "4"
RANK_DEPENDENCY_WAIT = "4"
RANK_SUBAGENT = "4"
RANK_COORDINATOR_MANAGED_WAIT = "4"
RANK_IDLE = "3"
RANK_ACCEPTED_COMPLETION = RANK_USER_REVIEW
RANK_LATER = "2"
RANK_WAIT = "1"
RANK_UNKNOWN = "0"
RANK_COORDINATOR_WAIT = RANK_DEPENDENCY_WAIT
LATER_WORKSPACE_PREFIXES = ("LATER", "💤")
WAIT_WORKSPACE_PREFIXES = ("WAIT", "⏳")
LEGACY_PENDING_LABEL = "◉ Awaiting reply"
SUBAGENT_LABEL = "🤖 Subagent working"
PARALLEL_WAIT_LABEL_PREFIX = "⏳ Warte auf "
COORDINATOR_WAIT_LABEL = "⏳ Waiting for Coordinator"
ACCEPTED_COMPLETION_LABEL = "✅ Accepted / pending completion"
COORDINATOR_MANAGED_WAIT_LABEL = "⏳ Waiting for Managed Sessions"
COORDINATOR_MANAGED_WAIT_LABEL_PREFIX = "⏳ Waiting for "
COORDINATOR_USER_REVIEW_LABEL = "🙋 Waiting for User Review"
COORDINATOR_RECOVERY_LABEL = "⚠ Recovery Review Required"
INTERNET_WAIT_LABEL = "⏳ Waiting for Internet"
INTERVIEW_LABEL = "⚠ Interview Pending"
WATCH_REQUEST_ID = SOURCE + ":watch"
WATCHER_VERSION = "0.20.5"
WATCH_TICK_SECONDS = 2
WATCH_FULL_RECONCILE_SECONDS = 30
SUBAGENT_STATUS_LIMIT = 1024 * 1024
SUBAGENT_STATUS_SCAN_LIMIT = 512
SUBAGENT_STATUS_STALE_SECONDS = 10 * 60
SESSION_TAIL_LIMIT = 1024 * 1024
METADATA_TOKEN_CHAR_LIMIT = 80
DISPLAY_TOKEN_CHAR_LIMIT = 256
# Herdr trims ordinary leading whitespace from metadata values. The blank
# braille cell survives normalization while rendering as a one-column indent.
STATUS_INDENT = "\u2800"
ACTIVE_RUN_STATES = ("queued", "running")
TERMINAL_RUN_STATES = ("complete", "completed", "failed", "stopped", "cancelled", "canceled")
SUBAGENT_STATES = ("blocked", "idle", "done", "working")
SUBAGENT_SUMMARY = re.compile(r"^⏳ [1-9][0-9]* subagents?(?: \([^()\n]*\))?$")
INTERNET_WAIT_ERROR = re.compile(
    r"(?:\b(?:ENETDOWN|ENETUNREACH|EHOSTDOWN|EHOSTUNREACH|ECONNABORTED|"
    r"ECONNREFUSED|ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|"
    r"UND_ERR_CONNECT_TIMEOUT|UND_ERR_HEADERS_TIMEOUT|UND_ERR_BODY_TIMEOUT|"
    r"UND_ERR_SOCKET)\b|kind=upstream-timeout|fetch failed|connection error|"
    r"network error|socket hang up|tunnel[_ -]destination[_ -]denied)",
    re.IGNORECASE,
)
VIEW = {
    "source": SOURCE,
    "label": "Priority+",
    "sort": [
        {"field": {"token": TOKEN}, "order": "desc"},
        {"field": "workspace_order", "order": "asc"},
        {"field": {"token": SESSION_TOKEN}, "order": "asc"},
        {"field": "seen", "order": "asc"},
        {"field": "state_change_seq", "order": "desc"},
    ],
}


class ApiError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("Herdr API error: " + code)


class Client:
    """Herdr uses one request per connection; verify the same boot on each."""
    def __init__(self, path):
        self.path = path
        self.boot = None
        self.serial = 0
        self.sock = None
        self.reader = None
        self.connect()

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        try:
            self.sock.connect(self.path)
            pid, uid, _ = struct.unpack("3i", self.sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if uid != os.getuid():
                raise RuntimeError("Herdr socket belongs to another user")
            # /proc stat's comm can contain spaces and parentheses.
            start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            identity = f"{boot}:{pid}:{start}"
            if self.boot is not None and self.boot != identity:
                raise RuntimeError("Herdr server changed during reconciliation; retry")
            self.boot = identity
            self.reader = self.sock.makefile("rb")
        except BaseException:
            self.sock.close()
            raise

    def close(self):
        if self.reader:
            self.reader.close()
            self.reader = None
        if self.sock:
            self.sock.close()
            self.sock = None

    def call(self, method, params=None):
        if self.sock is None:
            self.connect()
        try:
            return self.request(method, params)
        finally:
            self.close()

    def request(self, method, params):
        self.serial += 1
        request_id = str(self.serial)
        self.sock.sendall((json.dumps({"id": request_id, "method": method,
                                      "params": params or {}}) + "\n").encode())
        line = self.reader.readline(8 * 1024 * 1024 + 1)
        if len(line) > 8 * 1024 * 1024 or not line.endswith(b"\n"):
            raise RuntimeError("Herdr response missing or exceeds 8 MiB")
        response = json.loads(line)
        if response.get("id") != request_id:
            raise RuntimeError("Unexpected Herdr response id")
        if "error" in response:
            raise ApiError(response["error"].get("code", "unknown"))
        return response["result"]


def instance_folder(directory, socket_path):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(os.path.realpath(socket_path).encode()).hexdigest()
    folder = directory / key
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


@contextmanager
def state_lock(directory, socket_path):
    folder = instance_folder(directory, socket_path)
    fd = os.open(folder / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Priority+ state lock timed out")
                time.sleep(0.025)
        yield folder / "state.json"
    finally:
        os.close(fd)


def save_state(path, state):
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state(path, boot):
    if path.exists():
        state = json.loads(path.read_text())
        if (state.get("version") != 1 or not isinstance(state.get("agents"), dict)
                or type(state.get("enabled")) is not bool
                or type(state.get("cleaned")) is not bool):
            raise RuntimeError("Invalid Priority+ state; restore or remove state.json")
        for record in state["agents"].values():
            if (not isinstance(record, dict) or not isinstance(record.get("identity"), list)
                    or type(record.get("seq")) is not int
                    or type(record.get("pending")) is not bool
                    or not isinstance(record.get("status"), str)
                    or ("subagent" in record and type(record["subagent"]) is not bool)
                    or ("parallel_wait" in record
                        and record["parallel_wait"] is not None
                        and not isinstance(record["parallel_wait"], str))
                    or ("coordinator_wait" in record
                        and record["coordinator_wait"] is not None
                        and not isinstance(record["coordinator_wait"], str))
                    or ("waiting" in record and type(record["waiting"]) is not bool)
                    or ("interview" in record and type(record["interview"]) is not bool)
                    or ("internet_wait" in record
                        and type(record["internet_wait"]) is not bool)
                    or ("internet_recovery" in record
                        and type(record["internet_recovery"]) is not bool)
                    or ("transcript_checked" in record
                        and type(record["transcript_checked"]) is not bool)
                    or record.get("parking") not in (None, "later", "wait")):
                raise RuntimeError("Invalid Priority+ agent record")
        if state.get("boot") != boot:
            state["agents"] = {}  # Never compare lifecycle counters across boots.
            state["boot"] = boot
        return state
    return {"version": 1, "boot": boot, "enabled": True, "cleaned": False, "agents": {}}


def workspace_parking_kind(label):
    if not isinstance(label, str):
        return None
    if label.startswith(LATER_WORKSPACE_PREFIXES):
        return "later"
    if label.startswith(WAIT_WORKSPACE_PREFIXES):
        return "wait"
    return None


def is_wait_workspace(label):
    return workspace_parking_kind(label) == "wait"


def is_later_workspace(label):
    return workspace_parking_kind(label) == "later"


def is_parked_workspace(label):
    return workspace_parking_kind(label) is not None


def alphabetical_key(value):
    return value.casefold() if isinstance(value, str) else ""


def metadata_sort_key(value):
    """Match Herdr's bounded metadata token storage."""
    return alphabetical_key(value)[:METADATA_TOKEN_CHAR_LIMIT]


def workspace_order_key(position):
    """Lexically sortable position in the visible workspace list."""
    return f"{position:020d}"


def process_predates_run(pid, started_at_ms):
    """Reject stale artifacts whose PID has since been reused."""
    if type(pid) is not int or pid <= 0 or not isinstance(started_at_ms, (int, float)):
        return False
    os.kill(pid, 0)
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    process_start_ticks = int(fields[19])
    uptime_seconds = float(Path("/proc/uptime").read_text().split()[0])
    boot_epoch = time.time() - uptime_seconds
    process_started_at = boot_epoch + process_start_ticks / os.sysconf("SC_CLK_TCK")
    return process_started_at <= started_at_ms / 1000 + 5


def active_subagent_sessions(root=None, now_ms=None):
    """Return live parent session paths from recent pi-subagents artifacts."""
    if root is None:
        root = (Path(tempfile.gettempdir()) / f"pi-subagents-uid-{os.getuid()}"
                / "async-subagent-runs")
    root = Path(root)
    if not root.is_dir():
        return set()
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    candidates = []
    for path in root.glob("*/status.json"):
        try:
            info = path.stat(follow_symlinks=False)
            if (info.st_uid == os.getuid() and stat.S_ISREG(info.st_mode)
                    and info.st_size <= SUBAGENT_STATUS_LIMIT
                    and now_ms / 1000 - info.st_mtime <= SUBAGENT_STATUS_STALE_SECONDS):
                candidates.append((info.st_mtime_ns, path))
        except OSError:
            continue
    sessions = set()
    for _, path in sorted(candidates, reverse=True)[:SUBAGENT_STATUS_SCAN_LIMIT]:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if (info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode)
                        or info.st_size > SUBAGENT_STATUS_LIMIT):
                    continue
                payload = os.read(fd, SUBAGENT_STATUS_LIMIT + 1)
            finally:
                os.close(fd)
            if len(payload) > SUBAGENT_STATUS_LIMIT:
                continue
            status = json.loads(payload)
            if not isinstance(status, dict):
                continue
            session = status.get("sessionId")
            top_state = status.get("state")
            step_states = [step.get("status") for step in status.get("steps", [])
                           if isinstance(step, dict)]
            if top_state in TERMINAL_RUN_STATES:
                continue
            active = (top_state in ACTIVE_RUN_STATES
                      or (top_state is None and any(state in ACTIVE_RUN_STATES
                                                   for state in step_states)))
            last_update = status.get("lastUpdate")
            if (not isinstance(session, str) or not session or not active
                    or not isinstance(last_update, (int, float))
                    or now_ms - last_update > SUBAGENT_STATUS_STALE_SECONDS * 1000
                    or not process_predates_run(status.get("pid"), status.get("startedAt"))):
                continue
            sessions.add(session)
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError,
                IndexError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return sessions


def has_active_subagents(agent, active_sessions=None):
    summary = (agent.get("tokens") or {}).get("summary")
    if isinstance(summary, str) and SUBAGENT_SUMMARY.fullmatch(summary) is not None:
        return True
    session = (agent.get("agent_session") or {}).get("value")
    return isinstance(session, str) and session in (active_sessions or ())


def pi_session_path(agent):
    session = agent.get("agent_session") or {}
    if (agent.get("agent") != "pi" or session.get("source") != "herdr:pi"
            or session.get("agent") != "pi" or session.get("kind") != "path"):
        return None
    value = session.get("value")
    return value if isinstance(value, str) and os.path.isabs(value) else None


def inspect_session_tail(path, projector, tail_limit=SESSION_TAIL_LIMIT, cache=None):
    """Apply a metadata-only projection to a bounded, same-user Pi JSONL tail."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
                return False
            signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            cached = cache.get(path) if cache is not None else None
            if cached and cached[0] == signature:
                return cached[1]
            start = max(0, info.st_size - tail_limit)
            os.lseek(fd, start, os.SEEK_SET)
            remaining = min(info.st_size - start, tail_limit)
            chunks = []
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        return False

    payload = b"".join(chunks)
    if start:
        _, separator, payload = payload.partition(b"\n")
        if not separator:
            return False
    result = projector(payload)
    if cache is not None:
        cache[path] = (signature, result)
    return result


def session_messages(payload):
    messages = {}
    head = None
    for line in payload.splitlines():
        try:
            entry = json.loads(line)
        except (UnicodeDecodeError, ValueError, TypeError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str):
            continue
        messages[entry_id] = entry
        head = entry_id
    return messages, head


def tail_has_pending_interview(payload):
    messages, head = session_messages(payload)
    resolved = set()
    visited = set()
    while head in messages and head not in visited:
        visited.add(head)
        entry = messages[head]
        message = entry.get("message")
        if not isinstance(message, dict):
            message = {}
        role = message.get("role")
        if role == "toolResult" and message.get("toolName") == "interview":
            call_id = message.get("toolCallId")
            if isinstance(call_id, str):
                resolved.add(call_id)
        elif role == "assistant":
            content = message.get("content")
            if not isinstance(content, list):
                content = []
            calls = [
                part.get("id")
                for part in content
                if isinstance(part, dict) and part.get("type") == "toolCall"
                and part.get("name") == "interview" and isinstance(part.get("id"), str)
            ]
            if any(call_id not in resolved for call_id in calls):
                return True
        parent = entry.get("parentId")
        head = parent if isinstance(parent, str) else None
    return False


def tail_has_completed_response(payload):
    messages, head = session_messages(payload)
    entry = messages.get(head)
    message = entry.get("message") if isinstance(entry, dict) else None
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(part, dict) and part.get("type") == "text"
        and isinstance(part.get("text"), str) and bool(part["text"].strip())
        for part in content
    )


def tail_has_internet_wait(payload):
    """Return whether the active branch ends in a retryable transport failure."""
    messages, head = session_messages(payload)
    entry = messages.get(head)
    message = entry.get("message") if isinstance(entry, dict) else None
    if (not isinstance(message, dict) or message.get("role") != "assistant"
            or message.get("stopReason") != "error"):
        return False
    error = message.get("errorMessage")
    return isinstance(error, str) and INTERNET_WAIT_ERROR.search(error) is not None


def session_has_pending_interview(path, tail_limit=SESSION_TAIL_LIMIT, cache=None):
    """Inspect only the active JSONL branch's bounded tail for an unresolved interview call."""
    return inspect_session_tail(path, tail_has_pending_interview, tail_limit, cache)


def session_has_completed_response(path, tail_limit=SESSION_TAIL_LIMIT, cache=None):
    """Return whether the active JSONL branch ends in a visible assistant response."""
    return inspect_session_tail(path, tail_has_completed_response, tail_limit, cache)


def session_waits_for_internet(path, tail_limit=SESSION_TAIL_LIMIT, cache=None):
    """Return whether Pi is retrying after a network failure on the active branch."""
    return inspect_session_tail(path, tail_has_internet_wait, tail_limit, cache)


def internet_wait_sessions(agents):
    sessions = set()
    for agent in agents:
        if agent.get("agent_status") != "working":
            continue
        session = pi_session_path(agent)
        if session and session_waits_for_internet(session):
            sessions.add(session)
    return sessions


def pending_interview_sessions(agents):
    sessions = set()
    for agent in agents:
        if agent.get("agent_status") != "working":
            continue
        session = pi_session_path(agent)
        if session and session_has_pending_interview(session):
            sessions.add(session)
    return sessions


def tracked_session_projection(directory, socket_path, detector, cache=None):
    """Apply a transcript detector to persisted working Pi session identities."""
    path = instance_folder(directory, socket_path) / "state.json"
    try:
        records = json.loads(path.read_text()).get("agents", {}).values()
    except (OSError, ValueError, TypeError, AttributeError):
        return set()
    sessions = set()
    tracked_paths = set()
    for record in records:
        if not isinstance(record, dict) or record.get("status") != "working":
            continue
        identity = record.get("identity")
        if (not isinstance(identity, list) or len(identity) != 5
                or identity[:4] != ["pi", "herdr:pi", "pi", "path"]
                or not isinstance(identity[4], str) or not os.path.isabs(identity[4])):
            continue
        tracked_paths.add(identity[4])
        if detector(identity[4], cache=cache):
            sessions.add(identity[4])
    if cache is not None:
        for session in set(cache) - tracked_paths:
            cache.pop(session, None)
    return sessions


def tracked_pending_interview_sessions(directory, socket_path, cache=None):
    return tracked_session_projection(
        directory, socket_path, session_has_pending_interview, cache)


def tracked_internet_wait_sessions(directory, socket_path, cache=None):
    return tracked_session_projection(
        directory, socket_path, session_waits_for_internet, cache)


def has_fetch_recovery_wait(agent):
    """Return whether the Pi recovery extension reports a settled wait."""
    tokens = agent.get("tokens") or {}
    value = tokens.get(FETCH_RECOVERY_TOKEN) if isinstance(tokens, dict) else None
    return isinstance(value, str) and bool(value.strip())


def tracked_fetch_recovery_waits(socket_path):
    """Poll leased recovery metadata because token reports can bypass watcher edges."""
    try:
        agents = Client(socket_path).call("agent.list")["agents"]
        return frozenset(agent["terminal_id"] for agent in agents
                         if has_fetch_recovery_wait(agent))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        # A transient socket failure must not kill the long-lived watcher. The
        # next two-second tick retries, while the 30-second sweep remains a guard.
        return None


def parallel_wait_target(agent):
    tokens = agent.get("tokens") or {}
    target = tokens.get(PARALLEL_WAIT_TARGET_TOKEN)
    if not isinstance(target, str):
        ticket = tokens.get(RESOURCE_WAIT_TICKET_TOKEN)
        target = (tokens.get(RESOURCE_WAIT_PREDECESSOR_TOKEN)
                  if isinstance(ticket, str) and ticket.strip() else None)
    if not isinstance(target, str):
        return None
    target = " ".join(target.split())
    return target[:METADATA_TOKEN_CHAR_LIMIT] or None


def parallel_wait_label(target):
    return (PARALLEL_WAIT_LABEL_PREFIX + target)[:METADATA_TOKEN_CHAR_LIMIT]


def coordinator_wait_owner(agent):
    tokens = agent.get("tokens") or {}
    owner = tokens.get(COORDINATOR_WAIT_OWNER_TOKEN)
    if not isinstance(owner, str):
        return None
    owner = " ".join(owner.split())
    return owner[:METADATA_TOKEN_CHAR_LIMIT] or None


def coordinator_accepted_completion(agent):
    tokens = agent.get("tokens") or {}
    owner = tokens.get(COORDINATOR_ACCEPTED_COMPLETION_TOKEN)
    if not isinstance(owner, str):
        return None
    owner = " ".join(owner.split())
    return owner[:METADATA_TOKEN_CHAR_LIMIT] or None


def coordinator_accepted_completion_is_visible(agent):
    return (coordinator_accepted_completion(agent) is not None
            and agent.get("agent_status") not in ("working", "blocked"))


def coordinator_wait_is_visible(agent):
    return (coordinator_wait_owner(agent) is not None
            and agent.get("agent_status") not in ("working", "blocked"))


def coordinator_managed_wait(agent):
    tokens = agent.get("tokens") or {}
    reason = tokens.get(COORDINATOR_MANAGED_WAIT_TOKEN)
    if not isinstance(reason, str):
        return None
    reason = " ".join(reason.split())
    return reason[:METADATA_TOKEN_CHAR_LIMIT] or None


def coordinator_managed_wait_label(wait_value):
    if (isinstance(wait_value, str)
            and wait_value.startswith(COORDINATOR_MANAGED_WAIT_TARGET_PREFIX)):
        target = " ".join(
            wait_value[len(COORDINATOR_MANAGED_WAIT_TARGET_PREFIX):].split())
        if target:
            return (COORDINATOR_MANAGED_WAIT_LABEL_PREFIX + target)[:METADATA_TOKEN_CHAR_LIMIT]
    return COORDINATOR_MANAGED_WAIT_LABEL


def coordinator_managed_wait_is_visible(agent):
    return (coordinator_managed_wait(agent) is not None
            and agent.get("agent_status") not in ("working", "blocked"))


def is_coordinator_managed_wait_label(label):
    return (label == COORDINATOR_MANAGED_WAIT_LABEL
            or (isinstance(label, str)
                and label.startswith(COORDINATOR_MANAGED_WAIT_LABEL_PREFIX)))


def coordinator_user_review(agent):
    tokens = agent.get("tokens") or {}
    reason = tokens.get(COORDINATOR_USER_REVIEW_TOKEN)
    if not isinstance(reason, str):
        return None
    reason = " ".join(reason.split())
    return reason[:METADATA_TOKEN_CHAR_LIMIT] or None


def coordinator_user_review_is_visible(agent):
    return (coordinator_user_review(agent) is not None
            and agent.get("agent_status") != "working")


def coordinator_recovery(agent):
    tokens = agent.get("tokens") or {}
    marker = tokens.get(COORDINATOR_RECOVERY_TOKEN)
    if not isinstance(marker, str) or not marker.strip():
        return None
    reason = tokens.get(COORDINATOR_RECOVERY_REASON_TOKEN)
    if not isinstance(reason, str):
        reason = marker
    reason = " ".join(reason.split())
    return reason[:METADATA_TOKEN_CHAR_LIMIT] or None


def coordinator_recovery_is_visible(agent):
    return (coordinator_recovery(agent) is not None
            and agent.get("agent_status") not in ("working", "blocked"))


def needs_reconcile_event(message):
    """Select metadata-only presentation edges omitted from plugin hooks."""
    if not isinstance(message, dict):
        return False
    data = message.get("data")
    if not isinstance(data, dict):
        return False
    kind = data.get("type") or message.get("event")
    if not isinstance(kind, str) or kind.replace(".", "_") != "pane_updated":
        return False
    pane = data.get("pane")
    if not isinstance(pane, dict):
        return False
    tokens = pane.get("tokens") or {}
    if not isinstance(tokens, dict):
        return False
    labels = pane.get("state_labels") or {}
    if not isinstance(labels, dict):
        labels = {}
    if labels.get("idle") == LEGACY_PENDING_LABEL:
        return True
    if coordinator_recovery_is_visible(pane):
        return (tokens.get(TOKEN) != RANK_RECOVERY_REQUIRED
                or any(labels.get(status) != COORDINATOR_RECOVERY_LABEL
                       for status in SUBAGENT_STATES))
    if any(label == COORDINATOR_RECOVERY_LABEL for label in labels.values()):
        return True
    interview_visible = (tokens.get(TOKEN) == RANK_INTERVIEW
                         or any(label == INTERVIEW_LABEL for label in labels.values()))
    if interview_visible:
        return (tokens.get(TOKEN) != RANK_INTERVIEW
                or any(labels.get(status) != INTERVIEW_LABEL for status in SUBAGENT_STATES))
    recovery_wait = has_fetch_recovery_wait(pane)
    internet_marker = tokens.get(INTERNET_WAIT_TOKEN)
    internet_visible = (recovery_wait
                        or internet_marker in ("1", "recovery", "transcript")
                        or any(label == INTERNET_WAIT_LABEL for label in labels.values()))
    if internet_visible:
        if recovery_wait:
            expected_marker = "recovery"
        elif internet_marker in ("1", "recovery"):
            # The foreign lease disappeared. Reconcile immediately instead of
            # leaving the waiting badge and rank until the 30-second sweep.
            return True
        else:
            expected_marker = "transcript"
        return (tokens.get(TOKEN) != RANK_INTERNET_WAIT
                or internet_marker != expected_marker
                or any(labels.get(status) != INTERNET_WAIT_LABEL
                       for status in SUBAGENT_STATES))
    wait_target = parallel_wait_target(pane)
    if wait_target:
        wait_label = parallel_wait_label(wait_target)
        return (tokens.get(TOKEN) != RANK_WORKING
                or any(labels.get(status) != wait_label for status in SUBAGENT_STATES))
    if any(isinstance(label, str) and label.startswith(PARALLEL_WAIT_LABEL_PREFIX)
           for label in labels.values()):
        return True
    if coordinator_wait_is_visible(pane):
        return (tokens.get(TOKEN) != RANK_COORDINATOR_WAIT
                or any(labels.get(status) != COORDINATOR_WAIT_LABEL
                       for status in SUBAGENT_STATES))
    if any(label == COORDINATOR_WAIT_LABEL for label in labels.values()):
        return True
    if coordinator_accepted_completion_is_visible(pane):
        return (tokens.get(TOKEN) != RANK_ACCEPTED_COMPLETION
                or any(labels.get(status) != ACCEPTED_COMPLETION_LABEL
                       for status in SUBAGENT_STATES))
    if (coordinator_accepted_completion(pane) is not None
            and pane.get("agent_status") in ("working", "blocked")
            and (tokens.get(TOKEN) == RANK_ACCEPTED_COMPLETION
                 or any(label == ACCEPTED_COMPLETION_LABEL
                        for label in labels.values()))):
        return True
    if any(label == ACCEPTED_COMPLETION_LABEL for label in labels.values()):
        return True
    if coordinator_managed_wait_is_visible(pane):
        wait_label = coordinator_managed_wait_label(coordinator_managed_wait(pane))
        return (tokens.get(TOKEN) != RANK_COORDINATOR_MANAGED_WAIT
                or any(labels.get(status) != wait_label
                       for status in SUBAGENT_STATES))
    if any(is_coordinator_managed_wait_label(label) for label in labels.values()):
        return True
    if coordinator_user_review_is_visible(pane):
        return (tokens.get(TOKEN) != RANK_USER_REVIEW
                or any(labels.get(status) != COORDINATOR_USER_REVIEW_LABEL
                       for status in SUBAGENT_STATES))
    if (coordinator_user_review(pane) is not None
            and pane.get("agent_status") == "working"
            and tokens.get(TOKEN) == RANK_USER_REVIEW):
        return True
    if any(label == COORDINATOR_USER_REVIEW_LABEL for label in labels.values()):
        return True
    if has_active_subagents(pane):
        return (tokens.get(TOKEN) != RANK_SUBAGENT
                or any(labels.get(status) != SUBAGENT_LABEL for status in SUBAGENT_STATES))
    # The rank is our durable edge marker after pi-subagents removes summary.
    return tokens.get(TOKEN) == RANK_SUBAGENT


def watch_events(socket_path, on_reconcile, on_ready=None, on_tick=None,
                 tick_seconds=WATCH_TICK_SECONDS):
    """Subscribe until explicit stop; return true when the stream must recover."""
    watcher = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    watcher.settimeout(5)
    try:
        watcher.connect(socket_path)
        _, uid, _ = struct.unpack("3i", watcher.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if uid != os.getuid():
            raise RuntimeError("Herdr socket belongs to another user")
        request = {"id": WATCH_REQUEST_ID, "method": "events.subscribe",
                   "params": {"subscriptions": [{"type": "pane.updated"}]}}
        watcher.sendall((json.dumps(request) + "\n").encode())
        buffer = b""
        while b"\n" not in buffer:
            chunk = watcher.recv(65536)
            if not chunk:
                raise RuntimeError("Herdr subscription response missing")
            buffer += chunk
            if len(buffer) > 8 * 1024 * 1024:
                raise RuntimeError("Herdr subscription response exceeds 8 MiB")
        acknowledgement, buffer = buffer.split(b"\n", 1)
        response = json.loads(acknowledgement)
        if response.get("id") != WATCH_REQUEST_ID:
            raise RuntimeError("Unexpected Herdr subscription response id")
        if "error" in response:
            raise ApiError(response["error"].get("code", "unknown"))
        watcher.settimeout(None)
        if on_ready and on_ready() is False:
            return False
        next_tick = time.monotonic() + tick_seconds
        while True:
            now = time.monotonic()
            if now >= next_tick:
                next_tick = now + tick_seconds
                if on_tick and on_tick() is False:
                    return False
            if b"\n" not in buffer:
                timeout = max(0, next_tick - time.monotonic())
                readable, _, _ = select.select([watcher], [], [], timeout)
                if not readable:
                    continue
                chunk = watcher.recv(65536)
                if not chunk:
                    return True
                buffer += chunk
                if len(buffer) > 8 * 1024 * 1024:
                    raise RuntimeError("Herdr event exceeds 8 MiB")
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                data = message.get("data") if isinstance(message, dict) else None
                kind = ((data or {}).get("type") if isinstance(data, dict) else None) \
                    or (message.get("event") if isinstance(message, dict) else None)
                if isinstance(kind, str) and kind.replace(".", "_") == "events_lost":
                    return True
                if needs_reconcile_event(message) and on_reconcile() is False:
                    return False
                now = time.monotonic()
                if now >= next_tick:
                    next_tick = now + tick_seconds
                    if on_tick and on_tick() is False:
                        return False
    finally:
        watcher.close()


def observation_identity(agent):
    session = agent.get("agent_session") or {}
    return [agent.get("agent"), session.get("source"), session.get("agent"),
            session.get("kind"), session.get("value")]


def compatible_observation(agent, previous):
    if not previous:
        return None
    if (previous["identity"] != observation_identity(agent)
            or agent["state_change_seq"] < previous["seq"]):
        return None
    return previous


def completion_recoveries(agents, previous):
    """Inspect each unclassified idle Pi transcript once per compatible observation."""
    recoveries = {}
    for agent in agents:
        if agent.get("agent_status") != "idle":
            continue
        prior = compatible_observation(agent, previous.get(agent["terminal_id"]))
        if prior and prior.get("transcript_checked") is True:
            continue
        session = pi_session_path(agent)
        if session:
            recoveries[agent["terminal_id"]] = session_has_completed_response(session)
    return recoveries


def observe(agent, previous=None, parking=None, active_sessions=None, interview_sessions=None,
            recovered_completions=None, internet_waits=None):
    # Keep the persisted v1 `waiting` field for backward-compatible state files.
    identity = observation_identity(agent)
    status = agent["agent_status"]
    seq = agent["state_change_seq"]
    previous = compatible_observation(agent, previous)
    pending = status == "done"
    if status == "idle" and previous:
        pending = (previous["pending"] or previous["status"] in ("working", "blocked")
                   or seq > previous["seq"])
    transcript_checked = previous.get("transcript_checked", False) if previous else False
    recovered_completions = recovered_completions or {}
    if status == "idle" and agent["terminal_id"] in recovered_completions:
        pending = pending or recovered_completions[agent["terminal_id"]]
        transcript_checked = True
    # Working/blocked/unknown are not a completed run. Keep the underlying
    # pending flag while a child runs so its completion priority can return afterwards.
    session_path = pi_session_path(agent)
    internet_recovery = has_fetch_recovery_wait(agent)
    return {"identity": identity, "status": status, "seq": seq, "pending": pending,
            "subagent": has_active_subagents(agent, active_sessions),
            "parallel_wait": parallel_wait_target(agent),
            "coordinator_wait": coordinator_wait_owner(agent),
            "coordinator_accepted_completion": coordinator_accepted_completion(agent),
            "coordinator_managed_wait": coordinator_managed_wait(agent),
            "coordinator_user_review": coordinator_user_review(agent),
            "coordinator_recovery": coordinator_recovery(agent),
            "interview": session_path in (interview_sessions or ()),
            "internet_wait": (internet_recovery
                              or session_path in (internet_waits or ())),
            "internet_recovery": internet_recovery,
            "transcript_checked": transcript_checked,
            "waiting": parking is not None, "parking": parking,
            "applied": previous.get("applied") if previous else None}


def agent_display_label(workspace_label, tab_label):
    """Join the Space and tab labels without Herdr's inter-token dot."""
    parts = [value.strip() for value in (workspace_label, tab_label)
             if isinstance(value, str) and value.strip()]
    return " ".join(parts)[:DISPLAY_TOKEN_CHAR_LIMIT]


def status_display_label(label):
    """Indent a supplemental state so it reads as a child of the agent row."""
    return (STATUS_INDENT + "↳ " + label)[:DISPLAY_TOKEN_CHAR_LIMIT]


def is_reply_required_rank(value):
    """Return whether the rank belongs above direct working sessions."""
    return isinstance(value, str) and value > RANK_WORKING


def rank(record):
    # Moving a session into LATER is an explicit shelving decision. It overrides
    # every live/status projection until the Space label changes again.
    if record.get("parking") == "later":
        return RANK_LATER
    if record.get("interview"):
        return RANK_INTERVIEW
    if record.get("internet_wait"):
        return RANK_INTERNET_WAIT
    # Waiting on conflicting peer work or a durable resource-queue predecessor
    # stays below every directly working session.
    if record.get("parallel_wait"):
        return RANK_DEPENDENCY_WAIT
    if record.get("subagent"):
        return RANK_SUBAGENT
    # A concrete unanswered user gate remains authoritative until its publisher
    # clears the token, except while the session is directly working. Explicit
    # coordinator waits keep precedence during their intentional handoff overlap.
    if (record.get("coordinator_user_review")
            and record["status"] != "working"
            and not record.get("coordinator_wait")
            and not record.get("coordinator_managed_wait")):
        return RANK_USER_REVIEW
    if record["status"] == "blocked":
        return RANK_BLOCKED
    if record["status"] == "working":
        return RANK_WORKING
    # A recovery review outranks all settled coordination states. Active work
    # and blockers still win so a resumed turn is never presented as idle.
    if record.get("coordinator_recovery"):
        return RANK_RECOVERY_REQUIRED
    # Coordinator waits share the dependency-wait tier: below direct work and
    # above ordinary idle/LATER/WAIT sessions. Wait metadata still wins during
    # a publisher's brief overlap with ordinary user-review metadata.
    if record.get("coordinator_wait"):
        return RANK_COORDINATOR_WAIT
    # A coordinator waiting on managed sessions uses the same tier.
    if record.get("coordinator_managed_wait"):
        return RANK_COORDINATOR_MANAGED_WAIT
    if record.get("coordinator_accepted_completion"):
        return RANK_ACCEPTED_COMPLETION
    # Explicit coordinator waits above deliberately consumed an overlapping
    # review token. A remaining review token was already returned before the
    # generic working/blocked lifecycle ranks.
    if record.get("parking") == "wait":
        return RANK_WAIT
    if record["pending"]:
        return RANK_PENDING
    return {"idle": RANK_IDLE}.get(record["status"], RANK_UNKNOWN)


def presentation_label(record):
    """Resolve the one supplemental label Priority+ owns for this record."""
    # Shelved sessions are intentionally quiet in the Agents view. Keep the
    # underlying metadata so its status returns when the Space is unparked.
    if record.get("parking") == "later":
        return False
    if record.get("interview"):
        return INTERVIEW_LABEL
    if record.get("internet_wait"):
        return INTERNET_WAIT_LABEL
    if record.get("parallel_wait"):
        return parallel_wait_label(record["parallel_wait"])
    if (record.get("coordinator_recovery")
            and record["status"] not in ("working", "blocked")):
        return COORDINATOR_RECOVERY_LABEL
    if (record.get("coordinator_wait")
            and record["status"] not in ("working", "blocked")):
        return COORDINATOR_WAIT_LABEL
    if (record.get("coordinator_managed_wait")
            and record["status"] not in ("working", "blocked")):
        return coordinator_managed_wait_label(record["coordinator_managed_wait"])
    if record.get("subagent"):
        return SUBAGENT_LABEL
    if (record.get("coordinator_user_review")
            and record["status"] != "working"
            and not record.get("coordinator_wait")
            and not record.get("coordinator_managed_wait")):
        return COORDINATOR_USER_REVIEW_LABEL
    if (record.get("coordinator_accepted_completion")
            and record["status"] not in ("working", "blocked")):
        return ACCEPTED_COMPLETION_LABEL
    return False


def metadata(client, pane_id, **fields):
    try:
        client.call("pane.report_metadata", {"pane_id": pane_id, "source": SOURCE, **fields})
        return True
    except ApiError as error:
        if error.code == "pane_not_found":  # Pane closed after the snapshot.
            return False
        raise


def clear_pane(client, pane):
    fields = {"clear_state_labels": True}
    tokens = pane.get("tokens", {})
    owned = {token: None for token in OWNED_TOKENS if token in tokens}
    if owned:
        fields["tokens"] = owned
    metadata(client, pane["pane_id"], **fields)


def reconcile(client, state, path):
    agents = client.call("agent.list")["agents"]
    workspaces = client.call("workspace.list")["workspaces"]
    tabs = client.call("tab.list")["tabs"]
    workspace_by_id = {workspace["workspace_id"]: workspace for workspace in workspaces}
    parking_by_workspace = {
        workspace_id: workspace_parking_kind(workspace.get("label"))
        for workspace_id, workspace in workspace_by_id.items()
        if is_parked_workspace(workspace.get("label"))
    }
    workspace_order = {
        workspace["workspace_id"]: position
        for position, workspace in enumerate(workspaces)
    }
    tab_label_by_id = {tab["tab_id"]: tab.get("label") for tab in tabs}
    previous = state["agents"]
    active_sessions = active_subagent_sessions()
    interview_sessions = pending_interview_sessions(agents)
    internet_waits = internet_wait_sessions(agents)
    recovered_completions = completion_recoveries(agents, previous)
    records = {
        agent["terminal_id"]: observe(
            agent, previous.get(agent["terminal_id"]),
            parking_by_workspace.get(agent.get("workspace_id")), active_sessions,
            interview_sessions, recovered_completions, internet_waits)
        for agent in agents
    }
    # Save observations BEFORE side effects, so a killed hook cannot lose a
    # working -> idle transition. 'applied' is acknowledged only after success.
    state["agents"] = {**previous, **records}
    save_state(path, state)
    removed = previous.keys() - records.keys()
    if removed:
        for pane in client.call("pane.list")["panes"]:
            if pane["terminal_id"] in removed:
                clear_pane(client, pane)
    state["agents"] = records
    for agent in agents:
        record = records[agent["terminal_id"]]
        desired = rank(record)
        fields = {}
        current_tokens = agent.get("tokens", {})
        session_label = tab_label_by_id.get(agent.get("tab_id"))
        workspace_label = (workspace_by_id.get(agent.get("workspace_id")) or {}).get("label")
        display_label = agent_display_label(workspace_label, session_label)
        label_state = presentation_label(record)
        reply_required = is_reply_required_rank(desired)
        desired_tokens = {
            TOKEN: desired,
            WORKSPACE_TOKEN: None,
            SESSION_TOKEN: None,
            AGENT_DISPLAY_TOKEN: display_label if display_label and not reply_required else None,
            REPLY_DISPLAY_TOKEN: display_label if display_label and reply_required else None,
            STATUS_DISPLAY_TOKEN: (status_display_label(label_state)
                                   if label_state else None),
            INTERNET_WAIT_TOKEN: ("recovery" if record["internet_recovery"]
                                  else "transcript" if record["internet_wait"] else None),
            LEGACY_CONTEXT_SUPPRESSION_TOKEN: None,
        }
        position = workspace_order.get(agent.get("workspace_id"))
        if position is not None:
            desired_tokens[WORKSPACE_TOKEN] = workspace_order_key(position)
        if isinstance(session_label, str):
            desired_tokens[SESSION_TOKEN] = metadata_sort_key(session_label)
        token_updates = {
            token: value
            for token, value in desired_tokens.items()
            if (value is not None and current_tokens.get(token) != value)
            or (value is None and token in current_tokens)
        }
        if token_updates:
            fields["tokens"] = token_updates
        # Cache emitted text so dynamic interview/waiting/subagent labels stay idempotent.
        labels = agent.get("state_labels") or {}
        if label_state:
            # pi-subagents and coordinators may refresh their own labels. The
            # pane.updated watcher lets the stable Priority+ presentation win again.
            if (record["applied"] != label_state
                    or any(labels.get(status) != label_state for status in SUBAGENT_STATES)):
                fields["state_labels"] = {status: label_state for status in SUBAGENT_STATES}
        else:
            stale_badge = (labels.get("idle") == LEGACY_PENDING_LABEL
                           or any(label in (INTERVIEW_LABEL, INTERNET_WAIT_LABEL,
                                            COORDINATOR_WAIT_LABEL,
                                            COORDINATOR_MANAGED_WAIT_LABEL,
                                            COORDINATOR_USER_REVIEW_LABEL)
                                  for label in labels.values())
                           or any(isinstance(label, str)
                                  and (label.startswith(PARALLEL_WAIT_LABEL_PREFIX)
                                       or is_coordinator_managed_wait_label(label))
                                  for label in labels.values()))
            if record["applied"] != label_state or stale_badge:
                fields["clear_state_labels"] = True
        if fields:
            if not metadata(client, agent["pane_id"], **fields):
                continue
        record["applied"] = label_state
    save_state(path, state)


def watcher_lock_path(directory, socket_path):
    return instance_folder(directory, socket_path) / "watcher.lock"


def read_watcher_registration(directory, socket_path):
    path = watcher_lock_path(directory, socket_path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None, None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None, None
        except BlockingIOError:
            os.lseek(fd, 0, os.SEEK_SET)
            fields = os.read(fd, 128).decode().strip().split(maxsplit=1)
            try:
                return int(fields[0]), fields[1] if len(fields) == 2 else None
            except (IndexError, ValueError):
                return None, None
    finally:
        os.close(fd)


def read_watcher_pid(directory, socket_path):
    return read_watcher_registration(directory, socket_path)[0]


def ensure_watcher(directory, socket_path):
    # Serialize the cleaned-state check and process registration with cleanup.
    with state_lock(directory, socket_path) as state_path:
        if state_path.exists() and json.loads(state_path.read_text()).get("cleaned") is True:
            return
        watcher_pid, watcher_version = read_watcher_registration(directory, socket_path)
        if watcher_pid is not None and watcher_version == WATCHER_VERSION:
            return
        if watcher_pid is not None:
            stop_watcher(directory, socket_path)
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "watch"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            watcher_pid, watcher_version = read_watcher_registration(directory, socket_path)
            if watcher_pid is not None and watcher_version == WATCHER_VERSION:
                return
            if process.poll() is not None:
                raise RuntimeError("Priority+ watcher exited during startup")
            time.sleep(0.025)
        raise RuntimeError("Priority+ watcher did not acquire its lock")


def stop_watcher(directory, socket_path):
    pid = read_watcher_pid(directory, socket_path)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and read_watcher_pid(directory, socket_path) is not None:
        time.sleep(0.05)


def run_watcher(directory, socket_path):
    path = watcher_lock_path(directory, socket_path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {WATCHER_VERSION}".encode())
        os.fsync(fd)
        reconcile_now = lambda: run("sync", directory, socket_path)
        reconnect_now = lambda: run("startup", directory, socket_path)
        subagent_projection = active_subagent_sessions()
        interview_cache = {}
        interview_projection = tracked_pending_interview_sessions(
            directory, socket_path, interview_cache)
        internet_cache = {}
        internet_projection = tracked_internet_wait_sessions(
            directory, socket_path, internet_cache)
        recovery_projection = tracked_fetch_recovery_waits(socket_path)
        last_full_reconcile = time.monotonic()

        def tick():
            nonlocal subagent_projection, interview_projection, internet_projection
            nonlocal recovery_projection, last_full_reconcile
            current_subagents = active_subagent_sessions()
            current_interviews = tracked_pending_interview_sessions(
                directory, socket_path, interview_cache)
            current_internet_waits = tracked_internet_wait_sessions(
                directory, socket_path, internet_cache)
            current_recovery_waits = tracked_fetch_recovery_waits(socket_path)
            recovery_changed = (current_recovery_waits is not None
                                and current_recovery_waits != recovery_projection)
            now = time.monotonic()
            if (current_subagents != subagent_projection
                    or current_interviews != interview_projection
                    or current_internet_waits != internet_projection
                    or recovery_changed
                    or now - last_full_reconcile >= WATCH_FULL_RECONCILE_SECONDS):
                subagent_projection = current_subagents
                interview_projection = current_interviews
                internet_projection = current_internet_waits
                if current_recovery_waits is not None:
                    recovery_projection = current_recovery_waits
                last_full_reconcile = now
                return reconcile_now()
            return True

        while watch_events(socket_path, reconcile_now, on_ready=reconnect_now, on_tick=tick):
            time.sleep(0.05)
    finally:
        os.close(fd)


def run(action, directory, socket_path):
    with state_lock(directory, socket_path) as path:
        client = Client(socket_path)
        try:
            state = load_state(path, client.boot)
            if action == "cleanup":
                # Persist quiescence first; queued hooks must not re-add metadata.
                state["enabled"] = False
                state["cleaned"] = True
                save_state(path, state)
                client.call("agent.view.clear", {"source": SOURCE})
                for pane in client.call("pane.list")["panes"]:
                    clear_pane(client, pane)
                state["agents"] = {}
                save_state(path, state)
                return False
            if action in ("enable", "disable", "toggle"):
                state["enabled"] = (not state["enabled"] if action == "toggle"
                                    else action == "enable")
                if state["enabled"]:
                    state["cleaned"] = False
                save_state(path, state)
            if not state["cleaned"]:
                reconcile(client, state, path)
            # Event hooks never reclaim a view another plugin installed.
            if action != "sync":
                if state["enabled"]:
                    client.call("agent.view.set", VIEW)
                else:
                    client.call("agent.view.clear", {"source": SOURCE})
            return not state["cleaned"]
        finally:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sync", "startup", "enable", "disable", "toggle", "cleanup", "watch"))
    args = parser.parse_args()
    try:
        # No fallback to a default/live socket or a guessed plugin state dir.
        directory = os.environ["HERDR_PLUGIN_STATE_DIR"]
        socket_path = os.environ["HERDR_SOCKET_PATH"]
        if args.action == "watch":
            run_watcher(directory, socket_path)
            return 0
        active = run(args.action, directory, socket_path)
        if args.action == "cleanup":
            stop_watcher(directory, socket_path)
        elif active:
            # Startup and low-volume hooks are watchdogs for the event subscriber.
            ensure_watcher(directory, socket_path)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(f"Priority+: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
