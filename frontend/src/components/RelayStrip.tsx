import type { PhraseRow, Snapshot, VoicedBlock } from "../lib/session";

// Four interpreters share the commentary: each phrase on the original lane is handed to one booth,
// and its translation lands on the lane below in that booth's colour.

const WINDOW_BEFORE = 14;
const WINDOW_AFTER = 16;

function pct(t: number, now: number) {
  return ((t - (now - WINDOW_BEFORE)) / (WINDOW_BEFORE + WINDOW_AFTER)) * 100;
}

function Lane({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="lane">
      <span className="lane-label">{label}</span>
      <div className="lane-track">{children}</div>
    </div>
  );
}

export function RelayStrip({ snap }: { snap: Snapshot }) {
  const now = snap.now;
  const visible = (a: number, b: number) => b > now - WINDOW_BEFORE && a < now + WINDOW_AFTER;
  const phrases = snap.phrases.filter((p) => visible(p.tStart, p.tEnd ?? p.tStart + 1));
  const voiced = snap.voiced.filter((v) => visible(v.start, v.start + v.dur));
  const byIdx = new Map<number, PhraseRow>(phrases.map((p) => [p.idx, p]));
  const slots = snap.slots.length ? snap.slots : [0, 1, 2, 3].map((n) => ({ n, state: "idle" as const, idx: null }));

  return (
    <section className="relay" aria-label="Interpreter relay">
      <div className="booths" role="list" aria-label="Interpreter booths">
        {slots.map((s) => (
          <div key={s.n} role="listitem" className={`booth booth-${s.n} is-${s.state}`} title={`Booth ${s.n + 1}: ${s.state}`}>
            <span className="tally" />
            <span className="booth-name">Booth {s.n + 1}</span>
            <span className="booth-state">{s.state === "speaking" ? "on mic" : s.state === "listening" ? "listening" : s.state}</span>
          </div>
        ))}
        {snap.pending > 0 && <div className="queue">{snap.pending} phrase{snap.pending > 1 ? "s" : ""} waiting for a free booth</div>}
      </div>

      <div className="lanes">
        <Lane label="Original">
          {phrases.map((p) => (
            <div
              key={p.idx}
              className={`block src ${p.slot !== undefined ? `booth-${p.slot}` : ""} ${p.flags.length ? "flagged" : ""}`}
              style={{ left: `${pct(p.tStart, now)}%`, width: `${Math.max(0.6, pct(p.tEnd ?? p.tStart + 1, now) - pct(p.tStart, now))}%` }}
              title={p.src}
            >
              <span>{p.src}</span>
            </div>
          ))}
        </Lane>
        <svg className="handoffs" viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden>
          {voiced.map((v: VoicedBlock) => {
            const p = byIdx.get(v.idx);
            if (!p) return null;
            return <line key={v.idx} className={`booth-${v.slot ?? p.slot ?? 0}`} x1={pct(p.tStart, now)} y1={0} x2={pct(v.start, now)} y2={10} />;
          })}
        </svg>
        <Lane label="Translation">
          {voiced.map((v) => (
            <div
              key={v.idx}
              className={`block out booth-${v.slot ?? 0}`}
              style={{ left: `${pct(v.start, now)}%`, width: `${Math.max(0.6, (v.dur / (WINDOW_BEFORE + WINDOW_AFTER)) * 100)}%` }}
            />
          ))}
        </Lane>
        <div className="playhead" style={{ left: `calc(var(--lane-label) + (100% - var(--lane-label)) * ${WINDOW_BEFORE / (WINDOW_BEFORE + WINDOW_AFTER)})` }}>
          <span>{fmt(now)}</span>
        </div>
      </div>
    </section>
  );
}

export function fmt(t: number) {
  const s = Math.max(0, t);
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}
