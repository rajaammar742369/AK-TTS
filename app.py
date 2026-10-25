"""VoxForge - a small, self-hosted text-to-speech web app.

Backend: Flask + edge-tts (Microsoft Edge neural voices).
Features: English + Urdu voices, speed/pitch/volume, pauses, two-speaker dialogue,
unlimited text length (long text runs as a background job with progress), voice
cloning from a short sample, and a "My voices" library of saved cloned voices.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from collections import defaultdict, deque
from functools import lru_cache

import edge_tts
from flask import Flask, jsonify, render_template, request, send_file

import cloning
from ffmpeg_util import FFMPEG

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
VOICES_DIR = os.path.join(cloning.DATA_DIR, "saved_voices")  # survives new/updated project folders
LEGACY_VOICES_DIR = os.path.join(BASE_DIR, "saved_voices")  # older versions kept them here

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024  # long scripts + reference audio
log = logging.getLogger("voxforge")

# 0 = unlimited text length. Set MAX_CHARS=50000 etc. to cap it again.
MAX_CHARS = int(os.getenv("MAX_CHARS", "0"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "30"))
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "1500"))          # text per edge-tts request
CLONE_CHUNK_CHARS = int(os.getenv("CLONE_CHUNK_CHARS", "500"))  # smaller = less memory
CONCURRENCY = int(os.getenv("CONCURRENCY", "3"))             # parallel edge-tts requests
MAX_RUNNING_JOBS = int(os.getenv("MAX_RUNNING_JOBS", "2"))
JOB_TTL = int(os.getenv("JOB_TTL_HOURS", "3")) * 3600
VOICE_CACHE_TTL = 6 * 60 * 60  # seconds
DEFAULT_TAU = 0.5  # voice-similarity strength used when none is given
# Only these languages are offered. Change via env, e.g. ALLOWED_LANGS="en,ur,hi"
ALLOWED_LANGS = {
    x.strip().lower() for x in os.getenv("ALLOWED_LANGS", "en,ur").split(",") if x.strip()
}

PAUSE_RE = re.compile(r"\[pause:(\d+(?:\.\d+)?)\]")
DIALOGUE_RE = re.compile(r"^\s*([AB])\s*:\s*(.+)$", re.IGNORECASE)
SENTENCE_RE = re.compile(r"(?<=[.!?؟۔…])\s+|\n+")
ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
LETTER_RE = re.compile(r"[^\W\d_]")
VID_RE = re.compile(r"^[0-9a-f]{10}$")
HAS_FFMPEG = FFMPEG is not None

_voice_cache = {"time": 0.0, "voices": []}
_voice_lock = threading.Lock()
_hits = defaultdict(deque)
_jobs = {}
_jobs_lock = threading.Lock()
_slots = threading.Semaphore(MAX_RUNNING_JOBS)
_store_lock = threading.Lock()
_f0_cache = {}

os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(VOICES_DIR, exist_ok=True)


# ----------------------------------------------------------------- helpers
def run_async(coro):
    """Run a coroutine from sync code."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def get_voices():
    with _voice_lock:
        if _voice_cache["voices"] and time.time() - _voice_cache["time"] < VOICE_CACHE_TTL:
            return _voice_cache["voices"]
    raw = run_async(edge_tts.list_voices())
    voices = []
    for v in raw:
        locale = v.get("Locale", "")
        if locale.split("-")[0].lower() not in ALLOWED_LANGS:
            continue
        voices.append(
            {
                "id": v["ShortName"],
                "name": v.get("FriendlyName", v["ShortName"]).replace("Microsoft ", ""),
                "locale": locale,
                "lang": locale.split("-")[0],
                "country": locale.split("-")[-1] if "-" in locale else "",
                "gender": v.get("Gender", ""),
            }
        )
    voices.sort(key=lambda x: (x["locale"], x["name"]))
    with _voice_lock:
        _voice_cache.update(time=time.time(), voices=voices)
    return voices


def rate_limited(ip, bucket="job", limit=None):
    limit = limit or RATE_LIMIT_PER_MIN
    now = time.time()
    q = _hits[(ip, bucket)]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


def clamp(value, low, high, default=0):
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def clamp_float(value, low, high, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def truthy(value, default=False):
    if value is None or value == "":
        return default
    return str(value).lower() in ("1", "true", "yes", "on")


async def _synth(text, voice, rate, pitch, volume):
    comm = edge_tts.Communicate(
        text, voice, rate=f"{rate:+d}%", pitch=f"{pitch:+d}Hz", volume=f"{volume:+d}%"
    )
    out = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            out.extend(chunk["data"])
    if not out:
        raise RuntimeError("No audio was returned for this text/voice.")
    return bytes(out)


@lru_cache(maxsize=64)
def silence(seconds):
    """MP3 silence (24 kHz mono) so it can be appended to edge-tts output."""
    result = subprocess.run(
        [
            FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i",
            "anullsrc=r=24000:cl=mono", "-t", str(seconds), "-b:a", "48k",
            "-f", "mp3", "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return result.stdout


# ---------------------------------------------------- language / base voice
def detect_lang(text):
    """'ur' if the text is mostly Arabic-script (Urdu), otherwise 'en'."""
    letters = len(LETTER_RE.findall(text)) or 1
    return "ur" if len(ARABIC_RE.findall(text)) / letters > 0.3 else "en"


PREFERRED_BASE = {
    ("en", "Male"): ["en-US-GuyNeural", "en-US-ChristopherNeural", "en-US-EricNeural", "en-GB-RyanNeural"],
    ("en", "Female"): ["en-US-AriaNeural", "en-US-JennyNeural", "en-GB-SoniaNeural"],
    ("ur", "Male"): ["ur-PK-AsadNeural", "ur-IN-SalmanNeural"],
    ("ur", "Female"): ["ur-PK-UzmaNeural", "ur-IN-GulNeural"],
}


def pick_auto_base(gender, lang):
    """Choose a base Edge voice with the sample's gender and the text's language."""
    voices = get_voices()
    ids = {v["id"] for v in voices}
    gender = gender if gender in ("Male", "Female") else "Male"
    for vid in PREFERRED_BASE.get((lang, gender), []):
        if vid in ids:
            return vid
    for v in voices:
        if v["lang"] == lang and v["gender"] == gender:
            return v["id"]
    for v in voices:
        if v["lang"] == lang:
            return v["id"]
    return voices[0]["id"]


PROBE_EN = "The quick brown fox jumps over the lazy dog, and then rests under a tall green tree."
PROBE_UR = "آج موسم بہت اچھا ہے اور ہم سب باہر باغ میں چائے پینے جا رہے ہیں۔"


def base_f0(voice):
    """Typical pitch (Hz) of an Edge voice, measured once and cached."""
    if voice in _f0_cache:
        return _f0_cache[voice]
    f0 = None
    try:
        mp3 = run_async(_synth(PROBE_UR if voice.startswith("ur") else PROBE_EN, voice, 0, 0, 0))
        f0 = cloning.estimate_f0(cloning.decode_audio(mp3, cloning.REF_SR), cloning.REF_SR)
    except Exception:
        log.warning("Could not measure pitch of %s:\n%s", voice, traceback.format_exc())
    if f0:
        _f0_cache[voice] = f0
    return f0


# ---------------------------------------------------------- text splitting
def _hard_split(sentence, limit):
    """Split one over-long sentence at commas, then at spaces."""
    parts = []
    for piece in re.split(r"(?<=[,;:،])\s+", sentence):
        if len(piece) <= limit:
            parts.append(piece)
            continue
        cur = ""
        for word in piece.split():
            if cur and len(cur) + 1 + len(word) > limit:
                parts.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}" if cur else word
        if cur:
            parts.append(cur)
    return parts


def split_text(text, limit):
    """Pack sentences into chunks of at most `limit` characters."""
    pieces = []
    for s in (x.strip() for x in SENTENCE_RE.split(text) if x and x.strip()):
        pieces.extend([s] if len(s) <= limit else _hard_split(s, limit))
    chunks, cur = [], ""
    for p in pieces:
        if cur and len(cur) + 1 + len(p) > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def segments_for(text, voice, limit):
    """Turn text (may contain [pause:N]) into ("text", chunk, voice_key) / ("pause", s) items."""
    if not HAS_FFMPEG:
        text = PAUSE_RE.sub(" , ", text)  # fallback: punctuation pause
    segs, pos = [], 0

    def add_text(part):
        for chunk in split_text(part, limit):
            if re.search(r"\w", chunk):  # skip chunks with nothing to say
                segs.append(("text", chunk, voice))

    for m in PAUSE_RE.finditer(text):
        add_text(text[pos:m.start()])
        segs.append(("pause", min(float(m.group(1)), 10.0)))
        pos = m.end()
    add_text(text[pos:])
    return segs


def parse_dialogue(text):
    lines = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = DIALOGUE_RE.match(raw)
        if m:
            lines.append((m.group(1).upper(), m.group(2).strip()))
        elif lines:  # continuation of previous line
            lines[-1] = (lines[-1][0], lines[-1][1] + " " + raw.strip())
        else:
            raise ValueError("Every line must start with A: or B: in two-speaker mode.")
    return lines


def dialogue_segments(text, voices, limit):
    segs = []
    for speaker, line in parse_dialogue(text):
        segs.extend(segments_for(line, voices[speaker], limit))
        if HAS_FFMPEG:
            segs.append(("pause", 0.4))
    return segs


# --------------------------------------------------------- saved voices (store)
def _vdir(vid):
    return os.path.join(VOICES_DIR, vid) if VID_RE.match(vid or "") else None


def sample_path(vid):
    d = _vdir(vid)
    return os.path.join(d, "sample.wav") if d else None


def load_profile(vid):
    d = _vdir(vid)
    if not d:
        return None
    try:
        with open(os.path.join(d, "meta.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_profile(meta):
    d = _vdir(meta["id"])
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "meta.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(d, "meta.json"))


def list_profiles():
    out = []
    for name in os.listdir(VOICES_DIR):
        meta = load_profile(name)
        if meta and os.path.exists(sample_path(name)):
            out.append(meta)
    out.sort(key=lambda m: m.get("created", 0))
    return out


def public_profile(m):
    return {k: m.get(k) for k in
            ("id", "name", "base", "tau", "match_pitch", "rate", "pitch", "volume",
             "gender", "f0", "seconds", "created")}


def clean_name(value, default="My voice"):
    name = re.sub(r"\s+", " ", str(value or "")).strip()[:60]
    return name or default


def apply_settings(meta, src):
    """Copy the optional style settings from a dict-like (form/json) into `meta`."""
    if "base" in src:
        base = (src.get("base") or "auto").strip()
        if base != "auto":
            if base not in {v["id"] for v in get_voices()}:
                raise ValueError("Please choose a valid base voice (or Auto).")
        meta["base"] = base
    if "tau" in src:
        meta["tau"] = round(clamp_float(src.get("tau"), 0.1, 1.0, DEFAULT_TAU), 2)
    if "match_pitch" in src:
        meta["match_pitch"] = truthy(src.get("match_pitch"), True)
    if "rate" in src:
        meta["rate"] = clamp(src.get("rate"), -50, 100)
    if "pitch" in src:
        meta["pitch"] = clamp(src.get("pitch"), -50, 50)
    if "volume" in src:
        meta["volume"] = clamp(src.get("volume"), -50, 50)


def target_from_profile(meta):
    return {
        "ref_path": sample_path(meta["id"]), "tau": meta.get("tau", DEFAULT_TAU),
        "match_pitch": meta.get("match_pitch", True), "f0": meta.get("f0"),
        "gender": meta.get("gender"), "base": meta.get("base", "auto"),
        "rate": meta.get("rate", 0), "pitch": meta.get("pitch", 0), "volume": meta.get("volume", 0),
    }


def store_sample(upload, dst_wav):
    """Save an uploaded reference, clean it and measure it. Raises CloneUnavailable."""
    tmp = dst_wav + ".upload"
    os.makedirs(os.path.dirname(dst_wav), exist_ok=True)
    upload.save(tmp)
    try:
        return cloning.save_reference(tmp, dst_wav)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def sample_warning(info):
    if info["seconds"] < 10:
        return f"The sample is only {info['seconds']:.0f}s long. 20-60 seconds of clean speech works best."
    if not info.get("f0"):
        return "Could not measure the pitch of this sample (is it very noisy?). The voice will still be used."
    return ""


# --------------------------------------------------------------------- jobs
class JobCancelled(Exception):
    pass


class Job:
    def __init__(self, kind):
        self.id = uuid.uuid4().hex[:16]
        self.kind = kind
        self.status = "queued"  # queued | running | done | error | cancelled
        self.done = 0
        self.total = 1
        self.message = "Waiting in queue…"
        self.error = None
        self.cancelled = False
        self.created = time.time()
        self.finished = None
        self.dir = os.path.join(JOBS_DIR, self.id)
        self.path = os.path.join(JOBS_DIR, self.id + ".mp3")
        os.makedirs(self.dir, exist_ok=True)

    def tick(self, n=1):
        self.done += n
        if self.cancelled:
            raise JobCancelled()

    def public(self):
        return {
            "id": self.id, "status": self.status, "done": self.done, "total": self.total,
            "message": self.message, "error": self.error,
        }


def start_job(job, work):
    with _jobs_lock:
        _jobs[job.id] = job

    def runner():
        with _slots:
            if job.cancelled:
                job.status = "cancelled"
            else:
                job.status, job.message = "running", "Starting…"
                try:
                    work(job)
                    job.status, job.message = "done", "Done"
                except JobCancelled:
                    job.status, job.message = "cancelled", "Cancelled"
                except (ValueError, cloning.CloneUnavailable) as exc:
                    job.status, job.error = "error", str(exc)
                except Exception as exc:  # network issues etc.
                    log.error("Job %s failed:\n%s", job.id, traceback.format_exc())
                    job.status, job.error = "error", f"Speech generation failed: {exc}"
            job.finished = time.time()
            shutil.rmtree(job.dir, ignore_errors=True)

    threading.Thread(target=runner, daemon=True).start()


def cleanup_loop():
    while True:
        time.sleep(600)
        now = time.time()
        with _jobs_lock:
            old = [j for j in _jobs.values() if j.finished and now - j.finished > JOB_TTL]
            for j in old:
                _jobs.pop(j.id, None)
        for j in old:
            for p in (j.path,):
                if os.path.exists(p):
                    os.remove(p)


def _wipe_old_files():
    for name in os.listdir(JOBS_DIR):
        p = os.path.join(JOBS_DIR, name)
        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)


def plan_segments(job, segments, fx, targets):
    """Decide, per text segment: which Edge voice speaks it, which target (if any) it is
    converted to, and the rate/pitch/volume to use.  -> {index: (voice, target_key, rate, pitch, vol)}"""
    plan = {}
    for i, seg in enumerate(segments):
        if seg[0] != "text":
            continue
        if job.cancelled:
            raise JobCancelled()
        key, t = seg[2], targets.get(seg[2])
        if t is None:
            plan[i] = (key, None) + tuple(fx)
            continue
        if "info" not in t:  # temporary sample: analyse it once
            job.message = "Analysing the voice sample…"
            t["info"] = cloning.reference_info(t["ref_path"])
        f0 = t.get("f0") or t["info"]["f0"]
        gender = t.get("gender") or t["info"]["gender"]
        base = t["base"] if t["base"] != "auto" else pick_auto_base(gender, detect_lang(seg[1]))
        rate = max(-50, min(100, fx[0] + t["rate"]))
        pitch = fx[1] + t["pitch"]
        volume = max(-50, min(50, fx[2] + t["volume"]))
        if t["match_pitch"] and f0:
            job.message = "Matching pitch to the sample…"
            bf0 = base_f0(base)
            if bf0:
                pitch += round(f0 - bf0)
        plan[i] = (base, key, rate, max(-50, min(50, pitch)), volume)
    return plan


async def synth_all(segments, plan, job):
    """Synthesize every text segment (a few at a time). Returns {index: mp3 bytes}."""
    sem = asyncio.Semaphore(max(1, CONCURRENCY))
    results = {}

    async def one(i, seg):
        voice, _target, rate, pitch, volume = plan[i]
        async with sem:
            if job.cancelled:
                raise JobCancelled()
            last = None
            for attempt in range(4):
                try:
                    results[i] = await _synth(seg[1], voice, rate, pitch, volume)
                    break
                except Exception as exc:  # transient network / service errors
                    last = exc
                    await asyncio.sleep(1.5 * (attempt + 1))
            else:
                raise last
            job.tick()

    tasks = [asyncio.ensure_future(one(i, s)) for i, s in enumerate(segments) if s[0] == "text"]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return results


def render_job(job, segments, fx, targets=None):
    """Worker: synthesize (and convert to cloned voices where needed), write job.path."""
    targets = targets or {}
    n_text = sum(1 for s in segments if s[0] == "text")
    if not n_text:
        raise ValueError("Nothing to speak.")
    uses_clone = any(s[0] == "text" and s[2] in targets for s in segments)
    job.total = n_text * (2 if uses_clone else 1)
    job.done = 0
    job.message = "Preparing…"

    plan = plan_segments(job, segments, fx, targets)
    job.message = f"Generating speech (0/{n_text})…"

    def watch():  # progress text while synthesizing
        while job.status == "running" and job.done < n_text:
            job.message = f"Generating speech ({min(job.done, n_text)}/{n_text})…"
            time.sleep(0.5)

    threading.Thread(target=watch, daemon=True).start()
    audio = run_async(synth_all(segments, plan, job))

    if not uses_clone:
        with open(job.path, "wb") as out:
            for i, seg in enumerate(segments):
                out.write(audio[i] if seg[0] == "text" else silence(round(seg[1], 1)))
        return

    plan_segs = [
        ("text", seg[1], plan[i][0], plan[i][1]) if seg[0] == "text" else seg
        for i, seg in enumerate(segments)
    ]

    def set_message(m):
        job.message = m

    cloning.render_clone(plan_segs, audio, targets, job.path, job.tick, set_message)


# ------------------------------------------------------------------ routes
def _client_ip():
    return request.remote_addr or "unknown"


def _validate_text(text):
    if not text:
        return "Please enter some text."
    if MAX_CHARS and len(text) > MAX_CHARS:
        return f"Text is too long (max {MAX_CHARS} characters)."
    return None


@app.route("/")
def index():
    return render_template("index.html", max_chars=MAX_CHARS, has_ffmpeg=HAS_FFMPEG)


@app.route("/api/voices")
def api_voices():
    try:
        return jsonify(get_voices())
    except Exception as exc:  # network issues etc.
        return jsonify({"error": f"Could not load voices: {exc}"}), 502


@app.route("/api/preview", methods=["POST"])
def api_preview():
    """Short synchronous sample so voice previews feel instant."""
    if rate_limited(_client_ip(), "preview", 60):
        return jsonify({"error": "Too many requests. Please wait a minute."}), 429
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()[:300]
    voice = data.get("voice")
    try:
        if voice not in {v["id"] for v in get_voices()} or not text:
            return jsonify({"error": "Please choose a valid voice."}), 400
        audio = run_async(_synth(text, voice, 0, 0, 0))
    except Exception as exc:
        return jsonify({"error": f"Preview failed: {exc}"}), 502
    return app.response_class(audio, mimetype="audio/mpeg")


@app.route("/api/tts", methods=["POST"])
def api_tts():
    """Start a speech job. Returns {job_id}; poll /api/jobs/<id>.

    A voice may be an Edge voice id or "my:<id>" (a saved cloned voice)."""
    if rate_limited(_client_ip()):
        return jsonify({"error": "Too many requests. Please wait a minute."}), 429
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    err = _validate_text(text)
    if err:
        return jsonify({"error": err}), 400
    fx = (clamp(data.get("rate"), -50, 100), clamp(data.get("pitch"), -50, 50),
          clamp(data.get("volume"), -50, 50))

    targets = {}

    def check(key):
        """Return None if `key` is usable, else an error message."""
        if isinstance(key, str) and key.startswith("my:"):
            meta = load_profile(key[3:])
            if not meta or not os.path.exists(sample_path(meta["id"])):
                return "That saved voice no longer exists. Please pick another."
            ok, reason = cloning.availability()
            if not ok:
                return reason
            targets[key] = target_from_profile(meta)
            return None
        return None if key in valid else "Please choose a valid voice."

    try:
        valid = {v["id"] for v in get_voices()}
        dialogue = bool(data.get("dialogue"))
        keys = ({"A": data.get("voice_a"), "B": data.get("voice_b")} if dialogue
                else {"-": data.get("voice")})
        for k in keys.values():
            msg = check(k)
            if msg:
                return jsonify({"error": msg}), 400
        limit = CLONE_CHUNK_CHARS if targets else CHUNK_CHARS  # smaller chunks when cloning
        segments = (dialogue_segments(text, keys, limit) if dialogue
                    else segments_for(text, keys["-"], limit))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Could not load voices: {exc}"}), 502
    if not any(s[0] == "text" for s in segments):
        return jsonify({"error": "There is nothing to speak in this text."}), 400

    job = Job("tts")
    start_job(job, lambda j: render_job(j, segments, fx, targets))
    return jsonify({"job_id": job.id}), 202


@app.route("/api/clone/status")
def api_clone_status():
    ok, reason = cloning.availability()
    return jsonify({"available": ok, "reason": reason, "model_ready": cloning.model_ready()})


@app.route("/api/clone", methods=["POST"])
def api_clone():
    """One-off cloning from an uploaded sample (multipart form)."""
    if rate_limited(_client_ip()):
        return jsonify({"error": "Too many requests. Please wait a minute."}), 429
    ok, reason = cloning.availability()
    if not ok:
        return jsonify({"error": reason}), 400
    form = request.form
    if form.get("consent") != "true":
        return jsonify({"error": "Please confirm you have the right to clone this voice."}), 400
    ref = request.files.get("reference")
    if ref is None or not ref.filename:
        return jsonify({"error": "Please upload or record a voice sample first."}), 400
    text = (form.get("text") or "").strip()
    err = _validate_text(text)
    if err:
        return jsonify({"error": err}), 400
    fx = (clamp(form.get("rate"), -50, 100), clamp(form.get("pitch"), -50, 50),
          clamp(form.get("volume"), -50, 50))
    settings = {"base": form.get("base") or "auto", "tau": form.get("tau") or DEFAULT_TAU,
                "match_pitch": form.get("match_pitch") or "true"}
    meta = {}
    try:
        apply_settings(meta, settings)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Could not load voices: {exc}"}), 502
    segments = segments_for(text, "tmp", CLONE_CHUNK_CHARS)
    if not any(s[0] == "text" for s in segments):
        return jsonify({"error": "There is nothing to speak in this text."}), 400

    job = Job("clone")
    ref_path = os.path.join(job.dir, "reference")
    ref.save(ref_path)
    target = {"ref_path": ref_path, "tau": meta["tau"], "match_pitch": meta["match_pitch"],
              "base": meta["base"], "rate": 0, "pitch": 0, "volume": 0}
    start_job(job, lambda j: render_job(j, segments, fx, {"tmp": target}))
    return jsonify({"job_id": job.id}), 202


# ---- saved voices ("My voices")
def _profile_or_404(vid):
    meta = load_profile(vid)
    if not meta:
        return None, (jsonify({"error": "Voice not found."}), 404)
    return meta, None


@app.route("/api/myvoices")
def api_myvoices():
    with _store_lock:
        return jsonify([public_profile(m) for m in list_profiles()])


@app.route("/api/myvoices", methods=["POST"])
def api_myvoices_create():
    ok, reason = cloning.availability()
    if not ok:
        return jsonify({"error": reason}), 400
    form = request.form
    if form.get("consent") != "true":
        return jsonify({"error": "Please confirm you have the right to clone this voice."}), 400
    ref = request.files.get("reference")
    if ref is None or not ref.filename:
        return jsonify({"error": "Please upload or record a voice sample first."}), 400
    meta = {"id": uuid.uuid4().hex[:10], "name": clean_name(form.get("name")), "created": int(time.time()),
            "base": "auto", "tau": DEFAULT_TAU, "match_pitch": True, "rate": 0, "pitch": 0, "volume": 0}
    try:
        apply_settings(meta, form)
        with _store_lock:
            info = store_sample(ref, sample_path(meta["id"]))
            meta.update(f0=info["f0"], gender=info["gender"], seconds=info["seconds"])
            write_profile(meta)
    except (ValueError, cloning.CloneUnavailable) as exc:
        shutil.rmtree(_vdir(meta["id"]), ignore_errors=True)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        shutil.rmtree(_vdir(meta["id"]), ignore_errors=True)
        log.error("Saving voice failed:\n%s", traceback.format_exc())
        return jsonify({"error": f"Could not save the voice: {exc}"}), 500
    return jsonify({"voice": public_profile(meta), "warning": sample_warning(info)}), 201


@app.route("/api/myvoices/<vid>", methods=["PATCH"])
def api_myvoices_update(vid):
    data = request.get_json(silent=True) or {}
    with _store_lock:
        meta, bad = _profile_or_404(vid)
        if bad:
            return bad
        try:
            if "name" in data:
                meta["name"] = clean_name(data.get("name"), meta["name"])
            apply_settings(meta, data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": f"Could not load voices: {exc}"}), 502
        write_profile(meta)
    return jsonify({"voice": public_profile(meta)})


@app.route("/api/myvoices/<vid>/sample", methods=["POST"])
def api_myvoices_replace_sample(vid):
    ok, reason = cloning.availability()
    if not ok:
        return jsonify({"error": reason}), 400
    if request.form.get("consent") != "true":
        return jsonify({"error": "Please confirm you have the right to clone this voice."}), 400
    ref = request.files.get("reference")
    if ref is None or not ref.filename:
        return jsonify({"error": "Please choose a new voice sample."}), 400
    with _store_lock:
        meta, bad = _profile_or_404(vid)
        if bad:
            return bad
        tmp_wav = sample_path(vid) + ".new.wav"
        try:
            info = store_sample(ref, tmp_wav)
            os.replace(tmp_wav, sample_path(vid))
        except (ValueError, cloning.CloneUnavailable) as exc:
            if os.path.exists(tmp_wav):
                os.remove(tmp_wav)
            return jsonify({"error": str(exc)}), 400
        meta.update(f0=info["f0"], gender=info["gender"], seconds=info["seconds"])
        write_profile(meta)
    return jsonify({"voice": public_profile(meta), "warning": sample_warning(info)})


@app.route("/api/myvoices/<vid>/sample")
def api_myvoices_sample(vid):
    p = sample_path(vid)
    if not p or not os.path.exists(p):
        return jsonify({"error": "Voice not found."}), 404
    return send_file(p, mimetype="audio/wav", conditional=True)


@app.route("/api/myvoices/<vid>", methods=["DELETE"])
def api_myvoices_delete(vid):
    d = _vdir(vid)
    if not d:
        return jsonify({"error": "Voice not found."}), 404
    with _store_lock:
        shutil.rmtree(d, ignore_errors=True)
    return jsonify({"ok": True})


# ---- jobs
def _get_job(job_id):
    with _jobs_lock:
        return _jobs.get(job_id)


@app.route("/api/jobs/<job_id>")
def api_job(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found (it may have expired)."}), 404
    return jsonify(job.public())


@app.route("/api/jobs/<job_id>/audio")
def api_job_audio(job_id):
    job = _get_job(job_id)
    if not job or job.status != "done" or not os.path.exists(job.path):
        return jsonify({"error": "Audio is not ready."}), 404
    return send_file(job.path, mimetype="audio/mpeg", download_name="speech.mp3", conditional=True)


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def api_job_cancel(job_id):
    job = _get_job(job_id)
    if job:
        job.cancelled = True
    return jsonify({"ok": True})


def import_legacy_voices():
    """Copy saved voices from an older in-project folder to the shared folder (never overwrites)."""
    if not os.path.isdir(LEGACY_VOICES_DIR) or os.path.abspath(LEGACY_VOICES_DIR) == os.path.abspath(VOICES_DIR):
        return
    for name in os.listdir(LEGACY_VOICES_DIR):
        src, dst = os.path.join(LEGACY_VOICES_DIR, name), os.path.join(VOICES_DIR, name)
        if VID_RE.match(name) and os.path.isfile(os.path.join(src, "meta.json")) and not os.path.exists(dst):
            try:
                shutil.copytree(src, dst)
            except Exception:
                log.warning("Could not import saved voice %s", name)


import_legacy_voices()
_wipe_old_files()
threading.Thread(target=cleanup_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("DEBUG") == "1", threaded=True)
