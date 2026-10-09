import { useEffect, useMemo, useRef, useState } from "react";
import { RelayStrip } from "./components/RelayStrip";
import type { AppConfig, LibraryItem, Source } from "./lib/protocol";
import { SessionController, type Snapshot } from "./lib/session";

type Tab = "library" | "link" | "file" | "mic";

const SPORT_LABEL: Record<string, string> = {
  auto: "Detect from audio", cricket: "Cricket", football: "Football", kabaddi: "Kabaddi", tennis: "Tennis", hockey: "Hockey",
  badminton: "Badminton", basketball: "Basketball", f1: "Formula 1", other: "Other",
};

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [loadError, setLoadError] = useState("");
  const [tab, setTab] = useState<Tab>("library");
  const [source, setSource] = useState<Source | null>(null);
  const [lang, setLang] = useState("hi-IN");
  const [voice, setVoice] = useState("Orus");
  const [sport, setSport] = useState("auto");
  const [original, setOriginal] = useState(0.8);
  const [duck, setDuck] = useState(0.3); // share of the original level kept while the translator speaks
  const [translation, setTranslation] = useState(1);
  const [link, setLink] = useState("");
  const [linkBusy, setLinkBusy] = useState(false);
  const [linkError, setLinkError] = useState("");
  const [upBusy, setUpBusy] = useState(false);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const stage = useRef<HTMLDivElement>(null);
  const mixRef = useRef({ original, ducked: original * duck, translation });
  mixRef.current = { original, ducked: original * duck, translation };
  const ctl = useMemo(() => new SessionController(setSnap), []);

  useEffect(() => {
    fetch("/api/config")
      .then((r) => r.json())
      .then((c: AppConfig) => {
        setConfig(c);
        setVoice(c.default_voice);
      })
      .catch(() => setLoadError("The server isn't running. Start it with: uv run uvicorn commentary.server:app --app-dir backend --port 8000"));
    return () => void ctl.stop();
  }, [ctl]);

  const phase = snap?.phase ?? "idle";
  const running = phase !== "idle" && phase !== "ended" && phase !== "error";
  const langObj = config?.languages.find((l) => l.code === lang);

  async function start() {
    if (!source || !stage.current) return;
    await ctl.start({ source, lang, voice, sport, startAt: 0, stage: stage.current, mix: () => mixRef.current });
  }

  async function resolveLink(e: React.FormEvent) {
    e.preventDefault();
    setLinkBusy(true);
    setLinkError("");
    try {
      const r = await fetch("/api/youtube", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: link }) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "That link didn't work");
      setSource({ kind: "youtube", id: d.id, title: d.title, isLive: d.is_live });
    } catch (err) {
      setLinkError(err instanceof Error ? err.message : String(err));
    } finally {
      setLinkBusy(false);
    }
  }

  async function upload(file: File) {
    setUpBusy(true);
    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Upload failed");
      setSource({ kind: "file", mediaId: d.media_id, title: d.name, url: d.url, isVideo: d.is_video });
    } catch (err) {
      setLinkError(err instanceof Error ? err.message : String(err));
    } finally {
      setUpBusy(false);
    }
  }

  function pickLibrary(item: LibraryItem) {
    setSource({ kind: "youtube", id: item.id, title: item.title, isLive: false });
    if (item.sport) setSport(item.sport);
  }

  const sourceTitle = source ? (source.kind === "mic" ? "Your microphone" : source.title) : null;
  const stats = snap?.stats;
  const indic = config?.languages.filter((l) => l.group === "indic") ?? [];
  const global = config?.languages.filter((l) => l.group === "global") ?? [];

  return (
    <div className="app">
      <header className="masthead">
        <div className="brand">
          <span className={`onair ${phase === "onair" ? "is-live" : ""}`} aria-live="polite">{phase === "onair" ? "On air" : "Off air"}</span>
          <h1>Commentary Box</h1>
        </div>
        <p className="tagline">Any match, any commentary, called live in your language.</p>
      </header>

      <main className="desk">
        <section className="stage-col">
          <div className={`stage ${source?.kind === "mic" ? "is-mic" : ""}`}>
            <div ref={stage} className="programme" />
            {!running && phase !== "ended" && (
              <div className="stage-empty">
                {sourceTitle ? (
                  <>
                    <p className="eyebrow">Ready to call</p>
                    <p className="stage-title">{sourceTitle}</p>
                    <p className="stage-sub">in {langObj?.name} · voiced by {voice}</p>
                  </>
                ) : (
                  <p className="stage-title">Pick a match on the right, then choose who should hear it.</p>
                )}
              </div>
            )}
            {source?.kind === "mic" && running && (
              <div className="mic-viz" aria-hidden>
                <div className="mic-ring" style={{ transform: `scale(${1 + Math.min(1.5, (snap?.micLevel ?? 0) * 12)})` }} />
                <p>Commentate into your mic</p>
              </div>
            )}
            {(phase === "connecting" || phase === "buffering") && (
              <div className="stage-status" role="status">
                <span className="spinner" aria-hidden />
                {snap?.status}
              </div>
            )}
            {snap?.subtitle?.out && running && (
              <div className="subtitle" lang={lang}>
                <p className="sub-out">{snap.subtitle.out}</p>
                {snap.subtitle.src && <p className="sub-src">{snap.subtitle.src}</p>}
              </div>
            )}
          </div>

          <div className="scorebar">
            <Stat label={snap?.mode === "live" ? "Lag behind speaker" : "Voiced ahead"}
              value={snap?.mode === "live" ? (stats?.latency_s != null ? `${stats.latency_s.toFixed(1)} s` : "—")
                : stats?.ready_until != null && snap ? (stats.source_done ? "to the end" : `${Math.max(0, stats.ready_until - snap.now).toFixed(0)} s`) : "—"} />
            <Stat label="Time to voice" value={stats?.ttfa_ms ? `${(stats.ttfa_ms / 1000).toFixed(1)} s` : "—"} />
            <Stat label="Phrases" value={stats ? String(stats.phrases) : "—"} />
            <Stat label="Re-voiced" value={stats ? String(stats.retries) : "—"} />
            <Stat label="Original level" value={snap ? `${Math.round((snap.duckLevel ?? 0) * 100)}%` : "—"} />
          </div>

          {snap && <RelayStrip snap={snap} />}
          {phase === "error" && <p className="alert" role="alert">{snap?.status}</p>}
        </section>

        <aside className="controls">
          {loadError && <p className="alert" role="alert">{loadError}</p>}

          <fieldset className="panel" disabled={running}>
            <legend>Match</legend>
            <div className="tabs" role="tablist">
              {(["library", "link", "file", "mic"] as Tab[]).map((t) => (
                <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)} type="button">
                  {t === "library" ? "Library" : t === "link" ? "YouTube link" : t === "file" ? "Upload" : "Microphone"}
                </button>
              ))}
            </div>

            {tab === "library" && (
              <ul className="library">
                {config?.library.map((item) => (
                  <li key={item.id}>
                    <button type="button" className={source?.kind === "youtube" && source.id === item.id ? "picked" : ""} onClick={() => pickLibrary(item)}>
                      <img src={`https://i.ytimg.com/vi/${item.id}/mqdefault.jpg`} alt="" loading="lazy" />
                      <span className="lib-text">
                        <span className="lib-meta">{SPORT_LABEL[item.sport] ?? item.sport} · {item.source_lang} commentary</span>
                        <span className="lib-title">{item.title}</span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}

            {tab === "link" && (
              <form onSubmit={resolveLink} className="linkform">
                <label htmlFor="yt">YouTube video or live stream</label>
                <div className="row">
                  <input id="yt" value={link} onChange={(e) => setLink(e.target.value)} placeholder="https://www.youtube.com/watch?v=…" inputMode="url" />
                  <button type="submit" disabled={!link || linkBusy}>{linkBusy ? "Checking…" : "Use link"}</button>
                </div>
                {linkError && <p className="field-error">{linkError}</p>}
                {source?.kind === "youtube" && !config?.library.some((l) => l.id === source.id) && (
                  <p className="picked-note">{source.isLive ? "Live stream: " : ""}{source.title}</p>
                )}
              </form>
            )}

            {tab === "file" && (
              <div className="upload">
                <label className="drop">
                  <input type="file" accept="audio/*,video/*" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
                  <span>{upBusy ? "Uploading…" : "Choose a commentary recording (audio or video)"}</span>
                </label>
                {source?.kind === "file" && <p className="picked-note">{source.title}</p>}
              </div>
            )}

            {tab === "mic" && (
              <div className="micpanel">
                <p>Commentate in any language and hear yourself called in another, live.</p>
                <p className="hint">Use headphones so the translation doesn't feed back into your mic.</p>
                <button type="button" className={source?.kind === "mic" ? "picked" : ""} onClick={() => setSource({ kind: "mic" })}>
                  Use my microphone
                </button>
              </div>
            )}
          </fieldset>

          <fieldset className="panel" disabled={running}>
            <legend>Call it in</legend>
            <div className="langs" role="radiogroup" aria-label="Indian languages">
              {indic.map((l) => (
                <button key={l.code} type="button" role="radio" aria-checked={lang === l.code} className={`lang ${lang === l.code ? "on" : ""}`} onClick={() => setLang(l.code)}>
                  <span className="native" lang={l.code}>{l.native}</span>
                  <span className="en">{l.name}</span>
                </button>
              ))}
            </div>
            <details className="more-langs">
              <summary>More languages</summary>
              <div className="langs compact" role="radiogroup" aria-label="Other languages">
                {global.map((l) => (
                  <button key={l.code} type="button" role="radio" aria-checked={lang === l.code} className={`lang ${lang === l.code ? "on" : ""}`} onClick={() => setLang(l.code)}>
                    <span className="native" lang={l.code}>{l.native}</span>
                    <span className="en">{l.name}</span>
                  </button>
                ))}
              </div>
            </details>
            <div className="pair">
              <label>
                Commentator voice
                <select value={voice} onChange={(e) => setVoice(e.target.value)}>
                  {config?.voices.map((v) => (
                    <option key={v} value={v}>{v}{v === config.default_voice ? " (recommended)" : ""}</option>
                  ))}
                </select>
              </label>
              <label>
                Sport
                <select value={sport} onChange={(e) => setSport(e.target.value)}>
                  {config?.sports.map((s) => <option key={s} value={s}>{SPORT_LABEL[s] ?? s}</option>)}
                </select>
              </label>
            </div>
          </fieldset>

          <fieldset className="panel mix">
            <legend>Mix</legend>
            <Fader label="Original" value={original} onChange={setOriginal} />
            <Fader label="Original while translating" value={duck} onChange={setDuck} hint="share of the original kept under the translator" />
            <Fader label="Translation" value={translation} onChange={setTranslation} />
          </fieldset>

          <div className="actions">
            {running ? (
              <button type="button" className="stop" onClick={() => ctl.stop()}>Stop</button>
            ) : (
              <button type="button" className="go" disabled={!source || !config} onClick={start}>
                Start commentary
              </button>
            )}
          </div>
        </aside>
      </main>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}

function Fader({ label, value, onChange, hint }: { label: string; value: number; onChange: (v: number) => void; hint?: string }) {
  const id = label.replace(/\s+/g, "-").toLowerCase();
  return (
    <div className="fader">
      <label htmlFor={id}>{label}</label>
      <input id={id} type="range" min={0} max={1} step={0.01} value={value} onChange={(e) => onChange(Number(e.target.value))} aria-describedby={hint ? `${id}-hint` : undefined} />
      <output htmlFor={id}>{Math.round(value * 100)}%</output>
      {hint && <span id={`${id}-hint`} className="fader-hint">{hint}</span>}
    </div>
  );
}
