"""Playout timelines: turn per-phrase relay output into one continuous commentary track.

Dub mode (pre-recorded / VOD): translation runs ahead of the viewer. Each phrase is anchored at the
moment its source phrase starts, so the translated call lands on the action. A phrase that would run
into the next one is time-compressed (pitch-preserving WSOLA, <= MAX_RATE) to fit; pauses between
phrases mirror the commentator's own pauses instead of pipeline stalls.

Live mode (mic / live streams): chunks are released as soon as they arrive, phrases strictly in order.
The server tracks the listener's backlog and speeds up the next phrase slightly when it grows.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import numpy as np

from .audio import StreamingWSOLA, apply_edge_fades, float_to_pcm, pcm_to_float, roar_gate, roar_mask
from .config import OUT_RATE

Sink = Callable[[dict, bytes | None], Awaitable[None]]

MAX_RATE = 1.18
LEAD_S = 0.12  # translated phrase starts just after the source phrase starts
SPILL_S = 0.35  # may run this far into the next source phrase before we compress
MIN_GAP_S = 0.06


@dataclass
class _Seg:
    idx: int
    t_start: float
    attempt: int = 1
    pcm: bytearray = field(default_factory=bytearray)
    done: bool = False
    retry: bool = False
    out_text: str = ""
    src_text: str = ""


class DubTimeline:
    def __init__(self, sink: Sink):
        self.sink = sink
        self.segs: dict[int, _Seg] = {}
        self.next_idx = 0
        self.prev_end = -1e9
        self.frontier_src = 0.0  # segmenter has scanned the source up to here
        self.source_done = False
        self.playhead = 0.0
        self.placed_until = 0.0
        self.skipped: list[int] = []

    def phrase_started(self, idx: int, t_start: float) -> None:
        self.segs.setdefault(idx, _Seg(idx, t_start))

    def set_frontier(self, t: float, source_done: bool = False) -> None:
        self.frontier_src = t
        self.source_done = source_done

    @property
    def ready_until(self) -> float:
        """Source time up to which translated audio is final (for pre-roll / buffering decisions)."""
        nxt = self.segs.get(self.next_idx)
        if nxt is None:
            return self.frontier_src if not self.source_done else 1e9
        return nxt.t_start

    async def on_event(self, ev: dict) -> None:
        kind = ev["type"]
        if kind == "audio":
            s = self.segs.get(ev["idx"])
            if s is None:
                return
            if ev["attempt"] != s.attempt:
                s.attempt = ev["attempt"]
                s.pcm = bytearray()
            s.pcm.extend(ev["pcm"])
        elif kind == "phrase_done":
            s = self.segs.get(ev["idx"])
            if s is None:
                return
            if ev["retry"]:
                s.pcm = bytearray()
                s.attempt = ev["attempt"] + 1
                return
            s.done = True
            s.out_text = "" if ev.get("suppress") else ev["out_text"]
            s.src_text = ev["src_text"]
            if ev.get("suppress"):
                s.pcm = bytearray()
        await self.pump()

    async def tick(self, playhead: float) -> None:
        self.playhead = playhead
        await self.pump()

    async def pump(self) -> None:
        while True:
            s = self.segs.get(self.next_idx)
            if s is None:
                return
            if not s.done:
                # Hopelessly late (stuck model turn): drop it rather than stall everything behind it.
                if self.playhead > s.t_start + 4.0:
                    self.skipped.append(s.idx)
                    await self.sink({"type": "skip", "idx": s.idx}, None)
                    self.next_idx += 1
                    continue
                return
            if not s.pcm:
                await self.sink({"type": "skip", "idx": s.idx}, None)
                self.next_idx += 1
                continue
            x, gated = roar_gate(pcm_to_float(bytes(s.pcm)), OUT_RATE)
            dur = len(x) / OUT_RATE
            start = max(s.t_start + LEAD_S, self.prev_end + MIN_GAP_S, self.playhead + 0.15)
            nxt = self.segs.get(self.next_idx + 1)
            if nxt is not None:
                target_end = nxt.t_start + SPILL_S
            elif self.source_done or self.frontier_src >= start + dur:
                target_end = start + dur
            else:
                return  # next phrase may still start inside this one; wait for the segmenter
            rate = 1.0
            if dur > 0 and start + dur > target_end:
                rate = float(min(MAX_RATE, dur / max(target_end - start, 1e-3)))
            if len(x):
                if rate > 1.0:
                    w = StreamingWSOLA(rate, OUT_RATE)
                    x = np.concatenate([w.push(x), w.flush()])
                x = apply_edge_fades(x, OUT_RATE)
            out_dur = len(x) / OUT_RATE
            self.prev_end = start + out_dur
            self.placed_until = self.prev_end
            await self.sink(
                {"type": "segment", "idx": s.idx, "start": round(start, 3), "src_start": s.t_start, "dur": round(out_dur, 3),
                 "rate": round(rate, 3), "gated": round(gated, 3), "drift": round(start - s.t_start, 3), "text": s.out_text, "src_text": s.src_text},
                float_to_pcm(x) if len(x) else None,
            )
            self.next_idx += 1


class LiveTimeline:
    """Release audio immediately, phrases in order; WSOLA catch-up when the listener falls behind."""

    def __init__(self, sink: Sink, wall0: float | None = None):
        self.sink = sink
        self.wall0 = wall0 if wall0 is not None else time.monotonic()  # wall time of stream t=0
        self.segs: dict[int, _Seg] = {}
        self.cur = 0
        self.play_end = 0.0  # wall time when the listener's queue drains
        self._w: StreamingWSOLA | None = None
        self._rate = 1.0
        self._emitted = 0  # bytes of cur already released
        self._first = True
        self.latencies: list[float] = []

    def phrase_started(self, idx: int, t_start: float) -> None:
        self.segs.setdefault(idx, _Seg(idx, t_start))

    def backlog(self) -> float:
        return max(0.0, self.play_end - time.monotonic())

    def _pick_rate(self) -> float:
        b = self.backlog()
        return 1.0 if b < 0.8 else 1.08 if b < 2.0 else 1.15

    @staticmethod
    def _gate_new(s: _Seg, a: int, b: int) -> np.ndarray:
        """Roar-gate bytes [a, b) of the phrase using up to 2 s of history (causal)."""
        hist = max(0, a - OUT_RATE * 4)  # 2 s of int16
        x = pcm_to_float(bytes(s.pcm[hist:b]))
        mask, _ = roar_mask(x, OUT_RATE)
        new = x[(a - hist) // 2 :]
        if mask.any():
            hop = int(OUT_RATE * 0.02)
            frame_of = (np.arange(len(x)) // hop).clip(0, len(mask) - 1)
            g = np.where(mask[frame_of], 10 ** (-24 / 20), 1.0).astype(np.float32)
            new = new * g[(a - hist) // 2 :]
        return new

    async def on_event(self, ev: dict) -> None:
        kind = ev["type"]
        s = self.segs.get(ev.get("idx", -1))
        if s is None:
            return
        if kind == "audio":
            s.pcm.extend(ev["pcm"])
        elif kind == "phrase_done":
            s.done = True
            s.out_text, s.src_text = ev["out_text"], ev["src_text"]
        await self._drain()

    async def _release(self, x: np.ndarray, s: _Seg, final: bool) -> None:
        if self._first:
            now = time.monotonic()
            start_wall = max(now, self.play_end)
            lat = start_wall - (self.wall0 + s.t_start)
            self.latencies.append(lat)
            await self.sink({"type": "seg_begin", "idx": s.idx, "rate": self._rate, "latency": round(lat, 2), "src_start": s.t_start}, None)
            x = apply_edge_fades(x, OUT_RATE, fade_in_ms=8, fade_out_ms=0)
            self._first = False
        if final:
            x = apply_edge_fades(x, OUT_RATE, fade_in_ms=0, fade_out_ms=20)
        if len(x):
            now = time.monotonic()
            self.play_end = max(now, self.play_end) + len(x) / OUT_RATE
            await self.sink({"type": "chunk", "idx": s.idx}, float_to_pcm(x))

    async def _drain(self) -> None:
        while True:
            s = self.segs.get(self.cur)
            if s is None:
                return
            if self._w is None:
                if not s.pcm and not s.done:
                    return
                self._rate = self._pick_rate()
                self._w = StreamingWSOLA(self._rate, OUT_RATE)
                self._first = True
                self._emitted = 0
            new = bytes(s.pcm[self._emitted :])
            if new:
                await self._release(self._w.push(self._gate_new(s, self._emitted, len(s.pcm))), s, final=False)
            self._emitted = len(s.pcm)
            if not s.done:
                return
            tail = self._w.flush()
            if not self._first:
                await self._release(tail, s, final=True)
                await self.sink({"type": "seg_end", "idx": s.idx, "text": s.out_text, "src_text": s.src_text}, None)
            self._w = None
            del self.segs[self.cur]
            self.cur += 1
