"""Fetch library clips' audio from YouTube on a machine YouTube doesn't block, and store it in CACHE_BUCKET.
usage: CACHE_BUCKET=... python tools/seed_cache.py [video_id ...]   (defaults to the whole library)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from commentary.library import LIBRARY  # noqa: E402
from commentary.sources import YT_DIR, _download, _gcs_store  # noqa: E402

for vid in sys.argv[1:] or [item["id"] for item in LIBRARY]:
    hits = [p for p in YT_DIR.glob(f"{vid}.*") if p.suffix not in (".json", ".part")]
    path = hits[0] if hits and (YT_DIR / f"{vid}.json").exists() else _download(vid)  # _download also stores to GCS
    _gcs_store(path, YT_DIR / f"{vid}.json")
    print(f"{vid}: {path.name} ({path.stat().st_size // 1024} KiB) stored")
