#!/usr/bin/env python3
"""Install the built plugin; preserve the running 10.11.11 server on activation."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA = Path('/var/lib/jellyfin/finamp-lyrics')
PLUGIN = Path('/var/lib/jellyfin/plugins/Finamp Lyrics_1.0.0.0')
DROPIN = Path('/etc/systemd/system/jellyfin.service.d/finamp-lyrics.conf')
SERVER_PACKAGE = Path('/var/cache/pacman/pkg/jellyfin-server-10.11.11-1.1-x86_64_v4.pkg.tar.zst')
WEB_PACKAGE = Path('/var/cache/pacman/pkg/jellyfin-web-10.11.11-1-any.pkg.tar.zst')
DLL = ROOT / 'plugin/artifacts/Jellyfin.Plugin.FinampLyrics.dll'
BASE = 'http://localhost:8096'


def get_json(path, token=None):
    headers = {'X-Emby-Token': token} if token else {}
    with urllib.request.urlopen(urllib.request.Request(BASE + path, headers=headers), timeout=10) as response:
        return json.load(response)


def run(*arguments):
    subprocess.run(arguments, check=True, timeout=120, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activate', action='store_true', help='Install files and restart Jellyfin, pinned to 10.11.11')
    parser.add_argument('--update-only', action='store_true', help='Update the existing DLL and restart; preserve worker, cache, credentials and server override')
    parser.add_argument('--stage-only', action='store_true', help='Copy the updated DLL without restarting; requires --activate --update-only')
    parser.add_argument('--allow-active-playback', action='store_true', help='Restart despite active playback when explicitly requested')
    args = parser.parse_args()
    if args.update_only and not args.activate:
        parser.error('--update-only requires --activate')
    if args.stage_only and not (args.update_only and args.activate):
        parser.error('--stage-only requires --activate --update-only')
    if args.allow_active_playback and not args.activate:
        parser.error('--allow-active-playback requires --activate')
    for path in (DLL, SERVER_PACKAGE, WEB_PACKAGE):
        if not path.is_file():
            raise RuntimeError(f'Missing installation artifact: {path}')
    version = get_json('/System/Info/Public')['Version']
    if version != '10.11.11':
        raise RuntimeError('Installer requires the running server to be Jellyfin 10.11.11')
    credentials = {name: os.environ.get(name, '') for name in ('GENIUS_CLIENT_ACCESS', 'JELLYFIN_API_KEY')}
    if args.update_only:
        credentials = json.loads((DATA / 'credentials.json').read_text())
    if not all(credentials.values()):
        raise RuntimeError('Required environment credentials are missing')
    sessions = get_json('/Sessions', credentials['JELLYFIN_API_KEY'])
    if args.activate and not args.stage_only and not args.allow_active_playback and any(session.get('NowPlayingItem') for session in sessions):
        raise RuntimeError('Playback is active; defer activation until sessions stop')
    account = pwd.getpwnam('jellyfin')
    plan = {'plugin': str(PLUGIN), 'worker': str(DATA), 'service_override': str(DROPIN),
            'server_version': version, 'preserve_server_version': True, 'delay_seconds': 2,
            'background_count': 3, 'minimum_background_plays': 2,
            'plugin_sha256': hashlib.sha256(DLL.read_bytes()).hexdigest(),
            'active_playback_sessions': sum(bool(session.get('NowPlayingItem')) for session in sessions)}
    if not args.activate:
        print(json.dumps({'plan': plan}))
        return
    if os.geteuid() != 0:
        raise RuntimeError('Activation requires root')
    # Verify the actual service user's Python dependencies before changing anything.
    run('sudo', '-u', 'jellyfin', '/usr/bin/python3', '-c', 'import requests, bs4, sqlite3, fcntl')
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o750)
    os.chown(DATA, account.pw_uid, account.pw_gid)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = DATA / 'backups' / stamp
    backup.mkdir(parents=True, exist_ok=True)
    os.chmod(backup, 0o700)
    if args.update_only:
        installed = PLUGIN / DLL.name
        if not installed.is_file():
            raise RuntimeError('Update requires an already installed plugin')
        shutil.copy2(installed, backup / DLL.name)
        temporary = installed.with_suffix('.new')
        shutil.copy2(DLL, temporary)
        os.chmod(temporary, 0o644)
        os.chown(temporary, account.pw_uid, account.pw_gid)
        temporary.replace(installed)
        if args.stage_only:
            print(json.dumps({'staged': plan, 'backup': str(backup), 'restart_required': True}))
            return
        try:
            run('systemctl', 'restart', 'jellyfin')
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    plugins = get_json('/Plugins', credentials['JELLYFIN_API_KEY'])
                    loaded = next((p for p in plugins if p.get('Id', '').replace('-', '') == 'a7d2b5ac63af4a718197e4b4528b56c8'), None)
                    config = get_json('/Plugins/a7d2b5ac-63af-4a71-8197-e4b4528b56c8/Configuration', credentials['JELLYFIN_API_KEY'])
                    if loaded and loaded.get('Status', '').casefold() == 'active' and 'AdditionalArguments' in config:
                        print(json.dumps({'updated': plan, 'plugin': loaded, 'backup': str(backup),
                                          'worker_settings': {k: config.get(k) for k in (
                                              'PythonPath', 'ScriptPath', 'AdditionalArguments', 'BackgroundCount',
                                              'DelaySeconds', 'WorkerTimeoutMinutes')}}))
                        return
                except (OSError, ValueError):
                    pass
                time.sleep(2)
            raise RuntimeError('Updated plugin activation could not be verified')
        except Exception:
            shutil.copy2(backup / DLL.name, installed)
            run('systemctl', 'restart', 'jellyfin')
            raise
    for old in (DROPIN, DATA / 'credentials.json', DATA / 'lyrics_fetcher.py', PLUGIN / DLL.name):
        if old.exists():
            shutil.copy2(old, backup / old.name)
    for package, target in ((SERVER_PACKAGE, DATA / 'server-10.11.11'), (WEB_PACKAGE, DATA / 'web-10.11.11')):
        if not target.exists():
            target.mkdir()
            run('tar', '--no-same-owner', '-xf', str(package), '-C', str(target))
    shutil.copy2(ROOT / 'lyrics_fetcher.py', DATA / 'lyrics_fetcher.py')
    os.chmod(DATA / 'lyrics_fetcher.py', 0o644)
    secret_path = DATA / 'credentials.json'
    # Open with restricted permissions from creation; never print credential values.
    with os.fdopen(os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(credentials, stream)
    os.chmod(secret_path, 0o600)
    os.chown(secret_path, account.pw_uid, account.pw_gid)
    state = DATA / 'state'
    state.mkdir(exist_ok=True)
    if not (state / 'state.sqlite3').exists():
        with sqlite3.connect(ROOT / 'state/state.sqlite3') as source, sqlite3.connect(state / 'state.sqlite3') as target:
            source.backup(target)
        shutil.copytree(ROOT / 'state/lyrics', state / 'lyrics', dirs_exist_ok=True)
    for path in (state, *state.rglob('*')):
        os.chown(path, account.pw_uid, account.pw_gid)
        os.chmod(path, 0o750 if path.is_dir() else 0o640)
    PLUGIN.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DLL, PLUGIN / DLL.name)
    metadata = dict(name='Finamp Lyrics', guid='a7d2b5ac-63af-4a71-8197-e4b4528b56c8', version='1.0.0.0',
                    targetAbi='10.11.0.0', category='Music', owner='local', status='Active', autoUpdate=False,
                    description='Fetch Genius lyrics on music prefetch and playback.', overview='Music lyric worker',
                    timestamp=datetime.now(timezone.utc).isoformat(), assemblies=[])
    (PLUGIN / 'meta.json').write_text(json.dumps(metadata, indent=2) + '\n')
    for path in (PLUGIN, *PLUGIN.iterdir()):
        os.chown(path, account.pw_uid, account.pw_gid)
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    DROPIN.parent.mkdir(parents=True, exist_ok=True)
    DROPIN.write_text('[Service]\nExecStart=\nExecStart=' + str(DATA / 'server-10.11.11/usr/lib/jellyfin/jellyfin')
        + ' --webdir=' + str(DATA / 'web-10.11.11/usr/share/jellyfin/web')
        + ' $JELLYFIN_FFMPEG_OPT $JELLYFIN_SERVICE_OPT $JELLYFIN_NOWEBAPP_OPT $JELLYFIN_ADDITIONAL_OPTS\n')
    # A failed new plugin is disabled, never recovered by starting the newer server.
    try:
        run('systemctl', 'daemon-reload')
        run('systemctl', 'restart', 'jellyfin')
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if get_json('/System/Info/Public')['Version'] == '10.11.11':
                    plugins = get_json('/Plugins', credentials['JELLYFIN_API_KEY'])
                    loaded = next((p for p in plugins if p.get('Id', '').replace('-', '') == 'a7d2b5ac63af4a718197e4b4528b56c8'), None)
                    if loaded and loaded.get('Status', '').casefold() == 'active':
                        print(json.dumps({'installed': plan, 'plugin': loaded, 'backup': str(backup)}))
                        return
            except (OSError, ValueError):
                pass
            time.sleep(2)
        raise RuntimeError('Plugin activation could not be verified')
    except Exception:
        installed = PLUGIN / DLL.name
        if installed.exists():
            installed.rename(installed.with_suffix('.dll.disabled'))
        run('systemctl', 'restart', 'jellyfin')
        raise


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Requests and process errors can expose headers/env; print only safe types.
        if isinstance(exc, RuntimeError):
            print(json.dumps({'error': str(exc)}))
        else:
            print(json.dumps({'error_type': type(exc).__name__}))
        raise SystemExit(1)
