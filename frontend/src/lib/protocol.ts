// Wire types shared with backend/commentary/session.py

export type Language = { code: string; name: string; native: string; group: "indic" | "global"; verified: boolean | null };
export type LibraryItem = { id: string; title: string; sport: string; source_lang: string; channel: string; duration: number };
export type AppConfig = {
  languages: Language[];
  voices: string[];
  default_voice: string;
  voice_pick: { voice?: string; why?: string };
  sports: string[];
  library: LibraryItem[];
};

export type Source =
  | { kind: "youtube"; id: string; title: string; isLive: boolean }
  | { kind: "file"; mediaId: string; title: string; url: string; isVideo: boolean }
  | { kind: "mic" };

export type SlotState = "connecting" | "idle" | "listening" | "speaking" | "retiring";

export type ServerEvent =
  | { type: "status"; text: string }
  | { type: "error"; text: string }
  | { type: "started"; mode: "dub" | "live"; start_at: number; pool: number }
  | { type: "ready" }
  | { type: "source_done"; t: number }
  | { type: "stopped" }
  | { type: "skip"; idx: number }
  | { type: "slots"; slots: { n: number; state: SlotState; idx: number | null }[]; pending: number }
  | { type: "src_text"; idx: number; text: string }
  | { type: "out_text"; idx: number; text: string }
  | {
      type: "phrase_done"; idx: number; slot: number; t_start: number; t_end: number;
      src_text: string; out_text: string; out_s: number; ttfa: number | null; flags: string[]; retry: boolean;
    }
  | { type: "seg_begin"; idx: number; rate: number; latency: number; src_start: number }
  | { type: "seg_end"; idx: number; text: string; src_text: string }
  | {
      type: "stats"; mode: "dub" | "live"; phrases: number; flags: number; retries: number; ttfa_ms: number | null; pending: number;
      ready_until?: number; source_done?: boolean; skipped?: number; latency_s?: number | null; backlog_s?: number;
    };

export type SegmentMeta = {
  type: "segment"; idx: number; start: number; src_start: number; dur: number; rate: number; gated: number;
  drift: number; text: string; src_text: string;
};
export type ChunkMeta = { type: "chunk"; idx: number };

/** Binary frame: [u32 LE header length][JSON header][int16 PCM @ 24 kHz]. */
export function parseAudioFrame(buf: ArrayBuffer): { meta: SegmentMeta | ChunkMeta; pcm: Float32Array } {
  const view = new DataView(buf);
  const n = view.getUint32(0, true);
  const meta = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, n)));
  const off = 4 + n;
  const samples = (buf.byteLength - off) >> 1;
  const pcm = new Float32Array(samples);
  const i16 = new DataView(buf, off);
  for (let i = 0; i < samples; i++) pcm[i] = i16.getInt16(i * 2, true) / 32768;
  return { meta, pcm };
}

/** Transcription carries markup like "<no speech>" and "{pause}" that viewers should never see. */
export function cleanText(s: string): string {
  return s.replace(/<[^>]*>/g, " ").replace(/\{[^}]*\}/g, " ").replace(/\s+/g, " ").trim();
}
