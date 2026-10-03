# 100-song lyric batch

`finamp-lyrics-batch.service` runs one batch of up to 100 new eligible music tracks, with a two-second delay between Genius HTTP requests. It runs as `jellyfin`, loads the installed service-only credential file, and shares `/var/lib/jellyfin/finamp-lyrics/state/` with the playback plugin. Both use the same SQLite cache and process lock. Cached positives are reused, misses/errors honor their retry windows, and publication replaces existing lyrics only when the candidate text is strictly longer.

The batch first selects remaining played tracks by summed Jellyfin play count, including tracks played once. If fewer than 100 are eligible, it fills remaining slots using registered music files ordered by filesystem timestamps. It checks only audio inside configured music-library locations, excludes missing files and paths outside those roots, and never reads audio contents just to rank files. Access time is used on mounts that maintain it; modification time is used when the mount has `noatime`. On drives mounted with `noatime`, the fallback uses modification time. Even when enabled, access time can reflect scans or backups rather than listening. Unindexed files require a Jellyfin library scan before this worker can publish lyrics for them.

The supplied `finamp-lyrics-batch.timer` runs the service daily at **02:00 America/Chicago**, following Central daylight/standard time. Its persistent setting runs one catch-up batch when the machine starts after a missed scheduled run. Each scheduled run processes up to 100 eligible songs with the existing cache and two-second delay.

Check the schedule or start another batch manually:

```bash
sudo systemctl start finamp-lyrics-batch.service
systemctl list-timers finamp-lyrics-batch.timer
systemctl status finamp-lyrics-batch.service
sudo journalctl -u finamp-lyrics-batch.service -n 30 --no-pager
```

This is a `Type=oneshot` service. While a batch runs, its status is `activating (start)`; after successful completion it becomes `inactive (dead)` with `Result=success`. Starting it again selects the next eligible tracks. Enable the timer explicitly if boot scheduling is desired. A running service is not restarted when `start` is called again. Playback plugin priorities can arrive during a batch and are handled before background jobs.

Per-song parsing or publication failures are logged and cached but do not fail a fully drained batch. Configuration errors, failed catalog discovery, or a provider cooldown that leaves pending work cause a nonzero service exit. The service does not immediately retry a failed run; the daily timer invokes it again, or it can be started manually after the recorded retry/cooldown interval. A stop or timeout leaves queued work recoverable on the next worker invocation.

Change the count/delay using a systemd override:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /var/lib/jellyfin/finamp-lyrics/service_runner.py --top 100 --delay 2
```

Apply unit changes with `sudo systemctl daemon-reload`. Stop a batch with `sudo systemctl stop finamp-lyrics-batch.service`. The lyrics already published remain available.

Source files are `service_runner.py`, `service_setup.py`, and `systemd/finamp-lyrics-batch.service`. Installation copies the updated worker atomically, preserves the live cache and credentials, backs up previous files, validates the unit, and starts it without changing Jellyfin's service configuration:

```bash
sudo python3 service_setup.py --install --start
```

Install and enable the daily schedule with `sudo python3 service_setup.py --install --schedule`. Disable the schedule with `sudo systemctl disable --now finamp-lyrics-batch.timer`.

## Continuous manual worker

Use `service_runner.py --continuous` with your private JSON configuration for a
persistent worker. Creating a continuous systemd unit is a separate administrator
step; the repository does not install one automatically.

Continuous mode retries the same Genius HTTP request after a 429. Waits double from 60 seconds through 120, 240, etc., capped at 3,600 seconds; a longer provider `Retry-After` takes precedence. Both numeric and HTTP-date headers are supported. Backoff and its consecutive-failure count are saved in SQLite; a successful response resets the count. All invocations honor the shared cooldown. The retry behavior respects the rate-limit signal defined in [RFC 6585](https://www.rfc-editor.org/rfc/rfc6585.html#section-4).

After a completed batch, exhausted candidates, or an ordinary recoverable batch failure, continuous mode waits 15 minutes before the next pass. It keeps running until stopped, without repeatedly looking up successful songs. The daily 2am timer retains its existing 100-song/two-second settings; overlapping processes use the same worker lock. The continuous service is transient and is not enabled at boot.

```bash
systemctl status finamp-lyrics-continuous.service
sudo journalctl -fu finamp-lyrics-continuous.service
sudo systemctl stop finamp-lyrics-continuous.service
```
