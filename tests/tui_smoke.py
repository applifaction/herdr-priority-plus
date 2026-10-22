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
rows = [
  ["state_icon", { token = "$pp_reply_display", fg = "#000000", bold = true }, "$pp_agent_display"],
  [{ token = "$pp_status_display", rules = [
    { contains = "Waiting for User Review", fg = "#000000", bold = true },
    { contains = "Accepted / pending completion", fg = "#000000", bold = true },
    { contains = "Interview Pending", fg = "#cb4b16", bold = true },
    { contains = "", fg = "#268bd2", bold = false },
  ] }],
]
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

    def rendered_cells(needle):
        for row, line in enumerate(screen.display):
            if needle in line:
                start = line.index(needle)
                return [screen.buffer[row][column] for column in range(start, start + len(needle))]
        raise AssertionError(f'missing rendered text: {needle}')

    def fixture_order(lines):
        markers = [
            ('BLOCKED', 'BLOCKED'), ('UNREAD', 'UNREAD'), ('REVIEWED', 'REVIEWED'),
            ('RUNNING', 'RUNNING'), ('SUBAGENT', 'SUBAGENT'), ('IDLE', 'IDLE'),
            ('LATER_ALPHA_A', 'LA_A'), ('LATER_ALPHA_Z', 'LA_Z'),
            ('DEFERRED', 'DEFERRED'), ('WAIT_ALPHA_A', 'WA_A'),
            ('WAIT_ALPHA_Z', 'WA_Z'), ('WAITING', 'WAITING'),
        ]
        return [name for line in lines for name, marker in markers if marker in line]

    def report(pane, state):
        return rpc('pane.report_agent', {'pane_id': pane, 'source': 'test:priority-plus', 'agent': 'pi', 'state': state})

    def action(name):
        deadline = time.monotonic() + 15
        log_id = None
        while True:
            done = subprocess.run([herdr, 'plugin', 'action', 'invoke', 'local.priority-plus.'+name], env=env, capture_output=True, text=True, timeout=8)
            if not done.returncode:
                try:
                    log_id = json.loads(done.stdout)['result']['log']['log_id']
                except (KeyError, TypeError, ValueError) as error:
                    raise RuntimeError(f'cannot read accepted {name} log id: {done.stdout}') from error
                break
            failure = done.stdout + done.stderr
            if 'plugin_command_limit_reached' not in failure or time.monotonic() >= deadline:
                raise RuntimeError(failure)
            pump(.1)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            pump(.1)
            entry = next((entry for entry in rpc('plugin.log.list', {'plugin_id': 'local.priority-plus'})['logs']
                          if entry['log_id'] == log_id), None)
            if entry and entry['status'] == 'succeeded':
                return
            if entry and entry['status'] == 'failed':
                raise RuntimeError(entry.get('stderr') or f'{name} failed')
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
            for name in ['BLOCKED', 'REVIEWED', 'RUNNING', 'SUBAGENT', 'IDLE']:
                created = rpc('tab.create', {'workspace_id': ws, 'label': name, 'cwd': temp, 'focus': False})
                panes[name] = created['root_pane']['pane_id']
                report(panes[name], 'idle')
            unread_created = rpc('workspace.create', {
                'cwd': temp, 'label': 'action-lower', 'focus': False,
            })
            panes['UNREAD'] = unread_created['root_pane']['pane_id']
            rpc('tab.rename', {'tab_id': unread_created['root_pane']['tab_id'], 'label': 'UNREAD'})
            report(panes['UNREAD'], 'idle')
            wait_created = rpc('workspace.create', {'cwd': temp, 'label': 'WAIT/FIXTURE', 'focus': False})
            panes['WAITING'] = wait_created['root_pane']['pane_id']
            rpc('tab.rename', {'tab_id': wait_created['root_pane']['tab_id'], 'label': 'WAITING'})
            report(panes['WAITING'], 'idle')
            later_created = rpc('workspace.create', {'cwd': temp, 'label': 'LATER/FIXTURE', 'focus': False})
            panes['DEFERRED'] = later_created['root_pane']['pane_id']
            rpc('tab.rename', {'tab_id': later_created['root_pane']['tab_id'], 'label': 'DEFERRED'})
            report(panes['DEFERRED'], 'idle')
            later_alpha = rpc('workspace.create', {'cwd': temp, 'label': 'LATER/ALPHA', 'focus': False})
            panes['LATER_ALPHA_Z'] = later_alpha['root_pane']['pane_id']
            rpc('tab.rename', {'tab_id': later_alpha['root_pane']['tab_id'], 'label': 'LA_Z'})
            later_alpha_a = rpc('tab.create', {
                'workspace_id': later_alpha['workspace']['workspace_id'], 'label': 'LA_A',
                'cwd': temp, 'focus': False,
            })
            panes['LATER_ALPHA_A'] = later_alpha_a['root_pane']['pane_id']
            report(panes['LATER_ALPHA_Z'], 'idle')
            report(panes['LATER_ALPHA_A'], 'idle')
            wait_alpha = rpc('workspace.create', {'cwd': temp, 'label': 'WAIT/ALPHA', 'focus': False})
            panes['WAIT_ALPHA_Z'] = wait_alpha['root_pane']['pane_id']
            rpc('tab.rename', {'tab_id': wait_alpha['root_pane']['tab_id'], 'label': 'WA_Z'})
            wait_alpha_a = rpc('tab.create', {
                'workspace_id': wait_alpha['workspace']['workspace_id'], 'label': 'WA_A',
                'cwd': temp, 'focus': False,
            })
            panes['WAIT_ALPHA_A'] = wait_alpha_a['root_pane']['pane_id']
            report(panes['WAIT_ALPHA_Z'], 'idle')
            report(panes['WAIT_ALPHA_A'], 'idle')
            rpc('workspace.create', {'cwd': temp, 'label': 'normal-after-parked', 'focus': False})
            client = subprocess.Popen([herdr], env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            pump(1)
            os.write(master, b'\x1b[I')  # report focused OUTER test terminal only
            pump(.2)
            if args.plugin:
                action('enable')
            else:
                rpc('agent.view.set', {'source': 'test:priority-plus', 'label': 'Priority+', 'sort': [
                    {'field': {'token': 'pp_rank'}, 'order': 'desc'},
                    {'field': 'workspace_order', 'order': 'asc'},
                    {'field': {'token': 'pp_session_sort'}, 'order': 'asc'},
                    {'field': 'seen', 'order': 'asc'},
                    {'field': 'state_change_seq', 'order': 'desc'},
                ]})
            rpc('workspace.move', {
                'workspace_id': wait_alpha['workspace']['workspace_id'], 'insert_index': 0,
            })
            pump(.5)
            workspace_labels = [workspace['label'] for workspace in rpc('workspace.list')['workspaces']]
            assert workspace_labels == [
                'WAIT/ALPHA', 'fixture', 'action-lower', 'WAIT/FIXTURE',
                'LATER/FIXTURE', 'LATER/ALPHA', 'normal-after-parked',
            ], workspace_labels
            for name in [
                'BLOCKED', 'UNREAD', 'REVIEWED', 'RUNNING', 'SUBAGENT', 'DEFERRED', 'WAITING',
                'LATER_ALPHA_A', 'LATER_ALPHA_Z', 'WAIT_ALPHA_A', 'WAIT_ALPHA_Z',
            ]:
                report(panes[name], 'working')
            pump(.8)
            report(panes['BLOCKED'], 'blocked')
            report(panes['REVIEWED'], 'idle')
            report(panes['SUBAGENT'], 'idle')
            report(panes['WAITING'], 'idle')
            report(panes['DEFERRED'], 'idle')
            for name in ('LATER_ALPHA_A', 'LATER_ALPHA_Z', 'WAIT_ALPHA_A', 'WAIT_ALPHA_Z'):
                report(panes[name], 'idle')
            rpc('pane.focus', {'pane_id': panes['WAITING']})
            pump(.25)
            rpc('pane.focus', {'pane_id': panes['DEFERRED']})
            pump(.25)
            report(panes['UNREAD'], 'idle')
            summary = '⏳ 1 subagent (worker)'
            if args.plugin:
                rpc('pane.report_metadata', {'pane_id': panes['SUBAGENT'], 'source': 'pi-subagents:herdr',
                    'tokens': {'summary': summary},
                    'state_labels': {state: summary for state in ['idle', 'done', 'working']}})
                # Collapse the preceding lifecycle-event burst into one acknowledged
                # full reconciliation before asserting the visual order.
                action('enable')
                pump(.3)
            else:
                ranks = {
                    'BLOCKED': '7', 'UNREAD': '6', 'REVIEWED': '6', 'RUNNING': '5',
                    'SUBAGENT': '4', 'IDLE': '3', 'LATER_ALPHA_A': '2',
                    'LATER_ALPHA_Z': '2', 'DEFERRED': '2', 'WAIT_ALPHA_A': '1',
                    'WAIT_ALPHA_Z': '1', 'WAITING': '1',
                }
                workspace_positions = {
                    'UNREAD': 2,
                    'LATER_ALPHA_A': 5,
                    'LATER_ALPHA_Z': 5,
                    'DEFERRED': 4,
                    'WAIT_ALPHA_A': 0,
                    'WAIT_ALPHA_Z': 0,
                    'WAITING': 3,
                }
                for name, rank in ranks.items():
                    position = workspace_positions.get(name, 1)
                    workspace_name = {
                        'UNREAD': 'action-lower',
                        'DEFERRED': 'LATER/FIXTURE',
                        'LATER_ALPHA_A': 'LATER/ALPHA',
                        'LATER_ALPHA_Z': 'LATER/ALPHA',
                        'WAIT_ALPHA_A': 'WAIT/ALPHA',
                        'WAIT_ALPHA_Z': 'WAIT/ALPHA',
                        'WAITING': 'WAIT/FIXTURE',
                    }.get(name, 'fixture')
                    tab_name = {
                        'LATER_ALPHA_A': 'LA_A', 'LATER_ALPHA_Z': 'LA_Z',
                        'WAIT_ALPHA_A': 'WA_A', 'WAIT_ALPHA_Z': 'WA_Z',
                    }.get(name, name)
                    display = f'{workspace_name} {tab_name}'
                    tokens = {
                        'pp_rank': rank,
                        'pp_workspace_sort': f'{position:020d}',
                        'pp_session_sort': name.casefold(),
                        ('pp_reply_display' if rank > '5' else 'pp_agent_display'): display,
                    }
                    params = {'pane_id': panes[name], 'source': 'test:pp-presentation', 'tokens': tokens}
                    if name == 'SUBAGENT':
                        tokens['pp_status_display'] = '\u2800↳ 🤖 Subagent working'
                        params['state_labels'] = {'idle': '🤖 Subagent working'}
                    rpc('pane.report_metadata', params)
            lines = capture('before_review')
            expected_prefix = ['BLOCKED', 'REVIEWED', 'UNREAD', 'RUNNING', 'SUBAGENT']
            expected_suffix = [
                'IDLE', 'DEFERRED', 'LATER_ALPHA_A', 'LATER_ALPHA_Z',
                'WAIT_ALPHA_A', 'WAIT_ALPHA_Z', 'WAITING',
            ]
            order = fixture_order(lines)
            assert order[:5] == expected_prefix, order
            assert order[5:] == expected_suffix, order
            assert not any('Awaiting reply' in line for line in lines)
            assert sum('\u2800↳ 🤖 Subagent working' in line for line in lines) == 1
            assert not any(' · ' in line and any(name in line for name in panes) for line in lines)
            reply_cells = rendered_cells('fixture BLOCKED')
            assert all(cell.bold for cell in reply_cells), 'Reply-required name must be bold'
            assert all(cell.fg == '000000' for cell in reply_cells), 'Reply-required name must be black'
            wait_cells = rendered_cells('\u2800↳ 🤖 Subagent working')
            assert wait_cells[0].data == '\u2800', 'Status marker must have a blank leading cell'
            assert not any(cell.bold for cell in wait_cells), 'Operational wait must not be bold'
            assert all(cell.fg == '268bd2' for cell in wait_cells), 'Operational wait must be blue'
            for parked_name, marker in (
                ('LATER_ALPHA_A', 'LA_A'), ('LATER_ALPHA_Z', 'LA_Z'),
                ('DEFERRED', 'DEFERRED'), ('WAIT_ALPHA_A', 'WA_A'),
                ('WAIT_ALPHA_Z', 'WA_Z'), ('WAITING', 'WAITING'),
            ):
                parked_row = next(i for i, line in enumerate(lines) if marker in line)
                assert parked_row + 1 >= len(lines) or 'Awaiting reply' not in lines[parked_row + 1]
            row = next(i+1 for i, line in enumerate(lines) if 'REVIEWED' in line)
            col = lines[row-1].index('REVIEWED')+2
            os.write(master, f'\x1b[<0;{col};{row}M\x1b[<0;{col};{row}m'.encode())
            lines = capture('after_review_click')
            order = fixture_order(lines)
            assert order[:5] == expected_prefix, order
            assert order[5:] == expected_suffix, order
            assert not any('Awaiting reply' in line for line in lines)
            rpc('pane.send_text', {'pane_id': panes['REVIEWED'], 'text': 'draft-not-submitted'})
            lines = capture('after_typing_only')
            assert not any('Awaiting reply' in line for line in lines)
            report(panes['REVIEWED'], 'working')
            if args.plugin:
                action('refresh')
            if not args.plugin:
                rpc('pane.report_metadata', {'pane_id': panes['REVIEWED'], 'source': 'test:pp-presentation', 'clear_state_labels': True, 'tokens': {'pp_rank': '5', 'pp_reply_display': None, 'pp_agent_display': 'fixture REVIEWED'}})
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
            assert b'38;2;0;0;0' in raw, 'Reply-required names must render in configured black'
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
