# Catalog installation

Developer: [mcollard0](https://github.com/mcollard0).
Source and issues: https://github.com/mcollard0/finamp-lyrics.
License: MIT. Independent project; not endorsed by Jellyfin or Finamp.

This build targets Jellyfin 10.11.11 on Linux, with Python 3.10+ and `fcntl`.
It does not support Jellyfin 12.x. No server binaries, credentials, state, or
lyrics are included. Catalog installation installs files only: it does not
install Python dependencies, create credentials, or schedule a batch service.

Once the release and manifest are published, add this URL in Dashboard → Plugins
→ Repositories:

https://raw.githubusercontent.com/mcollard0/finamp-lyrics/main/manifest.json

Install Finamp Lyrics from the catalog and restart Jellyfin when convenient.
The ZIP includes `worker/lyrics_fetcher.py` and `worker/requirements.txt` beneath
the plugin installation directory. Install those dependencies using your system
package manager or a virtual environment accessible to the Jellyfin service.
Set PythonPath to that interpreter and ScriptPath to the bundled worker's absolute
path. Use a separate service-writable StateDirectory outside the plugin directory,
so upgrading the plugin does not discard your cache.

Create a separate credential JSON file outside the plugin directory, owned by
the Jellyfin service account and readable only by that account (mode 0600):

```json
{
  "GENIUS_CLIENT_ACCESS": "YOUR_GENIUS_ACCESS_TOKEN",
  "JELLYFIN_API_KEY": "YOUR_JELLYFIN_API_KEY"
}
```

Configure CredentialsFile and ServerUrl on the plugin settings page. All paths
must be paths inside the server/container; restart is not needed after saving
these settings. Test a single song before enabling background checks. The worker
uses Genius page extraction for lyrics; check provider permissions and terms.
Fetched lyrics can replace existing timed lyrics if the fetched text is longer.
Use the existing worker/cache if already installed instead of creating a second
state directory. Installation does not use the Arch-specific activation helper.
