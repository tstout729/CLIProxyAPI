import importlib.util
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


r = load('dev_reaper', 'dev-reaper.py')
installer = load('install_dev_reaper', 'install-dev-reaper.py')
START = 'Sun Oct 11 12:00:00 2026'


def process(pid, command, parent=1, cpu=0, started=START):
    return r.Process(pid, parent, started, cpu, command)


class DetectionTests(unittest.TestCase):
    def test_next_child_requires_development_ancestor(self):
        for mode, expected in [('dev', True), ('start', False)]:
            procs = {2: process(2, f'node /project/node_modules/.bin/next {mode}'),
                     3: process(3, 'next-server (v16.0.0)', parent=2)}
            self.assertEqual(r.is_dev(3, procs), expected)
        self.assertFalse(r.is_dev(3, {3: process(3, 'next-server (v16.0.0)')}))

    def test_only_development_node_commands_match(self):
        for cmd in ['node /project/node_modules/.bin/vite --host',
                    'node /project/node_modules/vite/bin/vite.js',
                    'bun /project/node_modules/.bin/astro dev']:
            self.assertTrue(r.is_dev(2, {2: process(2, cmd)}), cmd)
        for cmd in ['node /project/node_modules/vite/bin/vite.js preview',
                    'node /project/node_modules/.bin/vite build',
                    'node /project/server.js', 'postgres -D /private/tmp/db',
                    'zsh -c node /project/node_modules/.bin/vite',
                    'node /srv/production.js --mode-description next dev',
                    'node /project/node_modules/next/dist/bin/next start --description next dev',
                    'node /project/node_modules/next/dist/cli/next-start.js']:
            self.assertFalse(r.is_dev(2, {2: process(2, cmd)}), cmd)

    def test_next_ancestor_prompt_is_not_development_mode(self):
        procs = {1: process(1, 'claude next dev'),
                 2: process(2, 'node /project/node_modules/.bin/next start', parent=1),
                 3: process(3, 'next-server (v16.0.0)', parent=2)}
        self.assertFalse(r.is_dev(3, procs))

    def test_native_claude_install_is_protected(self):
        self.assertTrue(r.is_agent(process(2, '/Users/test/.local/share/claude/versions/2.1.0 --resume')))

    def test_sockets_count_inbound_not_outbound_connections(self):
        procs = {2: process(2, 'node /project/node_modules/.bin/vite')}
        rows = [dict(pid=2, state='LISTEN', address='*:3000'),
                dict(pid=2, state='ESTABLISHED', address='127.0.0.1:3000->127.0.0.1:42000'),
                dict(pid=2, state='ESTABLISHED', address='127.0.0.1:42001->127.0.0.1:443')]
        with patch.object(r, 'processes', return_value=procs), patch.object(r, 'sockets', return_value=rows), patch.object(r, 'cwds', return_value={2: '/project'}):
            self.assertEqual(r.snapshot()[2], {2: 1})

    def test_missing_agent_cwd_aborts_snapshot(self):
        procs = {2: process(2, 'claude')}
        with patch.object(r, 'processes', return_value=procs), patch.object(r, 'sockets', return_value=[]), patch.object(r, 'cwds', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'directories'):
                r.snapshot()

    def test_exited_agent_during_cwd_lookup_does_not_abort_snapshot(self):
        with patch.object(r, 'processes', side_effect=[{2: process(2, 'claude')}, {}]), patch.object(r, 'sockets', return_value=[]), patch.object(r, 'cwds', return_value={}):
            self.assertEqual(r.snapshot()[4], set())


class IdleTests(unittest.TestCase):
    def setUp(self):
        self.procs = {2: process(2, 'node /project/node_modules/.bin/vite')}

    def observe(self, now, before, connections=0, agents=None, rules=None, cpu=0):
        self.procs[2] = process(2, self.procs[2].command, cpu=cpu)
        return r.observe(now, self.procs, {2: {3000}}, {2: connections},
                         {2: '/project', 3: '/project/src'}, agents or set(), rules or [], before)

    def test_first_observation_does_not_inherit_process_age(self):
        self.assertEqual(self.observe(200000, {})['2']['last_active'], 200000)

    def test_idle_clock_survives_continuous_observation(self):
        state = self.observe(0, {})
        for now in range(300, 24 * r.HOUR + 1, 300):
            state = self.observe(now, state)
        self.assertEqual(state['2']['last_active'], 0)
        self.assertEqual(state['2']['observed_at'], r.IDLE)

    def test_connection_agent_keep_or_cpu_resets_idle(self):
        for kwargs in [{'connections': 1}, {'agents': {3}}, {'rules': ['3000']},
                       {'rules': ['/project']}, {'cpu': 1}]:
            state = self.observe(0, {})
            self.assertEqual(self.observe(300, state, **kwargs)['2']['last_active'], 300)

    def test_observation_gap_and_reused_pid_reset_idle(self):
        state = self.observe(0, {})
        self.assertEqual(self.observe(2000, state)['2']['last_active'], 2000)
        state['2']['started'] = 'another process'
        self.assertEqual(self.observe(300, state)['2']['last_active'], 300)

    def test_project_prefix_does_not_protect_another_project(self):
        self.assertEqual(r.protected(2, {3000}, {2: '/project-two', 3: '/project'}, {3}, []), '')
        self.assertEqual(r.protected(2, {3000}, {2: '/project', 3: str(Path.home())}, {3}, []), '')
        self.assertEqual(r.protected(2, {3000}, {2: '/project', 3: '/'}, {3}, []), '')

    def test_busy_compiler_child_resets_idle(self):
        self.procs[4] = process(4, 'node compiler.js', parent=2)
        state = self.observe(0, {})
        self.procs[4] = process(4, 'node compiler.js', parent=2, cpu=1)
        self.assertEqual(self.observe(300, state)['2']['last_active'], 300)


class ShutdownTests(unittest.TestCase):
    def test_pid_reuse_is_never_signaled(self):
        expected = process(2, 'node /project/node_modules/.bin/vite')
        replacement = process(2, expected.command, started='Sun Oct 11 13:00:00 2026')
        with patch.object(r, 'processes', return_value={2: replacement}), patch.object(r.os, 'kill') as kill:
            self.assertFalse(r.shutdown(expected))
            kill.assert_not_called()

    def test_nested_agent_or_server_is_never_signaled(self):
        parent = process(2, 'node /project/node_modules/.bin/vite')
        for command in ['claude', 'node /other/node_modules/.bin/vite']:
            with patch.object(r, 'processes', return_value={2: parent, 3: process(3, command, parent=2)}), patch.object(r.os, 'kill') as kill:
                self.assertFalse(r.shutdown(parent))
                kill.assert_not_called()

    def test_only_server_stops_and_sibling_and_shell_survive(self):
        shell = process(1, 'zsh')
        server = process(2, 'node /project/node_modules/.bin/vite', parent=1)
        sibling = process(3, 'node /other/node_modules/.bin/vite', parent=1)
        with patch.object(r, 'processes', side_effect=[{1: shell, 2: server, 3: sibling}, {1: shell, 3: sibling}]), patch.object(r.os, 'kill') as kill:
            self.assertTrue(r.shutdown(server))
            kill.assert_called_once_with(2, signal.SIGTERM)

    def test_dry_run_sends_no_signal(self):
        server = process(2, 'node /project/node_modules/.bin/vite')
        with patch.object(r, 'processes', return_value={2: server}), patch.object(r.os, 'kill') as kill:
            self.assertTrue(r.shutdown(server, dry_run=True))
            kill.assert_not_called()


class InstallationTests(unittest.TestCase):
    def test_install_schedule_backup_and_keep_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            binary = home / '.local/bin/dev-reaper'
            binary.parent.mkdir(parents=True)
            binary.write_text('original unfinished script')
            keep = home / '.config/dev-reaper/keep'
            keep.parent.mkdir(parents=True)
            keep.write_text('3010\n')
            plist = installer.install(home, Path(sys.executable))
            config = plistlib.loads(plist.read_bytes())
            self.assertEqual(config['StartInterval'], 300)
            self.assertTrue(config['RunAtLoad'])
            self.assertEqual(config['EnvironmentVariables']['TZ'], 'America/Los_Angeles')
            self.assertTrue(binary.stat().st_mode & 0o111)
            self.assertEqual(keep.read_text(), '3010\n')
            self.assertEqual(next((keep.parent / 'backups').iterdir()).read_text(), 'original unfinished script')


@unittest.skipUnless(os.environ.get('DEV_REAPER_LIVE_TEST') == '1', 'opt-in disposable live listener tests')
class LiveTests(unittest.TestCase):
    """Exercise real sockets and SIGTERM, with only our disposable PID eligible."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dev-reaper-test-')
        self.home = Path(self.temp.name)
        script = self.home / 'node_modules/.bin/vite'
        script.parent.mkdir(parents=True)
        script.write_text("require('net').createServer(() => {}).listen(0, '127.0.0.1', function() { console.log(this.address().port); });\n")
        self.child = subprocess.Popen(['/opt/homebrew/bin/node', str(script)], cwd=self.home,
                                      stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.port = int(self.child.stdout.readline().strip())
        self.config_patch = patch.object(r, 'CONFIG', self.home / 'config')
        self.log_patch = patch.object(r, 'LOG', self.home / 'reaper.log')
        self.config_patch.start()
        self.log_patch.start()
        r.CONFIG.mkdir()
        self.real_snapshot = r.snapshot

    def tearDown(self):
        if self.child.poll() is None:
            self.child.terminate()
            self.child.wait(timeout=5)
        self.child.stdout.close()
        self.config_patch.stop()
        self.log_patch.stop()
        self.temp.cleanup()

    def snapshot(self):
        procs, servers, connections, dirs, agents = self.real_snapshot()
        pid = self.child.pid
        if pid not in servers:
            return procs, {}, {}, dirs, agents
        return procs, {pid: servers[pid]}, {pid: connections[pid]}, dirs, agents

    def seed_idle(self, age=r.IDLE + 10):
        procs, servers, connections, dirs, agents = self.snapshot()
        state = r.observe(time.time(), procs, servers, connections, dirs, agents, [], {})
        state[str(self.child.pid)]['last_active'] = time.time() - age
        r.save_state(state)

    def run_main(self, *flags):
        with patch.object(r, 'snapshot', side_effect=self.snapshot), patch.object(sys, 'argv', ['dev-reaper', *flags]):
            r.main()

    def test_idle_listener_receives_sigterm(self):
        self.seed_idle()
        self.run_main()
        self.child.wait(timeout=5)
        self.assertEqual(self.child.returncode, -signal.SIGTERM)

    def test_open_connection_protects_listener(self):
        self.seed_idle()
        with socket.create_connection(('127.0.0.1', self.port)):
            self.run_main()
            self.assertIsNone(self.child.poll())
        state = json.loads((r.CONFIG / 'state.json').read_text())[str(self.child.pid)]
        self.assertGreater(state['last_active'], time.time() - 5)

    def test_dry_run_preserves_listener_and_state(self):
        self.seed_idle()
        saved = (r.CONFIG / 'state.json').read_bytes()
        self.run_main('--dry-run')
        self.assertIsNone(self.child.poll())
        self.assertEqual((r.CONFIG / 'state.json').read_bytes(), saved)

    def test_keep_rule_protects_listener(self):
        self.seed_idle()
        (r.CONFIG / 'keep').write_text(str(self.port) + '\n')
        self.run_main()
        self.assertIsNone(self.child.poll())

    def test_final_recheck_activity_refreshes_idle_clock(self):
        self.seed_idle()
        initial = self.snapshot()
        procs, servers, connections, dirs, agents = initial
        final = (procs, servers, {self.child.pid: 1}, dirs, agents)
        with patch.object(r, 'snapshot', side_effect=[initial, final]), patch.object(sys, 'argv', ['dev-reaper']):
            r.main()
        state = json.loads((r.CONFIG / 'state.json').read_text())[str(self.child.pid)]
        self.assertGreater(state['last_active'], time.time() - 5)
        self.assertIsNone(self.child.poll())

    def test_memory_pressure_uses_three_hour_cutoff(self):
        self.seed_idle(age=4 * r.HOUR)
        self.run_main()
        self.assertIsNone(self.child.poll())
        real_run = r.run

        def pressure(args, **kwargs):
            return '2\n' if args[0] == '/usr/sbin/sysctl' else real_run(args, **kwargs)

        with patch.object(r, 'run', side_effect=pressure):
            self.run_main()
        self.child.wait(timeout=5)
        self.assertEqual(self.child.returncode, -signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()
