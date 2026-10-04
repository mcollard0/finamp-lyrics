# finamp-lyrics

A Python worker that fetches Jellyfin music lyrics from Genius, starting with a requested track and then a configurable number of the most-played eligible tracks. It fills missing lyrics and replaces existing lyrics only when the fetched lyric text is strictly longer.

Separate plugin builds target Jellyfin 10.11.11 and 12.x; configure your own server URL. The program uses Jellyfin's existing lyric upload API, which saves a managed lyric file and queues a metadata refresh. MP3 tags and Jellyfin's database are not edited directly.

## Run

Use Python 3.10+ on Linux/macOS and install the dependencies if needed:

```bash
python3 -m pip install -r requirements.txt
```

Credentials are read from environment variables:

- `JELLYFIN_BASE_URL`
- `JELLYFIN_API_KEY`
- `GENIUS_CLIENT_ACCESS`

The API key needs access to user/library information and lyric management. The Genius client ID/secret are not needed for these reads. Credentials never appear in program output or the SQLite cache. Examples below use a local server URL; replace it for your installation.

Read the server configuration and inspect its most-played tracks:

```bash
python3 lyrics_fetcher.py --server-url http://localhost:8096/ --inspect
python3 lyrics_fetcher.py --server-url http://localhost:8096/ --list --top 20
```

Fetch lyrics for 25 of the most-played eligible tracks and publish them to Jellyfin:

```bash
python3 lyrics_fetcher.py --server-url http://localhost:8096/ --top 25 --delay 2 --upload
```

Omit `--upload` to save only in the local cache. Running later with `--upload` publishes the cached text without searching Genius again.

Check a specific Jellyfin song first, then fetch up to 10 background candidates:

```bash
python3 lyrics_fetcher.py --server-url http://localhost:8096/ \
  --priority JELLYFIN_ITEM_ID --top 10 --delay 2 --upload
```

`--priority` can be repeated; use `--top 0` to check only the requested track(s). The priority lookup runs before discovery of the background ranking.

Limit the ranking to a user or music library by name or ID:

```bash
python3 lyrics_fetcher.py --server-url http://localhost:8096/ \
  --user USER_NAME --library Music --top 25 --delay 3 --upload
```

Standalone Genius lookup, without a Jellyfin connection:

```bash
python3 lyrics_fetcher.py --title Swords --artist Leftfield --delay 1
```

## Ranking and limits

By default, the worker sums each track's `UserData.PlayCount` across enabled Jellyfin users in music libraries. `--user` narrows that scope. These are Jellyfin playback-start counts, not total listening time or a calculation from the Playback Reporting plugin.

The paginated query sorts by play count and stops when a user's results reach zero. Leading 1–3 digit track numbers followed by a dash or dot are stripped (`04-`, `004 -`, `04.`). When both track and album artist metadata are missing, `Artist - Song` titles fall back to the first spaced dash separator (`-`, `--`, `–`, or `—`), or the first unspaced dash if no spaced separator exists. This handles `04-shakira-animal city`; unspaced hyphenated artist names can be ambiguous. Numeric prefixes are not treated as artists. Tagged artist metadata takes precedence. Unplayed tracks and tracks still lacking title/artist metadata are not fetched. The ranking is cached for 15 minutes; change `--ranking-minutes` or use `--refresh-ranking` to update it immediately. `--list` always reads a fresh ranking and performs no lookup, upload, or queue mutation.

`--top N` means up to N **eligible background songs per invocation**, ordered by play count. Completed jobs and lookups still in their retry window are skipped. Tracks with existing lyrics are eligible for one comparison. A later batch advances to the next eligible songs. Genuine misses count toward the batch limit.

`--min-plays 2` restricts background fetching and `--list` to tracks played more than once, using summed counts across the selected users. It defaults to 1 and does not restrict explicitly requested `--priority` tracks. For example, `--min-plays 2 --top 187 --delay 2 --upload` processes up to 187 eligible tracks meeting that threshold.

With `--upload`, the worker reads the current server lyrics immediately before publication. Length means the number of Unicode letters and digits, excluding whitespace, punctuation, timestamps, LRC metadata, and conventional bracketed section labels such as `[Chorus]`. Repeated lyric lines count. Equal or shorter candidates leave the server lyrics untouched and produce status `kept`; strictly longer candidates produce `uploaded`. Longer text is a completeness heuristic, not evidence of accuracy. Without `--upload`, candidates are saved locally and server lyrics are untouched.

The worker uses the current lyric file's format (`txt`, `lrc`, or `elrc`) when replacing it, avoiding an older timed file taking precedence over a new TXT file. Genius supplies untimed text, so a replacement loses existing synchronization. Storage follows Jellyfin's library settings; a separately named or read-only external lyric file may still take precedence, so verify the visible lyrics after publication. The server API has no atomic comparison-and-write operation: another writer can change lyrics between the fresh read and upload.

`--delay` (alias `--initial-delay`) defaults to 0.5 seconds. Each subsequent HTTP request adds `--delay-step` (default 0.25), capped at `--max-delay` (default and hard maximum 10 seconds). Both API searches and lyric pages count, including retries. The ramp resets each worker execution; the ramp counter is never persisted. Shared pacing and HTTP 429 backoff remain persisted to coordinate workers and honor provider retry times. Request logs now include HTTP status, endpoint type, delay, and the actual Retry-After header.

## De-bouncing and shared queue

Keep one shared `--state-dir` for all invocations. Its default is `state/` alongside the script.

| Result | Stored behavior |
| --- | --- |
| Lyrics found | Keep a file and hash while publication is unfinished; successful publication removes unreferenced cache data |
| No exact song match | Cache the miss for 30 days; change `--missing-days` |
| Network, API, or parser failure | Cache a temporary error for one hour; change `--error-hours` |
| Upload failure | Keep fetched text; retry publication without a new Genius lookup |
| HTTP 429 | Persist the provider cooldown, end the worker run, and leave other jobs queued |
| Existing Jellyfin lyrics | Look up once; keep equal/shorter candidates and replace only strictly longer text with `--upload`; retain a compact durable success stub |

No dummy lyric files are created. A fake file could cause Jellyfin/Finamp to display a failure message as lyrics or suppress later successful retrieval.

`state/state.sqlite3` records lookup status, attempt/retry times, source URL, a 16-byte MD5 digest of each lyric, the item queue and ranking snapshots. Lyric bodies are stored only in `state/lyrics/<lookup-hash>.txt`; they are not duplicated in SQLite. The filename uses the existing SHA-256 title/artist identity key, while MD5 verifies the UTF-8 file content. Missing or changed cache files fail explicitly instead of being silently reused. Writes use atomic replacement and fsync before committing the hash. Legacy databases automatically migrate lyric text into files and rebuild the lookup table, preserving IDs, timestamps, statuses and retry deadlines. Back up the database and lyric directory together: hashes alone cannot reconstruct lyrics, and future provider responses may change. `state/worker.lock` provides a process lock; SQLite handles queue insertion while another worker is active.

Each incoming priority job is promoted to the front of pending work by refreshing its queue timestamp. Priority requests run newest first; background jobs retain descending play-count order. Re-requesting an existing pending job updates it without inserting a duplicate. An already active lookup finishes before the newly queued priority starts. Other invocations wait for the worker lock and then drain any remaining jobs, so there is no handoff gap that loses a request. A crashed worker's unfinished jobs are recovered on the next invocation. Each worker also promotes expired errors/misses back to pending, preserving their priority. A retry deadline makes a job eligible; processing can still wait for an active job, the shared worker lock, or provider cooldown. Use a local state directory shared by all invocations, not a separate cache per process.

Use `--retry` with `--priority ITEM_ID` to override a cached miss/error immediately. Successful `uploaded`/`kept` tracks are permanent successes: `--retry` does not reopen them, even if metadata or server lyric files later change. Success retains only server/item identifiers, status and queue/completion time; metadata JSON, lookup key, play count, publish flag and reason are cleared. Unreferenced positive lookups and their cache files are removed. Shared cache data is retained only while another incomplete or local-only job needs it. Failures retain identifiers, reason and retry deadline; workers resolve metadata through Jellyfin at retry time; misses retry after 30 days, transient errors after one hour (configurable), subject to the provider cooldown. Log files retain detailed outcomes. Existing server lyrics are replaced only if the candidate is strictly longer. Jobs marked `existing` by the earlier program are eligible for their first comparison under this policy. Lookup matching normalizes punctuation/case and recognizes explicit featured-artist credits; it retains remix/live/version words and rejects ambiguous exact matches.

This still uses the public Genius song page for lyric content. An inaccessible page or changed HTML is treated as a temporary error, never as proof that a song has no lyrics. See [third-party software and content](THIRD_PARTY.md) for source-use responsibilities.

## Observability

Every fetching/enqueue invocation appends JSON records to `events.jsonl` in its shared state directory and emits them to stdout (Jellyfin logs or the systemd journal). The live path is `/var/lib/jellyfin/finamp-lyrics/state/events.jsonl`; workspace runs default to `state/events.jsonl`.

Records include UTC `timestamp`, Unix `epoch` seconds, `run_id`, and `pid`. `run_started` proves Python was invoked, including enqueue-only calls; `run_finished` records pass/fail and exit code. Track events include `item_id`, `title`, `artists`, and queue/start/result/skip details. Results report pass/fail, status, reason, `lyrics_length` (normalized letter/digit count used for comparison), and `lyrics_characters` (raw character count). Failed searches have length zero; unavailable lengths on skipped jobs are null. `kept` is a successful comparison with `skipped: true`. Completed lyric jobs, local saved lyrics, missing metadata and retry cooldowns have explicit skip reasons. A cached file alone does not prevent longer-lyric comparisons.

The plugin logs received playback/prefetch triggers, queue acceptance, duplicate suppression, library exclusions, process PID, worker output, and exit code. Correlate item IDs and PIDs with the worker run IDs. These triggers apply to eligible Jellyfin clients; they do not by themselves identify a request as originating from Finamp.

```bash
sudo tail -f /var/lib/jellyfin/finamp-lyrics/state/events.jsonl
sudo journalctl -u jellyfin -f | rg 'Finamp Lyrics'
sudo journalctl -u finamp-lyrics-continuous -f
```

Logs contain song metadata, never credentials or lyric bodies. Writes are serialized across processes. On the first write of a new local calendar day, the active file is archived as `events.YYYY-MM-DD.jsonl`. Today and six preceding days are retained; older archives are deleted under a shared lock. This retention applies to application files; systemd manages its journal separately. Read-only `--list`, `--inspect`, and `--verify` retain their existing output and do not create run logs. Existing long-running workers pick up changes on their next process launch.

## Jellyfin plugin

The optional plugin queues work on metadata-prefetch and playback events. It
launches child processes without a shell and reads credentials from a protected
service file. See [plugin build and installation](plugin/README.md).

## Compact queue schema

Schema version 3 stores each server identity once in `servers`, referenced by a small integer. Jellyfin item IDs and SHA-256 lookup keys are stored as binary bytes; their external hex representation and lyric-cache filenames are unchanged. `jobs` and `lookups` use `WITHOUT ROWID`. The partial `pending_order` index serves the newest-priority-first queue query without sorting all pending jobs.

Remote queue entries contain only server/item identity, lookup key, status, priority, play count, publication flag, request/completion time, retry deadline and error reason. They contain no title/artist/album/path snapshot. Workers resolve current metadata through Jellyfin before fetching lyrics. Metadata failures keep the job with an error and retry deadline, and do not send a Genius request. Lookup keys are updated if metadata changes. Standalone jobs retain only a small local title/artist record because they have no Jellyfin metadata source. Success stubs remain permanent and clear their lookup key and other work data.

Legacy schema migrations are transactional. Stop old workers and back up the database
and lyric cache before upgrading; older executables cannot read the compact schema.

## SQLite maintenance

Once per local calendar month, on the first worker to acquire the shared lock, the application runs `PRAGMA quick_check`, `PRAGMA optimize`, and a passive WAL checkpoint. The completed month is committed in SQLite so restarts do not repeat maintenance. A missed first-of-month run catches up later. Jobs and cached lyrics are retained. Full `VACUUM` and `REINDEX` are not routine operations: they can block concurrent queue insertion and are unnecessary for normal planner/WAL maintenance.

## Service installation and tests

The Python worker requires `requests`, `beautifulsoup4`, and `fcntl` on Linux.
Run `python3 -m unittest -q test_lyrics_fetcher.py` for the automated checks.

`service_setup.py` prints an installation plan by default. With `--install`, it
requires root, backs up/replaces worker files and writes the systemd batch unit.
It requires an existing service credential file and initialized shared cache.
`--start` starts a batch; `--schedule` enables the supplied daily timer and may
run a catch-up batch. Review its timezone and all paths before enabling it.
Continuous mode is available through `service_runner.py --continuous`.
See [service operation](systemd/README.md).

Genius searching uses authenticated API requests; full lyrics are extracted from
song-page HTML. Provider permissions and terms must be checked for your use.
The software license does not grant rights to lyrics. No lyric samples are shipped.

## Private configuration

Copy `config.example.json` to `config.json` for the service runner. Set the credential-file location, state directory, server URL, and worker limits there; `--config` selects another file and explicit command-line options override its settings. `config.json` and credential files are ignored by Git. Keep tokens in the separate protected credential file, never in the example. The plugin exposes its own installation settings in Jellyfin; standard paths shown in this documentation are examples.

## Plugin distribution

Developed by [mcollard0](https://github.com/mcollard0). See [catalog installation](plugin/CATALOG_INSTALL.md) and [release preparation](plugin/RELEASE.md). Separate Linux plugin builds support Jellyfin 10.11.11 (.NET 9) and 12.x (.NET 10). See [Jellyfin 12 upgrade testing](plugin/JF12.md).

Published packages: [1.0.3.0 for Jellyfin 10.11.11](https://github.com/mcollard0/finamp-lyrics/releases/tag/plugin-v1.0.3.0) and [2.0.0.0 for Jellyfin 12.x](https://github.com/mcollard0/finamp-lyrics/releases/tag/plugin-v2.0.0.0).
Add this catalog URL in Jellyfin Dashboard → Plugins → Repositories; Jellyfin selects the compatible build:

```text
https://raw.githubusercontent.com/mcollard0/finamp-lyrics/main/manifest.json
```
