"""Isolated actual Herdr TUI proof; never connects to the user's sockets."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import socket
import struct
import subprocess
import tempfile
import termios
import time

import pyte

parser = argparse.ArgumentParser()
parser.add_argument('--plugin')
parser.add_argument('--output', default='/tmp/herdr-priority-plus-tui-evidence.json')
args = parser.parse_args()
herdr = shutil.which('herdr')
assert herdr
results = {}
with tempfile.TemporaryDirectory(prefix='hpp-tui-') as temp:
    root = Path(temp)
    env = {k: v for k, v in os.environ.items() if not k.startswith('HERDR_')}
    env.update(HOME=temp, XDG_CONFIG_HOME=temp+'/config', XDG_STATE_HOME=temp+'/state',
               XDG_DATA_HOME=temp+'/data', XDG_CACHE_HOME=temp+'/cache', XDG_RUNTIME_DIR=temp+'/runtime',
               HERDR_SOCKET_PATH=temp+'/api.sock', HERDR_CLIENT_SOCKET_PATH=temp+'/client.sock',
               HERDR_CONFIG_PATH=temp+'/config.toml', SHELL='/bin/sh', TERM='xterm-256color',
               LANG='C.UTF-8')
    (root/'runtime').mkdir(mode=0o700)
    (root/'config.toml').write_text('''onboarding = false
[update]
version_check = false
manifest_check = false
[ui]
agent_panel_sort = "priority"
status_indicators = "symbols"
[ui.sound]
enabled = false
[ui.sidebar.agents]
rows = [["state_icon", "tab"], [{ token = "state_text", rules = [{ equals = "⏳ Subagent working", fg = "#268bd2", bold = true }, { equals = "◉ Awaiting reply", fg = "#b58900", bold = true }, { contains = "", hide = true }] }]]
''')

    def rpc(method, params=None):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(4)
            sock.connect(env['HERDR_SOCKET_PATH'])
            sock.sendall((json.dumps({'id': 'tui-check', 'method': method, 'params': params or {}})+'\n').encode())
            with sock.makefile('rb') as stream:
                response = json.loads(stream.readline())
            if 'error' in response:
                raise RuntimeError(response['error'])
            return response['result']

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 48, 140, 0, 0))
    class TestScreen(pyte.Screen):
        def report_device_status(self, mode, **kwargs):
            return super().report_device_status(mode)

        def write_process_input(self, data):
            os.write(master, data.encode())

    screen = TestScreen(140, 48)
    stream = pyte.ByteStream(screen)
    server = client = None
    raw = bytearray()

    def pump(seconds=.5):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            readable, _, _ = select.select([master], [], [], min(.05, max(0, until-time.monotonic())))
            if readable:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                raw.extend(data)
                stream.feed(data)
        return screen.display

    def stop_owned(process):
        if process is None:
            return
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def capture(label):
        pump(.6)
        # Store just the fixture sidebar, never user data (this server is isolated).
        lines = [line.split('│', 1)[0].rstrip() for line in screen.display]
        results[label] = lines
        print(label + '\n' + '\n'.join(line for line in lines if line.strip()))
        return lines

    def fixture_order(lines):
        names = ['BLOCKED', 'UNREAD', 'REVIEWED', 'RUNNING', 'SUBAGENT', 'IDLE', 'WAITING']
        return [name for line in lines for name in names if name in line]

    def report(pane, state):
        return rpc('pane.report_agent', {'pane_id': pane, 'source': 'test:priority-plus', 'agent': 'pi', 'state': state})

    def action(name):
        before = {entry['log_id'] for entry in rpc('plugin.log.list', {'plugin_id': 'local.priority-plus'})['logs']}
        done = subprocess.run([herdr, 'plugin', 'action', 'invoke', 'local.priority-plus.'+name], env=env, capture_output=True, text=True, timeout=8)
        if done.returncode:
            raise RuntimeError(done.stderr)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            pump(.1)
            fresh = [entry for entry in rpc('plugin.log.list', {'plugin_id': 'local.priority-plus'})['logs']
                     if entry['log_id'] not in before and entry.get('action_id') == name]
            if any(entry['status'] == 'succeeded' for entry in fresh):
                return
            failed = next((entry for entry in fresh if entry['status'] == 'failed'), None)
            if failed:
                raise RuntimeError(failed.get('stderr') or f'{name} failed')
        raise RuntimeError(f'timed out waiting for {name}')

    try:
        with (root/'server.log').open('wb') as log:
            server = subprocess.Popen([herdr, 'server'], env=env, stdout=log, stderr=log)
            deadline = time.monotonic()+5
            while not (root/'api.sock').exists():
                if server.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError((root/'server.log').read_text()[-3000:])
                time.sleep(.05)
            if args.plugin:
                linked = subprocess.run([herdr, 'plugin', 'link', str(Path(args.plugin).resolve())], env=env, capture_output=True, text=True, timeout=8)
                if linked.returncode:
                    raise RuntimeError(linked.stderr)
            first = rpc('workspace.create', {'cwd': temp, 'label': 'fixture', 'focus': True})
            ws = first['workspace']['workspace_id']
            panes = {}
            for name in ['BLOCKED', 'UNREAD', 'REVIEWED', 'RUNNING', 'SUBAGENT', 'IDLE']:
                created = rpc('tab.create', {'workspace_id': ws, 'label': name, 'cwd': temp, 'focus': False})
                panes[name] = created['root_pane']['pane_id']
                report(panes[name], 'idle')
            wait_created = rpc('workspace.create', {'cwd': temp, 'label': 'WAIT/FIXTURE', 'focus': False})
            panes['WAITING'] = wait_created['root_pane']['pane_id']
            rpc('tab.rename', {'tab_id': wait_created['root_pane']['tab_id'], 'label': 'WAITING'})
            report(panes['WAITING'], 'idle')
            client = subprocess.Popen([herdr], env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            pump(1)
            os.write(master, b'\x1b[I')  # report focused OUTER test terminal only
            pump(.2)
            if args.plugin:
                action('enable')
            else:
                rpc('agent.view.set', {'source': 'test:priority-plus', 'label': 'Priority+', 'sort': [{'field': {'token': 'pp_rank'}, 'order': 'desc'}, {'field': 'seen', 'order': 'asc'}, {'field': 'state_change_seq', 'order': 'desc'}]})
            for name in ['BLOCKED', 'UNREAD', 'REVIEWED', 'RUNNING', 'SUBAGENT', 'WAITING']:
                report(panes[name], 'working')
            pump(.8)
            report(panes['BLOCKED'], 'blocked')
            report(panes['REVIEWED'], 'idle')
            report(panes['SUBAGENT'], 'idle')
            report(panes['WAITING'], 'idle')
            rpc('pane.focus', {'pane_id': panes['WAITING']})
            pump(.25)
            report(panes['UNREAD'], 'idle')
            summary = '⏳ 1 subagent (worker)'
            if args.plugin:
                rpc('pane.report_metadata', {'pane_id': panes['SUBAGENT'], 'source': 'pi-subagents:herdr',
                    'tokens': {'summary': summary},
                    'state_labels': {state: summary for state in ['idle', 'done', 'working']}})
                pump(1)
            else:
                for name, rank in [('BLOCKED', '5'), ('UNREAD', '4'), ('REVIEWED', '4'),
                                   ('RUNNING', '3'), ('SUBAGENT', '2'), ('IDLE', '1'), ('WAITING', '1')]:
                    params = {'pane_id': panes[name], 'source': 'test:pp-presentation', 'tokens': {'pp_rank': rank}}
                    if name in ['UNREAD', 'REVIEWED']:
                        params['state_labels'] = {'idle': '◉ Awaiting reply'}
                    elif name == 'SUBAGENT':
                        params['state_labels'] = {'idle': '⏳ Subagent working'}
                    rpc('pane.report_metadata', params)
            lines = capture('before_review')
            expected_prefix = ['BLOCKED', 'UNREAD', 'REVIEWED', 'RUNNING', 'SUBAGENT']
            order = fixture_order(lines)
            assert order[:5] == expected_prefix, order
            assert set(order[5:]) == {'IDLE', 'WAITING'}, order
            assert not any('Awaiting reply' in line for line in lines), 'Badge must stay hidden for unseen completions'
            assert sum('⏳ Subagent working' in line for line in lines) == 1
            waiting_row = next(i for i, line in enumerate(lines) if 'WAITING' in line)
            assert waiting_row + 1 >= len(lines) or 'Awaiting reply' not in lines[waiting_row + 1]
            row = next(i+1 for i, line in enumerate(lines) if 'REVIEWED' in line)
            col = lines[row-1].index('REVIEWED')+2
            os.write(master, f'\x1b[<0;{col};{row}M\x1b[<0;{col};{row}m'.encode())
            lines = capture('after_review_click')
            order = fixture_order(lines)
            assert order[:5] == expected_prefix, order
            assert set(order[5:]) == {'IDLE', 'WAITING'}, order
            assert sum('◉ Awaiting reply' in line for line in lines) == 1, 'Exactly the reviewed completion needs a badge'
            rpc('pane.send_text', {'pane_id': panes['REVIEWED'], 'text': 'draft-not-submitted'})
            lines = capture('after_typing_only')
            assert sum('◉ Awaiting reply' in line for line in lines) == 1
            report(panes['REVIEWED'], 'working')
            if not args.plugin:
                rpc('pane.report_metadata', {'pane_id': panes['REVIEWED'], 'source': 'test:pp-presentation', 'clear_state_labels': True, 'tokens': {'pp_rank': '2'}})
            lines = capture('next_working_run')
            assert not any('Awaiting reply' in line for line in lines)
            assert fixture_order(lines)[0:2] == ['BLOCKED', 'UNREAD']
            if args.plugin:
                action('disable')
                lines = capture('priority_plus_disabled')
                assert not any('Priority+' in line for line in lines)
                action('enable')
                lines = capture('priority_plus_enabled')
                assert any('Priority+' in line for line in lines)
            assert b'38;2;38;139;210' in raw, 'Subagent status must render in configured blue'
            results['passed'] = True
    finally:
        # ONLY the server spawned with our isolated environment is stopped.
        if server is not None:
            try:
                rpc('server.stop')
            except (OSError, ValueError, RuntimeError):
                pass
        try:
            stop_owned(server)
        finally:
            try:
                stop_owned(client)
            finally:
                os.close(master); os.close(slave)
                Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2)+'\n')
                Path(args.output+'.ansi').write_bytes(raw)
print('PASS: real TUI, client-local review, order, draft, restart-of-work' + (', toggling' if args.plugin else ''))
