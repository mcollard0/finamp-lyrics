"""Docker test-only provider fixtures; this file is never included in releases."""
import json
import requests

original = requests.Session.request


def fixture_request(self, method, url, **kwargs):
    if url.startswith('https://api.genius.com/search'):
        title = 'Upgrade Song' if 'Upgrade Song' in kwargs.get('params', {}).get('q', '') else 'Fixture Song'
        payload = {'response': {'hits': [{'type': 'song', 'result': {
            'id': 1 if title == 'Fixture Song' else 2, 'title': title,
            'primary_artist': {'name': 'Fixture Artist'},
            'url': 'https://genius.com/fixture-artist-' + title.lower().replace(' ', '-') + '-lyrics'}}]}}
        content = json.dumps(payload)
    elif url.startswith('https://genius.com/fixture-artist-'):
        content = '<div data-lyrics-container="true">Synthetic fixture words for isolated plugin testing<br/>This is not a published song</div>'
    else:
        return original(self, method, url, **kwargs)
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response._content = content.encode()
    response.encoding = 'utf-8'
    return response


requests.Session.request = fixture_request
