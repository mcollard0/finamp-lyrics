#!/usr/bin/env python3
"""Test in disposable Docker containers; optional live Genius with one copied song."""
import argparse
import json
import hashlib
import io
import struct
import wave
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

import requests

ROOT = Path(__file__).resolve().parents[1]
GUID = 'a7d2b5ac-63af-4a71-8197-e4b4528b56c8'
IMAGE = 'finamp-lyrics-smoke-runtime:10.11.11'


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.PIPE).strip()


def wait_ready(base):
    for _ in range(120):
        try:
            response = requests.get(base + '/System/Info/Public', timeout=2)
            if response.ok and requests.get(base + '/health', timeout=2).ok:
                return response.json()
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError('Disposable server did not become ready')


def fixture_audio(path, title):
    data = io.BytesIO()
    with wave.open(data, 'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000)
        audio.writeframes(b'\0' * 32000)
    tags = b'INFO'
    for tag, value in [(b'IART', 'Fixture Artist'), (b'INAM', title)]:
        value = value.encode() + b'\0'
        tags += tag + struct.pack('<I', len(value)) + value + (b'\0' if len(value) % 2 else b'')
    content = data.getvalue() + b'LIST' + struct.pack('<I', len(tags)) + tags
    content = content[:4] + struct.pack('<I', len(content) - 8) + content[8:]
    path.write_bytes(content)


def wait_uploaded(server, item):
    # State is intentionally private to the container's service user.
    query = """import sqlite3,sys
from pathlib import Path
p=Path('/config/lyrics-state/state.sqlite3')
status='pending'
if p.exists():
    with sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True) as db:
        try:
            row=db.execute('SELECT status FROM jobs WHERE item=?',(bytes.fromhex(sys.argv[1].replace('-','')),)).fetchone()
            status=row[0] if row else 'pending'
        except sqlite3.OperationalError: pass
print(status)
"""
    for _ in range(90):
        if docker('exec', server, '/usr/bin/python3', '-c', query, item) == 'uploaded':
            return
        time.sleep(1)
    raise RuntimeError('Bundled worker did not publish lyrics within 90 seconds')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, default=ROOT / 'plugin/artifacts/release-10.11')
    parser.add_argument('--previous-archive', type=Path, default=ROOT / 'plugin/artifacts/release/finamp-lyrics-1.0.1.0.zip')
    parser.add_argument('--server-version', default='10.11.11')
    parser.add_argument('--image', default=IMAGE)
    parser.add_argument('--upgrade-from-image', help='Start on 10.11.11, then migrate disposable data to --image')
    parser.add_argument('--server-line', choices=['10.11', '12'], default='10.11')
    parser.add_argument('--live-media', type=Path, help='Copy one authorized song into the disposable library instead of generating WAV fixtures')
    parser.add_argument('--genius-credentials-file', type=Path, help='With --live-media, read only the Genius token; never use production Jellyfin credentials')
    args = parser.parse_args()
    if bool(args.live_media) != bool(args.genius_credentials_file):
        parser.error('--live-media and --genius-credentials-file must be used together')
    genius_token = 'synthetic-genius-token'
    if args.live_media:
        try:
            credentials = json.loads(args.genius_credentials_file.read_text())
        except PermissionError:
            payload = subprocess.check_output(['sudo', '-n', '-u', 'jellyfin', 'python3', '-c',
                'import json,sys; print(json.dumps({"GENIUS_CLIENT_ACCESS":json.load(open(sys.argv[1]))["GENIUS_CLIENT_ACCESS"]}))',
                str(args.genius_credentials_file)], text=True)
            credentials = json.loads(payload)
        genius_token = credentials['GENIUS_CLIENT_ACCESS']
    manifest = json.loads((args.release / 'manifest.json').read_text())
    version = manifest[0]['versions'][0]['version']
    archive = args.release / ('finamp-lyrics-' + version + '.zip')
    suffix = uuid.uuid4().hex[:10]
    network, server, catalog = ['finamp-smoke-' + suffix + s for s in ('-net', '-server', '-catalog')]
    with tempfile.TemporaryDirectory(prefix='finamp-catalog-smoke-') as tmp:
        tmp = Path(tmp)
        public = tmp / 'catalog'; public.mkdir()
        shutil.copy2(archive, public / archive.name)
        manifest[0]['versions'][0]['sourceUrl'] = 'http://' + catalog + ':8000/' + archive.name
        # Install the previous catalog release first, preserving its actual DLL.
        previous = args.previous_archive
        old_version = previous.stem.removeprefix('finamp-lyrics-')
        assert old_version != version
        with zipfile.ZipFile(previous) as previous_zip:
            old_dll = tmp / 'previous.dll'
            old_dll.write_bytes(previous_zip.read('Jellyfin.Plugin.FinampLyrics.dll'))
        rejected = tmp / 'rejected-release'
        check = subprocess.run([sys.executable, str(ROOT / 'plugin/package_release.py'),
            '--server-line', args.server_line, '--dll', str(old_dll), '--output', str(rejected)], text=True, capture_output=True)
        assert check.returncode != 0 and 'DLL identity/version mismatch' in check.stderr
        assert not rejected.exists()
        print('Packager rejects previous DLL without creating a mislabeled release', flush=True)
        shutil.copy2(previous, public / previous.name)
        old_entry = dict(manifest[0]['versions'][0])
        old_entry.update(version=old_version, targetAbi='10.11.11', checksum=hashlib.md5(previous.read_bytes()).hexdigest(),
                         sourceUrl='http://' + catalog + ':8000/' + previous.name)
        new_entry = manifest[0]['versions'][0]
        manifest[0]['versions'] = [old_entry]
        (public / 'manifest.json').write_text(json.dumps(manifest))
        config = tmp / 'config'; config.mkdir()
        media = tmp / 'media'; media.mkdir()
        if args.live_media:
            for number in (1, 2):
                shutil.copyfile(args.live_media, media / (str(number) + args.live_media.suffix))
        else:
            for title in ['Fixture Song', 'Upgrade Song']:
                fixture_audio(media / ('Fixture Artist - ' + title + '.wav'), title)
        try:
            docker('network', 'create', *([] if args.live_media else ['--internal']), network)
            docker('run', '-d', '--name', catalog, '--network', network,
                   '-v', str(public) + ':/catalog:ro', 'python:3.12-alpine',
                   'python', '-m', 'http.server', '8000', '--directory', '/catalog')
            docker('run', '-d', '--name', server, '--network', network,
                   '-v', str(config) + ':/config', '-v', str(media) + ':/media:ro', args.upgrade_from_image or args.image)
            inspection = json.loads(docker('inspect', server))[0]
            address = inspection['NetworkSettings']['Networks'][network]['IPAddress']
            base = 'http://' + address + ':8096'
            info = wait_ready(base)
            assert info['version'] == ('10.11.11' if args.upgrade_from_image else args.server_version)
            session = requests.Session()

            def api(method, path, **kwargs):
                for _ in range(120):
                    response = session.request(method, base + path, timeout=120, **kwargs)
                    if response.status_code != 503:
                        break
                    time.sleep(1)
                if not response.ok:
                    raise RuntimeError(method + ' ' + path + ': HTTP ' + str(response.status_code))
                return json.loads(response.text, object_hook=lambda d: {k.casefold(): v for k, v in d.items()}) if response.content else None

            api('GET', '/Startup/User')
            password = secrets.token_urlsafe(24)
            api('POST', '/Startup/User', json={'Name': 'smoke-admin', 'Password': password})
            api('POST', '/Startup/Complete')
            session.headers['Authorization'] = 'MediaBrowser Client="CatalogSmoke", Device="Disposable", DeviceId="smoke", Version="1"'
            auth = api('POST', '/Users/AuthenticateByName', json={'Username': 'smoke-admin', 'Pw': password})
            session.headers['Authorization'] = 'MediaBrowser Client="CatalogSmoke", Device="Disposable", DeviceId="smoke", Version="1", Token="' + auth['accesstoken'] + '"'
            repository = 'http://' + catalog + ':8000/manifest.json'
            api('POST', '/Repositories', json=[{'name': 'Disposable test catalog', 'url': repository, 'enabled': True}])
            docker('restart', server)
            wait_ready(base)
            print('Test repository configuration:', api('GET', '/Repositories'), flush=True)
            packages = api('GET', '/Packages')
            if not any(p['guid'].replace('-', '').lower() == GUID.replace('-', '') for p in packages):
                logs = subprocess.check_output(['docker', 'logs', server], text=True, stderr=subprocess.STDOUT)
                print('Catalog diagnostics:', '\n'.join(line for line in logs.splitlines() if any(word in line.lower() for word in ('repository', 'manifest', 'error', 'exception')))[-5000:], flush=True)
                print('Catalog requests:', subprocess.check_output(['docker', 'logs', catalog], text=True, stderr=subprocess.STDOUT), flush=True)
                print('Packages:', packages, flush=True)
                raise RuntimeError('Test catalog package missing')
            package = next(p for p in packages if p['guid'].replace('-', '').lower() == GUID.replace('-', ''))
            assert package['owner'] == 'mcollard0'
            print('Catalog recognizes developer and package', flush=True)
            api('POST', '/Packages/Installed/Finamp%20Lyrics', params={'assemblyGuid': GUID, 'version': old_version, 'repositoryUrl': repository})
            old_dir = config / 'plugins' / ('Finamp Lyrics_' + old_version)
            docker('restart', server)
            wait_ready(base)
            settings = session.get(base + '/Plugins/' + GUID + '/Configuration', timeout=30).json()
            settings.update(PythonPath='/usr/bin/python3' if args.live_media else '/opt/finamp-smoke/python',
                            ScriptPath='/config/plugins/Finamp Lyrics_' + old_version + '/worker/lyrics_fetcher.py',
                            StateDirectory='/config/lyrics-state', CredentialsFile='/config/smoke-credentials.json',
                            ServerUrl='http://127.0.0.1:8096', BackgroundCount=0, DelaySeconds=0)
            credential_file = config / 'smoke-credentials.json'
            credential_file.write_text(json.dumps({'GENIUS_CLIENT_ACCESS': genius_token, 'JELLYFIN_API_KEY': auth['accesstoken']}))
            credential_file.chmod(0o600)
            api('POST', '/Plugins/' + GUID + '/Configuration', json=settings)
            api('POST', '/Library/VirtualFolders', params={'name': 'Fixture Music', 'collectionType': 'music', 'refreshLibrary': 'true'},
                json={'LibraryOptions': {'PathInfos': [{'Path': '/media'}]}})
            tracks = []
            for _ in range(90):
                tracks = api('GET', '/Items', params={'Recursive': 'true', 'IncludeItemTypes': 'Audio'})['items']
                if len(tracks) == 2:
                    break
                time.sleep(1)
            assert len(tracks) == 2
            first = tracks[0]['id'] if args.live_media else next(t for t in tracks if 'Fixture Song' in t['name'])['id']
            second = tracks[1]['id'] if args.live_media else next(t for t in tracks if 'Upgrade Song' in t['name'])['id']
            api('POST', '/Items/' + first + '/PlaybackInfo', json={})
            wait_uploaded(server, first)
            print('Previous catalog worker launched and uploaded lyrics', flush=True)
            manifest[0]['versions'] = [new_entry, old_entry]
            (public / 'manifest.json').write_text(json.dumps(manifest))
            if args.upgrade_from_image:
                # Quarantine the incompatible old assembly; keep config and lyric state.
                docker('exec', server, '/usr/bin/python3', '-c',
                       'import shutil,sys; shutil.move(sys.argv[1], "/config/smoke-disabled-plugin")',
                       '/config/plugins/Finamp Lyrics_' + old_version)
                docker('stop', server)
                docker('rm', server)
                docker('run', '-d', '--name', server, '--network', network,
                       '-v', str(config) + ':/config', '-v', str(media) + ':/media:ro', args.image)
                inspection = json.loads(docker('inspect', server))[0]
                base = 'http://' + inspection['NetworkSettings']['Networks'][network]['IPAddress'] + ':8096'
                info = wait_ready(base)
                assert info['version'].split('.')[:2] == args.server_version.split('.')[:2], info['version']
                auth = api('POST', '/Users/AuthenticateByName', json={'Username': 'smoke-admin', 'Pw': password})
                session.headers['Authorization'] = 'MediaBrowser Client="CatalogSmoke", Device="Disposable", DeviceId="smoke", Version="1", Token="' + auth['accesstoken'] + '"'
                credential_file.write_text(json.dumps({'GENIUS_CLIENT_ACCESS': genius_token, 'JELLYFIN_API_KEY': auth['accesstoken']}))
                credential_file.chmod(0o600)
                wait_uploaded(server, first)
                print('Disposable server upgraded to ' + info['version'] + '; earlier lyric state survived', flush=True)
            api('POST', '/Packages/Installed/Finamp%20Lyrics', params={'assemblyGuid': GUID, 'version': version, 'repositoryUrl': repository})
            plugin_dir = config / 'plugins' / ('Finamp Lyrics_' + version)
            with zipfile.ZipFile(archive) as z:
                for name in z.namelist():
                    assert (plugin_dir / name).read_bytes() == z.read(name), name
            print('Jellyfin downloaded, checksum-checked, and extracted the release ZIP', flush=True)
            docker('restart', server)
            wait_ready(base)
            plugins = api('GET', '/Plugins')
            plugin = next(p for p in plugins if p['id'].replace('-', '').lower() == GUID.replace('-', ''))
            assert plugin['status'] == 'Active', plugin['status']
            assert plugin['version'] == version
            assert not old_dir.exists(), 'Jellyfin should clean up the superseded plugin directory'
            saved = api('GET', '/Plugins/' + GUID + '/Configuration')
            assert saved['scriptpath'] == settings['ScriptPath']
            api('POST', '/Items/' + second + '/PlaybackInfo', json={})
            wait_uploaded(server, second)
            prior_lyric = api('GET', '/Audio/' + first + '/Lyrics')
            assert any(line['text'].strip() for line in prior_lyric['lyrics'])
            if not args.live_media:
                assert any('Synthetic fixture words' in line['text'] for line in prior_lyric['lyrics'])
            lyric = api('GET', '/Audio/' + second + '/Lyrics')
            assert any(line['text'].strip() for line in lyric['lyrics'])
            if not args.live_media:
                assert any('Synthetic fixture words' in line['text'] for line in lyric['lyrics'])
            print('Upgrade preserved settings and current worker lyrics are stored and retrievable', flush=True)
            page = session.get(base + '/web/ConfigurationPage', params={'name': 'Finamp Lyrics'}, timeout=30)
            if not page.ok:
                page = session.get(base + '/web/configurationpage', params={'name': 'Finamp Lyrics'}, timeout=30)
            assert page.ok
            assert 'https://github.com/mcollard0/finamp-lyrics' in page.text
            assert 'mcollard0' in page.text
            result = {'server_version': info['version'], 'plugin_version': version,
                      'catalog_install': 'pass', 'plugin_status': plugin['status'],
                      'settings_page': 'pass', 'archive_files': 'pass', 'worker_upload': 'pass',
                      'catalog_upgrade': 'pass', 'lyric_retrieval': 'pass', 'previous_lyrics_preserved': 'pass', 'server_upgrade': bool(args.upgrade_from_image), 'stale_dll_rejected': 'pass', 'old_version': old_version,
                      'live_genius': bool(args.live_media),
                      'scope': 'Real Genius retrieval on copied authorized library audio, with isolated Jellyfin credentials' if args.live_media else 'Real plugin-triggered worker execution and lyric upload on synthetic audio, before and after upgrade; Genius responses are fixtures'}
            (args.release / ('catalog-smoke-' + args.server_version + ('-live' if args.live_media else '') + '.json')).write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result), flush=True)
        finally:
            for name in (server, catalog):
                subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['docker', 'network', 'rm', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # The image's root-owned files are removed within the same disposable runtime.
            subprocess.run(['docker', 'run', '--rm', '-v', str(tmp) + ':/cleanup',
                            'python:3.12-alpine', 'python', '-c',
                            'import shutil; shutil.rmtree("/cleanup/config", ignore_errors=True)'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
