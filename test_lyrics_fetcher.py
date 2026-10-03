"""Behavior tests with an HTTP fixture transport; never contact real services."""
import io
import json
import os
import multiprocessing
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

import lyrics_fetcher as app
import service_runner as batch


def response(payload=None, status=200, text=None, headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = (text if text is not None else json.dumps(payload)).encode()
    r.headers.update(headers or {})
    return r


def item(item_id, name, artist, plays, lyric=False):
    return {"Id": item_id, "Name": name, "Type": "Audio", "Artists": [artist],
            "Album": "Test album", "UserData": {"PlayCount": plays},
            "MediaStreams": [{"Type": "Lyric"}] if lyric else []}


class Fixture:
    def __init__(self):
        self.library = {"ItemId": "music", "Name": "Music", "CollectionType": "music"}
        self.users = [{"Id": "u1", "Name": "Alice"}, {"Id": "u2", "Name": "Bob"}]
        self.items = {
            "u1": [item("a", "Alpha", "Artist", 2), item("b", "Beta", "Artist", 7),
                   item("c", "Gamma", "Artist", 4, True), item("zero", "Never played", "Artist", 0)],
            "u2": [item("a", "Alpha", "Artist", 10), item("b", "Beta", "Artist", 1),
                   item("c", "Gamma", "Artist", 1, True), item("zero", "Never played", "Artist", 0)]}
        self.requests = []
        self.genius_queries = []
        self.uploads = {}
        self.existing = {"c": "existing lyric"}
        self.upload_error = False

    def session(self):
        fixture = self
        class Session:
            headers = {}
            def __init__(self):
                self.headers = {}
            def request(self, method, url, **kwargs):
                return fixture.request(method, url, self.headers, **kwargs)
            def get(self, url, **kwargs):
                return self.request("GET", url, **kwargs)
        return Session()

    def request(self, method, url, session_headers, **kwargs):
        self.requests.append((method, url, kwargs))
        parts = urlsplit(url)
        path = parts.path
        params = kwargs.get("params", {})
        if parts.netloc == "api.genius.com":
            assert kwargs["headers"]["Authorization"] == "Bearer secret-genius"
            query = params["q"]
            self.genius_queries.append(query)
            title = query.removesuffix(" Artist")
            song = {"id": title, "title": title, "primary_artist": {"name": "Artist"},
                    "url": f"https://genius.com/{title.lower()}-lyrics"}
            return response({"response": {"hits": [{"type": "song", "result": song}]}})
        if parts.netloc == "genius.com":
            assert "Authorization" not in kwargs.get("headers", {})
            return response(text='<div data-lyrics-container="true">fixture line</div>')
        assert session_headers["X-Emby-Token"] == "secret-jellyfin"
        if path == "/Users":
            return response(self.users)
        if path == "/Library/VirtualFolders":
            return response([self.library, {"ItemId": "books", "Name": "Books", "CollectionType": "books"}])
        if path == "/Items":
            assert params["ParentId"] == "music"
            assert params["IncludeItemTypes"] == "Audio"
            selected = self.items[params.get("UserId", "u1")]
            if params.get("Ids"):
                selected = [t for t in selected if t["Id"] == params["Ids"]]
            if not params.get("Ids"):
                selected = sorted(selected, key=lambda t: -t["UserData"]["PlayCount"])
            start, limit = params.get("StartIndex", 0), params.get("Limit", 500)
            return response({"Items": selected[start:start + limit], "TotalRecordCount": len(selected)})
        if path.startswith("/Audio/") and path.endswith("/Lyrics"):
            item_id = path.split("/")[2]
            if method == "POST":
                assert kwargs["params"]["fileName"].endswith((".txt", ".lrc", ".elrc"))
                assert kwargs["headers"]["Content-Type"].startswith("text/plain")
                if self.upload_error:
                    return response(status=503)
                self.uploads[item_id] = kwargs["data"].decode()
                return response({"Lyrics": [{"Text": "fixture line"}]})
            text = self.uploads.get(item_id, self.existing.get(item_id))
            if text is not None:
                return response({"Lyrics": [{"Text": line} for line in text.splitlines()]})
            return response(status=404)
        raise AssertionError(f"Unexpected fixture request: {method} {url}")


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = app.State(Path(self.temp.name))
        self.fixture = Fixture()
        self.session_patch = patch.object(app.requests, "Session", self.fixture.session)
        self.session_patch.start()
        self.pace_patch = patch.object(app.State, "pace")
        self.pace_patch.start()
        self.output_patch = patch("sys.stdout", new_callable=io.StringIO)
        self.output_patch.start()
        self.genius = app.Genius("secret-genius", self.state, 2)
        self.jf = app.Jellyfin("http://fixture.test", "secret-jellyfin")

    def tearDown(self):
        self.output_patch.stop()
        self.pace_patch.stop()
        self.session_patch.stop()
        self.state.db.close()
        self.temp.cleanup()

    def worker(self, jf=None):
        return app.Worker(self.state, self.genius, jf or self.jf)

    def queue(self, title="Alpha", track_id="a", priority=1, plays=1, publish=False):
        track = app.Track(track_id, title, ["Artist"], plays)
        for items in self.fixture.items.values():
            if not any(i["Id"] == track_id for i in items):
                items.append(item(track_id, title, "Artist", plays))
        self.state.enqueue(self.jf.scope, track, priority, publish)
        return track

    def test_play_counts_sum_users_and_music_scope(self):
        tracks = self.jf.ranked()
        self.assertEqual([(t.id, t.plays) for t in tracks], [("a", 12), ("b", 8), ("c", 5)])
        self.assertEqual(self.jf.priority("a").title, "Alpha")
        with self.assertRaises(app.FetchError):
            self.jf.priority("audiobook")

    def test_user_selection_changes_ranking(self):
        jf = app.Jellyfin("http://fixture.test", "secret-jellyfin", users=["Alice"], libraries=["Music"])
        self.assertEqual(jf.ranked()[0].id, "b")
        with self.assertRaises(app.FetchError):
            app.Jellyfin("http://fixture.test", "secret-jellyfin", libraries=["Books"]).libraries

    def test_pagination(self):
        self.fixture.items["u1"] = [item(str(i), str(i), "Artist", 1) for i in range(501)]
        self.fixture.items["u2"] = []
        self.assertEqual(len(self.jf.ranked()), 501)
        self.assertTrue(any(kwargs.get("params", {}).get("StartIndex") == 500
                            for _, _, kwargs in self.fixture.requests))

    def test_scan_stops_when_sorted_page_reaches_unplayed(self):
        self.fixture.items["u1"] = [item("played", "Played", "Artist", 1)] + [
            item(str(i), str(i), "Artist", 0) for i in range(2000)]
        self.fixture.items["u2"] = []
        self.assertEqual([t.id for t in self.jf.ranked()], ["played"])
        pages = [url for _, url, _ in self.fixture.requests if url.endswith("/Items")]
        self.assertEqual(len(pages), 2)

    def test_existing_lyrics_compared_once_and_shorter_candidate_kept(self):
        track = self.jf.priority("c")
        self.assertTrue(track.has_lyrics)
        self.assertTrue(self.state.enqueue(self.jf.scope, track, 0, True))
        self.assertEqual(self.worker(self.jf).run(self.jf.scope), {"kept": 1})
        self.assertEqual(self.fixture.genius_queries, ["Gamma Artist"])
        self.assertEqual(self.fixture.uploads, {})
        self.assertFalse(self.state.enqueue(self.jf.scope, track, 0, True))
        self.assertEqual(self.worker(self.jf).run(self.jf.scope), {})

    def test_longer_candidate_replaces_existing_lyrics(self):
        self.fixture.existing["c"] = "brief"
        self.queue("Gamma", "c", publish=True)
        self.assertEqual(self.worker(self.jf).run(self.jf.scope), {"uploaded": 1})
        self.assertEqual(self.jf.lyrics("c"), "fixture line")

    def test_equal_candidate_retains_server_text(self):
        self.fixture.existing["c"] = "Fixture,    line!"
        self.queue("Gamma", "c", publish=True)
        self.assertEqual(self.worker(self.jf).run(self.jf.scope), {"kept": 1})
        self.assertEqual(self.fixture.uploads, {})
        self.assertEqual(self.jf.lyrics("c"), "Fixture,    line!")

    def test_length_ignores_timestamps_metadata_sections_and_formatting(self):
        self.assertEqual(app.lyric_length("[ar:Artist]\n[Verse 1]\n[00:12.35]<00:12.35>Fixturé, line!"),
                         app.lyric_length("Fixturé line"))
        self.assertEqual(app.lyric_length("e\u0301"), app.lyric_length("é"))
        self.assertGreater(app.lyric_length("line\nline"), app.lyric_length("line"))

    def test_retry_keeps_success_permanent_without_new_genius_search(self):
        track = self.queue("Gamma", "c", publish=True)
        self.worker(self.jf).run(self.jf.scope)
        self.fixture.existing["c"] = "brief"
        self.assertFalse(self.state.enqueue(self.jf.scope, track, 0, True, retry=True))
        self.assertEqual(self.worker(self.jf).run(self.jf.scope), {})
        self.assertEqual(self.fixture.genius_queries, ["Gamma Artist"])

    def test_legacy_existing_job_is_eligible_for_first_comparison(self):
        track = self.queue("Gamma", "c", publish=True)
        self.state.finish(self.jf.scope, "c", "existing")
        self.assertTrue(self.state.enqueue(self.jf.scope, track, 0, True))
        self.assertEqual(self.worker(self.jf).run(self.jf.scope), {"kept": 1})

    def test_server_update_during_lookup_is_compared_before_upload(self):
        self.queue("Gamma", "c", publish=True)
        def search(track):
            self.fixture.existing["c"] = "a much longer lyric added while the lookup was in progress"
            return "candidate", "https://genius.com/gamma-lyrics"
        with patch.object(self.genius, "search", side_effect=search):
            self.assertEqual(self.worker(self.jf).run(self.jf.scope), {"kept": 1})
        self.assertEqual(self.fixture.uploads, {})

    def test_longer_replacement_uses_active_lyric_file_format(self):
        for fmt in ("lrc", "elrc"):
            with self.subTest(fmt=fmt):
                self.fixture.uploads.clear()
                self.fixture.existing["c"] = "short"
                self.fixture.items["u1"][2]["MediaStreams"][0]["Path"] = "/music/Gamma." + fmt
                self.assertEqual(self.jf.upload(app.Track("c", "Gamma", ["Artist"]), "longer lyric text"), "uploaded")
                post = [kwargs for method, url, kwargs in self.fixture.requests if method == "POST"][-1]
                self.assertEqual(post["params"]["fileName"], "lyrics." + fmt)

    def test_invalid_existing_response_prevents_upload(self):
        with patch.object(self.jf.session, "get", return_value=response({"Lyrics": "invalid"})):
            with self.assertRaises(app.FetchError):
                self.jf.upload(app.Track("c", "Gamma", ["Artist"]), "longer lyric text")
        self.assertEqual(self.fixture.uploads, {})

    def test_priority_then_highest_plays(self):
        self.queue("Alpha", "a", plays=12)
        self.queue("Beta", "b", priority=0, plays=1)
        self.queue("Gamma", "c", plays=5)
        self.worker().run(self.jf.scope)
        self.assertEqual(self.fixture.genius_queries, ["Beta Artist", "Alpha Artist", "Gamma Artist"])

    def test_new_priority_arrives_while_worker_is_active(self):
        self.queue("Alpha", "a", plays=12)
        self.queue("Gamma", "c", plays=5)
        original = self.genius.search
        def search(track):
            if track.id == "a":
                self.queue("Beta", "b", priority=0)
            return original(track)
        with patch.object(self.genius, "search", side_effect=search):
            self.worker().run(self.jf.scope)
        self.assertEqual(self.fixture.genius_queries, ["Alpha Artist", "Beta Artist", "Gamma Artist"])

    def test_success_and_duplicate_files_reuse_cache(self):
        first = self.queue()
        self.worker().run(self.jf.scope)
        self.assertFalse(self.state.enqueue(self.jf.scope, first, 0))
        self.queue(track_id="duplicate")
        self.worker().run(self.jf.scope)
        self.assertEqual(self.fixture.genius_queries, ["Alpha Artist"])
        self.assertEqual(self.state.lookup(first.key)["status"], "found")

    def test_missing_result_debounced_until_expiry(self):
        track = self.queue()
        with patch.object(self.genius, "search", return_value=(None, None)) as search:
            self.worker().run(self.jf.scope)
            self.assertFalse(self.state.enqueue(self.jf.scope, track, 0))
            self.queue(track_id="duplicate")
            self.assertEqual(search.call_count, 1)
            with self.state.db:
                self.state.db.execute("UPDATE lookups SET retry_at=0")
                self.state.db.execute("UPDATE jobs SET retry_at=0")
            self.assertTrue(self.state.enqueue(self.jf.scope, track, 0))
            self.worker().run(self.jf.scope)
            self.assertEqual(search.call_count, 2)

    def test_failure_uses_short_error_cache_not_not_found(self):
        track = self.queue()
        with patch.object(self.genius, "search", side_effect=app.FetchError("HTTP 403")):
            self.assertEqual(self.worker().run(self.jf.scope), {"error": 1})
        cache = self.state.lookup(track.key)
        self.assertEqual(cache["status"], "error")
        self.assertAlmostEqual(cache["retry_at"] - cache["attempted"], 3600, delta=2)

    def test_retry_negative_cache(self):
        track = self.queue()
        with patch.object(self.genius, "search", return_value=(None, None)):
            self.worker().run(self.jf.scope)
        self.assertTrue(self.state.enqueue(self.jf.scope, track, 0, retry=True))
        self.worker().run(self.jf.scope)
        self.assertEqual(self.state.lookup(track.key)["status"], "found")

    def test_upload_failure_reuses_successful_lyrics(self):
        track = self.queue(publish=True)
        self.fixture.upload_error = True
        self.assertEqual(self.worker(self.jf).run(self.jf.scope), {"error": 1})
        self.assertEqual(self.state.lookup(track.key)["status"], "found")
        self.fixture.upload_error = False
        self.assertTrue(self.state.enqueue(self.jf.scope, track, 0, True, retry=True))
        self.assertEqual(self.worker(self.jf).run(self.jf.scope), {"uploaded": 1})
        self.assertEqual(self.fixture.genius_queries, ["Alpha Artist"])

    def test_local_then_upload_has_no_second_genius_lookup(self):
        track = self.queue()
        self.worker(self.jf).run(self.jf.scope)
        self.assertTrue(self.state.enqueue(self.jf.scope, track, 0, True))
        self.assertEqual(self.worker(self.jf).run(self.jf.scope), {"uploaded": 1})
        self.assertEqual(self.fixture.genius_queries, ["Alpha Artist"])
        self.assertTrue(self.jf.has_lyrics("a"))

    def test_explicit_retry_does_not_override_durable_success(self):
        track = self.queue(publish=True)
        self.worker(self.jf).run(self.jf.scope)
        del self.fixture.uploads["a"]
        self.assertFalse(self.state.enqueue(self.jf.scope, track, 0, True, retry=True))
        self.assertEqual(self.worker(self.jf).run(self.jf.scope), {})
        self.assertEqual(self.fixture.genius_queries, ["Alpha Artist"])

    def test_remixes_do_not_match_original(self):
        lyrics, url = self.genius.search(app.Track("a", "Alpha (remix)", ["Other artist"]))
        self.assertIsNone(lyrics)
        self.assertIsNone(url)
        self.assertNotEqual(app.Track("a", "Alpha", ["Artist"]).key,
                            app.Track("b", "Alpha (remix)", ["Artist"]).key)

    def test_featured_artist_credit_matches_lead_but_band_name_stays_whole(self):
        lyrics, _ = self.genius.search(app.Track("a", "Alpha", ["Artist Featuring Guest"]))
        self.assertEqual(lyrics, "fixture line")
        self.assertEqual(self.fixture.genius_queries, ["Alpha Artist"])
        self.assertEqual(app.lead_artist("Simon & Garfunkel"), "Simon & Garfunkel")
        self.assertEqual(app.lead_artist("Little Feat"), "Little Feat")

    def test_ranking_cache_refresh_and_user_scope(self):
        with patch.object(self.jf, "ranked", wraps=self.jf.ranked) as ranked:
            self.state.ranked(self.jf)
            self.state.ranked(self.jf)
            self.assertEqual(ranked.call_count, 1)
            self.state.ranked(self.jf, 0)
            self.assertEqual(ranked.call_count, 2)
        other = app.Jellyfin("http://fixture.test", "secret-jellyfin", users=["Alice"])
        self.assertEqual(self.state.ranked(other)[0].id, "b")

    def test_extract_removes_headers_and_preserves_line_breaks(self):
        html = ('<div data-lyrics-container="true"><div class="LyricsHeader_x">Title</div>'
                'first<br/>second<span data-exclude-from-selection="true">advert</span></div>'
                '<div data-lyrics-container="true">third</div>')
        self.assertEqual(app.Genius.extract(html), "first\nsecond\nthird")
        with self.assertRaises(app.FetchError):
            app.Genius.extract("<html>blocked</html>")

    def test_recover_interrupted_job(self):
        self.queue()
        self.state.finish(self.jf.scope, "a", "running")
        self.assertEqual(self.worker().run(self.jf.scope), {"saved": 1})

    def test_actual_cli_pipeline_priority_background_upload_and_second_run(self):
        env = {"JELLYFIN_BASE_URL": "http://fixture.test", "JELLYFIN_API_KEY": "secret-jellyfin",
               "GENIUS_CLIENT_ACCESS": "secret-genius"}
        args = ["--state-dir", self.temp.name, "--priority", "b", "--top", "1", "--upload"]
        with patch.dict(os.environ, env), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(app.main(args), 0)
            self.assertEqual(self.fixture.genius_queries, ["Beta Artist", "Alpha Artist"])
            self.assertEqual(set(self.fixture.uploads), {"a", "b"})
            # Repeat the priority without requesting an additional background song.
            self.assertEqual(app.main(args + ["--top", "0"]), 0)
            self.assertEqual(len(self.fixture.genius_queries), 2)

    def test_cli_minimum_plays_filters_list_and_background(self):
        env = {"JELLYFIN_BASE_URL": "http://fixture.test", "JELLYFIN_API_KEY": "secret-jellyfin",
               "GENIUS_CLIENT_ACCESS": "secret-genius"}
        args = ["--state-dir", self.temp.name, "--min-plays", "9", "--top", "100"]
        with patch.dict(os.environ, env), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(app.main(args + ["--list"]), 0)
            listed = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([(t["id"], t["plays"]) for t in listed], [("a", 12)])
            self.assertEqual(app.main(args + ["--upload"]), 0)
        self.assertEqual(self.fixture.genius_queries, ["Alpha Artist"])
        self.assertEqual(set(self.fixture.uploads), {"a"})

    def test_enqueue_only_does_not_wait_fetch_or_require_genius_token(self):
        env = {"JELLYFIN_BASE_URL": "http://fixture.test", "JELLYFIN_API_KEY": "secret-jellyfin"}
        with patch.dict(os.environ, env, clear=True), patch.object(app.Worker, "run", side_effect=AssertionError("must not drain")):
            self.assertEqual(app.main(["--state-dir", self.temp.name, "--priority", "b",
                                       "--upload", "--enqueue-only", "--top", "0"]), 0)
        job = self.state.db.execute("SELECT status,priority,publish FROM jobs WHERE item='b'").fetchone()
        self.assertEqual(tuple(job), ("pending", 0, 1))
        self.assertEqual(self.fixture.genius_queries, [])
        self.assertEqual(self.fixture.uploads, {})

    def disk_fixture(self):
        root = Path(self.temp.name) / "music"
        root.mkdir()
        self.fixture.library["Locations"] = [str(root)]
        for index, track in enumerate(self.fixture.items["u1"]):
            path = root / (track["Id"] + ".mp3")
            path.write_bytes(b"fixture")
            os.utime(path, (400 - index * 100, 100 + index * 100))
            track["Path"] = str(path)
        return root

    def test_disk_fallback_selects_mtime_on_noatime_filesystem(self):
        self.disk_fixture()
        with patch.object(app.os, "statvfs", return_value=SimpleNamespace(f_flag=os.ST_NOATIME)):
            tracks = self.jf.disk_ranked()
        self.assertEqual([track.id for track in tracks], ["zero", "c", "b", "a"])
        self.assertTrue(all(track.plays == 0 for track in tracks))

    def test_album_artist_objects_are_names_in_search_identity(self):
        raw = item("album", "Song", "", 0)
        raw["Artists"] = []
        raw["AlbumArtists"] = [{"Name": "Artist", "Id": "artist-id"}]
        track = app.Track.from_item(raw)
        self.assertEqual(track.artists, ["Artist"])
        self.assertEqual(track.key, app.Track("other", "Song", ["Artist"]).key)
        self.assertTrue(self.state.eligible(self.jf.scope, track, True))

    def test_disk_fallback_tolerates_missing_and_invalid_metadata(self):
        self.disk_fixture()
        raw = self.fixture.items["u1"][0]
        raw["Name"] = None
        raw["Artists"] = [None, " "]
        raw["AlbumArtists"] = [{"Id": "missing-name"}]
        raw["UserData"]["PlayCount"] = None
        self.jf._libraries = [self.fixture.library]
        # This transport orders by play count before returning the disk catalog.
        with patch.object(self.jf, "get", return_value={"Items": [raw], "TotalRecordCount": 1}):
            tracks = self.jf.disk_ranked()
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "")
        self.assertEqual(tracks[0].artists, [])
        self.assertFalse(self.state.eligible(self.jf.scope, tracks[0], True))

    def test_disk_fallback_selects_access_time_when_enabled(self):
        self.disk_fixture()
        with patch.object(app.os, "statvfs", return_value=SimpleNamespace(f_flag=0)):
            tracks = self.jf.disk_ranked()
        self.assertEqual([track.id for track in tracks], ["a", "b", "c", "zero"])

    def test_disk_fallback_skips_missing_files_and_files_outside_library(self):
        root = self.disk_fixture()
        (root / "a.mp3").unlink()
        self.fixture.items["u1"][1]["Path"] = str(Path(self.temp.name) / "outside.mp3")
        Path(self.fixture.items["u1"][1]["Path"]).write_bytes(b"outside")
        with patch.object(app.os, "statvfs", return_value=SimpleNamespace(f_flag=os.ST_NOATIME)):
            self.assertEqual([track.id for track in self.jf.disk_ranked()], ["zero", "c"])

    def test_service_batch_completes_played_then_disk_slots_with_shared_cache(self):
        self.disk_fixture()
        for track in self.jf.ranked():
            self.state.enqueue(self.jf.scope, track, 1, True)
            self.state.finish(self.jf.scope, track.id, "uploaded")
        env = {"JELLYFIN_BASE_URL": "http://fixture.test", "JELLYFIN_API_KEY": "secret-jellyfin",
               "GENIUS_CLIENT_ACCESS": "secret-genius"}
        args = ["--state-dir", self.temp.name, "--top", "1", "--disk-fallback", "--upload"]
        with patch.dict(os.environ, env), patch.object(app.os, "statvfs", return_value=SimpleNamespace(f_flag=os.ST_NOATIME)):
            self.assertEqual(app.main(args), 0)
        self.assertEqual(self.fixture.genius_queries, ["Never played Artist"])
        self.assertEqual(set(self.fixture.uploads), {"zero"})

    def test_allow_song_errors_succeeds_only_when_batch_fully_drained(self):
        env = {"JELLYFIN_BASE_URL": "http://fixture.test", "JELLYFIN_API_KEY": "secret-jellyfin",
               "GENIUS_CLIENT_ACCESS": "secret-genius"}
        args = ["--state-dir", self.temp.name, "--top", "1", "--allow-song-errors", "--upload"]
        with patch.dict(os.environ, env), patch.object(app.Genius, "search", side_effect=app.FetchError("Unreadable page")):
            self.assertEqual(app.main(args), 0)
        self.assertEqual(self.state.db.execute("SELECT status FROM jobs WHERE item='a'").fetchone()[0], "error")

    def test_allow_song_errors_does_not_hide_rate_limited_incomplete_batch(self):
        env = {"JELLYFIN_BASE_URL": "http://fixture.test", "JELLYFIN_API_KEY": "secret-jellyfin",
               "GENIUS_CLIENT_ACCESS": "secret-genius"}
        args = ["--state-dir", self.temp.name, "--top", "2", "--allow-song-errors", "--upload"]
        with patch.dict(os.environ, env), patch.object(app.Genius, "search", side_effect=app.FetchError("Rate limited", 120)):
            self.assertEqual(app.main(args), 1)
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM jobs WHERE status='pending'").fetchone()[0], 1)

    def test_service_runner_reuses_cache_and_loads_credentials_from_file(self):
        track = self.queue(publish=True)
        self.state.save_lookup(track.key, "found", "existing cached lyric", "https://genius.com/alpha-lyrics")
        self.state.finish(self.jf.scope, "a", "uploaded")
        secret_file = Path(self.temp.name) / "credentials.json"
        secret_file.write_text(json.dumps({"JELLYFIN_API_KEY": "secret-jellyfin", "GENIUS_CLIENT_ACCESS": "secret-genius"}))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(batch.main(['--top', '1', '--credentials-file', str(secret_file),
                '--state-dir', self.temp.name, '--server-url', 'http://fixture.test']), 0)
        self.assertEqual(self.fixture.genius_queries, ["Beta Artist"])
        self.assertEqual(set(self.fixture.uploads), {"b"})

    def test_service_missing_credentials_fails_before_network_calls(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(batch.main(['--credentials-file', str(Path(self.temp.name) / 'absent.json')]), 1)
        self.assertEqual(self.fixture.requests, [])

    def test_rate_limit_ends_worker_and_leaves_rest_pending(self):
        self.queue()
        self.queue("Beta", "b")
        with patch.object(self.genius, "search", side_effect=app.FetchError("HTTP 429", 120)):
            self.assertEqual(self.worker().run(self.jf.scope), {"error": 1})
        pending = self.state.db.execute("SELECT count(*) FROM jobs WHERE status='pending'").fetchone()[0]
        self.assertEqual(pending, 1)

    def test_http_rate_limit_sets_shared_cooldown(self):
        with patch.object(self.genius.session, "get", return_value=response(
                status=429, headers={"Retry-After": "120"})):
            with self.assertRaises(app.FetchError) as error:
                self.genius.request("https://api.genius.com/search", api=True)
        self.assertEqual(error.exception.retry_after, 120)
        next_request = self.state.db.execute(
            "SELECT value FROM settings WHERE key='next_request'").fetchone()[0]
        self.assertGreater(next_request - app.time.time(), 119)

    def test_retry_429_waits_then_retries_same_request_and_resets_backoff(self):
        self.pace_patch.stop()
        now = [1000.]
        def sleep(seconds):
            now[0] += seconds
        genius = app.Genius("secret-genius", self.state, 1, retry_429=True)
        responses = [response(status=429, headers={"Retry-After": "30"}),
                     response(status=429, headers={"Retry-After": "200"}), response({"ok": True})]
        with patch.object(app.time, "time", side_effect=lambda: now[0]), \
                patch.object(app.time, "sleep", side_effect=sleep) as sleeper, \
                patch.object(genius.session, "get", side_effect=responses) as getter:
            self.assertEqual(genius.request("https://api.genius.com/search", api=True).json(), {"ok": True})
        self.assertEqual(getter.call_count, 3)
        self.assertEqual([call.args[0] for call in sleeper.call_args_list if call.args[0] > 0], [1, 60, 200])
        self.assertEqual(self.state.db.execute("SELECT value FROM settings WHERE key='genius_429_streak'").fetchone()[0], 0)
        self.assertEqual(self.state.db.execute("SELECT value FROM settings WHERE key='request_count'").fetchone()[0], 3)

    def test_429_backoff_survives_new_state_connection_and_caps_exponential_wait(self):
        self.assertEqual(self.state.backoff_429(), (60, 1))
        second = app.State(Path(self.temp.name))
        try:
            self.assertEqual(second.backoff_429(), (120, 2))
            waits = [second.backoff_429()[0] for _ in range(6)]
            self.assertEqual(waits, [240, 480, 960, 1920, 3600, 3600])
            self.assertEqual(second.backoff_429(7200)[0], 7200)
            second.reset_429()
            self.assertEqual(self.state.backoff_429(), (60, 1))
        finally:
            second.db.close()

    def test_429_honors_http_date_retry_after(self):
        from email.utils import formatdate
        with patch.object(app.time, "time", return_value=1000), \
                patch.object(self.genius.session, "get", return_value=response(
                    status=429, headers={"Retry-After": formatdate(1180, usegmt=True)})):
            with self.assertRaises(app.FetchError) as error:
                self.genius.request("https://api.genius.com/search", api=True)
        self.assertEqual(error.exception.retry_after, 180)

    def test_continuous_runner_repeats_batches_and_pauses_until_interrupted(self):
        secret_file = Path(self.temp.name) / "credentials.json"
        secret_file.write_text(json.dumps({"JELLYFIN_API_KEY": "secret-jellyfin", "GENIUS_CLIENT_ACCESS": "secret-genius"}))
        with patch.dict(os.environ, {}, clear=True), patch.object(batch.time, "sleep", side_effect=[None, KeyboardInterrupt]) as sleeper:
            with self.assertRaises(KeyboardInterrupt):
                batch.main(['--top', '1', '--delay', '1', '--continuous', '--idle-seconds', '900',
                    '--credentials-file', str(secret_file), '--state-dir', self.temp.name,
                    '--server-url', 'http://fixture.test'])
        self.assertEqual(self.fixture.genius_queries, ["Alpha Artist", "Beta Artist"])
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [900, 900])
        self.assertEqual(set(self.fixture.uploads), {"a", "b"})

    def test_gap_starts_after_response_completion(self):
        now = [1000.]
        def get(*args, **kwargs):
            now[0] += 0.5
            return response({})
        with patch.object(app.time, "time", side_effect=lambda: now[0]), \
                patch.object(self.genius.session, "get", side_effect=get):
            self.genius.request("https://api.genius.com/search", api=True)
        next_request = self.state.db.execute(
            "SELECT value FROM settings WHERE key='next_request'").fetchone()[0]
        self.assertEqual(next_request, 1002.75)

    def test_worker_lock_serializes_processes_while_queue_accepts_priority(self):
        context = multiprocessing.get_context("fork")
        started, acquired = context.Event(), context.Event()
        directory = self.temp.name
        def competitor():
            state = app.State(Path(directory))
            started.set()
            with state.worker_lock():
                acquired.set()
            state.db.close()
        with self.state.worker_lock():
            process = context.Process(target=competitor)
            process.start()
            try:
                self.assertTrue(started.wait(3))
                self.assertFalse(acquired.wait(0.1))
                self.assertTrue(self.state.enqueue(self.jf.scope,
                    app.Track("priority", "Beta", ["Artist"]), 0))
            except BaseException:
                process.terminate()
                process.join()
                raise
        try:
            self.assertTrue(acquired.wait(3))
        finally:
            process.join(3)
            if process.is_alive():
                process.terminate()
                process.join()
        self.assertEqual(process.exitcode, 0)

    def test_delay_persists_across_worker_invocations(self):
        self.pace_patch.stop()
        now = [1000.0]
        def sleep(seconds):
            now[0] += seconds
        with patch.object(app.time, "time", side_effect=lambda: now[0]), \
                patch.object(app.time, "sleep", side_effect=sleep) as sleeper:
            self.state.pace(2)
            second = app.State(Path(self.temp.name))
            try:
                second.pace(2)
                self.assertEqual(now[0], 1002)
                self.state.cooldown(60)
                second.pace(2)
                self.assertEqual(now[0], 1062)
                self.assertEqual(sleeper.call_count, 2)
            finally:
                second.db.close()
        self.pace_patch.start()


if __name__ == "__main__":
    unittest.main()


class LoggingTests(unittest.TestCase):
    def test_persistent_skip_and_run_correlation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            state = app.State(path)
            track = app.Track("id", "Song", ["Artist"])
            token = app._run_id.set("test-run")
            try:
                state.save_lookup(track.key, "found", "Hello world", "https://genius.com/song-lyrics")
                state.enqueue("server", track, 0)
                state.finish("server", track.id, "saved")
                self.assertFalse(state.enqueue("server", track, 0))
            finally:
                app._run_id.reset(token)
                state.db.close()
            records = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
            skipped = records[-1]
            self.assertEqual(skipped["event"], "track_skipped")
            self.assertEqual(skipped["reason"], "local_lyric_already_saved")
            self.assertEqual(skipped["lyrics_length"], 10)
            self.assertEqual(skipped["lyrics_characters"], 11)
            self.assertEqual(skipped["artists"], ["Artist"])
            self.assertEqual(skipped["title"], "Song")
            self.assertEqual(skipped["run_id"], "test-run")
            self.assertIsInstance(skipped["epoch"], float)

    def test_failed_start_is_logged_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            self.assertEqual(app.main(["--state-dir", directory, "--top", "0"]), 1)
            records = [json.loads(line) for line in (Path(directory) / "events.jsonl").read_text().splitlines()]
            self.assertEqual([r["event"] for r in records], ["run_started", "run_finished"])
            self.assertEqual(records[-1]["result"], "fail")
            self.assertEqual(records[0]["run_id"], records[-1]["run_id"])


class TitleArtistFallbackTests(unittest.TestCase):
    def test_spaced_dash_fallback_and_preserved_song_suffix(self):
        for separator in (" - ", " -- ", " – ", " — "):
            with self.subTest(separator=separator):
                track = app.Track.from_item({"Id": "id", "Name": "Gorgon City" + separator + "Imagination - Remix"})
                self.assertEqual(track.artists, ["Gorgon City"])
                self.assertEqual(track.title, "Imagination - Remix")

    def test_metadata_takes_precedence(self):
        for field in ("Artists", "AlbumArtists"):
            track = app.Track.from_item({"Id": "id", "Name": "Artist - Song", field: ["Tagged Artist"]})
            self.assertEqual(track.artists, ["Tagged Artist"])
            self.assertEqual(track.title, "Artist - Song")

    def test_invalid_or_ambiguous_separators_do_not_invent_artist(self):
        for title in ("Artist - ", " - Song", "Song"):
            with self.subTest(title=title):
                track = app.Track.from_item({"Id": "id", "Name": title})
                self.assertEqual(track.artists, [])
                self.assertEqual(track.title, title)

    def test_search_uses_extracted_title_and_artist(self):
        with tempfile.TemporaryDirectory() as directory:
            state = app.State(Path(directory))
            track = app.Track.from_item({"Id": "id", "Name": "Artist - Alpha"})
            genius = app.Genius("token", state, 1)
            with patch.object(genius, "request", return_value=response({"response": {"hits": []}})) as request:
                self.assertEqual(genius.search(track), (None, None))
                self.assertEqual(request.call_args.kwargs["params"], {"q": "Alpha Artist"})
            state.db.close()


class TrackNumberTests(unittest.TestCase):
    def test_number_and_unspaced_artist_fallback(self):
        for prefix in ("4-", "04-", "004-", "04 - ", "04. "):
            track = app.Track.from_item({"Id": "id", "Name": prefix + "shakira-animal city"})
            self.assertEqual((track.title, track.artists), ("animal city", ["shakira"]))

    def test_tagged_artist_and_numeric_song_titles(self):
        for raw, expected in (("04 - More Or Less", "More Or Less"), ("1999", "1999"), ("1234 - Song", "1234 - Song"), ("99 Luftballons", "99 Luftballons")):
            track = app.Track.from_item({"Id": "id", "Name": raw, "Artists": ["Artist"]})
            self.assertEqual(track.title, expected)
            self.assertEqual(track.artists, ["Artist"])


class RampTests(unittest.TestCase):
    def test_delay_increases_caps_and_resets(self):
        with tempfile.TemporaryDirectory() as directory:
            state = app.State(Path(directory))
            with patch.object(state, "pace") as pace, patch.object(state, "cooldown"), patch.object(requests.Session, "get", return_value=response({})):
                first = app.Genius("token", state, 0.5, delay_step=0.25, max_delay=1)
                for _ in range(5):first.request("https://api.genius.com/search", api=True)
                second = app.Genius("token", state, 0.5, delay_step=0.25, max_delay=1)
                second.request("https://api.genius.com/search", api=True)
                self.assertEqual([c.args[0] for c in pace.call_args_list], [0.5,0.75,1,1,1,0.5])
            state.db.close()


class BackoffLoggingTests(unittest.TestCase):
    def test_429_header_and_computed_backoff_are_durable_log_records(self):
        for header, expected in ((None, 60), ("180", 180)):
            with self.subTest(header=header), tempfile.TemporaryDirectory() as directory:
                state = app.State(Path(directory))
                genius = app.Genius("secret", state, 0.5)
                with patch.object(state, "pace"), patch.object(genius.session, "get", return_value=response(status=429, headers={"Retry-After": header} if header else {})):
                    with self.assertRaises(app.FetchError):genius.request("https://api.genius.com/search", api=True)
                state.db.close()
                records = [json.loads(l) for l in (Path(directory)/"events.jsonl").read_text().splitlines()]
                backoff = next(r for r in records if r["event"] == "genius_backoff")
                self.assertEqual(backoff["retry_after"], header)
                self.assertEqual(backoff["http_status"], 429)
                self.assertEqual(backoff["seconds"], expected)
                reopened = app.State(Path(directory))
                self.assertEqual(reopened.db.execute("SELECT value FROM settings WHERE key='next_request'").fetchone()[0], backoff["retry_at"])
                reopened.db.close()


class MaintenanceTests(unittest.TestCase):
    def test_log_retention_and_rotation(self):
        from datetime import datetime, timedelta
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            today = datetime.now().date()
            old = today - timedelta(days=1)
            current = path / "events.jsonl"
            current.write_text("old record\n")
            old_time = datetime.combine(old, datetime.min.time()).timestamp()
            os.utime(current, (old_time, old_time))
            expired = path / ("events." + (today-timedelta(days=7)).isoformat()+".jsonl")
            expired.write_text("expired")
            app.audit(path, "test")
            self.assertEqual((path/("events."+old.isoformat()+".jsonl")).read_text(), "old record\n")
            self.assertFalse(expired.exists())
            self.assertEqual(json.loads(current.read_text())["event"], "test")

    def test_monthly_maintenance_marker_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            state = app.State(Path(directory))
            self.assertTrue(state.maintain())
            state.db.close()
            second = app.State(Path(directory))
            self.assertFalse(second.maintain())
            self.assertEqual(second.db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            second.db.close()

    def test_expired_error_requeues_without_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            state = app.State(Path(directory))
            track = app.Track("id", "Song", ["Artist"])
            state.enqueue("standalone", track, 0)
            state.finish("standalone", track.id, "error", retry_at=0)
            genius = SimpleNamespace(search=lambda track: ("real lyrics", "https://genius.com/song-lyrics"))
            self.assertEqual(app.Worker(state, genius).run("standalone"), {"saved": 1})
            state.db.close()


class HashedLyricsTests(unittest.TestCase):
    def test_database_stores_hash_and_missing_or_changed_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state = app.State(Path(directory))
            state.save_lookup("key", "found", "Real lyrics", "url")
            row = state.db.execute("SELECT * FROM lookups").fetchone()
            self.assertNotIn("lyrics", row.keys())
            self.assertEqual(len(row["lyrics_hash"]), 16)
            self.assertEqual(state.lookup("key")["lyrics"], "Real lyrics")
            (Path(directory)/"lyrics/key.txt").write_text("changed")
            with self.assertRaises(app.FetchError):state.lookup("key")
            (Path(directory)/"lyrics/key.txt").unlink()
            with self.assertRaises(app.FetchError):state.lookup("key")
            state.db.close()

    def test_legacy_migration_retains_text_status_and_retry_dates(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with sqlite3.connect(path/"state.sqlite3") as db:
                db.execute("CREATE TABLE lookups(key TEXT PRIMARY KEY,status TEXT NOT NULL,lyrics TEXT,url TEXT,attempted REAL NOT NULL,retry_at REAL NOT NULL,reason TEXT)")
                db.execute("INSERT INTO lookups VALUES('key','found','Real lyrics','url',123,456,NULL)")
                db.execute("INSERT INTO lookups VALUES('miss','not_found',NULL,NULL,123,456,'missing')")
            state = app.State(path)
            self.assertEqual(state.lookup("key")["lyrics"], "Real lyrics")
            self.assertEqual(state.lookup("key")["attempted"], 123)
            self.assertEqual(state.lookup("miss")["retry_at"], 456)
            state.db.close()
            reopened = app.State(path)
            self.assertEqual(reopened.lookup("key")["lyrics"], "Real lyrics")
            reopened.db.close()


class QueuePromotionTests(unittest.TestCase):
    def test_single_request_promotes_existing_priority_to_front_without_duplication(self):
        with tempfile.TemporaryDirectory() as directory:
            state = app.State(Path(directory))
            target = app.Track("target", "Target", ["Artist"], plays=1)
            other = app.Track("other", "Other", ["Artist"], plays=100)
            background = app.Track("background", "Background", ["Artist"], plays=1000)
            with patch.object(app.time, "time", return_value=100):state.enqueue("standalone", target, 0)
            with patch.object(app.time, "time", return_value=200):state.enqueue("standalone", other, 0)
            state.enqueue("standalone", background, 1)
            self.assertEqual(state.next_job("standalone")["item"], "other")
            with patch.object(app.time, "time", return_value=300):state.enqueue("standalone", target, 0)
            self.assertEqual(state.next_job("standalone")["item"], "target")
            self.assertEqual(state.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 3)
            seen=[]
            genius=SimpleNamespace(search=lambda track: (seen.append(track.id) or None, None))
            app.Worker(state, genius).run("standalone")
            self.assertEqual(seen, ["target", "other", "background"])
            state.db.close()


class SuccessStubTests(unittest.TestCase):
    def test_success_retains_only_stub_and_removes_unreferenced_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            state = app.State(Path(directory))
            track = app.Track("id", "Song", ["Artist"], path="/large/path.mp3")
            state.enqueue("server", track, 0, publish=True)
            state.save_lookup(track.key, "found", "Real lyric", "url")
            state.finish("server", track.id, "uploaded")
            row = state.db.execute("SELECT * FROM jobs").fetchone()
            self.assertIsNone(row["local_metadata"])
            self.assertEqual(row["key"], "")
            self.assertEqual(row["status"], "uploaded")
            self.assertIsNone(state.lookup(track.key))
            self.assertFalse((Path(directory)/"lyrics"/(track.key+".txt")).exists())
            self.assertFalse(state.enqueue("server", track, 0, True, retry=True))
            track.title="Changed metadata"
            self.assertFalse(state.enqueue("server", track, 0, True, retry=True))
            state.db.close()

    def test_cache_kept_until_other_duplicate_job_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            state = app.State(Path(directory))
            first = app.Track("a", "Song", ["Artist"])
            second = app.Track("b", "Song", ["Artist"])
            for track in (first,second):state.enqueue("server",track,0,True)
            state.save_lookup(first.key,"found","Real lyric","url")
            state.finish("server","a","uploaded")
            self.assertIsNotNone(state.lookup(first.key))
            state.finish("server","b","kept")
            self.assertIsNone(state.lookup(first.key))
            state.db.close()

    def test_compaction_preserves_pending_failed_and_local_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            state=app.State(Path(directory))
            for id in ("success","pending","failure"):
                state.enqueue("server",app.Track(id,id,["Artist"]),0,True)
            with state.db:
                state.db.execute("UPDATE jobs SET status='uploaded' WHERE item='success'")
                state.db.execute("UPDATE jobs SET status='error',retry_at=123,reason='failure' WHERE item='failure'")
            pending=tuple(state.db.execute("SELECT * FROM jobs WHERE item='pending'").fetchone())
            failed=tuple(state.db.execute("SELECT * FROM jobs WHERE item='failure'").fetchone())
            self.assertEqual(state.compact_successes()["success_stubs"],1)
            self.assertEqual(tuple(state.db.execute("SELECT * FROM jobs WHERE item='pending'").fetchone()),pending)
            self.assertEqual(tuple(state.db.execute("SELECT * FROM jobs WHERE item='failure'").fetchone()),failed)
            state.db.close()


class CompactQueueTests(unittest.TestCase):
    def test_legacy_queue_migration_preserves_state_and_local_inputs(self):
        import sqlite3
        scope = 'ab' * 32
        item_id = 'cd' * 16
        track = app.Track(item_id, 'Old title', ['Artist'], plays=8, path='/large/path.mp3')
        local = app.Track('local', 'Standalone song', ['Singer'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with sqlite3.connect(path / 'state.sqlite3') as db:
                db.execute('''CREATE TABLE jobs(server TEXT NOT NULL,item TEXT NOT NULL,track TEXT NOT NULL,
                    key TEXT NOT NULL,priority INTEGER NOT NULL,plays INTEGER NOT NULL,publish INTEGER NOT NULL,
                    status TEXT NOT NULL,queued REAL NOT NULL,retry_at REAL NOT NULL DEFAULT 0,reason TEXT,
                    PRIMARY KEY(server,item))''')
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (scope,item_id,json.dumps(app.asdict(track)),track.key,0,8,1,'error',100,200,'network'))
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    ('standalone','local',json.dumps(app.asdict(local)),local.key,1,0,0,'pending',101,0,None))
                db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value REAL NOT NULL)')
                db.execute("INSERT INTO settings VALUES('next_request',9999999999)")
            state = app.State(path)
            row = state.job(scope,item_id)
            self.assertNotIn('track',row)
            self.assertIsNone(row['local_metadata'])
            self.assertEqual((row['key'],row['status'],row['queued'],row['retry_at'],row['reason']),
                             (track.key,'error',100,200,'network'))
            raw=state.db.execute('SELECT item,key FROM jobs WHERE server=?',(state.server_id(scope),)).fetchone()
            self.assertEqual((len(raw['item']),len(raw['key'])),(16,32))
            self.assertEqual(json.loads(state.job('standalone','local')['local_metadata']),
                             {'title':'Standalone song','artists':['Singer']})
            self.assertEqual(state.db.execute("SELECT value FROM settings WHERE key='next_request'").fetchone()[0],9999999999)
            self.assertEqual(state.db.execute('PRAGMA user_version').fetchone()[0],3)
            self.assertEqual(state.db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertIn('WITHOUT ROWID',state.db.execute("SELECT sql FROM sqlite_schema WHERE name='jobs'").fetchone()[0])
            state.db.close()
            reopened=app.State(path)
            self.assertEqual(reopened.job(scope,item_id)['retry_at'],200)
            reopened.db.close()

    def test_worker_resolves_current_metadata_and_updates_lookup_key(self):
        with tempfile.TemporaryDirectory() as directory:
            state=app.State(Path(directory))
            old=app.Track('id','Old title',['Old artist'])
            fresh=app.Track('id','Current title',['Current artist'])
            state.enqueue('server',old,0)
            seen=[]
            genius=SimpleNamespace(search=lambda track: (seen.append((track.title,track.artists)) or 'Real lyrics','url'))
            jf=SimpleNamespace(priority=lambda id:fresh)
            self.assertEqual(app.Worker(state,genius,jf).run('server'),{'saved':1})
            self.assertEqual(seen,[('Current title',['Current artist'])])
            self.assertEqual(state.job('server','id')['key'],fresh.key)
            self.assertIsNone(state.lookup(old.key))
            self.assertEqual(state.lookup(fresh.key)['lyrics'],'Real lyrics')
            state.db.close()

    def test_jellyfin_metadata_outage_retains_job_without_provider_request(self):
        with tempfile.TemporaryDirectory() as directory:
            state=app.State(Path(directory))
            track=app.Track('id','Song',['Artist'])
            state.enqueue('server',track,0,True)
            from unittest.mock import Mock
            genius=SimpleNamespace(search=Mock())
            jf=SimpleNamespace(priority=Mock(side_effect=app.FetchError('Jellyfin network request failed')))
            self.assertEqual(app.Worker(state,genius,jf).run('server'),{'error':1})
            row=state.job('server','id')
            self.assertEqual(row['status'],'error')
            self.assertEqual(row['reason'],'Jellyfin network request failed')
            self.assertGreater(row['retry_at'],app.time.time()+3500)
            self.assertEqual(row['key'],track.key)
            genius.search.assert_not_called()
            state.db.close()

    def test_pending_order_uses_partial_index(self):
        with tempfile.TemporaryDirectory() as directory:
            state=app.State(Path(directory))
            state.enqueue('server',app.Track('id','Song',['Artist']),0)
            plan=state.db.execute('''EXPLAIN QUERY PLAN SELECT * FROM jobs
                WHERE server=? AND status='pending'
                ORDER BY priority ASC,CASE WHEN priority=0 THEN queued END DESC,
                         plays DESC,queued ASC,item ASC LIMIT 1''',(state.server_id('server'),)).fetchall()
            details=' '.join(row[3] for row in plan)
            self.assertIn('pending_order',details)
            self.assertNotIn('TEMP B-TREE',details)
            state.db.close()
