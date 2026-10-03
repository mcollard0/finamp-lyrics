#!/usr/bin/env python3
"""Install/start the reviewed one-shot batch service without changing Jellyfin."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
DATA = Path('/var/lib/jellyfin/finamp-lyrics')
UNIT = Path('/etc/systemd/system/finamp-lyrics-batch.service')
TIMER = UNIT.with_suffix('.timer')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--schedule', action='store_true', help='Enable the daily 2am America/Chicago timer')
    args = parser.parse_args()
    if args.start and not args.install:
        parser.error('--start requires --install')
    if args.schedule and not args.install:
        parser.error('--schedule requires --install')
    plan = dict(unit=str(UNIT), user='jellyfin', top=100, delay_seconds=2,
                shared_state=str(DATA / 'state'), disk_fallback='atime; mtime on noatime mounts',
                automatic_schedule=args.schedule or Path('/etc/systemd/system/timers.target.wants/' + TIMER.name).exists())
    if not args.install:
        print(json.dumps({'plan': plan}))
        return
    if os.geteuid() != 0:
        raise RuntimeError('Installation requires root')
    if not (DATA / 'credentials.json').is_file() or not (DATA / 'state/state.sqlite3').is_file():
        raise RuntimeError('Installed plugin credentials and shared cache are required')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = DATA / 'backups' / ('batch-' + stamp)
    backup.mkdir(parents=True)
    os.chmod(backup, 0o700)
    for name in ('lyrics_fetcher.py', 'service_runner.py', 'config.json'):
        destination = DATA / name
        if destination.exists():
            shutil.copy2(destination, backup / name)
        temporary = destination.with_suffix('.new')
        shutil.copyfile(ROOT / name, temporary)
        os.chmod(temporary, 0o600 if name == "config.json" else 0o644)
        if name == "config.json":
            import pwd
            os.chown(temporary, pwd.getpwnam("jellyfin").pw_uid, -1)
        temporary.replace(destination)
    if UNIT.exists():
        shutil.copy2(UNIT, backup / UNIT.name)
    shutil.copyfile(ROOT / 'systemd' / UNIT.name, UNIT)
    os.chmod(UNIT, 0o644)
    units = [str(UNIT)]
    if args.schedule:
        if TIMER.exists():
            shutil.copy2(TIMER, backup / TIMER.name)
        shutil.copyfile(ROOT / 'systemd' / TIMER.name, TIMER)
        os.chmod(TIMER, 0o644)
        units.append(str(TIMER))
        plan['schedule'] = 'Daily 02:00 America/Chicago; catch up after missed runs'
    subprocess.run(['systemd-analyze', 'verify', *units], check=True, timeout=30)
    subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=30)
    if args.schedule:
        subprocess.run(['systemctl', 'enable', '--now', TIMER.name], check=True, timeout=30)
    if args.start:
        subprocess.run(['systemctl', 'start', '--no-block', UNIT.name], check=True, timeout=30)
    print(json.dumps({'installed': plan, 'started': args.start, 'backup': str(backup)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__}))
        raise SystemExit(1)
