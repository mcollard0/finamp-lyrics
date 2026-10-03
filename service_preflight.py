#!/usr/bin/env python3
"""Read-only version and music-filesystem inspection; never print tokens."""
import json
import os
from pathlib import Path
import subprocess
import requests

session = requests.Session()
session.headers['X-Emby-Token'] = os.environ['JELLYFIN_API_KEY']
base = 'http://localhost:8096'
response = session.get(base + '/Library/VirtualFolders', timeout=(5, 30))
response.raise_for_status()
libraries = []
for library in response.json():
    if library.get('CollectionType') != 'music':
        continue
    paths = []
    for location in library.get('Locations', []):
        mount = subprocess.run(['findmnt', '-J', '-T', location, '-o', 'TARGET,SOURCE,FSTYPE,OPTIONS'],
                               text=True, capture_output=True, check=True)
        paths.append(dict(path=location, mount=json.loads(mount.stdout)))
    libraries.append(dict(name=library['Name'], id=library['ItemId'], paths=paths))
version = session.get(base + '/System/Info/Public', timeout=(5, 30))
version.raise_for_status()
result = dict(version_after_restart=version.json()['Version'], music_libraries=libraries)
Path('service-preflight.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
