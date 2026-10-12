#!/opt/homebrew/bin/python3
"""Stop observed idle development servers on macOS; keep active host work."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo('America/Los_Angeles')
HOUR = 3600
IDLE = 24 * HOUR
PRESSURE_IDLE = 3 * HOUR
FLUSH_AGE = 10 * 60
MAX_OBSERVATION_GAP = 15 * 60
CONFIG = Path.home() / '.config/dev-reaper'
LOG = Path.home() / 'Library/Logs/dev-reaper.log'

NODE = re.compile(r'^(?:\S*/)?(?:node|bun)(?:\s|$)')
FLUSH = re.compile(r'/next/dist/telemetry/detached-flush\.js(?:\s|$)')


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    started: str
    cpu: float
    command: str


def run(args, empty_ok=False):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if result.returncode and not (empty_ok and result.returncode == 1 and not result.stderr):
        raise RuntimeError(f'{Path(args[0]).name} inspection failed ({result.returncode})')
    return result.stdout


def seconds(value):
    days, _, rest = value.rpartition('-')
    parts = [float(p) for p in rest.split(':')]
    while len(parts) < 3:
        parts.insert(0, 0)
    return int(days or 0) * 86400 + parts[0] * HOUR + parts[1] * 60 + parts[2]


def processes():
    rows = run(['/bin/ps', '-axo', 'uid=,pid=,ppid=,lstart=,time=,command='])
    result = {}
    for row in rows.splitlines():
        fields = row.split(None, 9)
        if len(fields) != 10:
            raise RuntimeError('incomplete process snapshot')
        uid, pid, parent, *tail = fields
        if int(uid) == os.getuid():
            result[int(pid)] = Process(int(pid), int(parent), ' '.join(tail[:5]),
                                       seconds(tail[5]), tail[6])
    return result


def ancestors(pid, procs):
    seen = set()
    while pid in procs and pid not in seen:
        seen.add(pid)
        yield procs[pid]
        pid = procs[pid].parent


def descendants(pid, procs):
    found = {pid}
    while True:
        children = {p.pid for p in procs.values() if p.parent in found}
        if children <= found:
            return found - {pid}
        found |= children


def development_command(command):
    """Inspect the actual Node/Bun script and its subcommand, not arbitrary args."""
    if not NODE.match(command):
        return False
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    index = 1
    while index < len(args) and args[index].startswith('-'):
        if args[index] in {'-e', '--eval', '-p', '--print'}:
            return False
        index += 2 if args[index] in {'-r', '--require', '--import', '--loader'} else 1
    if index >= len(args):
        return False
    script, params = args[index], args[index + 1:]
    if '/node_modules/' not in script:
        return False
    if re.search(r'/(?:\.bin/vite|vite/bin/vite\.js)$', script):
        return not any(arg in {'build', 'preview'} for arg in params)
    if re.search(r'/(?:\.bin/)?webpack-dev-server(?:/bin/webpack-dev-server\.js)?$', script):
        return True
    name = re.sub(r'\.(?:js|cjs|mjs)$', '', Path(script).name)
    modes = {'next': 'dev', 'astro': 'dev', 'react-router': 'dev', 'remix': 'dev',
             'wrangler': 'dev', 'storybook': 'dev', 'nuxi': 'dev', 'nuxt': 'dev',
             'svelte-kit': 'dev', 'expo': 'start', 'webpack': 'serve'}
    return bool(params and name in modes and params[0] == modes[name])


def is_dev(pid, procs):
    p = procs[pid]
    if p.command.startswith('next-server (v'):
        return any(development_command(a.command) for a in list(ancestors(pid, procs))[1:])
    return development_command(p.command)


def is_agent(p):
    first = Path(p.command.split(None, 1)[0]).name
    return first in {'claude', 'codex'} or bool(
        re.search(r'/claude/versions/\d[^/\s]*$', p.command.split(None, 1)[0]) or
        NODE.match(p.command) and re.search(r'/(?:@anthropic-ai/claude-code|@openai/codex)/', p.command)
    )


def lsof(*args):
    return run(['/usr/sbin/lsof', '-nP', '-w', '-a', '-u', str(os.getuid()), *args], empty_ok=True)


def sockets():
    files, current, pid = [], None, None
    for line in lsof('-iTCP', '-FpfnT').splitlines():
        if line.startswith('p'):
            pid = int(line[1:])
            current = None
        elif line.startswith('f') and pid is not None:
            current = {'pid': pid}
            files.append(current)
        elif current is not None and line.startswith('n'):
            current['address'] = line[1:]
        elif current is not None and line.startswith('TST='):
            current['state'] = line[4:]
    return files


def port(address):
    return int(address.rsplit(':', 1)[1])


def cwds(pids):
    if not pids:
        return {}
    result, pid = {}, None
    for line in lsof('-p', ','.join(map(str, sorted(pids))), '-d', 'cwd', '-Fpn').splitlines():
        if line.startswith('p'):
            pid = int(line[1:])
        elif line.startswith('n') and pid is not None:
            result[pid] = line[1:]
    return result


def snapshot():
    procs, files = processes(), sockets()
    servers = {}
    for f in files:
        pid = f['pid']
        if f.get('state') == 'LISTEN' and pid in procs and is_dev(pid, procs):
            servers.setdefault(pid, set()).add(port(f['address']))
    connections = {pid: 0 for pid in servers}
    for f in files:
        if f.get('state') != 'ESTABLISHED' or '->' not in f.get('address', ''):
            continue
        for pid, ports in servers.items():
            if f['pid'] == pid and port(f['address'].split('->')[0]) in ports:
                connections[pid] += 1
    agents = {p.pid for p in procs.values() if is_agent(p)}
    dirs = cwds(set(servers) | agents)
    # Missing cwd makes protection unprovable, so the run must not stop anything.
    missing = (set(servers) | agents) - dirs.keys()
    if missing:
        current = processes()
        vanished = {pid for pid in missing if not identity_matches(procs[pid], current.get(pid))}
        agents -= vanished
        for pid in vanished:
            servers.pop(pid, None)
            connections.pop(pid, None)
        if missing - vanished:
            raise RuntimeError(f'cannot inspect server/agent working directories for {sorted(missing - vanished)}')
    return procs, servers, connections, dirs, agents


def keep_rules():
    if not (CONFIG / 'keep').exists():
        return []
    return [s for row in (CONFIG / 'keep').read_text().splitlines()
            if (s := row.strip()) and not s.startswith('#')]


def protected(pid, ports, dirs, agents, rules):
    cwd = dirs[pid]
    if any(rule in cwd or rule in {str(p) for p in ports} for rule in rules):
        return 'keep rule'
    # A generic working directory is not evidence of activity in every project.
    for agent in agents:
        folder = dirs[agent]
        if Path(folder).resolve() in {Path('/'), Path.home().resolve(),
                                     (Path.home() / 'projects').resolve(),
                                     Path(tempfile.gettempdir()).resolve(), Path('/private/tmp')}:
            continue
        if cwd == folder or cwd.startswith(folder.rstrip('/') + '/') or folder.startswith(cwd.rstrip('/') + '/'):
            return 'coding agent in project'
    return ''


def observe(now, procs, servers, connections, dirs, agents, rules, before):
    state = {}
    for pid, ports in servers.items():
        p, prev = procs[pid], before.get(str(pid), {})
        continuous = (prev.get('started') == p.started and 0 <= now - prev.get('observed_at', 0) <= MAX_OBSERVATION_GAP)
        reason = protected(pid, ports, dirs, agents, rules)
        usage = tree_usage(pid, procs)
        cpu_active = continuous and cpu_since(prev.get('tree_usage', {}), usage) >= 1
        active = bool(connections[pid] or reason or cpu_active)
        last_active = prev.get('last_active', now) if continuous and not active else now
        state[str(pid)] = {'started': p.started, 'observed_at': now, 'last_active': last_active,
                           'cpu': p.cpu, 'tree_usage': usage, 'ports': sorted(ports), 'cwd': dirs[pid],
                           'connections': connections[pid], 'protected': reason,
                           'cpu_active': cpu_active}
    return state


def tree_usage(pid, procs):
    return {str(child): {'started': procs[child].started, 'cpu': procs[child].cpu}
            for child in {pid} | descendants(pid, procs)}


def cpu_since(before, after):
    return sum(max(0, value['cpu'] - before[key]['cpu'])
               if key in before and before[key]['started'] == value['started'] else value['cpu']
               for key, value in after.items())


def save_state(state):
    temp = CONFIG / 'state.json.tmp'
    temp.write_text(json.dumps(state, indent=2) + '\n')
    temp.replace(CONFIG / 'state.json')


def log(message):
    line = f'{datetime.now(PACIFIC):%Y-%m-%d %H:%M:%S %Z} {message}'
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 5 * 1024**2:
        LOG.replace(LOG.with_suffix('.log.1'))
    with LOG.open('a') as f:
        f.write(line + '\n')


def identity_matches(expected, actual):
    return actual is not None and (expected.started, expected.command) == (actual.started, actual.command)


def shutdown(expected, dry_run=False):
    """Only terminate the selected server and its verified children, never parents."""
    procs = processes()
    if not identity_matches(expected, procs.get(expected.pid)):
        return False
    targets = {pid: procs[pid] for pid in descendants(expected.pid, procs)}
    # A nested agent or another server is protected; do not stop this subtree.
    if any(is_agent(p) or is_dev(p.pid, procs) for p in targets.values()):
        return False
    if dry_run:
        return True
    try:
        os.kill(expected.pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        live = processes()
        if not any(identity_matches(p, live.get(p.pid)) for p in [expected, *targets.values()]):
            return True
        time.sleep(0.5)
    # Do not force-kill the server. Log a refused shutdown for manual inspection.
    # Children may remain after a graceful parent exit; request their shutdown too.
    live = processes()
    for p in targets.values():
        if identity_matches(p, live.get(p.pid)):
            try:
                os.kill(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    os.environ['TZ'] = 'America/Los_Angeles'
    time.tzset()
    CONFIG.mkdir(parents=True, exist_ok=True)
    with (CONFIG / 'lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        before = json.loads((CONFIG / 'state.json').read_text()) if (CONFIG / 'state.json').exists() else {}
        if not isinstance(before, dict) or any(not isinstance(s, dict) for s in before.values()):
            raise ValueError('invalid saved state')
        now = time.time()
        procs, servers, connections, dirs, agents = snapshot()
        pressure = int(run(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_vm_pressure_level']).strip()) >= 2
        limit = PRESSURE_IDLE if pressure else IDLE
        rules = keep_rules()
        state = observe(now, procs, servers, connections, dirs, agents, rules, before)
        # Inspection commands are read-only, including the idle clock.
        if args.status or args.dry_run:
            print(f'memory pressure: {"yes" if pressure else "no"}; idle limit: {limit // HOUR} h')
            for pid, s in state.items():
                idle = (now - s['last_active']) / HOUR
                decision = s['protected'] or ('connected' if s['connections'] else 'active CPU' if s['cpu_active'] else 'idle')
                print(f'{pid:>6} ports {",".join(map(str, s["ports"])):<12} idle {idle:5.1f} h [{decision}] {s["cwd"]}')
            if args.status:
                return
        else:
            save_state(state)
        for pid, s in state.items():
            if now - s['last_active'] < limit or s['protected']:
                continue
            # Refresh all protection immediately before each server stop.
            current, listeners, busy, current_dirs, current_agents = snapshot()
            p = procs[int(pid)]
            if not identity_matches(p, current.get(p.pid)) or p.pid not in listeners:
                continue
            if (busy[p.pid] or cpu_since(s['tree_usage'], tree_usage(p.pid, current)) >= 1 or
                    protected(p.pid, listeners[p.pid], current_dirs, current_agents, keep_rules())):
                s['last_active'] = time.time()
                if not args.dry_run:
                    save_state(state)
                continue
            if shutdown(p, args.dry_run):
                log(f'{"WOULD STOP" if args.dry_run else "STOP requested"} pid {pid}; ports {s["ports"]}; idle {(now - s["last_active"]) / HOUR:.1f} h; {s["cwd"]}')
        for p in procs.values():
            if p.parent != 1 or not NODE.match(p.command) or not FLUSH.search(p.command):
                continue
            started = datetime.strptime(p.started, '%a %b %d %H:%M:%S %Y').replace(tzinfo=PACIFIC).timestamp()
            if now - started >= FLUSH_AGE and shutdown(p, args.dry_run):
                log(f'{"WOULD STOP" if args.dry_run else "STOP requested"} orphaned Next telemetry pid {p.pid}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
        log(f'SKIP: {type(error).__name__}: {error}')
        raise SystemExit(1)
