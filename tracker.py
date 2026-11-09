#!/usr/bin/env python3
"""YouTube stock-opinion tracker: discovery, transcript acquisition, and state."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import html
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "data" / "state.json"
LOCK_PATH = ROOT / "data" / ".tracker.lock"
TRANSCRIPTS = ROOT / "data" / "transcripts"
AUDIO = ROOT / "data" / "audio"
RESEARCH_TRANSCRIPTS = ROOT / "data" / "research" / "transcripts"
RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
FINAL_STATES = {"reported", "needs_manual_review"}
ARTIFACT_RETENTION_DAYS = 7


class DeferredVideo(RuntimeError):
    """An upcoming or ongoing live stream that must not be processed yet."""


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def initial_state() -> dict[str, Any]:
    return {"schema_version": 1, "last_checked_at": None, "videos": {}, "pending_delivery": []}


def cleanup_artifacts(state: dict[str, Any], retention_days: int = ARTIFACT_RETENTION_DAYS,
                      now: dt.datetime | None = None) -> list[str]:
    current = now or now_utc()
    cutoff = current - dt.timedelta(days=retention_days)
    removed: list[str] = []

    def remove(path: Path) -> bool:
        if not path.is_file():
            return False
        path.unlink()
        removed.append(str(path.resolve()))
        return True

    for video in state["videos"].values():
        reported_at = video.get("reported_at")
        if video.get("status") != "reported" or not reported_at or parse_time(reported_at) > cutoff:
            continue
        video_id = video["video_id"]
        for path in list(AUDIO.glob(f"{video_id}.*")) + list(AUDIO.glob(f"{video_id}-*")):
            remove(path)
        transcript_value = video.get("transcript_path")
        if transcript_value:
            transcript_path = Path(transcript_value).expanduser().resolve()
            if transcript_path.parent == TRANSCRIPTS.resolve() and remove(transcript_path):
                video["transcript_path"] = None
        video["artifacts_cleaned_at"] = iso(current)

    tracked_paths = {
        str(Path(video["transcript_path"]).expanduser().resolve())
        for video in state["videos"].values() if video.get("transcript_path")
    }
    for directory in (TRANSCRIPTS, RESEARCH_TRANSCRIPTS):
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            modified = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
            if str(path.resolve()) not in tracked_paths and modified <= cutoff:
                remove(path)
    return removed


@contextlib.contextmanager
def file_lock(path: Path = LOCK_PATH):
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another tracker instance is running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def beijing_cutoff(days: int, now: dt.datetime | None = None) -> dt.datetime:
    tz = ZoneInfo("Asia/Shanghai")
    local_now = (now or now_utc()).astimezone(tz)
    first_day = local_now.date() - dt.timedelta(days=max(days - 1, 0))
    return dt.datetime.combine(first_day, dt.time.min, tzinfo=tz).astimezone(dt.timezone.utc)


def fetch_url(url: str, timeout: int = 30, attempts: int = 3) -> bytes:
    user_agents = [
        "curl/8.7.1",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36",
        "Mozilla/5.0 YouTubeStockTracker/1.0",
    ]
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": user_agents[attempt % len(user_agents)],
                "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1 + attempt * 2)
    raise RuntimeError(f"RSS request failed after {attempts} attempts: {last_error}")


def parse_rss(data: bytes, channel: dict[str, Any]) -> list[dict[str, Any]]:
    ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(data)
    rows = []
    for entry in root.findall("atom:entry", ns):
        video_id = entry.findtext("yt:videoId", default="", namespaces=ns)
        published = entry.findtext("atom:published", default="", namespaces=ns)
        if not video_id or not published:
            continue
        rows.append({
            "video_id": video_id,
            "channel_id": channel["channel_id"],
            "channel_name": channel["name"],
            "title": entry.findtext("atom:title", default="", namespaces=ns),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "published_at": iso(parse_time(published)),
        })
    return rows


def discover(config: dict[str, Any], backfill_days: int, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    cutoff = beijing_cutoff(backfill_days, now)
    found: dict[str, dict[str, Any]] = {}
    for channel in config["channels"]:
        if not channel.get("enabled", True):
            continue
        try:
            items = parse_rss(fetch_url(RSS.format(channel["channel_id"]), timeout=10, attempts=1), channel)
        except Exception:
            items = discover_channel_page(config, channel, cutoff)
        for item in items:
            if parse_time(item["published_at"]) >= cutoff:
                found[item["video_id"]] = item
    return sorted(found.values(), key=lambda row: row["published_at"])


def new_video(item: dict[str, Any], when: str) -> dict[str, Any]:
    return {**item, "discovered_at": when, "status": "discovered", "attempt_count": 0,
            "last_error": None, "transcript_path": None, "report_path": None, "reported_at": None}


def recover_interrupted_transcribing(state: dict[str, Any], max_attempts: int = 3) -> None:
    """Close stale interrupted work instead of retrying it forever."""
    for video in state["videos"].values():
        if video.get("status") != "transcribing":
            continue
        if int(video.get("attempt_count") or 0) >= max_attempts:
            video["status"] = "needs_manual_review"
            video["last_error"] = "repeatedly interrupted during transcription; manual review required"
        else:
            video["status"] = "retry"
            video["last_error"] = "recovered after interrupted transcribing"


def ytdlp_base(config: dict[str, Any]) -> list[str]:
    # YouTube now requires EJS challenge solving and, for many clients, a
    # proof-of-origin token.  The current environment ships yt-dlp-ejs and
    # the bgutil PO-token provider plugin; Node is the supported JS runtime.
    cmd = [sys.executable, "-m", "yt_dlp", "--js-runtimes", "node"]
    browser = config.get("cookies_from_browser")
    if browser:
        cmd += ["--cookies-from-browser", browser]
    return cmd


def run_checked(cmd: list[str], timeout: int, attempts: int = 2) -> subprocess.CompletedProcess:
    errors = []
    for attempt in range(attempts):
        proc = None
        try:
            proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True)
            stdout, stderr = proc.communicate(timeout=timeout)
            result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
            if proc.returncode:
                raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)
            return result
        except subprocess.TimeoutExpired as exc:
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            errors.append(f"command timed out after {timeout}s: {' '.join(cmd[:3])}")
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            errors.append(detail[-2000:])
        except Exception as exc:
            errors.append(str(exc))
        if attempt + 1 < attempts:
            time.sleep(2 + attempt * 3)
    raise RuntimeError(" | ".join(errors))


def video_metadata(url: str, config: dict[str, Any]) -> dict[str, Any]:
    common = ["--dump-single-json", "--skip-download", "--no-check-formats", "--no-playlist", url]
    errors = []
    for extra in ([], ["--extractor-args", "youtube:player_client=android_vr"]):
        try:
            return json.loads(run_checked(ytdlp_base(config) + extra + common, 30, attempts=1).stdout)
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("metadata retrieval failed: " + " | ".join(errors))


def discover_channel_page(config: dict[str, Any], channel: dict[str, Any], cutoff: dt.datetime) -> list[dict[str, Any]]:
    """Fallback for periods when YouTube's Atom feed is unavailable."""
    page = channel["url"].rstrip("/") + "/videos"
    cmd = ytdlp_base(config) + ["--flat-playlist", "--playlist-end", "5",
                                "--dump-single-json", "--skip-download", page]
    payload = json.loads(run_checked(cmd, 30, attempts=1).stdout)
    rows = []
    for entry in payload.get("entries") or []:
        video_id = entry.get("id")
        if not video_id:
            continue
        try:
            meta = video_metadata(f"https://www.youtube.com/watch?v={video_id}", config)
            timestamp = meta.get("timestamp")
            if not timestamp:
                continue
            published = dt.datetime.fromtimestamp(float(timestamp), dt.timezone.utc)
            if published < cutoff:
                continue
            rows.append({"video_id": video_id, "channel_id": channel["channel_id"],
                         "channel_name": channel["name"],
                         "title": meta.get("title") or entry.get("title") or "",
                         "url": f"https://www.youtube.com/watch?v={video_id}",
                         "published_at": iso(published)})
        except Exception:
            continue
    return rows


def normalize_segments(items: Iterable[Any]) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        if isinstance(item, dict):
            start = float(item.get("start", 0))
            if "end" in item:
                end = float(item["end"])
            else:
                end = start + float(item.get("duration", 0))
            text = str(item.get("text", "")).strip()
        else:
            start = float(getattr(item, "start", 0))
            end = start + float(getattr(item, "duration", 0))
            text = str(getattr(item, "text", "")).strip()
        if text and end > start:
            rows.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return sorted(rows, key=lambda x: x["start"])


def coverage_metrics(segments: list[dict[str, Any]], duration: float) -> tuple[float, float]:
    if not segments or duration <= 0:
        return 0.0, duration if duration > 0 else 0.0
    intervals = [(max(0.0, s["start"]), min(duration, s["end"])) for s in segments]
    intervals = [(a, b) for a, b in intervals if b > a]
    if not intervals:
        return 0.0, duration
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start <= merged[-1][1] + 1.0:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    covered = sum(end - start for start, end in merged)
    gaps = [merged[0][0], duration - merged[-1][1]]
    gaps += [merged[i + 1][0] - merged[i][1] for i in range(len(merged) - 1)]
    return round(min(1.0, covered / duration), 4), round(max(gaps), 3)


def bound_segments(segments: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    bounded = []
    end_limit = max(0.0, duration - 0.001)
    for segment in segments:
        start = max(0.0, float(segment["start"]))
        end = min(end_limit, float(segment["end"]))
        if start < end_limit and end > start:
            bounded.append({"start": round(start, 3), "end": round(end, 3), "text": segment["text"]})
    return bounded


def meaningful(segments: list[dict[str, Any]], title: str) -> bool:
    text = "".join(s["text"] for s in segments).strip()
    normalized_title = re.sub(r"\W", "", title)
    normalized_text = re.sub(r"\W", "", text)
    return len(normalized_text) >= max(40, len(normalized_title) * 2) and normalized_text != normalized_title


def transcript_api(video_id: str) -> tuple[list[dict[str, Any]], str]:
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    available = list(api.list(video_id))
    preferred = sorted(available, key=lambda t: (t.is_generated, t.language_code not in {"zh", "zh-Hans", "zh-Hant", "en"}))
    if not preferred:
        raise RuntimeError("no YouTube transcript")
    chosen = preferred[0]
    method = "youtube_auto_captions" if chosen.is_generated else "youtube_official_captions"
    return normalize_segments(chosen.fetch()), method


def parse_vtt(path: Path) -> list[dict[str, Any]]:
    timing = re.compile(r"(\d+):(\d+):(\d+[.,]\d+)\s+-->\s+(\d+):(\d+):(\d+[.,]\d+)")
    rows, current = [], None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = timing.search(raw)
        if match:
            nums = [float(x.replace(",", ".")) for x in match.groups()]
            current = {"start": nums[0] * 3600 + nums[1] * 60 + nums[2],
                       "end": nums[3] * 3600 + nums[4] * 60 + nums[5], "text": []}
            rows.append(current)
        elif current and raw.strip() and not raw.startswith(("WEBVTT", "NOTE")):
            cleaned = re.sub(r"<[^>]+>", "", html.unescape(raw)).strip()
            if cleaned and (not current["text"] or cleaned != current["text"][-1]):
                current["text"].append(cleaned)
    return normalize_segments({"start": r["start"], "end": r["end"], "text": " ".join(r["text"])} for r in rows)


def ytdlp_subtitles(url: str, config: dict[str, Any], temp: Path) -> list[dict[str, Any]]:
    output = str(temp / "subtitle.%(ext)s")
    cmd = ytdlp_base(config) + ["--skip-download", "--write-subs", "--write-auto-subs",
          "--socket-timeout", "30", "--retries", "2", "--fragment-retries", "2",
          "--sub-langs", "zh-Hans,zh-Hant,zh,en", "--sub-format", "vtt", "-o", output, url]
    # Missing subtitle tracks should fail quickly and allow audio fallback.
    run_checked(cmd, 20, attempts=1)
    files = list(temp.glob("subtitle*.vtt"))
    if not files:
        raise RuntimeError("yt-dlp returned no subtitle file")
    return parse_vtt(files[0])


def download_audio(url: str, config: dict[str, Any], video_id: str) -> Path:
    AUDIO.mkdir(parents=True, exist_ok=True)
    audio_suffixes = {".m4a", ".mp4", ".webm", ".opus", ".mp3", ".aac"}
    existing = [p for p in AUDIO.glob(f"{video_id}.*")
                if p.suffix.lower() in audio_suffixes and p.stat().st_size > 0]
    if existing:
        return existing[0]
    output = str(AUDIO / f"{video_id}.%(ext)s")
    # Prefer a progressive stream before HLS/SABR, whose fragments may return
    # 403 outside the active browser session on newer uploads.
    common = ["--no-playlist", "--no-part", "--no-continue",
              "--socket-timeout", "30", "--retries", "2", "--fragment-retries", "2",
              "-f", "18/139/bestaudio/best", "-o", output, url]
    attempts = [ytdlp_base(config) + common,
                ytdlp_base(config) + ["--extractor-args", "youtube:player_client=android_vr"] + common]
    errors = []
    for cmd in attempts:
        try:
            # A blocked SABR/HLS stream must not stall the daily run for an hour.
            run_checked(cmd, 60, attempts=1)
            matches = [p for p in AUDIO.glob(f"{video_id}.*") if p.suffix != ".part"]
            if matches:
                return matches[0]
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("audio download failed: " + "; ".join(errors))


def whisper_audio(path: Path, model: str) -> list[dict[str, Any]]:
    import imageio_ffmpeg
    import mlx_whisper

    ffmpeg_source = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    ffmpeg_dir = ROOT / "data" / ".ffmpeg-bin"
    ffmpeg_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_link = ffmpeg_dir / "ffmpeg"
    # A Python-environment migration can leave a dangling symlink here.
    # Remove only that stale link; never overwrite a valid user file.
    if ffmpeg_link.is_symlink() and not ffmpeg_link.exists():
        ffmpeg_link.unlink()
    if not ffmpeg_link.exists():
        ffmpeg_link.symlink_to(ffmpeg_source)
    os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")
    result = mlx_whisper.transcribe(str(path), path_or_hf_repo=model, language="zh", verbose=False)
    return normalize_segments(result.get("segments", []))


def acquire_transcript(video: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    meta = video_metadata(video["url"], config)
    if meta.get("live_status") in {"is_upcoming", "is_live"}:
        raise DeferredVideo(f"live stream deferred: {meta['live_status']}")
    duration = float(meta.get("duration") or 0)
    segments: list[dict[str, Any]] = []
    method = "none"
    try:
        segments, method = transcript_api(video["video_id"])
    except Exception as exc:
        errors.append(f"youtube_transcript_api: {exc}")
    if not segments:
        with tempfile.TemporaryDirectory() as folder:
            try:
                segments = ytdlp_subtitles(video["url"], config, Path(folder))
                method = "yt_dlp_captions"
            except Exception as exc:
                errors.append(f"yt_dlp_captions: {exc}")
    if not segments:
        try:
            audio = download_audio(video["url"], config, video["video_id"])
            segments = whisper_audio(audio, config["whisper_model"])
            method = "mlx_whisper"
        except Exception as exc:
            errors.append(f"mlx_whisper: {exc}")
    segments = bound_segments(segments, duration)
    coverage, max_gap = coverage_metrics(segments, duration)
    is_short = duration <= 180 and ("/shorts/" in meta.get("webpage_url", "") or duration <= 60)
    threshold = 0.98 if is_short else 0.85
    ready = bool(segments) and coverage >= threshold and max_gap <= (15 if is_short else 300) and meaningful(segments, video["title"])
    return {"video_id": video["video_id"], "title": video["title"], "channel_name": video["channel_name"],
            "url": video["url"], "published_at": video["published_at"], "duration_seconds": duration,
            "method": method, "timeline_coverage": coverage, "max_gap_seconds": max_gap,
            "evidence_ready": ready, "is_short": is_short, "errors": errors, "segments": segments}


def process_video(video: dict[str, Any], config: dict[str, Any], state: dict[str, Any]) -> bool:
    video["status"] = "transcribing"
    video["attempt_count"] += 1
    save_json(STATE_PATH, state)
    try:
        transcript = acquire_transcript(video, config)
        path = TRANSCRIPTS / f"{video['video_id']}.json"
        save_json(path, transcript)
        video["transcript_path"] = str(path.resolve())
        if not transcript["evidence_ready"]:
            raise RuntimeError(f"evidence threshold not met: coverage={transcript['timeline_coverage']}, max_gap={transcript['max_gap_seconds']}")
        video["status"] = "ready"
        video["last_error"] = None
        return True
    except DeferredVideo as exc:
        video["status"] = "discovered"
        video["attempt_count"] = max(0, video["attempt_count"] - 1)
        video["last_error"] = str(exc)
        return False
    except Exception as exc:
        video["last_error"] = str(exc)
        video["status"] = "needs_manual_review" if video["attempt_count"] >= 3 else "retry"
        return False
    finally:
        save_json(STATE_PATH, state)


def run_command(args: argparse.Namespace) -> int:
    config = load_json(CONFIG_PATH)
    items = discover(config, args.backfill_days)
    if args.dry_run:
        print(json.dumps({"dry_run": True, "found": len(items), "videos": items}, ensure_ascii=False, indent=2))
        return 0
    with file_lock():
        state = load_json(STATE_PATH) if STATE_PATH.exists() else initial_state()
        when = iso(now_utc())
        cleaned = cleanup_artifacts(state)
        recover_interrupted_transcribing(state)
        new_ids = []
        for item in items:
            if item["video_id"] not in state["videos"]:
                state["videos"][item["video_id"]] = new_video(item, when)
                new_ids.append(item["video_id"])
        state["last_checked_at"] = when
        save_json(STATE_PATH, state)
        candidates = [v for v in state["videos"].values() if v["status"] in {"discovered", "retry"}]
        candidates.sort(key=lambda v: v["published_at"])
        if args.limit is not None:
            candidates = candidates[:args.limit]
        processed = []
        for video in candidates:
            if process_video(video, config, state):
                processed.append(video["video_id"])
        pending = [v["video_id"] for v in state["videos"].values() if v["status"] in {"ready", "report_ready"}]
        print(json.dumps({"new": len(new_ids), "new_ids": new_ids, "processed": processed,
                          "pending": pending, "cleaned": cleaned}, ensure_ascii=False))
    return 0


def status_command() -> int:
    state = load_json(STATE_PATH) if STATE_PATH.exists() else initial_state()
    counts: dict[str, int] = {}
    for video in state["videos"].values():
        counts[video["status"]] = counts.get(video["status"], 0) + 1
    print(json.dumps({"last_checked_at": state["last_checked_at"], "counts": counts,
                      "pending_delivery": state.get("pending_delivery", [])}, ensure_ascii=False, indent=2))
    return 0


def pending_command() -> int:
    state = load_json(STATE_PATH) if STATE_PATH.exists() else initial_state()
    rows = [v for v in state["videos"].values() if v["status"] in {"ready", "report_ready"}]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cleanup_command() -> int:
    with file_lock():
        state = load_json(STATE_PATH) if STATE_PATH.exists() else initial_state()
        removed = cleanup_artifacts(state)
        save_json(STATE_PATH, state)
    print(json.dumps({"retention_days": ARTIFACT_RETENTION_DAYS, "removed": removed}, ensure_ascii=False, indent=2))
    return 0


def retry_command(video_id: str) -> int:
    with file_lock():
        state = load_json(STATE_PATH)
        if video_id not in state["videos"]:
            raise SystemExit(f"unknown video ID: {video_id}")
        video = state["videos"][video_id]
        video["status"] = "retry"
        video["last_error"] = None
        save_json(STATE_PATH, state)
    print(json.dumps({"video_id": video_id, "status": "retry"}))
    return 0


def mark_report_ready(video_id: str, report: str) -> int:
    report_path = Path(report).expanduser().resolve()
    if not report_path.is_file():
        raise SystemExit(f"report does not exist: {report_path}")
    with file_lock():
        state = load_json(STATE_PATH)
        video = state["videos"].get(video_id)
        if not video:
            raise SystemExit(f"unknown video ID: {video_id}")
        if video["status"] != "ready":
            raise SystemExit(f"video is not ready for report preparation: {video['status']}")
        video["status"] = "report_ready"
        video["report_path"] = str(report_path)
        save_json(STATE_PATH, state)
    print(json.dumps({"video_id": video_id, "status": "report_ready", "report": str(report_path)}, ensure_ascii=False))
    return 0


def mark_delivery_pending(report: str) -> int:
    report_path = Path(report).expanduser().resolve()
    if not report_path.is_file():
        raise SystemExit(f"report does not exist: {report_path}")
    with file_lock():
        state = load_json(STATE_PATH)
        pending = state.setdefault("pending_delivery", [])
        if str(report_path) not in pending:
            pending.append(str(report_path))
        save_json(STATE_PATH, state)
    print(json.dumps({"pending_delivery": str(report_path)}, ensure_ascii=False))
    return 0


def mark_reported(video_id: str, report: str) -> int:
    report_path = Path(report).expanduser().resolve()
    if not report_path.is_file():
        raise SystemExit(f"report does not exist: {report_path}")
    with file_lock():
        state = load_json(STATE_PATH)
        video = state["videos"].get(video_id)
        if not video:
            raise SystemExit(f"unknown video ID: {video_id}")
        if video["status"] != "report_ready":
            raise SystemExit(f"video is not ready for reporting: {video['status']}")
        video["status"] = "reported"
        video["report_path"] = str(report_path)
        video["reported_at"] = iso(now_utc())
        state["pending_delivery"] = [x for x in state.get("pending_delivery", []) if x != str(report_path)]
        save_json(STATE_PATH, state)
    print(json.dumps({"video_id": video_id, "status": "reported", "report": str(report_path)}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--backfill-days", type=int, default=1)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--limit", type=int)
    sub.add_parser("status")
    sub.add_parser("pending")
    sub.add_parser("cleanup")
    retry = sub.add_parser("retry")
    retry.add_argument("--video-id", required=True)
    prepared = sub.add_parser("mark-report-ready")
    prepared.add_argument("--video-id", required=True)
    prepared.add_argument("--report", required=True)
    delivery = sub.add_parser("mark-delivery-pending")
    delivery.add_argument("--report", required=True)
    mark = sub.add_parser("mark-reported")
    mark.add_argument("--video-id", required=True)
    mark.add_argument("--report", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "run":
        return run_command(args)
    if args.command == "status":
        return status_command()
    if args.command == "pending":
        return pending_command()
    if args.command == "cleanup":
        return cleanup_command()
    if args.command == "retry":
        return retry_command(args.video_id)
    if args.command == "mark-report-ready":
        return mark_report_ready(args.video_id, args.report)
    if args.command == "mark-delivery-pending":
        return mark_delivery_pending(args.report)
    return mark_reported(args.video_id, args.report)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
