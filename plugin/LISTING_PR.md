Add Finamp Lyrics to the third-party plugin repository list.

Finamp Lyrics queues lyrics checks when Jellyfin music metadata is prefetched or
playback begins. Developer and maintainer: mcollard0.
Source: https://github.com/mcollard0/finamp-lyrics
Repository: https://raw.githubusercontent.com/mcollard0/finamp-lyrics/main/manifest.json

The plugin targets Jellyfin 10.11.11 on Linux and requires Python 3.10+, requests,
beautifulsoup4, an accessible Python worker, a Genius access token, and a Jellyfin
API key. The release includes the worker and setup instructions. Catalog
installation does not install Python dependencies or create credentials.
Lyrics are extracted from Genius song pages after API searching; users must check
source permissions and terms. This is an independent, MIT-licensed project.

Validation: built without warnings; 33 plugin checks passed. A fresh isolated
Jellyfin 10.11.11 instance recognized the catalog, installed the ZIP via the package
API, verified extracted files, loaded version 1.0.1.0 as Active after restart, and
served the settings page with developer/repository links. This integration test
did not contact Genius or exercise playback. No Jellyfin 12.x support is claimed.

This request is for the third-party repository directory, not official catalog
adoption. Public release and manifest availability must be verified before this
request is submitted.
