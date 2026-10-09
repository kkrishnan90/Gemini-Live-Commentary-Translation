"""Causal phrase segmenter for commentary audio.

Cuts a continuous 16 kHz stream into phrases of 1.2-6 s at natural pauses so each phrase can be
handed to one relay interpreter. Broadcast audio has a loud crowd bed and the most important
calls are shouted over the loudest roars, so energy thresholds fail exactly when it matters;
speech is detected with Silero VAD (neural, noise-robust) behind an automatic gain control, plus a
pitched-shout detector: a sustained screamed vowel ("GOOOOL") barely registers as speech to Silero but
is loud and strongly periodic, which crowd noise never is. When no pause shows up in time, the
cut goes on the least speech-like frame near the length limit and the next phrase starts at once.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import onnxruntime as ort

from .config import IN_RATE

FRAME = 512  # 32 ms @ 16 kHz, Silero v5 frame
FRAME_S = FRAME / IN_RATE
_MODEL = Path(__file__).parent / "models" / "silero_vad.onnx"


class SileroVAD:
    _session: ort.InferenceSession | None = None

    def __init__(self) -> None:
        if SileroVAD._session is None:
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            SileroVAD._session = ort.InferenceSession(str(_MODEL), opts, providers=["CPUExecutionProvider"])
        self._state = np.zeros((2, 1, 128), np.float32)
        self._context = np.zeros(64, np.float32)
        self._sr = np.array(IN_RATE, np.int64)

    def __call__(self, frame: np.ndarray) -> float:
        x = frame.astype(np.float32) / 32768.0
        inp = np.concatenate([self._context, x])[None, :]
        out, self._state = self._session.run(None, {"input": inp, "state": self._state, "sr": self._sr})
        self._context = x[-64:]
        return float(out[0, 0])


@dataclass
class SegmenterConfig:
    min_phrase_s: float = 1.2
    max_phrase_s: float = 6.0
    soft_cut_window_s: float = 1.5  # look for a low-speech frame this long before max
    pause_s: float = 0.25
    preroll_s: float = 0.25
    onset_frames: int = 2
    enter_prob: float = 0.4
    exit_prob: float = 0.3
    agc_target_rms: float = 3000.0  # int16 units (~ -21 dBFS)
    shout_periodicity: float = 0.5  # crowd beds sit at ~0.3; voiced shouting well above
    shout_share: float = 0.4  # share of periodic frames in the last 320 ms


# Live sources trade a little context for latency: the listener waits for each phrase to end.
LIVE_SEGMENTER = SegmenterConfig(min_phrase_s=1.0, max_phrase_s=3.5, soft_cut_window_s=1.0, pause_s=0.2)


@dataclass
class PhraseEvent:
    kind: str  # "start" | "audio" | "end"
    idx: int
    t: float  # stream time (s) of the event boundary
    pcm: np.ndarray | None = field(default=None, repr=False)


class PhraseSegmenter:
    def __init__(self, cfg: SegmenterConfig | None = None, t0: float = 0.0, start_idx: int = 0):
        self.cfg = cfg or SegmenterConfig()
        self.t0 = t0
        self.vad = SileroVAD()
        self._carry = np.zeros(0, np.int16)
        self._frames_seen = 0
        self._preroll: deque[np.ndarray] = deque(maxlen=max(1, round(self.cfg.preroll_s / FRAME_S)))
        self._onset = 0
        self._idx = start_idx - 1
        self._in_phrase = False
        self._phrase_frames = 0
        self._pause_run = 0
        self._probs: list[float] = []
        self._level = None  # EMA of active-frame RMS (for AGC)
        self._rms_hist: deque[float] = deque(maxlen=round(10 / FRAME_S))
        self._lmin, self._lmax = IN_RATE // 500, IN_RATE // 70
        self._periodic: deque[bool] = deque(maxlen=10)

    def _speech_score(self, frame: np.ndarray) -> float:
        c = self.cfg
        f = frame.astype(np.float32)
        rms = float(np.sqrt(np.mean(f**2))) + 1e-3
        if rms > 30:  # ignore digital silence when tracking level
            self._level = rms if self._level is None else 0.985 * self._level + 0.015 * rms
        gain = float(np.clip(c.agc_target_rms / (self._level or c.agc_target_rms), 0.5, 10.0))
        prob = self.vad(np.clip(f * gain, -32767, 32767))
        self._rms_hist.append(rms)
        floor = float(np.percentile(self._rms_hist, 20)) if len(self._rms_hist) > 20 else 0.0
        x = f - f.mean()
        ac = np.correlate(x, x, "full")[len(x) - 1 :]
        periodic = bool(rms >= floor and rms > 100 and ac[0] > 0 and ac[self._lmin : self._lmax].max() / ac[0] >= c.shout_periodicity)
        self._periodic.append(periodic)
        if sum(self._periodic) >= c.shout_share * self._periodic.maxlen:
            prob = max(prob, 0.75)
        return prob

    @property
    def now(self) -> float:
        return self.t0 + self._frames_seen * FRAME_S

    @property
    def in_phrase(self) -> bool:
        return self._in_phrase

    def feed(self, pcm: np.ndarray) -> list[PhraseEvent]:
        pcm = np.concatenate([self._carry, pcm.astype(np.int16, copy=False)])
        n = len(pcm) // FRAME
        self._carry = pcm[n * FRAME :]
        events: list[PhraseEvent] = []
        for i in range(n):
            events.extend(self._frame(pcm[i * FRAME : (i + 1) * FRAME]))
        return events

    def flush(self) -> list[PhraseEvent]:
        events: list[PhraseEvent] = []
        if len(self._carry):
            events.extend(self.feed(np.zeros(FRAME - len(self._carry), np.int16)))
        if self._in_phrase:
            events.append(self._end())
        return events

    def _frame(self, frame: np.ndarray) -> list[PhraseEvent]:
        c = self.cfg
        prob = self._speech_score(frame)
        t = self.now
        self._frames_seen += 1
        out: list[PhraseEvent] = []

        if not self._in_phrase:
            self._preroll.append(frame)
            self._onset = self._onset + 1 if prob >= c.enter_prob else 0
            if self._onset >= c.onset_frames:
                pre = np.concatenate(list(self._preroll))
                start_t = self.now - len(pre) / IN_RATE
                self._idx += 1
                self._in_phrase = True
                self._phrase_frames = len(pre) // FRAME
                self._pause_run = 0
                self._probs = []
                self._preroll.clear()
                out.append(PhraseEvent("start", self._idx, start_t))
                out.append(PhraseEvent("audio", self._idx, start_t, pre))
            return out

        out.append(PhraseEvent("audio", self._idx, t, frame))
        self._phrase_frames += 1
        self._probs.append(prob)
        self._pause_run = self._pause_run + 1 if prob < c.exit_prob else 0
        length = self._phrase_frames * FRAME_S

        if self._pause_run * FRAME_S >= c.pause_s and length >= c.min_phrase_s:
            out.append(self._end())
        elif length >= c.max_phrase_s - c.soft_cut_window_s:
            low = np.percentile(self._probs, 20)
            if prob <= min(low, 0.9) or length >= c.max_phrase_s:
                out.append(self._end(continue_speech=True))
        return out

    def _end(self, continue_speech: bool = False) -> PhraseEvent:
        ev = PhraseEvent("end", self._idx, self.now)
        self._in_phrase = False
        # A forced cut lands mid-flow: re-open on the next speech frame so no speech is lost.
        self._onset = self.cfg.onset_frames - 1 if continue_speech else 0
        return ev


def segment_array(pcm: np.ndarray, cfg: SegmenterConfig | None = None, t0: float = 0.0) -> list[tuple[float, float, np.ndarray]]:
    """Offline helper: [(t_start, t_end, pcm)] for a whole buffer."""
    seg = PhraseSegmenter(cfg, t0=t0)
    phrases: dict[int, list] = {}
    bounds: dict[int, list[float]] = {}
    events = []
    step = IN_RATE // 10
    for i in range(0, len(pcm), step):
        events.extend(seg.feed(pcm[i : i + step]))
    events.extend(seg.flush())
    for ev in events:
        if ev.kind == "start":
            phrases[ev.idx] = []
            bounds[ev.idx] = [ev.t, ev.t]
        elif ev.kind == "audio":
            phrases[ev.idx].append(ev.pcm)
        else:
            bounds[ev.idx][1] = ev.t
    return [(bounds[k][0], bounds[k][1], np.concatenate(phrases[k])) for k in sorted(phrases)]
