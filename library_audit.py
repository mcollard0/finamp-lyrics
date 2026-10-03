#!/usr/bin/env python3
"""Read-only inventory of the music folder, Jellyfin catalog, and lyric cache."""
import collections
import argparse
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import time

from lyrics_fetcher import Jellyfin, Track, unpack_id


def save_report(name, report):
    destination = Path(__file__).with_name(name)
    destination.write_text(json.dumps(report, indent=2) + '\n')
    os.chmod(destination, 0o644)
    if 'SUDO_UID' in os.environ:
        os.chown(destination, int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))


def performance():
    units = ['finamp-lyrics-batch.service', 'finamp-lyrics-manual-1000.service',
             'finamp-lyrics-continuous.service']
    metrics = {}
    data = Path('/var/lib/jellyfin/finamp-lyrics')
    db = sqlite3.connect(f'file:{data}/state/state.sqlite3?mode=ro', uri=True)
    scope = Jellyfin('http://localhost:8096/', 'unused').scope
    scope_row = db.execute('SELECT id FROM servers WHERE identity=?', (scope,)).fetchone()
    scope_id = scope_row[0] if scope_row else -1
    attempts = {unpack_id(item): attempted for item, attempted in db.execute('SELECT j.item,l.attempted FROM jobs j JOIN lookups l ON l.key=j.key WHERE j.server=?', (scope_id,))}
    for unit in units:
        output = subprocess.run(['journalctl', '-u', unit, '--since', '2026-09-30 16:20:00',
                                 '--no-pager', '-o', 'json',
                                 '--output-fields=MESSAGE,__REALTIME_TIMESTAMP'],
                                check=True, capture_output=True, text=True).stdout
        rows = []
        for line in output.splitlines():
            try:
                raw = json.loads(line)
                rows.append((int(raw['__REALTIME_TIMESTAMP']) / 1e6, json.loads(raw['MESSAGE'])))
            except (ValueError, KeyError, TypeError):
                continue
        if unit == units[-1]:
            starts = [t for t, m in rows if 'service_batch' in m]
            if starts:
                rows = [(t, m) for t, m in rows if t >= starts[-1]]
        songs = [(t, m) for t, m in rows if 'item_id' in m and 'status' in m]
        gaps = [b[0] - a[0] for a, b in zip(songs, songs[1:])]
        mean = statistics.mean(gaps) if gaps else None
        metrics[unit] = {'song_results': len(songs), 'statuses': dict(collections.Counter(m['status'] for _, m in songs)),
                         'active_span_seconds': songs[-1][0] - songs[0][0] if songs else 0,
                         'seconds_per_song': mean, 'median_seconds_per_song': statistics.median(gaps) if gaps else None,
                         'first_result': songs[0][0] if songs else None,
                         'last_result': songs[-1][0] if songs else None,
                         'latest': rows[-1][1] if rows else None}
        fresh_gaps = [b[0] - a[0] for a, b in zip(songs, songs[1:])
                      if a[0] < attempts.get(b[1]['item_id'], 0) <= b[0]]
        cached_gaps = [b[0] - a[0] for a, b in zip(songs, songs[1:])
                       if attempts.get(b[1]['item_id'], 0) <= a[0]]
        metrics[unit]['fresh_lookup_intervals'] = len(fresh_gaps)
        metrics[unit]['fresh_lookup_mean_seconds'] = statistics.mean(fresh_gaps) if fresh_gaps else None
        metrics[unit]['cached_intervals'] = len(cached_gaps)
        metrics[unit]['cached_mean_seconds'] = statistics.mean(cached_gaps) if cached_gaps else None
        if mean:
            metrics[unit]['estimates_hours'] = {str(n): n * mean / 3600 for n in (65535, 42143)}
    now = time.time()
    queue = dict(db.execute('SELECT status,count(*) FROM jobs WHERE server=? GROUP BY status', (scope_id,)))
    pending = queue.get('pending', 0) + queue.get('running', 0)
    searches = db.execute("""SELECT count(DISTINCT j.key) FROM jobs j LEFT JOIN lookups l ON l.key=j.key
                             WHERE j.server=? AND j.status IN ('pending','running')
                             AND (l.key IS NULL OR (l.status!='found' AND l.retry_at<=?))""", (scope_id, now)).fetchone()[0]
    report = {'generated': now, 'runs': metrics, 'live_jobs': queue,
              'remaining_files': pending, 'remaining_unique_genius_checks': searches}
    current = metrics[units[-1]]
    if current['seconds_per_song']:
        report['remaining_hours_at_observed_file_rate'] = pending * current['seconds_per_song'] / 3600
    if current['fresh_lookup_mean_seconds'] is not None and current['cached_mean_seconds'] is not None:
        report['remaining_hours_with_duplicate_cache_reuse'] = (
            searches * current['fresh_lookup_mean_seconds'] +
            (pending - searches) * current['cached_mean_seconds']) / 3600
    save_report('performance-audit.json', report)
    print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--performance-only', action='store_true')
    args = parser.parse_args()
    if args.performance_only:
        performance()
        return
    data = Path('/var/lib/jellyfin/finamp-lyrics')
    credentials = json.loads((data / 'credentials.json').read_text())
    jf = Jellyfin('http://localhost:8096/', credentials['JELLYFIN_API_KEY'])
    roots = {str(Path(p).resolve()) for lib in jf.libraries for p in lib.get('Locations', [])}
    extensions = collections.Counter()
    disk_paths = set()
    scan_errors = []
    for root in sorted(roots):
        for folder, _, files in os.walk(root, onerror=lambda e: scan_errors.append(str(e))):
            for name in files:
                path = Path(folder) / name
                if path.is_file():
                    extensions[path.suffix.lower()] += 1
                    disk_paths.add(str(path.resolve()))
    # Media formats present in ordinary music libraries; descriptors aren't audio files.
    audio_exts = set('.669 .3gp .aa .aac .aax .ac3 .act .adp .adplug .adx .afc .amf .aif .aiff .alac .amr .ape .ast .au .awb .cda .dmf .dsf .dsm .dsp .dts .dvf .eac3 .ec3 .far .flac .gdm .gsm .gym .hps .imf .it .m15 .m4a .m4b .mac .med .mka .mmf .mod .mogg .mp2 .mp3 .mpa .mpc .mpp .mp+ .msv .nmf .nsf .nsv .oga .ogg .okt .opus .ra .rf64 .rm .s3m .sfx .shn .sid .stm .ult .uni .vox .wav .wma .wv .xm .ymf'.split())
    disk_audio = {p for p in disk_paths if Path(p).suffix.lower() in audio_exts}
    report = {'generated': time.time(), 'roots': sorted(roots), 'disk_files': sum(extensions.values()),
              'mp3_files': extensions['.mp3'], 'audio_files': len(disk_audio),
              'extensions': dict(extensions.most_common()), 'scan_errors': scan_errors}
    print(json.dumps({'disk_inventory': report}), flush=True)
    items = {}
    for lib in jf.libraries:
        start = 0
        while True:
            page = jf.get('/Items', ParentId=lib['ItemId'], Recursive='true', IncludeItemTypes='Audio',
                          Fields='Path', EnableUserData='false', SortBy='SortName', StartIndex=start, Limit=5000)
            batch = page.get('Items', [])
            items.update({i['Id']: i for i in batch})
            start += len(batch)
            if start % 10000 == 0:
                print(json.dumps({'catalog_read': start}), flush=True)
            if not batch or start >= page.get('TotalRecordCount', start):
                break
    db = sqlite3.connect(f'file:{data}/state/state.sqlite3?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    scope_row = db.execute('SELECT id FROM servers WHERE identity=?', (jf.scope,)).fetchone()
    scope_id = scope_row[0] if scope_row else -1
    jobs = {unpack_id(r['item']): {**dict(r), 'key': unpack_id(r['key'])} for r in db.execute('SELECT item,key,status,retry_at FROM jobs WHERE server=?', (scope_id,))}
    lookups = {unpack_id(r['key']): r for r in db.execute('SELECT key,status,retry_at FROM lookups')}
    statuses = collections.Counter()
    bad = collections.Counter()
    examples = []
    object_artist_examples = []
    catalog_paths = set()
    valid_keys = set()
    new_keys = set()
    pending_keys = set()
    now = time.time()
    for i in items.values():
        if i.get('Path'):
            catalog_paths.add(str(Path(i['Path']).resolve()))
        title = i.get('Name')
        raw_artists = i.get('Artists') or i.get('AlbumArtists') or []
        if any(isinstance(a, dict) for a in raw_artists) and len(object_artist_examples) < 5:
            object_artist_examples.append({'id': i['Id'], 'title': title, 'artists': raw_artists})
        track = Track.from_item(i)
        artists = track.artists
        if not isinstance(title, str) or not title.strip():
            bad['missing_title'] += 1
        if not isinstance(artists, list) or not any(isinstance(a, str) and a.strip() for a in artists):
            bad['missing_artist'] += 1
        if not isinstance(title, str) or not title.strip() or not isinstance(artists, list) or not any(isinstance(a, str) and a.strip() for a in artists):
            statuses['missing_metadata'] += 1
            if len(examples) < 5:
                examples.append({'id': i['Id'], 'title': title, 'artists': artists})
            continue
        key = track.key
        valid_keys.add(key)
        job = jobs.get(i['Id'])
        lookup = lookups.get(key)
        if job and job['key'] == key and job['status'] in ('pending', 'running'):
            statuses[job['status']] += 1
            if not lookup or (lookup['status'] != 'found' and lookup['retry_at'] <= now):
                pending_keys.add(key)
        elif job and job['status'] in ('uploaded', 'kept'):
            statuses['completed'] += 1
        elif (job and job['key'] == key and job['retry_at'] > now) or (lookup and lookup['status'] != 'found' and lookup['retry_at'] > now):
            statuses['deferred_cached_attempt'] += 1
        else:
            statuses['eligible'] += 1
            if not lookup or lookup['status'] != 'found':
                new_keys.add(key)
    report.update({'catalog_audio_items': len(items), 'indexed_paths_on_disk': len(catalog_paths & disk_paths),
                   'unindexed_audio_files': len(disk_audio - catalog_paths),
                   'unindexed_audio_paths': sorted(disk_audio - catalog_paths),
                   'catalog_missing_files': len(catalog_paths - disk_paths),
                   'metadata_problems': dict(bad), 'metadata_examples': examples,
                   'object_artist_examples': object_artist_examples,
                   'cache_snapshot': dict(statuses), 'unique_valid_songs': len(valid_keys),
                   'remaining_unique_genius_checks': len(new_keys | pending_keys)})
    save_report('library-audit.json', report)
    print(json.dumps({'audit': report}), flush=True)


if __name__ == '__main__':
    main()
