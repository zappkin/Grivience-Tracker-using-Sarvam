import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

from sarvamai import SarvamAI

LANGS = {
    "hi-IN": "Hindi", "bn-IN": "Bengali", "kn-IN": "Kannada", "ml-IN": "Malayalam", "mr-IN": "Marathi",
    "od-IN": "Odia", "pa-IN": "Punjabi", "ta-IN": "Tamil", "te-IN": "Telugu", "en-IN": "English",
    "gu-IN": "Gujarati", "as-IN": "Assamese", "ur-IN": "Urdu", "ne-IN": "Nepali", "kok-IN": "Konkani",
    "ks-IN": "Kashmiri", "sd-IN": "Sindhi", "sa-IN": "Sanskrit", "sat-IN": "Santali", "mni-IN": "Manipuri",
    "brx-IN": "Bodo", "mai-IN": "Maithili", "doi-IN": "Dogri",
}
LLM_LANGS = {"hi-IN", "bn-IN", "ta-IN", "te-IN", "gu-IN", "en-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "od-IN"}
BULBUL_V4 = {"hi-IN", "en-IN", "bn-IN", "gu-IN", "kn-IN", "mr-IN", "pa-IN", "ta-IN", "te-IN"}
KEYTERMS = ["PM-KISAN", "UAN", "PPO", "EPFO", "CPGRAMS", "MGNREGA", "Aadhaar", "pension", "ration card"]
SPEAKER = os.getenv("SARVAM_TTS_SPEAKER", "ritu")

_client = None


def client() -> SarvamAI:
    global _client
    if _client is None:
        key = os.getenv("SARVAM_API_KEY")
        if not key:
            raise RuntimeError("SARVAM_API_KEY is not set")
        _client = SarvamAI(api_subscription_key=key)
    return _client


FAST_MODEL = os.getenv("SARVAM_FAST_MODEL", "sarvam-105b-conversations")
DRAFT_MODEL = os.getenv("SARVAM_DRAFT_MODEL", FAST_MODEL)


def llm_json(system: str, user: str, name: str, schema: dict, max_tokens: int = 3000, model: str | None = None) -> dict:
    model = model or FAST_MODEL
    extra = {"reasoning_effort": "low"} if model == "sarvam-105b" else {}
    last = None
    for _ in range(2):
        r = client().chat.completions(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
            max_tokens=max_tokens, temperature=0.2, **extra,
        )
        txt = (r.choices[0].message.content or "").strip()
        txt = re.sub(r"^```(?:json)?|```$", "", txt).strip()
        try:
            return json.loads(txt)
        except json.JSONDecodeError as e:
            last = e
    raise ValueError(f"LLM returned invalid JSON: {last}")


def transcribe(data: bytes, filename: str, mime: str) -> dict:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    codec = {"webm": "webm", "ogg": "ogg", "opus": "opus", "wav": "wav", "mp3": "mp3", "m4a": "x-m4a",
             "mp4": "mp4", "aac": "aac", "flac": "flac", "amr": "amr"}.get(ext)
    kw = dict(file=(filename, data, mime), model="saaras:v4", mode="codemix", language_code="unknown",
              with_timestamps=True, keyterms=KEYTERMS)
    if codec:
        kw["input_audio_codec"] = codec
    r = client().speech_to_text.transcribe(**kw)
    ts = r.timestamps
    start = end = None
    try:
        starts = getattr(ts, "start_time_seconds", None) or []
        ends = getattr(ts, "end_time_seconds", None) or []
        if starts and ends:
            start, end = int(starts[0] * 1000), int(ends[-1] * 1000)
    except Exception:
        pass
    return {"text": r.transcript or "", "lang": r.language_code or "unknown", "start_ms": start, "end_ms": end}


def detect_lang(text: str) -> str:
    try:
        return client().text.identify_language(input=text[:1000]).language_code or "en-IN"
    except Exception:
        return "en-IN"


def translate(text: str, src: str, tgt: str) -> str:
    if not text.strip() or src == tgt:
        return text
    chunks, cur = [], ""
    for part in re.split(r"(?<=[.?!।])\s+", text):
        if len(cur) + len(part) > 900 and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur} {part}".strip()
    if cur:
        chunks.append(cur)
    out = []
    for c in chunks:
        r = client().text.translate(input=c, source_language_code=src, target_language_code=tgt,
                                    model="sarvam-translate:v1", mode="formal")
        out.append(r.translated_text)
    return " ".join(out)


def translate_many(lines: list[str], src: str, tgt: str) -> list[str]:
    if src == tgt:
        return lines
    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(lambda t: translate(t, src, tgt), lines))


def tts(text: str, lang: str) -> str | None:
    """Base64 WAV, or None if Bulbul cannot speak this language."""
    if lang not in LLM_LANGS or not text.strip():
        return None
    try:
        model = "bulbul:v3"
        r = client().text_to_speech.convert(text=text[:2400], language_code=lang, model=model, speaker=SPEAKER)
        return r.audios[0]
    except Exception:
        return None
