#!/usr/bin/env python3
"""Queue Genius lyrics for Jellyfin music libraries. Credentials stay in headers."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone, date, timedelta
import uuid
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import unicodedata
from urllib.parse import quote, urlsplit

import requests
from bs4 import BeautifulSoup


_run_id = ContextVar("run_id", default=None)


def audit(directory, event, track=None, **fields):
    """Append one JSON record under a process lock; never log credentials/text."""
    timestamp = time.time()
    record = {"event": event, "epoch": timestamp,
              "timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
              "run_id": _run_id.get(), "pid": os.getpid()}
    if track is not None:
        record.update(item_id=track.id, title=track.title, artists=track.artists)
    record.update(fields)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    # A stable lock file coordinates append, rename and retention across processes.
    with (directory / "events.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        recycle_logs(directory, timestamp)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
    print(line, flush=True)


def recycle_logs(directory, timestamp):
    """Called with events.lock held; retain today plus six preceding local dates."""
    today = datetime.fromtimestamp(timestamp).date()
    current = directory / "events.jsonl"
    if current.exists():
        previous = datetime.fromtimestamp(current.stat().st_mtime).date()
        if previous < today:
            archive = directory / f"events.{previous.isoformat()}.jsonl"
            # Append if an archive already exists; never overwrite historical records.
            with archive.open("ab") as target, current.open("rb") as source:
                import shutil
                shutil.copyfileobj(source, target)
            current.unlink()
    cutoff = today - timedelta(days=6)
    for archive in directory.glob("events.*.jsonl"):
        try:
            archive_day = date.fromisoformat(archive.name[7:-6])
        except ValueError:
            continue
        if archive_day < cutoff:
            archive.unlink()


class FetchError(Exception):
    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(re.sub(r"[^\w]+", " ", "".join(
        char for char in value if not unicodedata.combining(char)
    )).split())


def lead_artist(value: str) -> str:
    # Jellyfin tags often combine the main artist with explicit featured credits.
    # Do not split "&" or "and": those can be part of the band's actual name.
    return re.split(r"\s+(?:[\[(]\s*)?(?:featuring|feat\.|ft\.)\s+",
                    value, maxsplit=1, flags=re.IGNORECASE)[0].strip()


def lyric_length(value: str) -> int:
    """Count lyric letters/digits, excluding formatting and LRC metadata."""
    value = re.sub(r"\[\d{1,3}:\d{2}(?:[.:]\d+)?\]|<\d{1,3}:\d{2}(?:[.:]\d+)?>", "", value)
    value = re.sub(r"\[(?:ar|ti|al|by|offset|length|re|ve):[^\]\n]*\]", "", value,
                   flags=re.IGNORECASE)
    value = re.sub(r"(?m)^\s*\[(?:verse|chorus|pre[- ]chorus|post[- ]chorus|bridge|intro|outro|hook|refrain|instrumental)(?:\s[^\]\n]*|:[^\]\n]*)?\]\s*$", "", value,
                   flags=re.IGNORECASE)
    return sum(char.isalnum() for char in unicodedata.normalize("NFC", value))


@dataclass
class Track:
    id: str
    title: str
    artists: list[str]
    plays: int = 0
    album: str = ""
    has_lyrics: bool = False
    lyric_format: str = "txt"
    path: str = ""

    @property
    def key(self) -> str:
        # Preserve remix/live/version words; share results across duplicate files.
        identity = [normalized(self.title), sorted(normalized(a) for a in self.artists)]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    @classmethod
    def from_item(cls, item: dict) -> Track:
        def artist_names(values):
            if not isinstance(values, list):
                return []
            names = [v.get("Name") if isinstance(v, dict) else v for v in values]
            return [v.strip() for v in names if isinstance(v, str) and v.strip()]

        title = item.get("Name") or ""
        if not isinstance(title, str):
            title = ""
        # Strip a filename-style track number, never digits embedded in a name.
        title = re.sub(r"^\s*\d{1,3}\s*[-–—.]+\s*", "", title)
        artists = artist_names(item.get("Artists")) or artist_names(item.get("AlbumArtists"))
        if not artists:
            # Prefer spaced separators to preserve internal artist hyphens.
            parts = re.split(r"\s+[-–—]+\s+", title, maxsplit=1)
            if len(parts) == 1:
                parts = re.split(r"[-–—]+", title, maxsplit=1)
            if len(parts) == 2:
                artist, song = (part.strip() for part in parts)
                # Track-number prefixes such as "04 - More Or Less" are not artists.
                if artist and song and not artist.isdecimal():
                    artists, title = [artist], song
        streams = [s for s in (item.get("MediaStreams") or []) if s.get("Type") == "Lyric"]
        lyric_format = Path(streams[0].get("Path") or "lyrics.txt").suffix.lstrip(".").lower() if streams else "txt"
        return cls(item["Id"], title, artists, int((item.get("UserData") or {})
                   .get("PlayCount", 0) or 0), item.get("Album") or "",
                   bool(streams), lyric_format, item.get("Path") or "")


def response_ok(response: requests.Response, service: str, allowed=()):
    if response.status_code in allowed:
        return
    if not response.ok:
        retry = response.headers.get("Retry-After", "0")
        try:
            retry_seconds = max(0, float(retry))
        except ValueError:
            # HTTP-date values are supported without logging response bodies.
            from email.utils import parsedate_to_datetime
            try:
                retry_seconds = max(0, parsedate_to_datetime(retry).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                retry_seconds = 0
        raise FetchError(f"{service} returned HTTP {response.status_code}", retry_seconds)


class Jellyfin:
    def __init__(self, base: str, key: str, users=(), libraries=()):
        parts = urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
            raise FetchError("JELLYFIN_BASE_URL must be an HTTP(S) server base URL")
        if parts.username or parts.password:
            raise FetchError("Use headers for authentication, not credentials in the URL")
        self.base = base.rstrip("/")
        self.scope = hashlib.sha256(self.base.encode()).hexdigest()
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f'MediaBrowser Token="{quote(key, safe="")}", Client="finamp-lyrics", Version="0.1"',
                                     "User-Agent": "finamp-lyrics/0.1"})
        self.user_selectors = list(users)
        self.library_selectors = list(libraries)
        self._users = self._libraries = None

    def request(self, method: str, path: str, **kwargs):
        try:
            response = self.session.request(method, self.base + path, timeout=(5, 30), **kwargs)
        except requests.RequestException:
            raise FetchError("Jellyfin network request failed") from None
        response_ok(response, "Jellyfin")
        return response

    def get(self, path: str, **params):
        try:
            return self.request("GET", path, params=params).json()
        except ValueError:
            raise FetchError("Jellyfin did not return valid JSON") from None

    @staticmethod
    def select(items, selectors, id_field, description):
        if not selectors:
            return items
        result = []
        for selector in selectors:
            matches = [item for item in items if selector == item[id_field] or
                       selector.casefold() == item.get("Name", "").casefold()]
            if len(matches) != 1:
                raise FetchError(f"Unknown or ambiguous {description}: {selector}")
            if matches[0] not in result:
                result.append(matches[0])
        return result

    @property
    def users(self):
        if self._users is None:
            available = [u for u in self.get("/Users")
                         if not (u.get("Policy") or {}).get("IsDisabled", False)]
            self._users = self.select(available, self.user_selectors, "Id", "user")
            if not self._users:
                raise FetchError("No enabled Jellyfin users")
        return self._users

    @property
    def libraries(self):
        if self._libraries is None:
            music = [lib for lib in self.get("/Library/VirtualFolders")
                     if lib.get("CollectionType") == "music"]
            self._libraries = self.select(music, self.library_selectors, "ItemId", "music library")
            if not self._libraries:
                raise FetchError("No selected music libraries")
        return self._libraries

    def priority(self, item_id: str) -> Track:
        # Query inside a music parent; a playback event must not fetch audiobook lyrics.
        for library in self.libraries:
            for user in self.users:
                data = self.get("/Items", UserId=user["Id"], ParentId=library["ItemId"],
                                Ids=item_id, Recursive="true", IncludeItemTypes="Audio",
                                Fields="MediaStreams", EnableUserData="true")
                for item in data.get("Items", []):
                    if item["Id"] == item_id and item.get("Type") == "Audio":
                        return Track.from_item(item)
        raise FetchError("Priority item is not an accessible track in a selected music library")

    def ranked(self) -> list[Track]:
        tracks = {}
        for user in self.users:
            for library in self.libraries:
                start = 0
                while True:
                    data = self.get("/Items", UserId=user["Id"], ParentId=library["ItemId"],
                                    Recursive="true", IncludeItemTypes="Audio",
                                    Fields="MediaStreams", EnableUserData="true",
                                    SortBy="PlayCount,SortName", SortOrder="Descending",
                                    StartIndex=start, Limit=500)
                    items = data.get("Items", [])
                    for item in items:
                        if item.get("Type") != "Audio":
                            continue
                        track = Track.from_item(item)
                        if track.plays <= 0:
                            continue
                        if track.id in tracks:
                            tracks[track.id].plays += track.plays
                            tracks[track.id].has_lyrics |= track.has_lyrics
                        else:
                            tracks[track.id] = track
                    start += len(items)
                    # Sorted by play count: once a page reaches zero, later pages
                    # cannot contribute to a most-played ranking for this user.
                    reached_unplayed = bool(items) and int((items[-1].get("UserData") or {})
                                                          .get("PlayCount", 0)) <= 0
                    if not items or reached_unplayed or start >= data.get("TotalRecordCount", start):
                        break
        return sorted(tracks.values(), key=lambda t: (-t.plays, t.title.casefold(), t.id))

    def disk_ranked(self) -> list[Track]:
        """Rank registered music files without reading content or changing atime."""
        candidates = {}
        sources = []
        skipped = 0
        for library in self.libraries:
            roots = []
            for location in library.get("Locations", []):
                root = Path(location).resolve()
                try:
                    flags = os.statvfs(root).f_flag
                except OSError:
                    continue
                mode = "mtime" if flags & os.ST_NOATIME else "atime"
                roots.append((root, mode))
                sources.append({"root": str(root), "order": mode})
            if not roots:
                continue
            start = 0
            while True:
                params = dict(ParentId=library["ItemId"], Recursive="true", IncludeItemTypes="Audio",
                              Fields="Path,MediaStreams", EnableUserData="false", SortBy="SortName",
                              StartIndex=start, Limit=500)
                # Honor user scoping when explicitly requested; otherwise use the
                # admin API's music catalog rather than scanning it per user.
                if self.user_selectors:
                    params["UserId"] = self.users[0]["Id"]
                data = self.get("/Items", **params)
                items = data.get("Items", [])
                for item in items:
                    if item.get("Type") != "Audio" or not item.get("Path"):
                        skipped += 1
                        continue
                    path = Path(item["Path"]).resolve()
                    mode = next((mode for root, mode in roots if path.is_relative_to(root)), None)
                    if mode is None:
                        skipped += 1
                        continue
                    try:
                        stat = path.stat()
                        if not path.is_file():
                            skipped += 1
                            continue
                    except OSError:
                        skipped += 1
                        continue
                    track = Track.from_item(item)
                    track.plays = 0  # Preserve filesystem order after played candidates.
                    candidates[track.id] = (stat.st_atime_ns if mode == "atime" else stat.st_mtime_ns, track)
                start += len(items)
                if not items or start >= data.get("TotalRecordCount", start):
                    break
        print(json.dumps({"disk_fallback": {"sources": sources, "registered_files": len(candidates),
                                            "skipped_files": skipped}}), flush=True)
        return [entry[1] for entry in sorted(candidates.values(),
                    key=lambda entry: (-entry[0], entry[1].title.casefold(), entry[1].id))]

    def lyrics(self, item_id: str) -> str:
        try:
            response = self.session.get(self.base + f"/Audio/{item_id}/Lyrics", timeout=(5, 30))
        except requests.RequestException:
            raise FetchError("Jellyfin lyric check failed") from None
        response_ok(response, "Jellyfin", allowed=(404,))
        if response.status_code == 404:
            return ""
        try:
            lines = response.json().get("Lyrics")
            if not isinstance(lines, list) or any(not isinstance(line, dict) or
                    not isinstance(line.get("Text"), str) for line in lines):
                raise ValueError("Invalid lyric lines")
            return "\n".join(line["Text"] for line in lines)
        except (ValueError, AttributeError):
            raise FetchError("Jellyfin lyric check returned invalid JSON") from None

    def has_lyrics(self, item_id: str) -> bool:
        return bool(self.lyrics(item_id))

    def upload(self, track: Track, lyrics: str):
        # Read fresh metadata and text rather than trusting the ranking snapshot.
        fresh = self.priority(track.id)
        existing = self.lyrics(track.id)
        if lyric_length(lyrics) <= lyric_length(existing):
            return "kept"
        # Reuse the active format so an older LRC cannot take precedence over TXT.
        lyric_format = fresh.lyric_format if existing else "txt"
        if lyric_format not in ("txt", "lrc", "elrc"):
            raise FetchError("Cannot replace an unsupported lyric format")
        self.request("POST", f"/Audio/{track.id}/Lyrics",
                     params={"fileName": "lyrics." + lyric_format},
                     headers={"Content-Type": "text/plain; charset=utf-8"},
                     data=lyrics.encode("utf-8"))
        # Jellyfin's upload endpoint queues its own targeted metadata refresh.
        return "uploaded"


def pack_id(value):
    """Pack existing hex identities; keep nonhex standalone/test IDs reversible."""
    if isinstance(value, str) and re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{64})", value):
        return bytes.fromhex(value)
    return value


def unpack_id(value):
    return value.hex() if isinstance(value, bytes) else value


def decode_job(row):
    if row is None:
        return None
    result = dict(row)
    result["item"] = unpack_id(result["item"])
    result["key"] = unpack_id(result["key"])
    return result


class State:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.db = sqlite3.connect(directory / "state.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS lookups(
            key TEXT PRIMARY KEY, status TEXT NOT NULL, lyrics_hash BLOB, url TEXT,
            attempted REAL NOT NULL, retry_at REAL NOT NULL, reason TEXT);
        CREATE TABLE IF NOT EXISTS jobs(
            server TEXT NOT NULL, item TEXT NOT NULL, track TEXT NOT NULL,
            key TEXT NOT NULL, priority INTEGER NOT NULL, plays INTEGER NOT NULL,
            publish INTEGER NOT NULL, status TEXT NOT NULL, queued REAL NOT NULL,
            retry_at REAL NOT NULL DEFAULT 0, reason TEXT,
            PRIMARY KEY(server,item));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS rankings(
            scope TEXT PRIMARY KEY, generated REAL NOT NULL, tracks TEXT NOT NULL);
        """)
        self.db.commit()
        self.migrate_lyrics()
        self.migrate_queue()
        self._server_ids = {}
        self.db.execute("PRAGMA foreign_keys=ON")

    def write_lyrics(self, key, lyrics):
        folder = self.directory / "lyrics"
        folder.mkdir(exist_ok=True)
        path = folder / (key + ".txt")
        import tempfile
        descriptor, temporary = tempfile.mkstemp(dir=folder, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(lyrics)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return hashlib.md5(lyrics.encode("utf-8"), usedforsecurity=False).digest()

    def migrate_lyrics(self):
        with (self.directory / "schema.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(lookups)")}
            if "lyrics" not in columns:
                return
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("""CREATE TABLE lookups_hashed(
                    key TEXT PRIMARY KEY, status TEXT NOT NULL, lyrics_hash BLOB, url TEXT,
                    attempted REAL NOT NULL, retry_at REAL NOT NULL, reason TEXT)""")
                rows = self.db.execute("SELECT * FROM lookups").fetchall()
                for row in rows:
                    digest = self.write_lyrics(row["key"], row["lyrics"]) if row["lyrics"] is not None else None
                    self.db.execute("INSERT INTO lookups_hashed VALUES(?,?,?,?,?,?,?)",
                        (row["key"], row["status"], digest, row["url"], row["attempted"], row["retry_at"], row["reason"]))
                self.db.execute("DROP TABLE lookups")
                self.db.execute("ALTER TABLE lookups_hashed RENAME TO lookups")
                self.db.execute("PRAGMA user_version=2")
            audit(self.directory, "lyrics_hash_migration", result="pass", lookups=len(rows), algorithm="md5")

    def lookup(self, key):
        key = unpack_id(key)
        row = self.db.execute("SELECT * FROM lookups WHERE key=?", (pack_id(key),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["key"] = unpack_id(result["key"])
        result["lyrics"] = None  # Virtual field; text exists only on disk.
        if row["lyrics_hash"] is not None:
            try:
                text = (self.directory / "lyrics" / (key + ".txt")).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise FetchError("Cached lyric file is missing or unreadable") from None
            digest = hashlib.md5(text.encode("utf-8"), usedforsecurity=False).digest()
            if digest != row["lyrics_hash"]:
                raise FetchError("Cached lyric file hash mismatch")
            result["lyrics"] = text
        return result

    def migrate_queue(self):
        """Preserve queue state while replacing snapshots and textual identifiers."""
        with (self.directory / "schema.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
            if "track" not in columns:
                return
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("CREATE TABLE IF NOT EXISTS servers(id INTEGER PRIMARY KEY, identity TEXT NOT NULL UNIQUE)")
                self.db.execute("""CREATE TABLE jobs_compact(
                    server INTEGER NOT NULL REFERENCES servers(id), item BLOB NOT NULL,
                    local_metadata TEXT, key BLOB NOT NULL, priority INTEGER NOT NULL,
                    plays INTEGER NOT NULL, publish INTEGER NOT NULL, status TEXT NOT NULL,
                    queued REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0, reason TEXT,
                    PRIMARY KEY(server,item)) WITHOUT ROWID""")
                jobs = self.db.execute("SELECT * FROM jobs").fetchall()
                identities = sorted({row["server"] for row in jobs})
                self.db.executemany("INSERT OR IGNORE INTO servers(identity) VALUES(?)", [(v,) for v in identities])
                servers = {row["identity"]: row["id"] for row in self.db.execute("SELECT * FROM servers")}
                for row in jobs:
                    metadata = None
                    if row["server"] == "standalone":
                        track = json.loads(row["track"])
                        if track:
                            metadata = json.dumps({"title": track["title"], "artists": track["artists"]})
                    self.db.execute("INSERT INTO jobs_compact VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (servers[row["server"]], pack_id(row["item"]), metadata, pack_id(row["key"]),
                         row["priority"], row["plays"], row["publish"], row["status"],
                         row["queued"], row["retry_at"], row["reason"]))
                self.db.execute("""CREATE TABLE lookups_compact(
                    key BLOB PRIMARY KEY, status TEXT NOT NULL, lyrics_hash BLOB, url TEXT,
                    attempted REAL NOT NULL, retry_at REAL NOT NULL, reason TEXT) WITHOUT ROWID""")
                lookups = self.db.execute("SELECT * FROM lookups").fetchall()
                for row in lookups:
                    self.db.execute("INSERT INTO lookups_compact VALUES(?,?,?,?,?,?,?)",
                        (pack_id(row["key"]), row["status"], row["lyrics_hash"], row["url"],
                         row["attempted"], row["retry_at"], row["reason"]))
                self.db.execute("DROP TABLE jobs")
                self.db.execute("ALTER TABLE jobs_compact RENAME TO jobs")
                self.db.execute("DROP TABLE lookups")
                self.db.execute("ALTER TABLE lookups_compact RENAME TO lookups")
                self.db.execute("""CREATE INDEX pending_order ON jobs(
                    server,priority,(CASE WHEN priority=0 THEN queued END) DESC,plays DESC,queued,item)
                    WHERE status='pending'""")
                self.db.execute("PRAGMA user_version=3")
            audit(self.directory, "compact_queue_migration", result="pass", jobs=len(jobs), lookups=len(lookups))

    def server_id(self, identity):
        if identity in self._server_ids:
            return self._server_ids[identity]
        row = self.db.execute("SELECT id FROM servers WHERE identity=?", (identity,)).fetchone()
        if row is None:
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO servers(identity) VALUES(?)", (identity,))
            row = self.db.execute("SELECT id FROM servers WHERE identity=?", (identity,)).fetchone()
        self._server_ids[identity] = row[0]
        return row[0]

    def job(self, server, item):
        row = self.db.execute("SELECT * FROM jobs WHERE server=? AND item=?",
                              (self.server_id(server), pack_id(item))).fetchone()
        return decode_job(row)

    def update_key(self, server, item, key):
        server_id = self.server_id(server)
        with self.db:
            self.db.execute("UPDATE jobs SET key=? WHERE server=? AND item=?",
                            (pack_id(key), server_id, pack_id(item)))

    def pending_count(self, server):
        return self.db.execute("SELECT count(*) FROM jobs WHERE server=? AND status IN ('pending','running')",
                               (self.server_id(server),)).fetchone()[0]

    def requeue_expired(self, server):
        server_id = self.server_id(server)
        with self.db:
            return self.db.execute("""UPDATE jobs SET status='pending'
                WHERE server=? AND status IN ('error','not_found') AND retry_at<=?""",
                (server_id, time.time())).rowcount

    def eligible(self, server, track, publish=False, retry=False):
        def skipped(reason, status=None):
            cache = None if reason == "durable_success" else self.lookup(track.key)
            text = cache["lyrics"] if cache and cache["lyrics"] else None
            audit(self.directory, "track_skipped", track, result="skip", reason=reason,
                  status=status, lyrics_length=lyric_length(text) if text is not None else None,
                  lyrics_characters=len(text) if text is not None else None,
                  lyric_file_exists=(self.directory / "lyrics" / (track.key + ".txt")).is_file())
            return False

        job = self.job(server, track.id)
        if job and job["status"] in ("uploaded", "kept"):
            return skipped("durable_success", job["status"])
        if not track.title or not track.artists:
            return skipped("missing_title_or_artist")
        if job and job["key"] == track.key:
            if job["status"] in ("pending", "running"):
                return skipped("already_queued", job["status"])
            if job["status"] == "saved" and not publish:
                return skipped("local_lyric_already_saved", job["status"])
            if job["retry_at"] > time.time() and not retry:
                return skipped("job_retry_cooldown", job["status"])
        cache = self.lookup(track.key)
        if cache and cache["status"] != "found" and cache["retry_at"] > time.time() and not retry:
            return skipped("lookup_retry_cooldown", cache["status"])
        return True

    def enqueue(self, server, track, priority, publish=False, retry=False):
        server_id = self.server_id(server)
        item = pack_id(track.id)
        with self.db:
            job = self.job(server, track.id)
            if job and job["key"] == track.key and job["status"] in ("pending", "running"):
                self.db.execute("""UPDATE jobs SET priority=min(priority,?),
                    publish=max(publish,?), queued=CASE WHEN ?=0 THEN ? ELSE queued END
                    WHERE server=? AND item=?""",
                    (priority, publish, priority, time.time(), server_id, item))
                audit(self.directory, "track_queued", track, status=job["status"], promoted=True)
                return True
            if not self.eligible(server, track, publish, retry):
                return False
            if retry:
                self.db.execute("DELETE FROM lookups WHERE key=? AND status!='found'", (pack_id(track.key),))
            metadata = json.dumps({"title": track.title, "artists": track.artists}) if server == "standalone" else None
            self.db.execute("""INSERT INTO jobs
                (server,item,local_metadata,key,priority,plays,publish,status,queued,retry_at,reason)
                VALUES(?,?,?,?,?,?,?,'pending',?,0,NULL)
                ON CONFLICT(server,item) DO UPDATE SET local_metadata=excluded.local_metadata,key=excluded.key,
                priority=excluded.priority,plays=excluded.plays,publish=excluded.publish,
                status='pending',queued=excluded.queued,retry_at=0,reason=NULL""",
                (server_id, item, metadata, pack_id(track.key), priority, track.plays, publish, time.time()))
        audit(self.directory, "track_queued", track, status="pending", publish=bool(publish), priority=priority)
        return True

    def next_job(self, server):
        row = self.db.execute("""SELECT * FROM jobs
            WHERE server=? AND status='pending'
            ORDER BY priority ASC, CASE WHEN priority=0 THEN queued END DESC,
                     plays DESC, queued ASC, item ASC LIMIT 1""", (self.server_id(server),)).fetchone()
        return decode_job(row)

    def cleanup_success_cache(self, keys):
        removed = 0
        for key in set(keys):
            if not key:
                continue
            with self.db:
                referenced = self.db.execute("SELECT 1 FROM jobs WHERE key=? LIMIT 1", (pack_id(key),)).fetchone()
                if referenced:
                    continue
                count = self.db.execute("DELETE FROM lookups WHERE key=? AND status='found'", (pack_id(key),)).rowcount
            if count:
                (self.directory / "lyrics" / (unpack_id(key) + ".txt")).unlink(missing_ok=True)
                removed += 1
        return removed

    def compact_successes(self):
        with self.db:
            keys = [row[0] for row in self.db.execute(
                "SELECT DISTINCT key FROM jobs WHERE status IN ('uploaded','kept') AND key!=''")]
            count = self.db.execute("""UPDATE jobs SET local_metadata=NULL,key='',plays=0,
                priority=0,publish=0,retry_at=0,reason=NULL
                WHERE status IN ('uploaded','kept') AND (local_metadata IS NOT NULL OR key!='')""").rowcount
        return {"success_stubs": count, "cache_files_removed": self.cleanup_success_cache(keys)}

    def finish(self, server, item, status, retry_at=0, reason=None):
        server_id = self.server_id(server)
        item = pack_id(item)
        with self.db:
            if status in ("uploaded", "kept"):
                row = self.db.execute("SELECT key FROM jobs WHERE server=? AND item=?", (server_id, item)).fetchone()
                key = row[0] if row else None
                self.db.execute("""UPDATE jobs SET status=?,local_metadata=NULL,key='',plays=0,
                    priority=0,publish=0,retry_at=0,reason=NULL,queued=? WHERE server=? AND item=?""",
                    (status, time.time(), server_id, item))
            else:
                self.db.execute("UPDATE jobs SET status=?,retry_at=?,reason=? WHERE server=? AND item=?",
                    (status, retry_at, reason, server_id, item))
                key = None
        if key:
            self.cleanup_success_cache([key])

    def save_lookup(self, key, status, lyrics=None, url=None, retry_at=0, reason=None):
        digest = self.write_lyrics(key, lyrics) if lyrics is not None else None
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO lookups VALUES(?,?,?,?,?,?,?)",
                            (pack_id(key), status, digest, url, time.time(), retry_at, reason))

    def pace(self, delay):
        # Called under the worker lock: shared across separate plugin invocations.
        row = self.db.execute("SELECT value FROM settings WHERE key='next_request'").fetchone()
        if row:
            time.sleep(max(0, row[0] - time.time()))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('next_request',?)",
                            (time.time() + delay,))
            self.db.execute("""INSERT INTO settings VALUES('request_count',1)
                ON CONFLICT(key) DO UPDATE SET value=value+1""")

    def cooldown(self, seconds):
        with self.db:
            self.db.execute("""INSERT INTO settings VALUES('next_request',?)
                ON CONFLICT(key) DO UPDATE SET value=max(value,excluded.value)""",
                            (time.time() + seconds,))

    def backoff_429(self, retry_after=0):
        with self.db:
            row = self.db.execute("SELECT value FROM settings WHERE key='genius_429_streak'").fetchone()
            streak = min(64, int(row[0]) + 1 if row else 1)
            seconds = max(retry_after, min(3600, 60 * 2 ** min(streak - 1, 6)))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('genius_429_streak',?)", (streak,))
            self.db.execute("""INSERT INTO settings VALUES('next_request',?)
                ON CONFLICT(key) DO UPDATE SET value=max(value,excluded.value)""", (time.time() + seconds,))
        return seconds, streak

    def reset_429(self):
        with self.db:
            self.db.execute("UPDATE settings SET value=0 WHERE key='genius_429_streak' AND value!=0")

    def ranked(self, jellyfin, ttl=900):
        scope = hashlib.sha256(json.dumps(
            [jellyfin.scope, sorted(jellyfin.user_selectors),
             sorted(jellyfin.library_selectors)]).encode()).hexdigest()
        row = self.db.execute("SELECT * FROM rankings WHERE scope=?", (scope,)).fetchone()
        if row and time.time() - row["generated"] < ttl:
            return [Track(**track) for track in json.loads(row["tracks"])]
        tracks = jellyfin.ranked()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO rankings VALUES(?,?,?)",
                            (scope, time.time(), json.dumps([asdict(t) for t in tracks])))
        return tracks

    def maintain(self, force=False):
        """Run once per local calendar month while holding the worker lock."""
        now = time.time()
        month = int(datetime.fromtimestamp(now).strftime("%Y%m"))
        previous = self.db.execute("SELECT value FROM settings WHERE key='maintenance_month'").fetchone()
        if not force and previous and previous[0] >= month:
            return False
        health = [row[0] for row in self.db.execute("PRAGMA quick_check")]
        if health != ["ok"]:
            audit(self.directory, "database_maintenance", result="fail", reason="SQLite quick_check failed")
            raise FetchError("SQLite health check failed")
        self.db.execute("PRAGMA optimize=0x10002")
        checkpoint = list(self.db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('maintenance_month',?)", (month,))
        audit(self.directory, "database_maintenance", result="pass", month=month,
              quick_check="ok", optimize=True, checkpoint=checkpoint)
        return True

    @contextmanager
    def worker_lock(self):
        # Queue insertion happens before this blocking lock. A running worker will
        # see new priority jobs; the waiting process drains any remaining work.
        with (self.directory / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self.maintain()
                with self.db:
                    self.db.execute("UPDATE jobs SET status='pending' WHERE status='running'")
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


class Genius:
    def __init__(self, token: str, state: State, delay: float, retry_429=False, delay_step=0.25, max_delay=10):
        self.token, self.state, self.delay = token, state, delay
        self.retry_429 = retry_429
        self.delay_step, self.max_delay = delay_step, max_delay
        self.request_count = 0  # Per execution; never restored from SQLite.
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "finamp-lyrics/0.1"

    def request(self, url, api=False, **kwargs):
        while True:
            delay = min(self.max_delay, self.delay + self.request_count * self.delay_step)
            if self.request_count == 0:
                self.state.cooldown(delay)
            self.state.pace(delay)
            self.request_count += 1
            audit(self.state.directory, "genius_request_started", request_number=self.request_count,
                  delay_seconds=delay, endpoint="api_search" if api else "song_page")
            try:
                response = self.session.get(url, timeout=(5, 30),
                                            headers={"Authorization": "Bearer " + self.token} if api else {},
                                            **kwargs)
            except requests.RequestException:
                raise FetchError("Genius network request failed") from None
            finally:
                self.state.cooldown(min(self.max_delay, self.delay + self.request_count * self.delay_step))
            audit(self.state.directory, "genius_response", http_status=response.status_code,
                  retry_after=response.headers.get("Retry-After"), request_number=self.request_count)
            if response.status_code == 429:
                try:
                    response_ok(response, "Genius")
                except FetchError as exc:
                    seconds, streak = self.state.backoff_429(exc.retry_after)
                    deadline = self.state.db.execute(
                        "SELECT value FROM settings WHERE key='next_request'").fetchone()[0]
                    audit(self.state.directory, "genius_backoff", http_status=429,
                          seconds=seconds, consecutive=streak,
                          retry_after=response.headers.get("Retry-After"),
                          provider_retry_seconds=exc.retry_after, retry_at=deadline,
                          retry_in_place=self.retry_429)
                    if self.retry_429:
                        continue  # pace() waits for the persisted provider cooldown.
                    raise FetchError(str(exc), seconds) from None
            response_ok(response, "Genius")
            self.state.reset_429()
            return response

    @staticmethod
    def extract(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for header in soup.find_all("div", class_=re.compile("LyricsHeader")):
            header.decompose()
        containers = soup.select('[data-lyrics-container="true"]')
        if not containers:
            # Missing selectors could mean blocking or an HTML change, not no lyrics.
            raise FetchError("Genius page has no recognized lyric containers")
        texts = []
        for container in containers:
            for excluded in container.select('[data-exclude-from-selection="true"]'):
                excluded.decompose()
            for br in container.find_all("br"):
                br.replace_with("\n")
            texts.append(container.get_text())
        lyrics = "\n".join(texts).strip()
        if not lyrics:
            raise FetchError("Genius returned empty lyric containers")
        return lyrics

    def search(self, track: Track):
        try:
            payload = self.request("https://api.genius.com/search", api=True,
                                   params={"q": f"{track.title} {lead_artist(track.artists[0])}"}).json()
        except ValueError:
            raise FetchError("Genius search returned invalid JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("response", {}).get("hits"), list):
            raise FetchError("Genius search returned an unexpected response")
        matches = {}
        for hit in payload["response"]["hits"]:
            song = hit.get("result", {})
            if (hit.get("type", "song") == "song" and
                    normalized(song.get("title", "")) == normalized(track.title) and
                    normalized(song.get("primary_artist", {}).get("name", "")) in
                    {normalized(a) for artist in track.artists
                     for a in (artist, lead_artist(artist))}):
                matches[song["id"]] = song
        if not matches:
            return None, None
        if len(matches) != 1:
            raise FetchError("Genius returned multiple exact song matches")
        song = next(iter(matches.values()))
        parts = urlsplit(song.get("url", ""))
        if parts.scheme != "https" or parts.netloc.lower() != "genius.com" or not parts.path.endswith("-lyrics"):
            raise FetchError("Genius match is not a recognized song page")
        return self.extract(self.request(song["url"]).text), song["url"]


class Worker:
    def __init__(self, state, genius, jellyfin=None, missing_days=30, error_hours=1):
        self.state, self.genius, self.jellyfin = state, genius, jellyfin
        self.missing_seconds, self.error_seconds = missing_days * 86400, error_hours * 3600

    def run(self, server):
        totals = {}
        with self.state.worker_lock():
            requeued = self.state.requeue_expired(server)
            if requeued:
                audit(self.state.directory, "retries_requeued", count=requeued)
            while True:
                row = self.state.next_job(server)
                if row is None:
                    break
                track = Track(row["item"], "", [])
                self.state.finish(server, track.id, "running")
                status, reason, cache = "error", None, None
                try:
                    if row["local_metadata"] is not None:
                        metadata = json.loads(row["local_metadata"])
                        track = Track(row["item"], metadata["title"], metadata["artists"], plays=row["plays"])
                    elif self.jellyfin is not None:
                        track = self.jellyfin.priority(row["item"])
                    else:
                        raise FetchError("Jellyfin is required to resolve queued track metadata")
                    if not track.title or not track.artists:
                        raise FetchError("Current track metadata lacks title or artist")
                    if row["key"] != track.key:
                        self.state.update_key(server, track.id, track.key)
                    audit(self.state.directory, "track_started", track)
                    cache = self.state.lookup(track.key)
                    if cache and cache["status"] != "found" and cache["retry_at"] > time.time():
                        status, reason = cache["status"], cache["reason"]
                        self.state.finish(server, track.id, status, cache["retry_at"], reason)
                        audit(self.state.directory, "track_result", track, result="fail", status=status,
                              reason=reason, cached=True, lyrics_length=0, lyrics_characters=0)
                        totals[status] = totals.get(status, 0) + 1
                        continue
                    else:
                        if not cache or cache["status"] != "found":
                            try:
                                lyrics, url = self.genius.search(track)
                            except FetchError as exc:
                                self.state.save_lookup(track.key, "error",
                                    retry_at=time.time() + max(self.error_seconds, exc.retry_after),
                                    reason=str(exc))
                                raise
                            if lyrics:
                                self.state.save_lookup(track.key, "found", lyrics, url)
                            else:
                                self.state.save_lookup(track.key, "not_found",
                                    retry_at=time.time() + self.missing_seconds,
                                    reason="No exact title/artist match on Genius")
                            cache = self.state.lookup(track.key)
                        if cache["status"] == "not_found":
                            status, reason = "not_found", cache["reason"]
                        else:
                            # A priority invocation can promote a running local job
                            # to upload while its Genius request is in flight.
                            current = self.state.job(server, track.id)
                            status = self.jellyfin.upload(track, cache["lyrics"]) if current["publish"] else "saved"
                            if status == "kept":
                                reason = "Candidate lyric text is not longer than existing lyrics"
                    retry_at = cache["retry_at"] if status == "not_found" and cache else 0
                    self.state.finish(server, track.id, status, retry_at, reason)
                except FetchError as exc:
                    reason = str(exc)
                    self.state.finish(server, track.id, "error",
                                      time.time() + max(self.error_seconds, exc.retry_after), reason)
                    if exc.retry_after:
                        audit(self.state.directory, "track_result", track, result="fail", status="error",
                              reason=reason, lyrics_length=lyric_length(cache["lyrics"] or "") if cache else 0,
                              lyrics_characters=len(cache["lyrics"] or "") if cache else 0)
                        totals["error"] = totals.get("error", 0) + 1
                        break
                text = cache["lyrics"] or "" if cache else ""
                audit(self.state.directory, "track_result", track,
                      result="pass" if status in ("saved", "uploaded", "kept") else "fail",
                      status=status, reason=reason, lyrics_length=lyric_length(text),
                      lyrics_characters=len(text), skipped=status == "kept")
                totals[status] = totals.get(status, 0) + 1
        return totals


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parent / "state")
    p.add_argument("--server-url", default=os.environ.get("JELLYFIN_BASE_URL"),
                   help="Override JELLYFIN_BASE_URL, for example http://localhost:8096")
    p.add_argument("--top", type=int, default=10, help="Background lookup count (eligible most-played tracks)")
    p.add_argument("--min-plays", type=int, default=1,
                   help="Minimum summed play count for background tracks and --list")
    p.add_argument("--disk-fallback", action="store_true",
                   help="Fill remaining batch slots from registered music files by access time (mtime on noatime mounts)")
    p.add_argument("--allow-song-errors", action="store_true",
                   help="Completed batches with cached per-song errors succeed; interrupted batches still fail")
    p.add_argument("--retry-429", action="store_true",
                   help="Wait with exponential backoff and retry rate-limited Genius requests until stopped")
    p.add_argument("--priority", action="append", default=[], metavar="ITEM_ID",
                   help="Check this Jellyfin item before discovering background candidates")
    p.add_argument("--enqueue-only", action="store_true",
                   help="Queue priority items for a running worker without waiting or fetching")
    p.add_argument("--user", action="append", default=[], help="User name/ID; default sums all enabled users")
    p.add_argument("--library", action="append", default=[], help="Music library name/ID; default all music libraries")
    p.add_argument("--delay", "--initial-delay", type=float, default=0.5, help="Initial Genius request delay in seconds (0–10)")
    p.add_argument("--delay-step", type=float, default=0.25, help="Increase delay per HTTP request; resets per execution")
    p.add_argument("--max-delay", type=float, default=10, help="Maximum request delay (up to 10 seconds)")
    p.add_argument("--missing-days", type=float, default=30)
    p.add_argument("--error-hours", type=float, default=1)
    p.add_argument("--ranking-minutes", type=float, default=15,
                   help="Cache user play-count ranking for this many minutes")
    p.add_argument("--refresh-ranking", action="store_true", help="Refresh ranking immediately")
    p.add_argument("--inspect", action="store_true", help="Read server version, users and music libraries")
    p.add_argument("--verify", metavar="ITEM_ID",
                   help="Check the server lyric stream and compare its text with the local cache")
    p.add_argument("--upload", action="store_true", help="Publish through Jellyfin; otherwise save locally")
    p.add_argument("--retry", action="store_true", help="Bypass negative cache for explicitly prioritized tracks")
    p.add_argument("--list", action="store_true", help="Show ranked tracks without fetching, queuing, or uploading")
    p.add_argument("--title", help="Standalone Genius check without Jellyfin")
    p.add_argument("--artist", help="Artist for standalone check")
    return p


def _main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not 0 <= args.delay <= args.max_delay <= 10 or args.delay_step < 0 or args.top < 0 or args.min_plays < 1 or args.missing_days <= 0 or args.error_hours <= 0 or args.ranking_minutes <= 0:
        p.error("delay must be 0–max-delay <= 10, delay-step >= 0, top >= 0, min-plays >= 1, and retry intervals > 0")
    if bool(args.title) != bool(args.artist):
        p.error("--title and --artist must be supplied together")
    if args.title and (args.upload or args.priority or args.list or args.inspect or args.verify):
        p.error("standalone mode cannot use --upload, --priority, --list, or --inspect")
    if args.retry and not (args.title or args.priority):
        p.error("--retry requires --priority or a standalone --title/--artist")
    if args.enqueue_only and (not args.priority or args.title or args.list or args.inspect or args.verify):
        p.error("--enqueue-only requires Jellyfin --priority items")
    state = None
    try:
        if not args.list and not args.inspect and not args.verify and not args.enqueue_only and not os.environ.get("GENIUS_CLIENT_ACCESS"):
            raise FetchError("Set GENIUS_CLIENT_ACCESS")
        if args.title:
            state = State(args.state_dir)
            track = Track("standalone", args.title, [args.artist])
            # Multiple standalone songs require independent job IDs.
            track.id = track.key
            state.enqueue("standalone", track, 0, retry=args.retry)
            worker = Worker(state, Genius(os.environ["GENIUS_CLIENT_ACCESS"], state, args.delay, args.retry_429, args.delay_step, args.max_delay),
                            missing_days=args.missing_days, error_hours=args.error_hours)
            result = worker.run("standalone")
        else:
            if not args.server_url or not os.environ.get("JELLYFIN_API_KEY"):
                raise FetchError("Set JELLYFIN_BASE_URL and JELLYFIN_API_KEY")
            jf = Jellyfin(args.server_url, os.environ["JELLYFIN_API_KEY"],
                          args.user, args.library)
            if args.inspect:
                print(json.dumps({"version": jf.get("/System/Info/Public").get("Version"),
                    "users": [{"id": u["Id"], "name": u["Name"]} for u in jf.users],
                    "music_libraries": [{"id": lib["ItemId"], "name": lib["Name"]}
                                        for lib in jf.libraries]}))
                return 0
            if args.verify:
                track = jf.priority(args.verify)
                data = jf.get(f"/Audio/{track.id}/Lyrics")
                text = "\n".join(line.get("Text", "") for line in data.get("Lyrics", [])).strip()
                state = State(args.state_dir)
                cache = state.lookup(track.key)
                matches = text == cache["lyrics"].strip() if cache and cache["lyrics"] else None
                print(json.dumps({"item_id": track.id, "lyric_stream": track.has_lyrics,
                                  "lines": len(text.splitlines()), "matches_cache": matches}))
                return 0 if track.has_lyrics and text and matches is not False else 1
            if args.list:
                tracks = [track for track in jf.ranked() if track.plays >= args.min_plays]
                for track in tracks[:args.top]:
                    print(json.dumps(asdict(track)))
                return 0
            state = State(args.state_dir)
            result = {}
            for item_id in args.priority:
                track = jf.priority(item_id)
                queued = state.enqueue(jf.scope, track, 0, args.upload, args.retry)
                print(json.dumps({"priority": item_id, "queued": queued}), flush=True)
            if args.enqueue_only:
                return 0
            worker = Worker(state, Genius(os.environ["GENIUS_CLIENT_ACCESS"], state, args.delay, args.retry_429, args.delay_step, args.max_delay), jf,
                            args.missing_days, args.error_hours)
            # Process priority before the potentially expensive full-library scan.
            if args.priority:
                result = worker.run(jf.scope)
                if result.get("error"):
                    print(json.dumps({"summary": result}), flush=True)
                    return 1
            count = 0
            if args.top:
                for track in state.ranked(jf, 0 if args.refresh_ranking else args.ranking_minutes * 60):
                    if track.plays < args.min_plays:
                        continue
                    if state.eligible(jf.scope, track, args.upload):
                        if state.enqueue(jf.scope, track, 1, args.upload):
                            count += 1
                    if count >= args.top:
                        break
                if args.disk_fallback and count < args.top:
                    for track in jf.disk_ranked():
                        if state.eligible(jf.scope, track, args.upload):
                            if state.enqueue(jf.scope, track, 1, args.upload):
                                count += 1
                        if count >= args.top:
                            break
            print(json.dumps({"background_queued": count}), flush=True)
            for status, amount in worker.run(jf.scope).items():
                result[status] = result.get(status, 0) + amount
        print(json.dumps({"summary": result}), flush=True)
        if result.get("error"):
            if args.allow_song_errors and not args.title:
                pending = state.pending_count(jf.scope)
                if not pending:
                    return 0
            return 1
        return 0
    except FetchError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    finally:
        if state:
            state.db.close()


def main(argv=None):
    args = parser().parse_args(argv)
    if args.list or args.inspect or args.verify:
        return _main(argv)
    token = _run_id.set(uuid.uuid4().hex)
    try:
        audit(args.state_dir, "run_started", priority=args.priority, enqueue_only=args.enqueue_only,
              source="priority" if args.priority else "standalone" if args.title else "batch")
        try:
            result = _main(argv)
        except BaseException as exc:
            audit(args.state_dir, "run_finished", result="fail", exception_type=type(exc).__name__)
            raise
        audit(args.state_dir, "run_finished", result="pass" if result == 0 else "fail", exit_code=result)
        return result
    finally:
        _run_id.reset(token)


if __name__ == "__main__":
    raise SystemExit(main())
