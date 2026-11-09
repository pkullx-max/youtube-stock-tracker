import argparse
import datetime as dt
import json
import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tracker


CHANNEL = {"name": "测试频道", "channel_id": "UC123"}
RSS = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry><yt:videoId>abc123</yt:videoId><title>Test</title><published>2026-07-16T01:00:00+00:00</published></entry>
</feed>'''


def hold_lock(path, ready, release):
    with tracker.file_lock(Path(path)):
        ready.set()
        release.wait(5)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / ".lock"
        self.patchers = [
            mock.patch.object(tracker, "STATE_PATH", self.state_path),
            mock.patch.object(tracker, "LOCK_PATH", self.lock_path),
            mock.patch.object(tracker, "TRANSCRIPTS", self.root / "transcripts"),
            mock.patch.object(tracker, "AUDIO", self.root / "audio"),
            mock.patch.object(tracker, "RESEARCH_TRANSCRIPTS", self.root / "research" / "transcripts"),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def test_rss_parsing(self):
        rows = tracker.parse_rss(RSS, CHANNEL)
        self.assertEqual(rows[0]["video_id"], "abc123")
        self.assertEqual(rows[0]["channel_name"], "测试频道")

    def test_beijing_seven_natural_day_boundary(self):
        now = dt.datetime(2026, 7, 16, 10, 0, tzinfo=dt.timezone.utc)
        cutoff = tracker.beijing_cutoff(7, now)
        self.assertEqual(cutoff, dt.datetime(2026, 7, 9, 16, 0, tzinfo=dt.timezone.utc))

    def test_discovery_deduplicates_video_ids(self):
        config = {"channels": [dict(CHANNEL, enabled=True), dict(CHANNEL, name="重复", enabled=True)]}
        with mock.patch.object(tracker, "fetch_url", return_value=RSS):
            rows = tracker.discover(config, 7, dt.datetime(2026, 7, 16, tzinfo=dt.timezone.utc))
        self.assertEqual(len(rows), 1)

    def test_dry_run_does_not_write_state(self):
        args = argparse.Namespace(backfill_days=7, dry_run=True, limit=None)
        with mock.patch.object(tracker, "load_json", return_value={"channels": []}), \
             mock.patch.object(tracker, "discover", return_value=[]):
            tracker.run_command(args)
        self.assertFalse(self.state_path.exists())

    def test_coverage_threshold(self):
        segments = [{"start": 0, "end": 90, "text": "a"}, {"start": 90, "end": 180, "text": "b"}]
        coverage, gap = tracker.coverage_metrics(segments, 200)
        self.assertEqual(coverage, 0.9)
        self.assertEqual(gap, 20)

    def test_large_gap_is_detected(self):
        segments = [{"start": 0, "end": 100, "text": "a"}, {"start": 500, "end": 600, "text": "b"}]
        _, gap = tracker.coverage_metrics(segments, 600)
        self.assertEqual(gap, 400)

    def test_segments_are_bounded_by_video_duration(self):
        rows = tracker.bound_segments([
            {"start": -1, "end": 2, "text": "a"},
            {"start": 9, "end": 12, "text": "b"},
            {"start": 11, "end": 13, "text": "discard"},
        ], 10)
        self.assertEqual(rows, [
            {"start": 0.0, "end": 2.0, "text": "a"},
            {"start": 9.0, "end": 9.999, "text": "b"},
        ])

    def test_manual_retry(self):
        state = tracker.initial_state()
        state["videos"]["v1"] = {"status": "needs_manual_review", "last_error": "x"}
        tracker.save_json(self.state_path, state)
        tracker.retry_command("v1")
        video = tracker.load_json(self.state_path)["videos"]["v1"]
        self.assertEqual(video["status"], "retry")
        self.assertIsNone(video["last_error"])

    def test_missing_report_cannot_mark_reported(self):
        state = tracker.initial_state()
        state["videos"]["v1"] = {"status": "ready"}
        tracker.save_json(self.state_path, state)
        with self.assertRaises(SystemExit):
            tracker.mark_reported("v1", str(self.root / "missing.md"))
        self.assertEqual(tracker.load_json(self.state_path)["videos"]["v1"]["status"], "ready")

    def test_report_ready_then_reported_transition(self):
        report = self.root / "report.md"
        report.write_text("report")
        state = tracker.initial_state()
        state["videos"]["v1"] = {"status": "ready", "report_path": None, "reported_at": None}
        tracker.save_json(self.state_path, state)
        tracker.mark_report_ready("v1", str(report))
        self.assertEqual(tracker.load_json(self.state_path)["videos"]["v1"]["status"], "report_ready")
        tracker.mark_reported("v1", str(report))
        self.assertEqual(tracker.load_json(self.state_path)["videos"]["v1"]["status"], "reported")

    def test_ongoing_live_is_deferred_without_failed_attempt(self):
        state = tracker.initial_state()
        video = {"video_id": "v1", "status": "discovered", "attempt_count": 0,
                 "last_error": None, "title": "live"}
        state["videos"]["v1"] = video
        with mock.patch.object(tracker, "acquire_transcript", side_effect=tracker.DeferredVideo("live stream deferred")):
            self.assertFalse(tracker.process_video(video, {}, state))
        self.assertEqual(video["status"], "discovered")
        self.assertEqual(video["attempt_count"], 0)

    def test_repeatedly_interrupted_transcription_requires_manual_review(self):
        state = tracker.initial_state()
        state["videos"] = {
            "stale": {"status": "transcribing", "attempt_count": 3, "last_error": None},
            "recoverable": {"status": "transcribing", "attempt_count": 2, "last_error": None},
        }
        tracker.recover_interrupted_transcribing(state)
        self.assertEqual(state["videos"]["stale"]["status"], "needs_manual_review")
        self.assertEqual(state["videos"]["recoverable"]["status"], "retry")

    def test_concurrent_lock_rejected(self):
        ready, release = multiprocessing.Event(), multiprocessing.Event()
        process = multiprocessing.Process(target=hold_lock, args=(self.lock_path, ready, release))
        process.start()
        self.assertTrue(ready.wait(3))
        try:
            with self.assertRaises(RuntimeError):
                with tracker.file_lock(self.lock_path):
                    pass
        finally:
            release.set()
            process.join(3)

    def test_repeated_run_does_not_reprocess_reported_video(self):
        item = {"video_id": "v1", "channel_id": "c", "channel_name": "n", "title": "t",
                "url": "u", "published_at": "2026-07-16T00:00:00Z"}
        state = tracker.initial_state()
        state["videos"]["v1"] = tracker.new_video(item, "2026-07-16T01:00:00Z")
        state["videos"]["v1"]["status"] = "reported"
        tracker.save_json(self.state_path, state)
        args = argparse.Namespace(backfill_days=7, dry_run=False, limit=None)
        with mock.patch.object(tracker, "load_json", side_effect=lambda p: {"channels": []} if p == tracker.CONFIG_PATH else json.loads(p.read_text())), \
             mock.patch.object(tracker, "discover", return_value=[item]), \
             mock.patch.object(tracker, "process_video") as process_video:
            tracker.run_command(args)
        process_video.assert_not_called()

    def test_cleanup_removes_only_expired_reported_artifacts(self):
        now = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
        transcripts = self.root / "transcripts"
        audio = self.root / "audio"
        transcripts.mkdir()
        audio.mkdir()
        old_transcript = transcripts / "old.json"
        recent_transcript = transcripts / "recent.json"
        ready_transcript = transcripts / "ready.json"
        old_audio = audio / "old.m4a"
        for path in (old_transcript, recent_transcript, ready_transcript, old_audio):
            path.write_text("x")
        state = tracker.initial_state()
        state["videos"] = {
            "old": {"video_id": "old", "status": "reported", "reported_at": "2026-07-12T00:00:00Z",
                    "transcript_path": str(old_transcript), "report_path": str(self.root / "report.md")},
            "recent": {"video_id": "recent", "status": "reported", "reported_at": "2026-07-18T00:00:00Z",
                       "transcript_path": str(recent_transcript)},
            "ready": {"video_id": "ready", "status": "ready", "reported_at": None,
                      "transcript_path": str(ready_transcript)},
        }

        removed = tracker.cleanup_artifacts(state, now=now)

        self.assertEqual(set(removed), {str(old_transcript.resolve()), str(old_audio.resolve())})
        self.assertFalse(old_transcript.exists())
        self.assertFalse(old_audio.exists())
        self.assertTrue(recent_transcript.exists())
        self.assertTrue(ready_transcript.exists())
        self.assertIsNone(state["videos"]["old"]["transcript_path"])
        self.assertEqual(state["videos"]["old"]["report_path"], str(self.root / "report.md"))


if __name__ == "__main__":
    unittest.main()
