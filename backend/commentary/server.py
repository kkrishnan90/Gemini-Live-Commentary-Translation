"""HTTP + WebSocket API. Run: uv run uvicorn commentary.server:app --app-dir backend --port 8000"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import DEFAULT_VOICE, LANGUAGES, MALE_VOICES, ROOT, SPORTS
from .library import LIBRARY
from .session import TranslationSession
from .sources import UPLOAD_DIR, YouTubeBlocked, cached_youtube_audio, probe_duration, youtube_audio, youtube_info

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("server")

app = FastAPI(title="Commentary Live")
DATA = Path(__file__).resolve().parent / "data"
MATRIX = DATA / "language_matrix.json"  # per-language verification results
VOICE_PICK = DATA / "voice_pick.json"  # default-voice decision and its evidence
ALLOWED_UPLOAD = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".webm", ".mp4", ".mov", ".mkv", ".flac"}


def _verified() -> dict:
    if not MATRIX.exists():
        return {}
    rows = json.loads(MATRIX.read_text())
    return {k: bool(v.get("judge", {}).get("is_target_language")) or k.startswith("en-") for k, v in rows.items() if "judge" in v}


@app.get("/api/config")
def config() -> dict:
    verified = _verified()
    pick = json.loads(VOICE_PICK.read_text()) if VOICE_PICK.exists() else {}
    return {
        "languages": [{"code": l.code, "name": l.name, "native": l.native, "group": l.group, "verified": verified.get(l.code)} for l in LANGUAGES],
        "voices": MALE_VOICES,
        "default_voice": pick.get("voice", DEFAULT_VOICE),
        "voice_pick": pick,
        "sports": SPORTS,
        "library": LIBRARY,
    }


class YouTubeReq(BaseModel):
    url: str


@app.post("/api/youtube")
async def youtube(req: YouTubeReq) -> dict:
    try:
        info = await youtube_info(req.url)
    except YouTubeBlocked as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(400, f"Could not read that YouTube link: {e}") from e
    if not info.is_live and cached_youtube_audio(info.id) is None:
        import asyncio

        asyncio.create_task(youtube_audio(info.id))  # warm the cache while the user picks a language
    return {"id": info.id, "title": info.title, "duration": info.duration, "is_live": info.is_live, "channel": info.channel}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_UPLOAD:
        raise HTTPException(400, f"Unsupported file type {ext}")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    media_id = uuid.uuid4().hex[:12]
    dest = UPLOAD_DIR / f"{media_id}{ext}"
    with dest.open("wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    return {"media_id": media_id, "name": file.filename, "duration": await probe_duration(dest), "url": f"/media/{media_id}",
            "is_video": ext in {".mp4", ".mov", ".mkv", ".webm"}}


@app.get("/media/{media_id}")
def media(media_id: str):
    if not media_id.isalnum():
        raise HTTPException(404)
    hits = list(UPLOAD_DIR.glob(f"{media_id}.*"))
    if not hits:
        raise HTTPException(404)
    return FileResponse(hits[0])


@app.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    await socket.accept()
    session = TranslationSession(socket.send_text, socket.send_bytes)
    try:
        while True:
            msg = await socket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                session.on_mic(msg["bytes"])
            elif msg.get("text"):
                await session.on_client(json.loads(msg["text"]))
    except WebSocketDisconnect:
        pass
    finally:
        await session.stop()


_dist = ROOT / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="app")
