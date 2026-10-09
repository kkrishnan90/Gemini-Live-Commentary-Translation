"""Relay interpreter: a pool of gemini-3.8-live sessions that take turns translating phrases.

Why a pool: a 3.8 Live session is turn-based and streams its answer at ~1x realtime, and any
audio that arrives while it is speaking is discarded. One session therefore silently loses
every phrase spoken during its previous answer. With N sessions, phrase k goes to whichever
session is idle; if none is, the phrase is buffered and burst-sent to the first one that frees.

Each session only hears every Nth phrase, so a shared context bus feeds it the running source
transcript (collected from every session's input transcription) as a bracketed text note.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from google import genai
from google.genai import types

from .config import IN_RATE, INTERPRETER_MODEL, OUT_RATE, SETTINGS
from .prompts import build_system_instruction, context_note

log = logging.getLogger("relay")

CHUNK = IN_RATE // 20  # 50 ms send granularity


class SlotState(enum.Enum):
    CONNECTING = "connecting"
    IDLE = "idle"
    LISTENING = "listening"
    SPEAKING = "speaking"
    RETIRING = "retiring"


@dataclass
class RelayConfig:
    language: str
    voice: str
    sport: str = "auto"
    pool_size: int = SETTINGS.pool_size
    context_chars: int = 160  # A/B (tools/context_ab.py): +0.4 accuracy vs none; 360 doubled duplications
    use_context: bool = True
    max_turns_per_session: int = 40
    max_session_age_s: float = 8 * 60
    retry_flagged: bool = False  # dub mode: re-voice a phrase whose output looks wrong
    model: str = INTERPRETER_MODEL


@dataclass
class Phrase:
    idx: int
    t_start: float
    t_end: float | None = None
    chunks: list[bytes] = field(default_factory=list)  # full source audio (kept for retries)
    sent: int = 0  # chunks already sent to the owning slot
    slot: int | None = None
    attempts: int = 0
    src_text: str = ""
    out_text: str = ""
    out_samples: int = 0
    t_dispatch: float | None = None
    t_activity_end: float | None = None
    t_first_audio: float | None = None
    no_context: bool = False
    opened: bool = False  # activity_start (+ context) sent to the owning slot

    @property
    def ended(self) -> bool:
        return self.t_end is not None

    @property
    def src_dur(self) -> float:
        return (self.t_end or self.t_start) - self.t_start


def live_config(cfg: RelayConfig) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=build_system_instruction(cfg.language, cfg.sport),
        speech_config=types.SpeechConfig(
            language_code=cfg.language,
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=cfg.voice)),
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True),
            activity_handling=types.ActivityHandling.NO_INTERRUPTION,
            turn_coverage=types.TurnCoverage.TURN_INCLUDES_ALL_INPUT,
        ),
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=24000, sliding_window=types.SlidingWindow(target_tokens=12000)
        ),
    )


def _ngrams(s: str, n: int = 3) -> set[str]:
    s = "".join(s.split())
    return {s[i : i + n] for i in range(max(0, len(s) - n + 1))}


def repeat_score(text: str, previous: list[str]) -> float:
    """Share of this output's character trigrams already present in recent outputs (context-leak detector)."""
    a = _ngrams(text)
    if len(a) < 6:
        return 0.0
    b = set().union(*(_ngrams(p) for p in previous)) if previous else set()
    return len(a & b) / len(a)


def is_revoiced(src: str, out: str, recent: list[tuple[str, str]]) -> bool:
    """Output replays earlier commentary (often stitched from several phrases) while the source did not repeat.

    Commentators who really repeat themselves are left alone: then the source repeats too.
    """
    if not recent:
        return False
    return repeat_score(out, [o for _, o in recent]) > 0.6 and repeat_score(src, [s for s, _ in recent]) < 0.4


class Slot:
    def __init__(self, relay: RelayInterpreter, n: int):
        self.relay = relay
        self.n = n
        self.state = SlotState.CONNECTING
        self.phrase: Phrase | None = None
        self.turns = 0
        self.born = time.monotonic()
        self.session: Any = None
        self._cm: Any = None
        self._rx: asyncio.Task | None = None
        self._spoke_at: float | None = None
        self.send_lock = asyncio.Lock()

    async def open(self) -> None:
        self._cm = self.relay.client.aio.live.connect(model=self.relay.cfg.model, config=self.relay.live_cfg)
        self.session = await self._cm.__aenter__()
        self.born = time.monotonic()
        self.turns = 0
        self.state = SlotState.IDLE
        self._rx = asyncio.create_task(self._receive(), name=f"slot{self.n}-rx")

    async def close(self) -> None:
        if self._rx:
            self._rx.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._rx
        if self._cm:
            with contextlib.suppress(Exception):
                await self._cm.__aexit__(None, None, None)
        self._cm = self.session = None

    @property
    def worn_out(self) -> bool:
        c = self.relay.cfg
        return self.turns >= c.max_turns_per_session or time.monotonic() - self.born > c.max_session_age_s

    async def send_audio(self, pcm: bytes) -> None:
        await self.session.send_realtime_input(audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={IN_RATE}"))

    async def _receive(self) -> None:
        try:
            while True:
                async for msg in self.session.receive():
                    await self._on_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # connection dropped: hand the phrase back and get replaced
            log.warning("slot %d receive ended: %s", self.n, e)
            await self.relay._slot_failed(self)

    async def _on_message(self, msg: types.LiveServerMessage) -> None:
        if msg.go_away:
            self.relay._retire_soon(self)
        sc = msg.server_content
        if not sc:
            return
        p = self.phrase
        if p is None:
            return
        if sc.input_transcription and sc.input_transcription.text:
            p.src_text += sc.input_transcription.text
            self.relay._emit({"type": "src_text", "idx": p.idx, "text": p.src_text})
        if sc.output_transcription and sc.output_transcription.text:
            p.out_text += sc.output_transcription.text
            self.relay._emit({"type": "out_text", "idx": p.idx, "text": p.out_text})
        if sc.model_turn:
            for part in sc.model_turn.parts or []:
                if part.inline_data and part.inline_data.data:
                    if p.t_first_audio is None:
                        p.t_first_audio = time.monotonic()
                    p.out_samples += len(part.inline_data.data) // 2
                    self.relay._emit({"type": "audio", "idx": p.idx, "attempt": p.attempts, "pcm": part.inline_data.data})
        if sc.turn_complete:
            self.turns += 1
            await self.relay._phrase_done(self, p)


class RelayInterpreter:
    """Feed phrases with begin/audio/end; consume translated audio + transcripts from .events."""

    def __init__(self, cfg: RelayConfig, client: genai.Client | None = None):
        self.cfg = cfg
        self.client = client or genai.Client(vertexai=True, project=SETTINGS.project, location=SETTINGS.location)
        self.live_cfg = live_config(cfg)
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.slots: list[Slot] = []
        self.phrases: dict[int, Phrase] = {}
        self._pending: deque[Phrase] = deque()
        self._recent: deque[tuple[str, str]] = deque(maxlen=12)  # (source, output) of the last ~40 s
        self._lock = asyncio.Lock()
        self._watchdog: asyncio.Task | None = None
        self._closed = False

    # ---------- lifecycle ----------
    async def start(self) -> None:
        self.slots = [Slot(self, i) for i in range(self.cfg.pool_size)]
        await asyncio.gather(*(s.open() for s in self.slots))
        self._watchdog = asyncio.create_task(self._watch(), name="relay-watchdog")
        self._emit_slots()

    async def close(self) -> None:
        self._closed = True
        if self._watchdog:
            self._watchdog.cancel()
        await asyncio.gather(*(s.close() for s in self.slots), return_exceptions=True)

    async def __aenter__(self) -> RelayInterpreter:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ---------- input side ----------
    async def begin(self, idx: int, t_start: float) -> None:
        async with self._lock:
            p = Phrase(idx, t_start)
            self.phrases[idx] = p
            self._pending.append(p)
            await self._dispatch_locked()

    async def audio(self, idx: int, pcm: bytes) -> None:
        p = self.phrases[idx]
        p.chunks.append(pcm)
        if p.slot is not None:
            await self._pump(p)

    async def end(self, idx: int, t_end: float) -> None:
        p = self.phrases[idx]
        p.t_end = t_end
        if p.slot is not None:
            await self._pump(p)

    async def submit(self, idx: int, t_start: float, t_end: float, pcm: bytes) -> None:
        """Whole phrase at once (pre-recorded sources)."""
        await self.begin(idx, t_start)
        await self.audio(idx, pcm)
        await self.end(idx, t_end)

    def idle_slots(self) -> int:
        return sum(s.state is SlotState.IDLE for s in self.slots)

    # ---------- dispatch ----------
    async def _dispatch_locked(self) -> None:
        while self._pending:
            slot = next((s for s in self.slots if s.state is SlotState.IDLE), None)
            if slot is None:
                return
            p = self._pending.popleft()
            slot.state = SlotState.LISTENING
            slot.phrase = p
            p.slot = slot.n
            p.sent = 0
            p.opened = False
            p.attempts += 1
            p.t_dispatch = time.monotonic()
            asyncio.create_task(self._open_turn(slot, p))
        self._emit_slots()

    async def _open_turn(self, slot: Slot, p: Phrase) -> None:
        async with slot.send_lock:
            await slot.session.send_realtime_input(activity_start=types.ActivityStart())
            ctx = self._context_for(p)
            if ctx:
                await slot.session.send_realtime_input(text=context_note(ctx))
            p.opened = True
        await self._pump(p)

    def _context_for(self, p: Phrase) -> str:
        if not self.cfg.use_context or p.no_context:
            return ""
        prev = " ".join(self.phrases[j].src_text.strip() for j in sorted(self.phrases) if j < p.idx and self.phrases[j].src_text)
        return prev[-self.cfg.context_chars :].strip()

    async def _pump(self, p: Phrase) -> None:
        """Send whatever source audio the owning slot has not seen yet; close the turn when the phrase ended."""
        if p.slot is None or not p.opened:
            return
        slot = self.slots[p.slot]
        if slot.phrase is not p or slot.state is not SlotState.LISTENING:
            return
        async with slot.send_lock:
            if slot.phrase is not p or slot.state is not SlotState.LISTENING:
                return
            while p.sent < len(p.chunks):
                data = p.chunks[p.sent]
                for i in range(0, len(data), CHUNK * 2):
                    await slot.send_audio(data[i : i + CHUNK * 2])
                p.sent += 1
            if p.ended and slot.state is SlotState.LISTENING:
                await slot.session.send_realtime_input(activity_end=types.ActivityEnd())
                p.t_activity_end = time.monotonic()
                slot.state = SlotState.SPEAKING
                slot._spoke_at = time.monotonic()
                self._emit_slots()

    async def _phrase_done(self, slot: Slot, p: Phrase) -> None:
        out_s = p.out_samples / OUT_RATE
        flags = []
        if is_revoiced(p.src_text, p.out_text, list(self._recent)):
            flags.append("repeat")
        if out_s > 2.0 * p.src_dur + 1.5:
            flags.append("long")  # far longer than its source: hallucinated or replayed earlier commentary
        if p.src_dur > 3.0 and out_s < 0.35 * p.src_dur and len(p.src_text.split()) > 4:
            flags.append("short")
        if p.out_samples == 0 and len(p.src_text.split()) > 2:
            flags.append("silent")
        retry = bool(flags) and self.cfg.retry_flagged and p.attempts < 2
        # Still wrong after a context-free retry: silence over the ducked original beats wrong commentary.
        suppress = self.cfg.retry_flagged and not retry and bool({"repeat", "long"} & set(flags))
        ttfa = (p.t_first_audio - p.t_activity_end) if (p.t_first_audio and p.t_activity_end) else None
        self._emit(
            {
                "type": "phrase_done",
                "idx": p.idx,
                "slot": slot.n,
                "attempt": p.attempts,
                "t_start": p.t_start,
                "t_end": p.t_end,
                "src_text": p.src_text,
                "out_text": p.out_text,
                "out_s": round(out_s, 3),
                "ttfa": None if ttfa is None else round(ttfa, 3),
                "flags": flags,
                "retry": retry,
                "suppress": suppress,
            }
        )
        if not retry and not suppress and p.out_text:
            self._recent.append((p.src_text, p.out_text))
        async with self._lock:
            slot.phrase = None
            if slot.worn_out:
                slot.state = SlotState.RETIRING
                asyncio.create_task(self._replace(slot))
            else:
                slot.state = SlotState.IDLE
            if retry:
                p.slot = None
                p.out_text = ""
                p.out_samples = 0
                p.t_first_audio = None
                p.no_context = bool({"repeat", "long"} & set(flags))
                self._pending.appendleft(p)
            await self._dispatch_locked()

    # ---------- session health ----------
    def _retire_soon(self, slot: Slot) -> None:
        slot.turns = self.cfg.max_turns_per_session  # replaced as soon as its current turn ends

    async def _replace(self, slot: Slot) -> None:
        await slot.close()
        if self._closed:
            return
        try:
            await slot.open()
        except Exception as e:
            log.error("slot %d reopen failed: %s", slot.n, e)
            await asyncio.sleep(1.0)
            asyncio.create_task(self._replace(slot))
            return
        async with self._lock:
            await self._dispatch_locked()

    async def _slot_failed(self, slot: Slot) -> None:
        if self._closed:
            return
        async with self._lock:
            p = slot.phrase
            slot.phrase = None
            slot.state = SlotState.RETIRING
            if p is not None and p.attempts < 3:
                p.slot = None
                self._pending.appendleft(p)
        asyncio.create_task(self._replace(slot))

    async def _watch(self) -> None:
        """A turn that never completes would wedge its slot forever."""
        while True:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            for s in self.slots:
                if s.state is SlotState.SPEAKING and s.phrase and s._spoke_at:
                    limit = 10.0 + 2.5 * s.phrase.src_dur
                    if now - s._spoke_at > limit:
                        log.warning("slot %d stuck on phrase %d; recycling", s.n, s.phrase.idx)
                        s.turns = self.cfg.max_turns_per_session
                        await self._phrase_done(s, s.phrase)

    # ---------- output side ----------
    def _emit(self, ev: dict) -> None:
        self.events.put_nowait(ev)

    def _emit_slots(self) -> None:
        self._emit(
            {
                "type": "slots",
                "slots": [{"n": s.n, "state": s.state.value, "idx": s.phrase.idx if s.phrase else None} for s in self.slots],
                "pending": len(self._pending),
            }
        )


def pcm_bytes(x: np.ndarray) -> bytes:
    return x.astype(np.int16, copy=False).tobytes()
