"""Runtime settings, model ids, languages and voices."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
# override=True: the shell exports GOOGLE_CLOUD_LOCATION=global, where gemini-3.8-live does not exist.
load_dotenv(ROOT / ".env", override=True)

INTERPRETER_MODEL = "gemini-3.8-live"

IN_RATE = 16000  # Live API input PCM rate
OUT_RATE = 24000  # Live API output PCM rate


@dataclass(frozen=True)
class Settings:
    project: str | None = os.environ.get("GOOGLE_CLOUD_PROJECT") or None
    location: str = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    cache_dir: Path = ROOT / "cache"
    pool_size: int = int(os.environ.get("RELAY_POOL_SIZE", "4"))
    # Shared source-audio cache. Cloud Run's IPs are bot-challenged by YouTube, so clips fetched
    # elsewhere are kept here and the server never has to contact YouTube for them.
    cache_bucket: str | None = os.environ.get("CACHE_BUCKET") or None


SETTINGS = Settings()


@dataclass(frozen=True)
class Language:
    code: str  # BCP-47 passed to speech_config.language_code
    name: str
    native: str
    group: str  # "indic" | "global"


LANGUAGES: list[Language] = [
    Language("hi-IN", "Hindi", "हिन्दी", "indic"),
    Language("bn-IN", "Bengali", "বাংলা", "indic"),
    Language("mr-IN", "Marathi", "मराठी", "indic"),
    Language("gu-IN", "Gujarati", "ગુજરાતી", "indic"),
    Language("pa-IN", "Punjabi", "ਪੰਜਾਬੀ", "indic"),
    Language("ta-IN", "Tamil", "தமிழ்", "indic"),
    Language("te-IN", "Telugu", "తెలుగు", "indic"),
    Language("kn-IN", "Kannada", "ಕನ್ನಡ", "indic"),
    Language("ml-IN", "Malayalam", "മലയാളം", "indic"),
    Language("or-IN", "Odia", "ଓଡ଼ିଆ", "indic"),
    Language("as-IN", "Assamese", "অসমীয়া", "indic"),
    Language("ur-IN", "Urdu", "اردو", "indic"),
    Language("en-IN", "English (India)", "English", "indic"),
    Language("en-US", "English (US)", "English", "global"),
    Language("en-GB", "English (UK)", "English", "global"),
    Language("es-ES", "Spanish", "Español", "global"),
    Language("es-US", "Spanish (Latin America)", "Español", "global"),
    Language("fr-FR", "French", "Français", "global"),
    Language("de-DE", "German", "Deutsch", "global"),
    Language("it-IT", "Italian", "Italiano", "global"),
    Language("pt-BR", "Portuguese (Brazil)", "Português", "global"),
    Language("ar-XA", "Arabic", "العربية", "global"),
    Language("ja-JP", "Japanese", "日本語", "global"),
    Language("ko-KR", "Korean", "한국어", "global"),
    Language("cmn-CN", "Mandarin Chinese", "中文", "global"),
    Language("id-ID", "Indonesian", "Bahasa Indonesia", "global"),
    Language("th-TH", "Thai", "ไทย", "global"),
    Language("vi-VN", "Vietnamese", "Tiếng Việt", "global"),
    Language("tr-TR", "Turkish", "Türkçe", "global"),
    Language("ru-RU", "Russian", "Русский", "global"),
    Language("nl-NL", "Dutch", "Nederlands", "global"),
    Language("pl-PL", "Polish", "Polski", "global"),
]
LANG_BY_CODE = {lang.code: lang for lang in LANGUAGES}

# The 16 male voices among the 30 core prebuilt voices (gender from client.voices.list()).
# Only prebuilt_voice_config.voice_name is honored by gemini-3.8-live; VoiceConfig.voice is silently ignored.
MALE_VOICES = [
    "Achird", "Algenib", "Algieba", "Alnilam", "Charon", "Enceladus", "Fenrir", "Iapetus",
    "Orus", "Puck", "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Umbriel", "Zubenelgenubi",
]
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "Orus")  # winner of the voice study, see data/voice_pick.json

SPORTS = ["auto", "cricket", "football", "kabaddi", "tennis", "hockey", "badminton", "basketball", "f1", "other"]
