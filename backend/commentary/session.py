"""One browser translation session: source -> phrase segmenter -> relay interpreter -> timeline -> client.

Wire format to the browser: JSON text frames for events; binary frames for audio:
    [u32 little-endian header length][UTF-8 JSON header][int16 mono PCM @ 24 kHz]
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np

from .config import DEFAULT_VOICE, LANG_BY_CODE, MALE_VOICES
from .relay import RelayConfig, RelayInterpreter
from .segmenter import LIVE_SEGMENTER, PhraseSegmenter
from .sources import UPLOAD_DIR, ffmpeg_pcm, youtube_audio, youtube_info
from .timeline import DubTimeline, LiveTimeline

log = logging.getLogger("session")

PREROLL_S = 8.0  # dub mode: translated audio buffered before the video starts
DUB_POOL = 6  # pre-recorded: every phrase arrives at once on a cold start; more booths, ~same token cost
LIVE_POOL = 4
AHEAD_MAX_S = 45.0  # dub mode: never translate further than this ahead of the playhead


@dataclass
class StartParams:
    kind: str  # youtube | file | mic
    lang: str
    voice: str
    sport: str = "auto"
    start_at: float = 0.0
    youtube_id: str | None = None
    media_id: str | None = None

    @classmethod
    def parse(cls, msg: dict) -> StartParams:
        src = msg.get("source") or {}
        lang = msg.get("lang", "hi-IN")
        voice = msg.get("voice") or DEFAULT_VOICE
        if lang not in LANG_BY_CODE:
            raise ValueError(f"unsupported language {lang}")
        if voice not in MALE_VOICES:
            raise ValueError(f"unsupported voice {voice}")
        return cls(
            kind=src.get("kind", "mic"), lang=lang, voice=voice, sport=msg.get("sport", "auto"),
            start_at=float(msg.get("start_at") or 0.0), youtube_id=src.get("id"), media_id=src.get("media_id"),
        )


class TranslationSession:
    def __init__(self, send_text: Callable[[str], Awaitable[None]], send_bytes: Callable[[bytes], Awaitable[None]]):
        self._send_text = send_text
        self._send_bytes = send_bytes
        self.params: StartParams | None = None
        self.relay: RelayInterpreter | None = None
        self.timeline: DubTimeline | LiveTimeline | None = None
        self.seg: PhraseSegmenter | None = None
        self.mode = "dub"
        self.playhead = 0.0
        self.playing = False
        self._tasks: list[asyncio.Task] = []
        self._mic_q: asyncio.Queue[np.ndarray | None] = asyncio.Queue()
        self._ready_sent = False
        self._t0 = time.monotonic()
        self._stats = {"phrases": 0, "flags": 0, "retries": 0, "ttfa": []}

    # ---------- transport ----------
    async def send(self, ev: dict) -> None:
        with contextlib.suppress(Exception):
            await self._send_text(json.dumps(ev, ensure_ascii=False))

    async def sink(self, meta: dict, pcm: bytes | None) -> None:
        if pcm is None:
            await self.send(meta)
            return
        head = json.dumps(meta, ensure_ascii=False).encode()
        with contextlib.suppress(Exception):
            await self._send_bytes(struct.pack("<I", len(head)) + head + pcm)

    # ---------- control ----------
    async def start(self, params: StartParams) -> None:
        await self.stop()
        self.params = params
        self._ready_sent = False
        self.playhead = params.start_at
        is_live_source = params.kind == "mic"
        src_path = None
        live_url = None
        if params.kind == "youtube":
            info = await youtube_info(params.youtube_id)
            if info.is_live:
                is_live_source = True
                live_url = info.stream_url
            else:
                await self.send({"type": "status", "text": "Fetching source audio…"})
                src_path = await youtube_audio(info.id)
        elif params.kind == "file":
            hits = list(UPLOAD_DIR.glob(f"{params.media_id}.*"))
            if not hits:
                raise ValueError("upload not found")
            src_path = hits[0]
        self.mode = "live" if is_live_source else "dub"
        await self.send({"type": "status", "text": "Opening interpreter relay…"})
        self.relay = RelayInterpreter(
            RelayConfig(language=params.lang, voice=params.voice, sport=params.sport, retry_flagged=self.mode == "dub",
                        pool_size=DUB_POOL if self.mode == "dub" else LIVE_POOL)
        )
        await self.relay.start()
        self._t0 = time.monotonic()
        t0 = 0.0 if self.mode == "live" else params.start_at
        self.seg = PhraseSegmenter(LIVE_SEGMENTER if self.mode == "live" else None, t0=t0)
        self.timeline = DubTimeline(self.sink) if self.mode == "dub" else LiveTimeline(self.sink, wall0=self._t0)
        await self.send({"type": "started", "mode": self.mode, "start_at": params.start_at, "pool": len(self.relay.slots)})
        if self.mode == "live":
            self._tasks.append(asyncio.create_task(self._read_live(live_url), name="reader"))
        else:
            self._tasks.append(asyncio.create_task(self._read_file(src_path, params.start_at), name="reader"))
        self._tasks.append(asyncio.create_task(self._pump_events(), name="events"))
        self._tasks.append(asyncio.create_task(self._heartbeat(), name="heartbeat"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        if self.relay:
            await self.relay.close()
            self.relay = None
        self._mic_q = asyncio.Queue()

    async def on_client(self, msg: dict) -> None:
        action = msg.get("action")
        if action == "start":
            try:
                await self.start(StartParams.parse(msg))
            except Exception as e:
                log.exception("start failed")
                await self.send({"type": "error", "text": str(e)})
        elif action == "clock":
            self.playhead = float(msg.get("t", self.playhead))
            self.playing = bool(msg.get("playing", True))
            if isinstance(self.timeline, DubTimeline):
                await self.timeline.tick(self.playhead)
        elif action == "seek" and self.params:
            self.params.start_at = float(msg.get("t", 0.0))
            await self.start(self.params)
        elif action == "stop":
            await self.stop()
            await self.send({"type": "stopped"})

    def on_mic(self, data: bytes) -> None:
        if self.mode == "live" and self.relay is not None:
            self._mic_q.put_nowait(np.frombuffer(data, np.int16))

    # ---------- pipeline ----------
    async def _feed(self, chunk: np.ndarray) -> None:
        for ev in self.seg.feed(chunk):
            await self._route(ev)

    async def _route(self, ev) -> None:
        if ev.kind == "start":
            self.timeline.phrase_started(ev.idx, ev.t)
            self._stats["phrases"] += 1
            await self.relay.begin(ev.idx, ev.t)
        elif ev.kind == "audio":
            await self.relay.audio(ev.idx, ev.pcm.tobytes())
        else:
            await self.relay.end(ev.idx, ev.t)

    async def _read_file(self, path, start_at: float) -> None:
        async for chunk in ffmpeg_pcm(path, start=start_at):
            await self._feed(chunk)
            self.timeline.set_frontier(self.seg.now)
            while self.seg.now > self.playhead + AHEAD_MAX_S:
                await asyncio.sleep(0.25)
        for ev in self.seg.flush():
            await self._route(ev)
        self.timeline.set_frontier(self.seg.now, source_done=True)
        await self.timeline.pump()
        await self.send({"type": "source_done", "t": self.seg.now})

    async def _read_live(self, url: str | None) -> None:
        if url:  # live stream: ffmpeg paces it at realtime
            async for chunk in ffmpeg_pcm(url, live=True):
                await self._feed(chunk)
        else:  # microphone: chunks arrive from the browser
            while (chunk := await self._mic_q.get()) is not None:
                await self._feed(chunk)

    async def _pump_events(self) -> None:
        while True:
            ev = await self.relay.events.get()
            kind = ev["type"]
            if kind in ("audio", "phrase_done"):
                await self.timeline.on_event(ev)
            if kind == "phrase_done":
                if ev["flags"]:
                    self._stats["flags"] += 1
                if ev["retry"]:
                    self._stats["retries"] += 1
                if ev["ttfa"] is not None:
                    self._stats["ttfa"].append(ev["ttfa"])
                await self.send({k: ev[k] for k in ("type", "idx", "slot", "t_start", "t_end", "src_text", "out_text", "out_s", "ttfa", "flags", "retry")})
            elif kind == "slots":
                await self.send(ev)
            elif kind in ("src_text", "out_text"):
                await self.send(ev)
            if self.mode == "dub" and not self._ready_sent:
                await self._check_ready()

    async def _check_ready(self) -> None:
        tl = self.timeline
        if tl.ready_until >= self.params.start_at + PREROLL_S or tl.source_done:
            self._ready_sent = True
            await self.send({"type": "ready"})

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            tt = self._stats["ttfa"]
            stats = {
                "type": "stats", "mode": self.mode, "phrases": self._stats["phrases"], "flags": self._stats["flags"],
                "retries": self._stats["retries"], "ttfa_ms": int(1000 * float(np.median(tt[-20:]))) if tt else None,
                "pending": len(self.relay._pending) if self.relay else 0,
            }
            if isinstance(self.timeline, DubTimeline):
                stats["ready_until"] = round(min(self.timeline.ready_until, 1e6), 2)
                stats["source_done"] = self.timeline.source_done
                stats["skipped"] = len(self.timeline.skipped)
                if not self._ready_sent:
                    await self._check_ready()
            elif isinstance(self.timeline, LiveTimeline):
                lat = self.timeline.latencies[-10:]
                stats["latency_s"] = round(float(np.median(lat)), 2) if lat else None
                stats["backlog_s"] = round(self.timeline.backlog(), 2)
            await self.send(stats)
