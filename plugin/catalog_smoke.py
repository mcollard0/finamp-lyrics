#!/usr/bin/env python3
"""Test a release in disposable Docker containers; no production config or credentials."""
import argparse
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile

import requests

ROOT = Path(__file__).resolve().parents[1]
GUID = 'a7d2b5ac-63af-4a71-8197-e4b4528b56c8'
IMAGE = 'jellyfin/jellyfin:10.11.11'


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, default=ROOT / 'plugin/artifacts/release')
    args = parser.parse_args()
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
        (public / 'manifest.json').write_text(json.dumps(manifest))
        config = tmp / 'config'; config.mkdir()
        try:
            docker('network', 'create', '--internal', network)
            docker('run', '-d', '--name', catalog, '--network', network,
                   '-v', str(public) + ':/catalog:ro', 'python:3.12-alpine',
                   'python', '-m', 'http.server', '8000', '--directory', '/catalog')
            docker('run', '-d', '--name', server, '--network', network,
                   '-v', str(config) + ':/config', IMAGE)
            inspection = json.loads(docker('inspect', server))[0]
            address = inspection['NetworkSettings']['Networks'][network]['IPAddress']
            base = 'http://' + address + ':8096'
            info = wait_ready(base)
            assert info['version'] == '10.11.11'
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
            session.headers['X-Emby-Token'] = auth['accesstoken']
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
            api('POST', '/Packages/Installed/Finamp%20Lyrics', params={'assemblyGuid': GUID, 'version': version, 'repositoryUrl': repository})
            plugin_dir = next((config / 'plugins').glob('Finamp Lyrics_*'))
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
            page = session.get(base + '/web/ConfigurationPage', params={'name': 'Finamp Lyrics'}, timeout=30)
            if not page.ok:
                page = session.get(base + '/web/configurationpage', params={'name': 'Finamp Lyrics'}, timeout=30)
            assert page.ok
            assert 'https://github.com/mcollard0/finamp-lyrics' in page.text
            assert 'mcollard0' in page.text
            result = {'server_version': info['version'], 'plugin_version': version,
                      'catalog_install': 'pass', 'plugin_status': plugin['status'],
                      'settings_page': 'pass', 'archive_files': 'pass',
                      'scope': 'Isolated catalog install and plugin load; no media, real credentials, or Genius lookup'}
            (args.release / 'catalog-smoke.json').write_text(json.dumps(result, indent=2) + '\n')
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
