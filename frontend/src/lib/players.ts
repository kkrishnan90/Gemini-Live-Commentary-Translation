// The "programme": whatever the viewer watches/hears underneath the translation.

export interface Programme {
  time(): number;
  playing(): boolean;
  play(): void;
  pause(): void;
  seek(t: number): void;
  /** 0..1 */
  setVolume(v: number): void;
  destroy(): void;
}

// ---------- YouTube IFrame API ----------
type YTPlayer = {
  getCurrentTime(): number; getPlayerState(): number; playVideo(): void; pauseVideo(): void;
  seekTo(t: number, allow: boolean): void; setVolume(v: number): void; unMute(): void; destroy(): void;
  getDuration(): number; unloadModule(m: string): void;
};
declare global {
  interface Window { YT?: { Player: new (el: HTMLElement, opts: object) => YTPlayer }; onYouTubeIframeAPIReady?: () => void }
}

function hideCaptions(p: YTPlayer) {
  for (const m of ["captions", "cc"]) {
    try { p.unloadModule(m); } catch { /* module not loaded */ }
  }
}

let apiReady: Promise<void> | null = null;
function loadYouTubeApi(): Promise<void> {
  if (window.YT?.Player) return Promise.resolve();
  apiReady ??= new Promise((resolve) => {
    window.onYouTubeIframeAPIReady = () => resolve();
    const s = document.createElement("script");
    s.src = "https://www.youtube.com/iframe_api";
    document.head.appendChild(s);
  });
  return apiReady;
}

export async function createYouTube(el: HTMLElement, videoId: string, startAt: number, onState?: (s: number) => void): Promise<Programme> {
  await loadYouTubeApi();
  const host = document.createElement("div");
  el.replaceChildren(host);
  const player = await new Promise<YTPlayer>((resolve) => {
    const p: YTPlayer = new window.YT!.Player(host, {
      videoId,
      width: "100%",
      height: "100%",
      // cc_load_policy 0 + unloadModule: YouTube's own captions would collide with the translated subtitles
      playerVars: { start: Math.floor(startAt), playsinline: 1, rel: 0, modestbranding: 1, controls: 1, iv_load_policy: 3, cc_load_policy: 0 },
      events: {
        onReady: () => resolve(p),
        onStateChange: (e: { data: number }) => {
          if (e.data === 1) hideCaptions(p); // YouTube re-attaches captions when playback starts
          onState?.(e.data);
        },
        onApiChange: () => hideCaptions(p),
      },
    });
  });
  let lastVol = -1;
  return {
    time: () => player.getCurrentTime() || 0,
    playing: () => player.getPlayerState() === 1,
    play: () => { player.unMute(); player.playVideo(); },
    pause: () => player.pauseVideo(),
    seek: (t) => player.seekTo(t, true),
    setVolume: (v) => {
      const n = Math.round(Math.max(0, Math.min(1, v)) * 100);
      if (n !== lastVol) { player.setVolume(n); lastVol = n; }
    },
    destroy: () => { try { player.destroy(); } catch { /* gone */ } el.replaceChildren(); },
  };
}

// ---------- uploaded file ----------
export function createMedia(el: HTMLElement, url: string, isVideo: boolean): Programme {
  const m = document.createElement(isVideo ? "video" : "audio") as HTMLMediaElement;
  m.src = url;
  m.controls = true;
  m.preload = "auto";
  m.setAttribute("playsinline", "");
  m.className = "programme-media";
  el.replaceChildren(m);
  return {
    time: () => m.currentTime,
    playing: () => !m.paused && !m.ended && m.readyState >= 3,
    play: () => void m.play(),
    pause: () => m.pause(),
    seek: (t) => { m.currentTime = t; },
    setVolume: (v) => { m.volume = Math.max(0, Math.min(1, v)); },
    destroy: () => { m.pause(); m.removeAttribute("src"); el.replaceChildren(); },
  };
}

// ---------- microphone: there is no programme to play back, only a clock ----------
export function createMicClock(): Programme {
  const t0 = performance.now();
  return {
    time: () => (performance.now() - t0) / 1000,
    playing: () => true,
    play: () => {}, pause: () => {}, seek: () => {}, setVolume: () => {}, destroy: () => {},
  };
}
