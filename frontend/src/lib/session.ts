// Drives one translation run: websocket, programme clock, audio scheduling and ducking.
import { AudioEngine, Ducker } from "./audioEngine";
import { startMic, type MicHandle } from "./mic";
import { createMedia, createMicClock, createYouTube, type Programme } from "./players";
import { cleanText, parseAudioFrame, type ServerEvent, type SlotState, type Source } from "./protocol";

export type Phase = "idle" | "connecting" | "buffering" | "onair" | "ended" | "error";

export type PhraseRow = { idx: number; tStart: number; tEnd?: number; src: string; out: string; slot?: number; flags: string[] };
export type VoicedBlock = { idx: number; start: number; dur: number; slot?: number };

export type Snapshot = {
  phase: Phase;
  status: string;
  mode: "dub" | "live" | null;
  now: number;
  slots: { n: number; state: SlotState; idx: number | null }[];
  pending: number;
  phrases: PhraseRow[];
  voiced: VoicedBlock[];
  subtitle: { out: string; src: string } | null;
  stats: Extract<ServerEvent, { type: "stats" }> | null;
  micLevel: number;
  duckLevel: number;
};

export type StartOptions = {
  source: Source;
  lang: string;
  voice: string;
  sport: string;
  startAt: number;
  stage: HTMLElement;
  mix: () => { original: number; ducked: number; translation: number };
};

export class SessionController {
  private ws?: WebSocket;
  private engine?: AudioEngine;
  private prog?: Programme;
  private mic?: MicHandle;
  private timer?: number;
  private ducker = new Ducker(0.8);
  private lastClockSent = 0;
  private lastT = 0;
  private lastTick = 0;
  private sourceDone = false;
  private readyUntil = 0;
  private awaitingReady = false;
  private phrases = new Map<number, PhraseRow>();
  private voiced = new Map<number, VoicedBlock & { text: string; src: string }>();
  private snap: Snapshot = {
    phase: "idle", status: "", mode: null, now: 0, slots: [], pending: 0, phrases: [], voiced: [],
    subtitle: null, stats: null, micLevel: 0, duckLevel: 1,
  };

  private onUpdate: (s: Snapshot) => void;
  constructor(onUpdate: (s: Snapshot) => void) {
    this.onUpdate = onUpdate;
  }

  private set(p: Partial<Snapshot>) {
    this.snap = { ...this.snap, ...p };
    this.onUpdate(this.snap);
  }

  async start(o: StartOptions) {
    await this.stop();
    this.phrases.clear();
    this.voiced.clear();
    this.sourceDone = false;
    this.readyUntil = 0;
    this.set({ phase: "connecting", status: "Connecting…", mode: null, slots: [], phrases: [], voiced: [], subtitle: null, stats: null });
    this.engine = new AudioEngine();
    await this.engine.resume(); // inside the click: unlocks audio output
    const s = o.source;
    if (s.kind === "youtube") {
      this.prog = await createYouTube(o.stage, s.id, o.startAt, (state) => {
        if (state === 0) this.set({ phase: "ended", status: "Match clip finished" });
      });
    } else if (s.kind === "file") {
      this.prog = createMedia(o.stage, s.url, s.isVideo);
      if (o.startAt) this.prog.seek(o.startAt);
    } else {
      this.prog = createMicClock();
      o.stage.replaceChildren();
    }
    this.prog.pause();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      const source =
        s.kind === "youtube" ? { kind: "youtube", id: s.id } : s.kind === "file" ? { kind: "file", media_id: s.mediaId } : { kind: "mic" };
      ws.send(JSON.stringify({ action: "start", source, lang: o.lang, voice: o.voice, sport: o.sport, start_at: o.startAt }));
    };
    ws.onmessage = (e) => (typeof e.data === "string" ? this.onEvent(JSON.parse(e.data), o) : this.onAudio(e.data));
    ws.onclose = () => {
      if (this.snap.phase !== "idle" && this.snap.phase !== "ended") this.set({ phase: "error", status: "Lost connection to the server. Press Start to reconnect." });
    };
    this.lastTick = performance.now();
    this.timer = window.setInterval(() => this.tick(o), 100);
  }

  async stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
    if (this.ws) {
      this.ws.onclose = null;
      if (this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ action: "stop" }));
      this.ws.close();
    }
    this.ws = undefined;
    this.mic?.stop();
    this.mic = undefined;
    this.engine?.clear();
    await this.engine?.ctx.close().catch(() => {});
    this.engine = undefined;
    this.prog?.destroy();
    this.prog = undefined;
    this.set({ phase: "idle", status: "", micLevel: 0 });
  }

  private send(m: object) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(m));
  }

  private async onEvent(ev: ServerEvent, o: StartOptions) {
    switch (ev.type) {
      case "status":
        this.set({ status: ev.text });
        break;
      case "error":
        this.set({ phase: "error", status: ev.text });
        break;
      case "started":
        this.set({ mode: ev.mode });
        if (ev.mode === "dub") {
          this.awaitingReady = true;
          this.set({ phase: "buffering", status: "Voicing the first calls…" });
        } else if (o.source.kind === "mic") {
          try {
            this.mic = await startMic((pcm) => this.ws?.readyState === WebSocket.OPEN && this.ws.send(pcm));
            this.set({ phase: "onair", status: "Listening — start commentating" });
          } catch {
            this.set({ phase: "error", status: "Microphone access was blocked. Allow it in the browser's site settings, then press Start." });
          }
        } else {
          this.prog?.play();
          this.set({ phase: "onair", status: "Live" });
        }
        break;
      case "ready":
        this.awaitingReady = false;
        this.prog?.play();
        this.set({ phase: "onair", status: "On air" });
        break;
      case "source_done":
        this.sourceDone = true;
        break;
      case "slots":
        this.set({ slots: ev.slots, pending: ev.pending });
        break;
      case "src_text":
      case "out_text": {
        const r = this.phrases.get(ev.idx);
        if (r) ev.type === "src_text" ? (r.src = cleanText(ev.text)) : (r.out = cleanText(ev.text));
        break;
      }
      case "phrase_done":
        this.phrases.set(ev.idx, {
          idx: ev.idx, tStart: ev.t_start, tEnd: ev.t_end, src: cleanText(ev.src_text), out: cleanText(ev.out_text), slot: ev.slot, flags: ev.flags,
        });
        { const v = this.voiced.get(ev.idx); if (v) v.slot = ev.slot; }
        break;
      case "seg_begin":
        if (!this.phrases.has(ev.idx)) this.phrases.set(ev.idx, { idx: ev.idx, tStart: ev.src_start, src: "", out: "", flags: [] });
        break;
      case "seg_end": {
        const v = this.voiced.get(ev.idx);
        if (v) { v.text = cleanText(ev.text); v.src = cleanText(ev.src_text); }
        break;
      }
      case "stats":
        if (ev.ready_until !== undefined) this.readyUntil = ev.ready_until;
        if (ev.source_done) this.sourceDone = true;
        this.set({ stats: ev });
        break;
    }
  }

  private onAudio(buf: ArrayBuffer) {
    if (!this.engine) return;
    const { meta, pcm } = parseAudioFrame(buf);
    if (meta.type === "segment") {
      this.engine.addSegment(meta.idx, meta.start, pcm);
      this.voiced.set(meta.idx, {
        idx: meta.idx, start: meta.start, dur: meta.dur, slot: this.phrases.get(meta.idx)?.slot,
        text: cleanText(meta.text), src: cleanText(meta.src_text),
      });
    } else {
      const at = this.engine.enqueueLive(pcm);
      const progStart = (this.prog?.time() ?? 0) + (at - this.engine.ctx.currentTime);
      const dur = pcm.length / 24000;
      const v = this.voiced.get(meta.idx);
      if (v) v.dur = progStart + dur - v.start;
      else this.voiced.set(meta.idx, { idx: meta.idx, start: progStart, dur, slot: this.phrases.get(meta.idx)?.slot, text: "", src: "" });
    }
  }

  private tick(o: StartOptions) {
    const prog = this.prog, engine = this.engine;
    if (!prog || !engine) return;
    const mix = o.mix();
    engine.setLevel(mix.translation);
    const t = prog.time();
    const playing = prog.playing();
    const nowMs = performance.now();
    const dt = (nowMs - this.lastTick) / 1000;
    this.lastTick = nowMs;
    const mode = this.snap.mode;

    if (mode === "dub") {
      // the viewer scrubbed: restart translation from the new position
      const expected = this.lastT + (playing ? dt : 0);
      if (this.snap.phase === "onair" && Math.abs(t - expected) > 1.5) {
        engine.clear();
        this.voiced.clear();
        prog.pause();
        this.awaitingReady = true;
        this.send({ action: "seek", t });
        this.set({ phase: "buffering", status: "Catching up from here…" });
      }
      // translation has not reached the playhead yet: hold the picture instead of playing it silent
      if (this.snap.phase === "onair" && playing && !this.sourceDone && this.readyUntil < t + 0.3) {
        prog.pause();
        this.set({ phase: "buffering", status: "Voicing the next calls…" });
      } else if (this.snap.phase === "buffering" && !this.awaitingReady && (this.readyUntil > t + 3 || this.sourceDone)) {
        prog.play();
        this.set({ phase: "onair", status: "On air" });
      }
      engine.tick(t, playing);
      if (nowMs - this.lastClockSent > 250) {
        this.send({ action: "clock", t, playing });
        this.lastClockSent = nowMs;
      }
    }
    this.lastT = t;

    const active = mode === "dub" ? engine.dubActiveAt(t + 0.15) : engine.liveActive();
    const level = this.ducker.step(active, mix.ducked, mix.original);
    prog.setVolume(level);

    let subtitle: Snapshot["subtitle"] = null;
    for (const v of this.voiced.values()) {
      if (t >= v.start - 0.1 && t <= v.start + v.dur + 0.6) {
        const p = this.phrases.get(v.idx);
        subtitle = { out: v.text || p?.out || "", src: v.src || p?.src || "" };
      }
    }
    this.set({
      now: t,
      subtitle,
      micLevel: this.mic?.level() ?? 0,
      duckLevel: level,
      phrases: [...this.phrases.values()].sort((a, b) => a.idx - b.idx).slice(-80),
      voiced: [...this.voiced.values()].sort((a, b) => a.idx - b.idx).slice(-80),
    });
  }
}
