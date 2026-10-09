from pathlib import Path

import numpy as np
import pytest

from commentary.audio import StreamingWSOLA, roar_gate, time_compress
from commentary.config import LANGUAGES, OUT_RATE
from commentary.prompts import build_system_instruction
from commentary.relay import is_revoiced, repeat_score
from commentary.timeline import DubTimeline, LiveTimeline

SR = OUT_RATE
STIMULUS = Path(__file__).resolve().parents[2] / "cache" / "stimuli" / "icc_ind_ban_2016_16k.pcm"


def voiced(seconds: float, f0: float = 140.0) -> np.ndarray:
    """Speech-like: harmonic stack with moving pitch and syllabic amplitude modulation."""
    t = np.arange(int(SR * seconds)) / SR
    pitch = f0 * (1 + 0.15 * np.sin(2 * np.pi * 0.7 * t))
    phase = 2 * np.pi * np.cumsum(pitch) / SR
    x = sum(np.sin(k * phase) / k for k in range(1, 12))
    env = 0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 3.0 * t))
    return (0.15 * x * env).astype(np.float32)


def roar(seconds: float) -> np.ndarray:
    rng = np.random.default_rng(0)
    n = rng.standard_normal(int(SR * seconds)).astype(np.float32)
    k = np.ones(8, np.float32) / 8
    return (0.2 * np.convolve(n, k, mode="same")).astype(np.float32)


class TestWSOLA:
    def test_passthrough_at_unity(self):
        x = voiced(1.0)
        w = StreamingWSOLA(1.0)
        assert np.array_equal(w.push(x), x)

    @pytest.mark.parametrize("rate", [1.08, 1.18])
    def test_length_and_pitch(self, rate):
        t = np.arange(SR * 2) / SR
        x = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        y = time_compress(x, rate)
        assert abs(len(y) / SR - 2 / rate) < 0.05
        peak = np.fft.rfftfreq(len(y), 1 / SR)[np.argmax(np.abs(np.fft.rfft(y)))]
        assert abs(peak - 220) < 2

    def test_streaming_matches_offline_length(self):
        x = voiced(2.0)
        w = StreamingWSOLA(1.12)
        parts = [w.push(x[i : i + 1234]) for i in range(0, len(x), 1234)] + [w.flush()]
        assert abs(sum(map(len, parts)) - len(time_compress(x, 1.12))) < SR * 0.01


class TestRoarGate:
    def test_speech_untouched(self):
        x = voiced(3.0)
        y, share = roar_gate(x)
        assert share == 0.0 and np.array_equal(x, y)

    def test_roar_attenuated(self):
        x = np.concatenate([voiced(1.5), roar(2.0), voiced(1.5)])
        y, share = roar_gate(x)
        assert share > 0.25
        mid = slice(int(SR * 2.0), int(SR * 3.0))
        assert np.sqrt(np.mean(y[mid] ** 2)) < 0.15 * np.sqrt(np.mean(x[mid] ** 2))
        head = slice(0, int(SR * 1.2))
        assert np.allclose(y[head], x[head])


def pcm(seconds: float) -> bytes:
    return (voiced(seconds) * 32767).astype(np.int16).tobytes()


class Sink:
    def __init__(self):
        self.items = []

    async def __call__(self, meta, data):
        self.items.append((meta, data))


async def done(tl, idx, attempt=1, retry=False):
    await tl.on_event({"type": "phrase_done", "idx": idx, "attempt": attempt, "retry": retry, "out_text": f"t{idx}", "src_text": ""})


class TestDubTimeline:
    async def test_anchor_and_compress(self):
        sink = Sink()
        tl = DubTimeline(sink)
        tl.phrase_started(0, 10.0)
        tl.phrase_started(1, 12.0)
        await tl.on_event({"type": "audio", "idx": 0, "attempt": 1, "pcm": pcm(2.6)})  # overruns next phrase
        await done(tl, 0)
        seg = [m for m, _ in sink.items if m["type"] == "segment"]
        assert len(seg) == 1
        assert seg[0]["start"] == pytest.approx(10.12)
        assert seg[0]["rate"] > 1.0
        assert seg[0]["start"] + seg[0]["dur"] <= 12.0 + 0.35 + 0.05

    async def test_in_order_and_waits_for_predecessor(self):
        sink = Sink()
        tl = DubTimeline(sink)
        for i, t in enumerate([0.0, 3.0, 6.0]):
            tl.phrase_started(i, t)
        await tl.on_event({"type": "audio", "idx": 1, "attempt": 1, "pcm": pcm(1.0)})
        await done(tl, 1)
        assert not [m for m, _ in sink.items if m["type"] == "segment"]
        await tl.on_event({"type": "audio", "idx": 0, "attempt": 1, "pcm": pcm(1.0)})
        await done(tl, 0)
        assert [m["idx"] for m, _ in sink.items if m["type"] == "segment"] == [0, 1]

    async def test_retry_discards_first_attempt(self):
        sink = Sink()
        tl = DubTimeline(sink)
        tl.phrase_started(0, 0.0)
        tl.set_frontier(0.0, source_done=True)
        await tl.on_event({"type": "audio", "idx": 0, "attempt": 1, "pcm": pcm(3.0)})
        await done(tl, 0, attempt=1, retry=True)
        await tl.on_event({"type": "audio", "idx": 0, "attempt": 2, "pcm": pcm(1.0)})
        await done(tl, 0, attempt=2)
        seg = [m for m, _ in sink.items if m["type"] == "segment"]
        assert len(seg) == 1 and seg[0]["dur"] == pytest.approx(1.0, abs=0.05)

    async def test_skips_hopelessly_late_phrase(self):
        sink = Sink()
        tl = DubTimeline(sink)
        tl.phrase_started(0, 0.0)
        tl.phrase_started(1, 2.0)
        await tl.tick(5.0)
        assert ("skip", 0) in [(m["type"], m.get("idx")) for m, _ in sink.items]


class TestLiveTimeline:
    async def test_streams_in_phrase_order(self):
        sink = Sink()
        tl = LiveTimeline(sink)
        tl.phrase_started(0, 0.0)
        tl.phrase_started(1, 2.0)
        await tl.on_event({"type": "audio", "idx": 1, "pcm": pcm(0.5)})
        assert not [m for m, _ in sink.items if m["type"] == "chunk"]
        await tl.on_event({"type": "audio", "idx": 0, "pcm": pcm(0.5)})
        await done(tl, 0)
        await done(tl, 1)
        chunks = [m["idx"] for m, _ in sink.items if m["type"] == "chunk"]
        assert chunks and chunks == sorted(chunks) and set(chunks) == {0, 1}


def test_repeat_score_flags_context_leak():
    prev = ["ഡീപ് സ്ക്വയർ ലെഗിൽ ക്യാച്ചായി!"]
    assert repeat_score("ഡീപ് സ്ക്വയർ ലെഗിൽ ക്യാച്ച്!", prev) > 0.6
    assert repeat_score("എന്ത് തിരിച്ചുവരവാണ്!", prev) < 0.3


@pytest.mark.parametrize("lang", [l.code for l in LANGUAGES])
def test_prompt_names_target_language(lang):
    si = build_system_instruction(lang, "cricket")
    name = next(l.name for l in LANGUAGES if l.code == lang)
    assert f"RESPOND IN {name.upper()}" in si and "cricket" in si


@pytest.mark.skipif(not STIMULUS.exists(), reason="broadcast stimulus not cached")
def test_segmenter_keeps_shouted_climax():
    from commentary.segmenter import segment_array

    x = np.frombuffer(STIMULUS.read_bytes(), np.int16)
    phrases = segment_array(x[16000 * 128 : 16000 * 160], t0=128.0)
    assert all(1.2 <= b - a <= 6.1 for a, b, _ in phrases[:-1])  # last one is cut by the buffer edge
    # the last-ball call shouted over the crowd roar (131-143 s) must be covered without holes
    covered = sum(min(b, 143) - max(a, 131) for a, b, _ in phrases if b > 131 and a < 143)
    assert covered > 10.5


SPANISH = STIMULUS.parent / "ojvz9Ko_FMQ_16k.pcm"


@pytest.mark.skipif(not SPANISH.exists(), reason="spanish stimulus not cached")
def test_segmenter_catches_screamed_gol():
    """A compressed radio call of 'gol gol gol...' barely registers with Silero; the shout detector must catch it."""
    from commentary.segmenter import segment_array

    x = np.frombuffer(SPANISH.read_bytes(), np.int16)[16000 * 12 : 16000 * 41]
    phrases = segment_array(x, t0=12.0)
    assert sum(b - a for a, b, _ in phrases) / 29 > 0.85


def test_cached_audio_ignores_metadata_sidecar(tmp_path, monkeypatch):
    import commentary.sources as src

    monkeypatch.setattr(src, "YT_DIR", tmp_path)
    (tmp_path / "abcdefghijk.json").write_text("{}")
    assert src.cached_youtube_audio("abcdefghijk") is None
    (tmp_path / "abcdefghijk.webm").write_bytes(b"x")
    assert src.cached_youtube_audio("abcdefghijk").suffix == ".webm"


async def test_dub_suppressed_phrase_is_skipped_not_placed():
    sink = Sink()
    tl = DubTimeline(sink)
    tl.phrase_started(0, 0.0)
    tl.phrase_started(1, 3.0)
    tl.set_frontier(10.0, source_done=True)
    await tl.on_event({"type": "audio", "idx": 0, "attempt": 2, "pcm": pcm(4.0)})
    await tl.on_event({"type": "phrase_done", "idx": 0, "attempt": 2, "retry": False, "suppress": True, "out_text": "x", "src_text": ""})
    await tl.on_event({"type": "audio", "idx": 1, "attempt": 1, "pcm": pcm(1.0)})
    await done(tl, 1)
    kinds = [(m["type"], m.get("idx")) for m, _ in sink.items]
    assert ("skip", 0) in kinds
    seg = [m for m, _ in sink.items if m["type"] == "segment"]
    assert [m["idx"] for m in seg] == [1] and seg[0]["start"] == pytest.approx(3.12)


# Real failure from a Cloud Run Tamil run of the ICC climax: phrase 9 ("What a comeback from India!")
# came out as a replay of phrases 3, 4 and 8.
EARLIER = [
    ("Is he in?", "அவர் உள்ளே வந்துட்டாரா?"),
    ("We'll go upstairs. Some good work from Johnny, he thinks he's got him.", "தேர்ட் அம்பயர்ட்ட போகலாம்! ஜானியிடமிருந்து ஒரு நல்ல வேலை"),
    ("Is he run out?", "ரன் அவுட்டா?"),
]


def test_revoiced_stitched_replay_is_caught():
    out = "அவர் ரன் அவுட்டா? உள்ளே வந்துட்டாரா? தேர்ட் அம்பயர்ட்ட போகலாம்!"
    assert is_revoiced("What a comeback from India!", out, EARLIER)


def test_genuine_source_repeat_is_not_flagged():
    recent = [("Incluso la palabra golazo se queda corta", "गोलाज़ो शब्द भी कम पड़ जाता है!")]
    assert not is_revoiced("Incluso la palabra golazo se queda corta ante este gol", "गोलाज़ो शब्द भी कम पड़ जाता है!", recent)


def test_new_content_is_not_flagged():
    assert not is_revoiced("What a comeback from India!", "இந்தியாவிடமிருந்து என்ன ஒரு கம்பேக்!", EARLIER)
