// Plays translated commentary in sync with the programme and ducks the original under it.

export const OUT_RATE = 24000;

type DubSeg = { idx: number; start: number; dur: number; buffer: AudioBuffer; node?: AudioBufferSourceNode };

export class AudioEngine {
  readonly ctx: AudioContext;
  private out: GainNode;
  private dub = new Map<number, DubSeg>();
  private liveEnd = 0;
  private liveNodes: AudioBufferSourceNode[] = [];

  constructor() {
    this.ctx = new AudioContext({ latencyHint: "interactive" });
    this.out = this.ctx.createGain();
    this.out.connect(this.ctx.destination);
  }

  async resume() {
    if (this.ctx.state !== "running") await this.ctx.resume();
  }

  setLevel(v: number) {
    this.out.gain.setTargetAtTime(v, this.ctx.currentTime, 0.05);
  }

  private toBuffer(pcm: Float32Array): AudioBuffer {
    const b = this.ctx.createBuffer(1, Math.max(1, pcm.length), OUT_RATE);
    b.copyToChannel(pcm as Float32Array<ArrayBuffer>, 0);
    return b;
  }

  // ---------- dub mode: segments carry an absolute programme time ----------
  addSegment(idx: number, start: number, pcm: Float32Array) {
    const buffer = this.toBuffer(pcm);
    this.dub.set(idx, { idx, start, dur: buffer.duration, buffer });
  }

  /** Called ~10x/s with the programme clock. Schedules what is about to play; never double-schedules. */
  tick(programTime: number, playing: boolean) {
    if (!playing) {
      this.stopDub();
      return;
    }
    const now = this.ctx.currentTime;
    for (const s of this.dub.values()) {
      if (s.node || s.start + s.dur <= programTime || s.start > programTime + 1.5) continue;
      const node = this.ctx.createBufferSource();
      node.buffer = s.buffer;
      node.connect(this.out);
      const lead = s.start - programTime;
      if (lead >= 0) node.start(now + lead);
      else node.start(now, -lead); // joined mid-phrase (resume after pause/buffering)
      node.onended = () => {
        if (s.node === node) s.node = undefined;
      };
      s.node = node;
    }
    for (const [k, s] of this.dub) if (s.start + s.dur < programTime - 30) this.dub.delete(k);
  }

  stopDub() {
    for (const s of this.dub.values()) {
      if (s.node) {
        try { s.node.stop(); } catch { /* already stopped */ }
        s.node = undefined;
      }
    }
  }

  dubActiveAt(t: number): boolean {
    for (const s of this.dub.values()) if (t >= s.start - 0.15 && t <= s.start + s.dur + 0.1) return true;
    return false;
  }

  dubSegments(): { idx: number; start: number; dur: number }[] {
    return [...this.dub.values()].map(({ idx, start, dur }) => ({ idx, start, dur }));
  }

  // ---------- live mode: gapless queue, played as soon as it arrives ----------
  enqueueLive(pcm: Float32Array): number {
    const buffer = this.toBuffer(pcm);
    const node = this.ctx.createBufferSource();
    node.buffer = buffer;
    node.connect(this.out);
    const at = Math.max(this.ctx.currentTime + 0.06, this.liveEnd);
    node.start(at);
    this.liveEnd = at + buffer.duration;
    this.liveNodes.push(node);
    node.onended = () => (this.liveNodes = this.liveNodes.filter((n) => n !== node));
    return at;
  }

  liveActive(): boolean {
    return this.ctx.currentTime < this.liveEnd + 0.1;
  }

  clear() {
    this.stopDub();
    this.dub.clear();
    for (const n of this.liveNodes) try { n.stop(); } catch { /* noop */ }
    this.liveNodes = [];
    this.liveEnd = 0;
  }
}

/** Smooth ducking: fast attack when the translator speaks, slow release in the gaps. */
export class Ducker {
  level: number;
  constructor(initial: number) {
    this.level = initial;
  }
  step(active: boolean, duckTo: number, base: number): number {
    const target = active ? duckTo : base;
    const k = target < this.level ? 0.45 : 0.08;
    this.level += (target - this.level) * k;
    return this.level;
  }
}
