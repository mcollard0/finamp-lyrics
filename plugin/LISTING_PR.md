Add Finamp Lyrics to the third-party plugin repository list.

Finamp Lyrics queues lyric checks when Jellyfin music metadata is prefetched or
playback begins. Developer and maintainer: mcollard0.
Source: https://github.com/mcollard0/finamp-lyrics
Repository: https://raw.githubusercontent.com/mcollard0/finamp-lyrics/main/manifest.json

Separate Linux builds support Jellyfin 10.11.11 (.NET 9, plugin 1.0.3.0) and
12.x (.NET 10, plugin 2.0.0.0; tested on 12.0 and 12.1). Both require Python 3.10+,
requests, beautifulsoup4, a Genius access token and a Jellyfin API key. The release
includes its worker and setup instructions; catalog installation does not install
Python dependencies or create credentials. Lyrics are extracted from Genius song
pages after API searching. Users must check source permissions and terms. This
is an independent, MIT-licensed project.

Validation: builds have no warnings, 39 plugin checks passed per server line and
72 Python tests passed. Disposable Jellyfin tests install the ZIP through the
catalog API, verify extracted files, Active status, settings/developer links and
real child-worker execution. Tests cover the 1.0.1.0 → 1.0.3.0 catalog upgrade
on 10.11.11 and migration of 10.11.11 data to 12.0 and 12.1 with 2.0.0.0. Earlier
lyrics and settings survive and newly fetched lyrics are stored and retrievable.
Most tests use synthetic audio and fixture Genius responses. A separate 12.1
migration test also verifies real Genius retrieval on a copied authorized library
recording. Production data is untouched. Future 12.x releases require validation.

This request is for the third-party repository directory. Verify public release
and manifest availability, and complete user testing before submitting this text.
