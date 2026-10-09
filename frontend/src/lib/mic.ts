// Microphone -> 16 kHz mono int16 chunks of 100 ms (what the Live API expects).

const WORKLET = `
class Pcm16 extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Int16Array(1600); this.n = 0; }
  process(inputs) {
    const ch = inputs[0][0];
    if (ch) for (let i = 0; i < ch.length; i++) {
      const v = Math.max(-1, Math.min(1, ch[i]));
      this.buf[this.n++] = v < 0 ? v * 32768 : v * 32767;
      if (this.n === this.buf.length) { this.port.postMessage(this.buf.buffer, [this.buf.buffer]); this.buf = new Int16Array(1600); this.n = 0; }
    }
    return true;
  }
}
registerProcessor("pcm16", Pcm16);`;

export type MicHandle = { stop(): void; level(): number };

export async function startMic(onChunk: (pcm: ArrayBuffer) => void): Promise<MicHandle> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: false, autoGainControl: true },
  });
  // A 16 kHz context makes the browser resample the mic for us.
  const ctx = new AudioContext({ sampleRate: 16000 });
  const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
  await ctx.audioWorklet.addModule(url);
  URL.revokeObjectURL(url);
  const src = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "pcm16");
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  src.connect(node);
  src.connect(analyser);
  node.port.onmessage = (e) => onChunk(e.data as ArrayBuffer);
  const data = new Float32Array(analyser.fftSize);
  return {
    stop: () => { stream.getTracks().forEach((t) => t.stop()); node.disconnect(); void ctx.close(); },
    level: () => {
      analyser.getFloatTimeDomainData(data);
      let s = 0;
      for (const v of data) s += v * v;
      return Math.sqrt(s / data.length);
    },
  };
}
