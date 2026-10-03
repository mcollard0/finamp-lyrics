#!/usr/bin/env python3
"""Verify the installed plugin and trigger a real cached-track metadata prefetch."""
import json
import os
import argparse
import time
from pathlib import Path
import requests

base = 'http://localhost:8096'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--item-id', default='07b52b435cf8b7b472b3edd17586bb45')
parser.add_argument('--wait-for-lyrics', action='store_true')
parser.add_argument('--configuration-only', action='store_true', help='Verify worker controls without triggering a lyric job')
args = parser.parse_args()
session = requests.Session()
session.headers['X-Emby-Token'] = os.environ['JELLYFIN_API_KEY']


def get(path, **params):
    response = session.get(base + path, params=params, timeout=(5, 30))
    response.raise_for_status()
    return response


plugin_id = 'a7d2b5ac63af4a718197e4b4528b56c8'
plugins = get('/Plugins').json()
plugin = next(p for p in plugins if p['Id'].replace('-', '') == plugin_id)
config = get(f'/Plugins/{plugin_id}/Configuration').json()
if args.configuration_only:
    page = get('/web/ConfigurationPage', name='Finamp Lyrics')
    keys = ('PythonPath', 'ScriptPath', 'AdditionalArguments', 'ServerUrl', 'StateDirectory',
            'CredentialsFile', 'WorkerTimeoutMinutes', 'BackgroundCount', 'MinimumPlays', 'DelaySeconds')
    controls = all(f'id="{key}"' in page.text for key in keys)
    result = {'version': get('/System/Info/Public').json()['Version'], 'plugin_status': plugin['Status'],
              'configuration': {key: config.get(key) for key in keys},
              'configuration_page': page.status_code, 'all_worker_controls_present': controls,
              'command_preview_present': 'id="CommandPreview"' in page.text}
    Path('plugin/configuration-verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    raise SystemExit(0 if controls and 'AdditionalArguments' in config and plugin['Status'].casefold() == 'active' else 1)
users = get('/Users').json()
user = next(u for u in users if u['Name'] == 'Mcollard')
item_id = args.item_id
response = get(f'/Items/{item_id}/PlaybackInfo', UserId=user['Id'])
initial_stream = any(stream.get('Type') == 'Lyric' for source in response.json().get('MediaSources', [])
                    for stream in source.get('MediaStreams', []))
if args.wait_for_lyrics:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        lyric_response = session.get(base + f'/Audio/{item_id}/Lyrics', timeout=(5, 10))
        if lyric_response.ok and lyric_response.json().get('Lyrics'):
            response = get(f'/Items/{item_id}/PlaybackInfo', UserId=user['Id'])
            break
        time.sleep(1)
page = get('/web/ConfigurationPage', name='Finamp Lyrics')
result = dict(version=get('/System/Info/Public').json()['Version'], plugin=plugin,
              configuration={key: config[key] for key in ('Enabled', 'PrefetchEnabled', 'PlaybackEnabled',
                    'BackgroundCount', 'MinimumPlays', 'DelaySeconds', 'StateDirectory')},
              prefetch=dict(item_id=item_id, status=response.status_code,
                    initial_lyric_stream=initial_stream,
                    lyric_stream=any(stream.get('Type') == 'Lyric' for source in response.json().get('MediaSources', [])
                        for stream in source.get('MediaStreams', []))),
              configuration_page=page.status_code,
              settings_page_contains_form='finampLyricsForm' in page.text)
report_path = Path('plugin/live-verification.json')
if report_path.exists():
    previous = json.loads(report_path.read_text())
    result['previous_prefetch_checks'] = previous.get('previous_prefetch_checks', []) + [previous['prefetch']]
report_path.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
