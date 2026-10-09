"""Source audio: YouTube (via yt-dlp), uploaded files, and ffmpeg decoding to 16 kHz mono PCM."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import IN_RATE, SETTINGS

YT_DIR = SETTINGS.cache_dir / "yt"
UPLOAD_DIR = SETTINGS.cache_dir / "uploads"
_YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")

# YouTube extraction needs a JS runtime; node is what this machine has.
_YDL_BASE = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "js_runtimes": {"node": {}},
             "retries": 5, "fragment_retries": 5}


def youtube_id(url: str) -> str | None:
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url.strip()):
        return url.strip()
    m = _YT_ID.search(url)
    return m.group(1) if m else None


@dataclass
class YouTubeInfo:
    id: str
    title: str
    duration: float | None
    is_live: bool
    channel: str
    stream_url: str | None  # direct bestaudio URL (used for live streams)


class YouTubeBlocked(RuntimeError):
    """YouTube refused this server (datacenter IP bot check)."""


BLOCKED_HELP = (
    "YouTube is blocking this server from fetching that video (it asks the server to sign in). "
    "Library clips still work. For other videos, download the audio and use Upload."
)


def _gcs_bucket():
    if not SETTINGS.cache_bucket:
        return None
    from google.cloud import storage

    return storage.Client(project=SETTINGS.project).bucket(SETTINGS.cache_bucket)


def _gcs_fetch(vid: str) -> Path | None:
    bucket = _gcs_bucket()
    if bucket is None:
        return None
    YT_DIR.mkdir(parents=True, exist_ok=True)
    found = None
    for blob in bucket.list_blobs(prefix=f"yt/{vid}."):
        dest = YT_DIR / blob.name.split("/", 1)[1]
        if not dest.exists():
            blob.download_to_filename(dest)
        if dest.suffix != ".json":
            found = dest
    return found


def _gcs_store(*paths: Path) -> None:
    bucket = _gcs_bucket()
    if bucket is None:
        return
    for p in paths:
        if p.exists():
            bucket.blob(f"yt/{p.name}").upload_from_filename(p)


def _cached_info(vid: str) -> YouTubeInfo | None:
    meta = YT_DIR / f"{vid}.json"
    if not meta.exists() and cached_youtube_audio(vid) is None:
        if _gcs_fetch(vid) is None:
            return None
    if meta.exists():
        d = json.loads(meta.read_text())
        return YouTubeInfo(vid, d.get("title") or vid, d.get("duration"), False, d.get("channel", ""), None)
    return YouTubeInfo(vid, vid, None, False, "", None)


def _extract(url: str) -> dict:
    import yt_dlp

    with yt_dlp.YoutubeDL({**_YDL_BASE, "format": "bestaudio/best", "skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False)


async def youtube_info(url: str) -> YouTubeInfo:
    vid = youtube_id(url)
    if not vid:
        raise ValueError("not a YouTube link")
    if (cached := await asyncio.to_thread(_cached_info, vid)) is not None:
        return cached
    try:
        info = await asyncio.to_thread(_extract, f"https://www.youtube.com/watch?v={vid}")
    except Exception as e:
        if "confirm you" in str(e) and "bot" in str(e):
            raise YouTubeBlocked(BLOCKED_HELP) from e
        raise
    return YouTubeInfo(
        id=vid,
        title=info.get("title") or vid,
        duration=info.get("duration"),
        is_live=bool(info.get("is_live")),
        channel=info.get("channel") or info.get("uploader") or "",
        stream_url=info.get("url"),
    )


_downloads: dict[str, asyncio.Task] = {}


def _download(vid: str, attempts: int = 3) -> Path:
    import time

    import yt_dlp

    YT_DIR.mkdir(parents=True, exist_ok=True)
    opts = {**_YDL_BASE, "format": "bestaudio/best", "outtmpl": str(YT_DIR / f"{vid}.%(ext)s")}
    for attempt in range(attempts):
        try:
            # a fresh extractor each time: YouTube intermittently 403s a signed media URL
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
                path = Path(ydl.prepare_filename(info))
            meta = YT_DIR / f"{vid}.json"
            meta.write_text(json.dumps({k: info.get(k) for k in ("title", "duration", "channel")}, ensure_ascii=False))
            _gcs_store(path, meta)
            return path
        except yt_dlp.utils.DownloadError as e:
            if "confirm you" in str(e) and "bot" in str(e):
                raise YouTubeBlocked(BLOCKED_HELP) from e
            if attempt == attempts - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def cached_youtube_audio(vid: str) -> Path | None:
    hits = [p for p in YT_DIR.glob(f"{vid}.*") if p.suffix not in (".part", ".json")]  # .json = metadata
    return hits[0] if hits else None


async def youtube_audio(vid: str) -> Path:
    """Download (once) the audio track of a VOD; concurrent callers share the download."""
    if (p := cached_youtube_audio(vid)) is not None:
        return p
    if (p := await asyncio.to_thread(_gcs_fetch, vid)) is not None:
        return p
    task = _downloads.get(vid)
    if task is None:
        task = _downloads[vid] = asyncio.create_task(asyncio.to_thread(_download, vid))
    try:
        return await task
    finally:
        _downloads.pop(vid, None)


async def probe_duration(path: Path) -> float | None:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return None


async def ffmpeg_pcm(src: str | Path, start: float = 0.0, chunk_s: float = 0.1, live: bool = False) -> AsyncIterator[np.ndarray]:
    """Decode anything ffmpeg reads into 16 kHz mono int16 chunks."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found")
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if live:
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4"]
    elif start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(src), "-vn", "-ac", "1", "-ar", str(IN_RATE), "-f", "s16le", "pipe:1"]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    nbytes = int(IN_RATE * chunk_s) * 2
    try:
        while True:
            data = await proc.stdout.readexactly(nbytes)
            yield np.frombuffer(data, np.int16)
    except asyncio.IncompleteReadError as e:
        if e.partial:
            yield np.frombuffer(e.partial[: len(e.partial) // 2 * 2], np.int16)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
