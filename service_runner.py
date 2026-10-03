#!/usr/bin/env python3
"""Run bounded batches or a continuous worker using the plugin's shared state."""
import argparse
import json
import os
import time
import traceback
from pathlib import Path

import lyrics_fetcher


def main(argv=None):
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    initial, _ = bootstrap.parse_known_args(argv)
    settings = json.loads(initial.config.read_text()) if initial.config.is_file() else {}
    parser = argparse.ArgumentParser(description=__doc__, parents=[bootstrap])
    parser.add_argument('--top', type=int, default=100)
    parser.add_argument('--delay', type=float, default=0.5)
    parser.add_argument('--delay-step', type=float, default=0.25)
    parser.add_argument('--max-delay', type=float, default=10)
    parser.add_argument('--continuous', action='store_true', help='Keep running until stopped; retry 429 with backoff')
    parser.add_argument('--idle-seconds', type=float, default=900, help='Pause between completed batches in continuous mode')
    parser.add_argument('--credentials-file', type=Path,
                        default=Path('/var/lib/jellyfin/finamp-lyrics/credentials.json'))
    parser.add_argument('--state-dir', type=Path, default=Path('/var/lib/jellyfin/finamp-lyrics/state'))
    parser.add_argument('--server-url', default='http://localhost:8096/')
    parser.set_defaults(**{k: v for k, v in settings.items() if k in {a.dest for a in parser._actions}})
    args = parser.parse_args(argv)
    args.credentials_file = Path(args.credentials_file)
    args.state_dir = Path(args.state_dir)
    if args.top < 1 or not 0 <= args.delay <= args.max_delay <= 10 or args.delay_step < 0 or args.idle_seconds <= 0:
        parser.error('top >= 1, delay 0–max-delay <= 10, delay-step >= 0, and idle-seconds > 0 are required')
    try:
        credentials = json.loads(args.credentials_file.read_text())
        for name in ('GENIUS_CLIENT_ACCESS', 'JELLYFIN_API_KEY'):
            if not isinstance(credentials.get(name), str) or not credentials[name].strip():
                raise ValueError('Missing required credentials')
            os.environ[name] = credentials[name]
    except (OSError, ValueError, AttributeError) as exc:
        print(json.dumps({'service_error': type(exc).__name__}), flush=True)
        return 1
    arguments = ['--server-url', args.server_url, '--state-dir', str(args.state_dir),
        '--top', str(args.top), '--delay', str(args.delay), '--delay-step', str(args.delay_step),
        '--max-delay', str(args.max_delay), '--min-plays', '1', '--refresh-ranking',
        '--disk-fallback', '--allow-song-errors', '--upload']
    if args.continuous:
        arguments.append('--retry-429')
    while True:
        print(json.dumps({'service_batch': {'limit': args.top, 'delay_seconds': args.delay,
                          'played_first': True, 'disk_fallback': True, 'continuous': args.continuous}}), flush=True)
        try:
            result = lyrics_fetcher.main(arguments)
        except Exception as exc:
            if not args.continuous:
                raise
            frames = traceback.extract_tb(exc.__traceback__)
            print(json.dumps({'batch_exception': type(exc).__name__, 'frames': [
                {'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                for f in frames]}), flush=True)
            result = 1
        if not args.continuous:
            return result
        print(json.dumps({'continuous_wait': {'seconds': args.idle_seconds, 'last_exit_code': result}}), flush=True)
        time.sleep(args.idle_seconds)


if __name__ == '__main__':
    raise SystemExit(main())
