#!/opt/homebrew/bin/python3
"""Install the local development-server cleanup service, preserving replaced files."""

import argparse
from datetime import datetime
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
from zoneinfo import ZoneInfo

LABEL = 'io.tstout.dev-reaper'


def install(home, python):
    source = Path(__file__).with_name('dev-reaper.py')
    binary = home / '.local/bin/dev-reaper'
    config = home / '.config/dev-reaper'
    plist = home / f'Library/LaunchAgents/{LABEL}.plist'
    config.mkdir(parents=True, exist_ok=True)
    (home / 'Library/Logs').mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%Y%m%d-%H%M%S-%f')
    payload = {
        'Label': LABEL,
        'ProgramArguments': [str(python), str(binary)],
        'StartInterval': 300,
        'RunAtLoad': True,
        'ProcessType': 'Background',
        'Nice': 10,
        'EnvironmentVariables': {'TZ': 'America/Los_Angeles'},
        'StandardOutPath': '/dev/null',
        'StandardErrorPath': str(home / 'Library/Logs/dev-reaper.err'),
    }
    for target, data, mode in [(binary, source.read_bytes(), 0o755),
                               (plist, plistlib.dumps(payload), 0o644)]:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != data:
            backup = config / 'backups' / f'{target.name}.{stamp}'
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
            temp.write(data)
            temp_path = Path(temp.name)
        temp_path.chmod(mode)
        temp_path.replace(target)
    keep = config / 'keep'
    if not keep.exists():
        keep.write_text('# One TCP port or project-path substring per line.\n'
                        '# Matching development servers remain running.\n'
                        '# Example: 3010\n# Example: /projects/keep-this-preview\n')
    return plist


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--python', type=Path, default=Path('/opt/homebrew/bin/python3'))
    parser.add_argument('--load', action='store_true')
    args = parser.parse_args()
    if not args.python.is_file():
        parser.error('Python interpreter does not exist')
    if args.load and args.home.resolve() != Path.home().resolve():
        parser.error('--load requires the current user home')
    plist = install(args.home, args.python)
    if args.load:
        domain = f'gui/{os.getuid()}'
        name = domain + '/' + LABEL
        if subprocess.run(['/bin/launchctl', 'print', name], capture_output=True).returncode == 0:
            subprocess.run(['/bin/launchctl', 'bootout', name], check=True)
        subprocess.run(['/bin/launchctl', 'enable', name], check=True)
        subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(plist)], check=True)
    print(f'Installed {plist}; schedule: every 5 minutes; loaded: {args.load}')


if __name__ == '__main__':
    main()
