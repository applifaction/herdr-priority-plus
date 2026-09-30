#!/usr/bin/env python3
"""One-shot Priority+ reconciliation. Linux, Python standard library only."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import time

PLUGIN = "local.priority-plus"
SOURCE = "plugin:" + PLUGIN
TOKEN = "pp_rank"
LABEL = "◉ Awaiting reply"
VIEW = {
    "source": SOURCE,
    "label": "Priority+",
    "sort": [
        {"field": {"token": TOKEN}, "order": "desc"},
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


@contextmanager
def state_lock(directory, socket_path):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(os.path.realpath(socket_path).encode()).hexdigest()
    folder = directory / key
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
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
                    or not isinstance(record.get("status"), str)):
                raise RuntimeError("Invalid Priority+ agent record")
        if state.get("boot") != boot:
            state["agents"] = {}  # Never compare lifecycle counters across boots.
            state["boot"] = boot
        return state
    return {"version": 1, "boot": boot, "enabled": True, "cleaned": False, "agents": {}}


def observe(agent, previous=None):
    session = agent.get("agent_session") or {}
    identity = [agent.get("agent"), session.get("source"), session.get("agent"),
                session.get("kind"), session.get("value")]
    status = agent["agent_status"]
    seq = agent["state_change_seq"]
    if previous and (previous["identity"] != identity or seq < previous["seq"]):
        previous = None
    pending = status == "done"
    if status == "idle" and previous:
        pending = (previous["pending"] or previous["status"] in ("working", "blocked")
                   or seq > previous["seq"])
    # Working/blocked/unknown are not a completed run. Blocked ranks first.
    return {"identity": identity, "status": status, "seq": seq, "pending": pending,
            "applied": previous.get("applied") if previous else None}


def rank(record):
    if record["status"] == "blocked":
        return "4"
    if record["pending"]:
        return "3"
    return {"working": "2", "idle": "1"}.get(record["status"], "0")


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
    if TOKEN in pane.get("tokens", {}):
        fields["tokens"] = {TOKEN: None}
    metadata(client, pane["pane_id"], **fields)


def reconcile(client, state, path):
    agents = client.call("agent.list")["agents"]
    previous = state["agents"]
    records = {a["terminal_id"]: observe(a, previous.get(a["terminal_id"])) for a in agents}
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
        if agent.get("tokens", {}).get(TOKEN) != desired:
            fields["tokens"] = {TOKEN: desired}
        # Cache the emitted text, not just a boolean: upgrades can refresh
        # an existing badge without resetting its pending completion.
        label_state = LABEL if record["pending"] else False
        if record["applied"] != label_state:
            if record["pending"]:
                fields["state_labels"] = {"idle": LABEL}
            else:
                fields["clear_state_labels"] = True
        if fields:
            if not metadata(client, agent["pane_id"], **fields):
                continue
        record["applied"] = label_state
    save_state(path, state)


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
                return
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
        finally:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sync", "startup", "enable", "disable", "toggle", "cleanup"))
    args = parser.parse_args()
    try:
        # No fallback to a default/live socket or a guessed plugin state dir.
        run(args.action, os.environ["HERDR_PLUGIN_STATE_DIR"], os.environ["HERDR_SOCKET_PATH"])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(f"Priority+: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
