"""PCM helpers and a streaming, pitch-preserving WSOLA time-compressor."""

from __future__ import annotations

import numpy as np


def pcm_to_float(b: bytes) -> np.ndarray:
    return np.frombuffer(b, np.int16).astype(np.float32) / 32768.0


def float_to_pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


def apply_edge_fades(x: np.ndarray, rate: int, fade_in_ms: float = 8, fade_out_ms: float = 25) -> np.ndarray:
    x = x.copy()
    a = min(len(x), int(rate * fade_in_ms / 1000))
    b = min(len(x), int(rate * fade_out_ms / 1000))
    if a:
        x[:a] *= np.linspace(0, 1, a, dtype=np.float32)
    if b:
        x[-b:] *= np.linspace(1, 0, b, dtype=np.float32)
    return x


class StreamingWSOLA:
    """Waveform-similarity overlap-add. rate > 1 shortens audio without changing pitch.

    push() accepts arbitrary chunk sizes and returns whatever output is final so far;
    flush() returns the tail. rate == 1 is an exact passthrough.
    """

    def __init__(self, rate: float, sr: int = 24000, win_ms: float = 30.0, tol_ms: float = 8.0):
        self.rate = float(rate)
        self.win = int(sr * win_ms / 1000) & ~1
        self.hs = self.win // 2
        self.ha = self.hs * self.rate
        self.tol = int(sr * tol_ms / 1000)
        n = np.arange(self.win)
        self.window = (0.5 - 0.5 * np.cos(2 * np.pi * n / self.win)).astype(np.float32)  # periodic Hann: COLA at 50%
        self._in = np.zeros(0, np.float32)
        self._in_off = 0  # absolute index of self._in[0]
        self._pos = 0.0  # absolute analysis position of next frame
        self._prev_nat: np.ndarray | None = None  # natural continuation of previous frame
        self._ola = np.zeros(self.win, np.float32)

    def push(self, x: np.ndarray) -> np.ndarray:
        if self.rate == 1.0:
            return x
        self._in = np.concatenate([self._in, x.astype(np.float32)])
        return self._run(final=False)

    def flush(self) -> np.ndarray:
        if self.rate == 1.0:
            return np.zeros(0, np.float32)
        out = self._run(final=True)
        tail = self._ola[: self.hs].copy()
        return np.concatenate([out, tail])

    def _seg(self, start: int) -> np.ndarray:
        s = start - self._in_off
        seg = self._in[max(s, 0) : s + self.win]
        if len(seg) < self.win:
            seg = np.pad(seg, (0, self.win - len(seg)))
        return seg

    def _run(self, final: bool) -> np.ndarray:
        out = []
        while True:
            center = int(self._pos)
            need = center + self.tol + self.win + self.hs
            if need - self._in_off > len(self._in) and not (final and center - self._in_off < len(self._in)):
                break
            if self._prev_nat is None:
                best = center
            else:
                lo = max(center - self.tol, self._in_off)
                hi = center + self.tol
                region = self._in[lo - self._in_off : hi - self._in_off + self.win]
                if len(region) < self.win + 1:
                    best = center
                else:
                    tmpl = self._prev_nat
                    corr = np.correlate(region, tmpl, mode="valid")
                    energy = np.sqrt(np.convolve(region**2, np.ones(self.win), mode="valid")) + 1e-6
                    best = lo + int(np.argmax(corr / energy))
            frame = self._seg(best) * self.window
            self._ola += frame
            out.append(self._ola[: self.hs].copy())
            self._ola = np.concatenate([self._ola[self.hs :], np.zeros(self.hs, np.float32)])
            self._prev_nat = self._seg(best + self.hs)
            self._pos += self.ha
            drop = int(self._pos) - self.tol - self._in_off - self.win
            if drop > 0:
                self._in = self._in[drop:]
                self._in_off += drop
            if final and int(self._pos) - self._in_off >= len(self._in):
                break
        return np.concatenate(out) if out else np.zeros(0, np.float32)


def time_compress(x: np.ndarray, rate: float, sr: int = 24000) -> np.ndarray:
    w = StreamingWSOLA(rate, sr)
    return np.concatenate([w.push(x), w.flush()])


# ---------- roar gate ----------
# gemini-3.8-live sometimes answers a crowd-roar phrase by synthesizing the roar itself: seconds of
# stationary, weakly periodic sound instead of speech. Under the ducked original that plays as a
# doubled, smeared crowd. Those runs are detected (low pitch periodicity AND low spectral flux,
# sustained >= 300 ms) and attenuated.

_HOP_S, _FRAME_S = 0.02, 0.04


def _frame_features(x: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    hop, fl = int(sr * _HOP_S), int(sr * _FRAME_S)
    n = max(0, (len(x) - fl) // hop + 1)
    if n == 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    idx = np.arange(fl)[None, :] + hop * np.arange(n)[:, None]
    frames = x[idx]
    frames = frames - frames.mean(1, keepdims=True)
    rms = np.sqrt((frames**2).mean(1))
    spec = np.abs(np.fft.rfft(frames, 2 * fl, axis=1)) ** 2
    ac = np.fft.irfft(spec, axis=1)[:, :fl]
    lmin, lmax = int(sr / 500), int(sr / 70)
    per = ac[:, lmin:lmax].max(1) / np.maximum(ac[:, 0], 1e-9)
    win = np.hanning(fl)
    mag = np.abs(np.fft.rfft(frames * win, axis=1)) ** 2
    freqs = np.fft.rfftfreq(fl, 1 / sr)
    edges = np.geomspace(100, min(8000, sr / 2 - 1), 41)
    bands = np.stack([mag[:, (freqs >= lo) & (freqs < hi)].sum(1) for lo, hi in zip(edges[:-1], edges[1:])], 1)
    db = 10 * np.log10(bands + 1e-10)
    flux = np.r_[0.0, np.abs(np.diff(db, axis=0)).mean(1)]
    return per, flux, rms


def _rolling_median(x: np.ndarray, k: int = 15) -> np.ndarray:
    if len(x) < k:
        return x
    pad = np.pad(x, (k // 2, k - 1 - k // 2), mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(pad, k), axis=1)


ROAR_PERIODICITY = 0.5
ROAR_FLUX_DB = 4.4
ROAR_MIN_FRAMES = 25  # 500 ms: strained shouts are rough but short; synthesized roars last seconds


def roar_mask(x: np.ndarray, sr: int = 24000) -> tuple[np.ndarray, np.ndarray]:
    per, flux, rms = _frame_features(x, sr)
    active = rms > 0.01
    cand = active & (_rolling_median(per) < ROAR_PERIODICITY) & (_rolling_median(flux) < ROAR_FLUX_DB)
    mask = np.zeros_like(cand)
    run = 0
    for i, v in enumerate(np.r_[cand, False]):
        if v:
            run += 1
        else:
            if run >= ROAR_MIN_FRAMES:
                mask[i - run : i] = True
            run = 0
    return mask, active


def roar_gate(x: np.ndarray, sr: int = 24000, atten_db: float = -24.0) -> tuple[np.ndarray, float]:
    """Attenuate synthesized-crowd runs. Returns (audio, share of active time gated)."""
    mask, active = roar_mask(x, sr)
    if not mask.any():
        return x, 0.0
    hop = int(sr * _HOP_S)
    g = np.where(mask, 10 ** (atten_db / 20), 1.0).astype(np.float32)
    ramp = max(1, int(0.03 / _HOP_S))
    g = np.convolve(np.pad(g, (ramp, ramp), mode="edge"), np.ones(2 * ramp + 1) / (2 * ramp + 1), mode="same")[ramp:-ramp]
    gain = np.interp(np.arange(len(x)), np.arange(len(g)) * hop + int(sr * _FRAME_S) // 2, g).astype(np.float32)
    return x * gain, float(mask.sum() / max(active.sum(), 1))
