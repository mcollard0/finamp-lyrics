# Finamp Lyrics plugin

The Jellyfin plugin queues the Python worker after successful authenticated `GET`/`POST /Items/{id}/PlaybackInfo` requests and on `ISessionManager.PlaybackStart`. Finamp uses that metadata endpoint when prefetching its queue. The middleware returns the normal response without waiting for a Genius lookup, and the playback event only queues work. All clients using those endpoints can trigger checks, not just Finamp.

Both triggers restrict items to audio inside music collection folders. Optional library IDs further narrow the scope. A requested song is checked regardless of its play count; background songs default to at least two summed plays. The Python worker resolves the title and artists from the Jellyfin item ID, retains the conservative Genius matching policy, and replaces lyrics only if the candidate has more lyric text.

The defaults are a two-second gap between Genius HTTP requests and three eligible background tracks per worker launch. Simultaneous triggers are grouped while a worker is active; the background count is not multiplied for every prefetch in that group. A 30-second per-item debounce combines metadata and playback events. SQLite preserves successful results and miss/error retry windows beyond that interval.

The service launches a short `--enqueue-only` process for each accepted request, which promotes the item into SQLite without waiting for the worker lock or contacting Genius. A single owned fetching process checks priorities first and drains them ahead of background jobs. If a request arrives as that process exits, a final drain closes the handoff gap. Child arguments use `ProcessStartInfo.ArgumentList`, and tokens go only into the child environment. Worker output is recorded in Jellyfin logs. Failed requests leave playback running.

## Build and checks

Two builds share the same C# source. Jellyfin 10.11.11 uses .NET 9 and plugin
1.0.3.0; Jellyfin 12.x uses .NET 10 and plugin 2.0.0.0. The latter is compiled
against 12.0 and tested on 12.0 and 12.1. Supply compatible server reference
assemblies yourself; they are not distributed in this repository. See
[release instructions](RELEASE.md) for extracting them from Docker images and
building both packages. With the references in place, run from the repository root:

```bash
dotnet build plugin/FinampLyrics/FinampLyrics.csproj -c Release -p:JellyfinLine=10.11 -o plugin/artifacts/build-10.11
dotnet run --project plugin/Checks/Checks.csproj -c Release -p:JellyfinLine=10.11
dotnet build plugin/FinampLyrics/FinampLyrics.csproj -c Release -p:JellyfinLine=12 -o plugin/artifacts/build-12
dotnet run --project plugin/Checks/Checks.csproj -c Release -p:JellyfinLine=12
python3 -m unittest -q test_lyrics_fetcher.py
```

The checks cover authentication and response status, exact endpoint matching, downstream response completion, playback subscription/unsubscription, ignored non-audio events, disabled triggers, command arguments, credential handling, actual process dispatch, music/library scoping, concurrent worker promotion, and final drain behavior. The Python suite verifies cache, pacing, matching, publication, and enqueue-only behavior.

## Installation and configuration

Use [catalog installation](CATALOG_INSTALL.md) for portable installation, or
[Jellyfin 12 testing and upgrade](JF12.md) when migrating an existing server.
The historical installer below pins an Arch installation to 10.11.11; do not
use it to install or update a Jellyfin 12 plugin.

`install.py` performs a read-only preflight by default. Activation requires root and the existing `GENIUS_CLIENT_ACCESS` and `JELLYFIN_API_KEY` environment values. The installer checks for active playback before restarting, copies the cache using SQLite's backup API, stores credentials in a file readable only by the service user, installs the plugin, restarts, and verifies the plugin is active through Jellyfin's API.

```bash
python3 plugin/install.py
sudo --preserve-env=GENIUS_CLIENT_ACCESS,JELLYFIN_API_KEY python3 plugin/install.py --activate
```

Installed paths:

| Purpose | Path |
| --- | --- |
| Plugin | `/var/lib/jellyfin/plugins/Finamp Lyrics_1.0.0.0/` |
| Script | `/var/lib/jellyfin/finamp-lyrics/lyrics_fetcher.py` |
| Shared live state | `/var/lib/jellyfin/finamp-lyrics/state/` |
| Service credential file | `/var/lib/jellyfin/finamp-lyrics/credentials.json` |
| Settings | Jellyfin Dashboard → Plugins → Finamp Lyrics |
| Service override | `/etc/systemd/system/jellyfin.service.d/finamp-lyrics.conf` |

The settings page exposes the worker executable/interpreter, script path and additional arguments, along with the server URL, shared state, credentials file and timeout. The command preview updates as you edit. Settings are saved through Jellyfin's plugin configuration API; existing installations retain their paths and use an empty additional-arguments list until configured.

| Setting | Default | Example setting |
| --- | --- | --- |
| Worker executable / interpreter | `/usr/bin/python3` | Same |
| Worker script | Empty: resolve the active bundled worker | Leave empty for catalog installs; set a stable absolute path for an external worker |
| Additional arguments | Empty | Optionally `--retry-429` |
| Server URL | `http://localhost:8096/` | Same |
| Shared state | `/var/lib/jellyfin/finamp-lyrics/state` | Same; shares the continuous worker's cache |
| Credentials file | `/var/lib/jellyfin/finamp-lyrics/credentials.json` | Same |
| Background songs | `3` | `0` while the continuous batch runs; `3` for ordinary use |
| Minimum background plays | `2` | `2` |
| Delay | `2` seconds | `2`, or `1` with `--retry-429` for faster fetching |
| Worker timeout | `15` minutes | `15`; increase for longer backoff waits |

Additional arguments use one argument per line, with no shell quoting. For example:

```text
--retry-429
--ranking-minutes
30
```

This enables rate-limit retries and a 30-minute ranking cache. Default invocation, shown here with shell quoting for readability:

```bash
/usr/bin/python3 /var/lib/jellyfin/finamp-lyrics/lyrics_fetcher.py \
  --server-url http://localhost:8096/ \
  --state-dir /var/lib/jellyfin/finamp-lyrics/state \
  --priority SONG_ITEM_ID --upload --delay 2 --min-plays 2 --top 3
```

The actual song ID is supplied at each trigger. The short enqueue call automatically uses `--top 0 --enqueue-only`. Credentials are supplied in the child environment. A replacement executable/script must support this queue interface. Additional arguments cannot override the dedicated fields or switch to standalone/list/inspection modes; those would break priority handoff.

To deploy a rebuilt DLL onto this existing installation without replacing worker files, credentials, cache or server pin:

```bash
sudo python3 plugin/install.py --activate --update-only
```

The update checks for active playback, backs up the DLL, restarts Jellyfin and verifies that the new configuration property is present. It restores the old DLL if activation fails.

Add `--stage-only` to copy the new DLL while playback continues. The settings appear after Jellyfin next restarts. When an immediate restart during playback is explicitly requested, `--allow-active-playback` overrides the playback check.

The activation installer is specialized for Arch Linux Jellyfin 10.11.11 packages.
It copies server/web files and writes a service override that pins that version.
`--activate` can restart Jellyfin; it is not a portable installation command.
Review package paths and service changes before use. The packaged 12.x build
instead uses corresponding .NET 10 assemblies and the catalog/manual installation
described above.

Future checks can be disabled immediately using the plugin's Enable checkbox. A currently active Python lookup may finish. Removing the plugin DLL and restarting the server uninstalls the trigger while preserving its cached lyrics. Select the package matching the server version when upgrading.

New lyric files do not force Finamp to invalidate metadata already held in memory. Prefetch provides an earlier opportunity to fetch, but the first metadata response still returns promptly and may precede publication. Newly fetched lyrics may become visible after Finamp reloads the track. Offline playback without server requests cannot trigger the server plugin.

## Developer and releases

Developer: [mcollard0](https://github.com/mcollard0). [Source and issues](https://github.com/mcollard0/finamp-lyrics). MIT licensed.

See [catalog setup](CATALOG_INSTALL.md) and [release and listing steps](RELEASE.md).
The published catalog is https://raw.githubusercontent.com/mcollard0/finamp-lyrics/main/manifest.json.
Jellyfin selects the compatible package for 10.11.11 or 12.x.
