"""Voice cloning for VoxForge.

How it works
------------
1. The text is first spoken by a normal Edge neural voice (any language, so Urdu
   and English both work).
2. The OpenVoice *tone-color converter* (MIT licence, MyShell.ai) then changes the
   timbre of that speech to match a short reference recording of the target voice.

Because only the timbre is converted, accent / pronunciation / rhythm still come
from the base voice you pick. The result sounds like the reference speaker, but it
is not a perfect clone.

Extra requirements: torch, librosa, soundfile, numpy (see requirements-cloning.txt)
and ffmpeg (found automatically; see ffmpeg_util.py). The converter model (~130 MB) downloads automatically the first
time cloning is used (or run  python download_model.py  once).
"""
import os
import shutil
import subprocess
import threading
import urllib.request
import zipfile

from ffmpeg_util import FFMPEG

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Model and saved voices live outside the project folder, so a new/updated copy of VoxForge
# keeps using them. Override with the VOXFORGE_DATA environment variable.
DATA_DIR = os.getenv("VOXFORGE_DATA") or os.path.join(os.path.expanduser("~"), ".voxforge")
MODEL_DIR = os.path.join(DATA_DIR, "models", "openvoice", "converter")
LOCAL_MODEL_DIR = os.path.join(BASE_DIR, "models", "openvoice", "converter")  # older versions
# Where to fetch the converter (config.json + checkpoint.pth). Tried in order.
# Override with OPENVOICE_CONVERTER_BASE=<url folder containing both files>
# or OPENVOICE_MODEL_URL=<zip containing converter/config.json + converter/checkpoint.pth>.
HF_BASE = "https://huggingface.co/myshell-ai/OpenVoiceV2/resolve/main/converter"
S3_ZIP = "https://myshell-public-repo-host.s3.amazonaws.com/openvoice/checkpoints_v2_0417.zip"
MODEL_FILES = ("config.json", "checkpoint.pth")
CONFIG_PATH = os.path.join(MODEL_DIR, "config.json")
CKPT_PATH = os.path.join(MODEL_DIR, "checkpoint.pth")

_engine = None
_engine_lock = threading.Lock()
# Only one conversion at a time: it is CPU/GPU heavy and the model is shared.
run_lock = threading.Lock()


class CloneUnavailable(Exception):
    """Raised with a user-friendly message when cloning cannot run."""


# ------------------------------------------------------------------ helpers
def availability():
    """Return (ok, reason). Cheap check used by the UI before a job starts."""
    if FFMPEG is None:
        return False, (
            "ffmpeg was not found. Easiest fix: run  pip install imageio-ffmpeg  and restart "
            "the server (or install ffmpeg and open a NEW terminal before running python app.py).")
    missing = []
    for mod in ("torch", "librosa", "soundfile", "numpy"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if missing:
        return False, (
            "Voice cloning needs extra packages (" + ", ".join(missing) + "). "
            "Run: pip install -r requirements-cloning.txt"
        )
    return True, ""


def model_ready():
    return adopt_local_model()


def decode_audio(data, sr):
    """Decode any audio bytes (mp3 etc.) to mono float32 at `sr` using ffmpeg."""
    import numpy as np

    proc = subprocess.run(
        [FFMPEG, "-loglevel", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1",
         "-ar", str(sr), "pipe:1"],
        input=data, capture_output=True,
    )
    if proc.returncode != 0:
        raise CloneUnavailable("Could not read the audio: " + proc.stderr.decode(errors="ignore")[:200])
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def prepare_reference(path, sr, max_seconds=90):
    """Load the user's reference clip: mono, resampled, long silences removed."""
    import numpy as np

    proc = subprocess.run(
        [FFMPEG, "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(sr),
         "-af", "silenceremove=start_periods=1:start_threshold=-50dB:"
                "stop_periods=-1:stop_threshold=-50dB:stop_duration=0.6",
         "-t", "300", "-f", "f32le", "pipe:1"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise CloneUnavailable(
            "Could not read the reference audio. Try a clear WAV/MP3/M4A recording of the voice.")
    audio = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    seconds = len(audio) / sr
    if seconds < 3:
        raise CloneUnavailable(
            f"The reference has only {seconds:.1f}s of speech. Use at least 10 seconds (ideally 20-60s) "
            "of clean, single-speaker audio.")
    return audio[: int(max_seconds * sr)]


REF_SR = 22050
MALE_F0_MAX = 165.0  # median pitch below this -> treated as a male voice


def estimate_f0(audio, sr, fmin=70.0, fmax=380.0):
    """Median pitch (Hz) of the voiced parts of `audio`, or None if it can't be measured.

    Plain numpy autocorrelation (no extra dependencies). Good enough to tell
    male/female and to line up the base voice's pitch with the sample's.
    """
    import numpy as np

    x = np.asarray(audio, dtype=np.float32)[: int(40 * sr)]
    frame, hop = int(0.04 * sr), int(0.01 * sr)
    if len(x) < frame * 4:
        return None
    n = 1 + (len(x) - frame) // hop
    win = np.hanning(frame).astype(np.float32)
    nfft = 2 * frame
    win_ac = np.fft.irfft(np.abs(np.fft.rfft(win, nfft)) ** 2)[:frame]
    win_ac = np.maximum(win_ac / win_ac[0], 1e-3)
    lmin, lmax = int(sr / fmax), min(int(sr / fmin), frame - 2)

    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    frames = x[idx] * win[None, :]
    rms = np.sqrt((frames ** 2).mean(axis=1))
    gate = max(float(np.percentile(rms, 90)) * 0.25, 1e-4)

    f0s = []
    for k in range(n):
        if rms[k] < gate:
            continue
        spec = np.fft.rfft(frames[k], nfft)
        ac = np.fft.irfft(np.abs(spec) ** 2)[:frame]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0] / win_ac
        seg = ac[lmin - 1: lmax + 2]
        peaks = [i for i in range(1, len(seg) - 1) if seg[i] > seg[i - 1] and seg[i] >= seg[i + 1]]
        if not peaks:
            continue
        best = max(seg[i] for i in peaks)
        if best < 0.5:
            continue
        lag = next(i for i in peaks if seg[i] >= 0.9 * best)
        a, b, c = seg[lag - 1], seg[lag], seg[lag + 1]  # parabolic refinement
        denom = a - 2 * b + c
        shift = 0.5 * (a - c) / denom if denom != 0 else 0.0
        f0s.append(sr / (lmin - 1 + lag + shift))
    if len(f0s) < 25:
        return None
    return float(np.median(f0s))


def gender_from_f0(f0):
    if not f0:
        return None
    return "Male" if f0 < MALE_F0_MAX else "Female"


def reference_info(path):
    """Analyse a reference clip: {'seconds', 'f0', 'gender'} (raises CloneUnavailable if unusable)."""
    audio = prepare_reference(path, REF_SR)
    f0 = estimate_f0(audio, REF_SR)
    return {"seconds": round(len(audio) / REF_SR, 1), "f0": f0, "gender": gender_from_f0(f0)}


def save_reference(src_path, dst_wav, max_seconds=60):
    """Clean a reference clip and store it as a mono 22.05 kHz WAV. Returns reference_info-like dict."""
    import soundfile as sf

    audio = prepare_reference(src_path, REF_SR, max_seconds=max_seconds)
    os.makedirs(os.path.dirname(dst_wav), exist_ok=True)
    sf.write(dst_wav, audio, REF_SR, subtype="PCM_16")
    f0 = estimate_f0(audio, REF_SR)
    return {"seconds": round(len(audio) / REF_SR, 1), "f0": f0, "gender": gender_from_f0(f0)}


def _fetch(url, dest, progress=None, timeout=60, attempts=4):
    """Download url -> dest (via a .part file), retrying dropped connections."""
    import time
    import urllib.error

    headers = {"User-Agent": "Mozilla/5.0 (VoxForge model downloader)", "Accept": "*/*"}
    part = dest + ".part"
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(part, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    block = resp.read(1024 * 256)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
            if total and done < total:
                raise ConnectionError(f"incomplete download ({done} of {total} bytes)")
            os.replace(part, dest)
            return done
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):  # retrying will not help
                raise
            last = exc
        except Exception as exc:  # dropped connection, timeout, ...
            last = exc
        finally:
            if os.path.exists(part):
                os.remove(part)
        time.sleep(2 * (attempt + 1))
    raise last


def _valid_model_files(config_path=None, ckpt_path=None):
    """Cheap sanity check so a half-downloaded / HTML-error file is never used."""
    config_path, ckpt_path = config_path or CONFIG_PATH, ckpt_path or CKPT_PATH
    try:
        import json

        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if "model" not in cfg or "data" not in cfg:
            return False
        return os.path.getsize(ckpt_path) > 5 * 1024 * 1024
    except Exception:
        return False


def adopt_local_model():
    """If an older copy of the model sits inside the project folder, move it to the shared folder."""
    if _valid_model_files():
        return True
    lc, lk = (os.path.join(LOCAL_MODEL_DIR, f) for f in MODEL_FILES)
    if os.path.abspath(LOCAL_MODEL_DIR) != os.path.abspath(MODEL_DIR) and _valid_model_files(lc, lk):
        os.makedirs(MODEL_DIR, exist_ok=True)
        for src, dst in ((lc, CONFIG_PATH), (lk, CKPT_PATH)):
            shutil.copyfile(src, dst)
        return _valid_model_files()
    return False


def _from_hf_hub(progress):
    """Optional extra route: the huggingface_hub library (handles proxies/retries itself)."""
    from huggingface_hub import hf_hub_download

    for fname in MODEL_FILES:
        path = hf_hub_download("myshell-ai/OpenVoiceV2", f"converter/{fname}")
        shutil.copyfile(path, os.path.join(MODEL_DIR, fname))


def _from_zip(url, progress):
    tmp_zip = os.path.join(os.path.dirname(MODEL_DIR), "_download.zip")
    try:
        _fetch(url, tmp_zip, progress)
        with zipfile.ZipFile(tmp_zip) as zf:
            found = 0
            for name in zf.namelist():
                norm = name.replace("\\", "/")
                for fname in MODEL_FILES:
                    if norm.endswith("converter/" + fname):
                        with zf.open(name) as src, open(os.path.join(MODEL_DIR, fname), "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        found += 1
        if found < len(MODEL_FILES):
            raise RuntimeError("the zip did not contain converter/config.json and converter/checkpoint.pth")
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)


def manual_instructions():
    return (
        f"Put these two files into this folder:  {MODEL_DIR}\n"
        f"(or, if you have an older VoxForge folder, copy its  models\\openvoice\\converter  folder there)\n"
        f"  1) {HF_BASE}/config.json\n"
        f"  2) {HF_BASE}/checkpoint.pth\n"
        "(open each link in your browser and save it with exactly that file name; "
        "the folder must contain config.json and checkpoint.pth). Then click Generate again."
    )


def download_model(progress=None):
    """Fetch the OpenVoice V2 converter, trying every known source in turn."""
    os.makedirs(MODEL_DIR, exist_ok=True)
    if adopt_local_model():
        return
    attempts = []

    sources = []
    base = os.getenv("OPENVOICE_CONVERTER_BASE")
    if base:
        sources.append(("custom folder", "files", base.rstrip("/")))
    if os.getenv("OPENVOICE_MODEL_URL"):
        sources.append(("custom zip", "zip", os.environ["OPENVOICE_MODEL_URL"]))
    sources.append(("Hugging Face", "files", HF_BASE))
    sources.append(("Hugging Face (huggingface_hub)", "hub", None))
    sources.append(("MyShell S3 zip", "zip", S3_ZIP))

    for label, kind, url in sources:
        try:
            if kind == "files":
                for fname in MODEL_FILES:
                    _fetch(f"{url}/{fname}", os.path.join(MODEL_DIR, fname), progress)
            elif kind == "hub":
                _from_hf_hub(progress)
            else:
                _from_zip(url, progress)
            if _valid_model_files():
                return
            attempts.append(f"{label}: downloaded files looked invalid")
        except Exception as exc:
            attempts.append(f"{label}: {exc}")
        for fname in MODEL_FILES:  # don't leave half-baked files around
            p = os.path.join(MODEL_DIR, fname)
            if os.path.exists(p) and not _valid_model_files():
                os.remove(p)

    raise CloneUnavailable(
        "Could not download the voice-clone model automatically.\n"
        + "\n".join("  - " + a for a in attempts) + "\n\n" + manual_instructions())


# ------------------------------------------------------------------- engine
class CloneEngine:
    def __init__(self):
        import torch
        from openvoice_lite import utils
        from openvoice_lite.models import SynthesizerTrn

        self.torch = torch
        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        hps = utils.get_hparams_from_file(CONFIG_PATH)
        model = SynthesizerTrn(
            len(getattr(hps, "symbols", [])),
            hps.data.filter_length // 2 + 1,
            n_speakers=hps.data.n_speakers,
            **hps.model,
        ).to(self.device)
        model.eval()
        try:
            try:
                ckpt = torch.load(CKPT_PATH, map_location=torch.device(self.device), weights_only=True)
            except Exception:
                # Some checkpoints hold non-tensor objects; this file comes from the
                # official OpenVoice release downloaded above.
                ckpt = torch.load(CKPT_PATH, map_location=torch.device(self.device), weights_only=False)
        except Exception as exc:
            for p in (CKPT_PATH, CONFIG_PATH):  # force a clean re-download next time
                if os.path.exists(p):
                    os.remove(p)
            raise CloneUnavailable(
                f"The model file was corrupted ({type(exc).__name__}). It has been removed - "
                "click Generate again to download it fresh.")
        state = ckpt["model"] if isinstance(ckpt, dict) and "model" in ckpt else ckpt
        result = model.load_state_dict(state, strict=False)
        core = ("dec.", "enc_q.", "flow.", "ref_enc.")
        if any(k.startswith(core) for k in result.missing_keys):
            raise CloneUnavailable(
                "The model file does not match this app (wrong or corrupted checkpoint.pth). "
                f"Delete {CKPT_PATH} and {CONFIG_PATH} and try again.")
        self.model = model
        self.hps = hps
        self.sr = hps.data.sampling_rate

    def _spec(self, audio):
        from openvoice_lite.mel_processing import spectrogram_torch

        torch, hps = self.torch, self.hps
        y = torch.from_numpy(audio).float().to(self.device).unsqueeze(0)
        return spectrogram_torch(y, hps.data.filter_length, hps.data.sampling_rate,
                                 hps.data.hop_length, hps.data.win_length, center=False)

    def speaker_embedding(self, audio, window_seconds=10, min_seconds=2):
        """Average speaker embedding over ~10 s windows of `audio` (float32 at self.sr)."""
        torch = self.torch
        step = int(window_seconds * self.sr)
        gs = []
        with torch.no_grad():
            for start in range(0, len(audio), step):
                win = audio[start:start + step]
                if len(win) < min_seconds * self.sr:
                    continue
                spec = self._spec(win)
                g = self.model.ref_enc(spec.transpose(1, 2)).unsqueeze(-1)
                gs.append(g.detach())
        if not gs:
            raise CloneUnavailable("Not enough speech to measure the voice.")
        return torch.stack(gs).mean(0)

    def convert(self, audio, src_se, tgt_se, tau=0.3):
        """Convert float32 audio (at self.sr) from src voice to tgt voice."""
        torch = self.torch
        with torch.no_grad():
            spec = self._spec(audio)
            lengths = torch.LongTensor([spec.size(-1)]).to(self.device)
            out = self.model.voice_conversion(spec, lengths, sid_src=src_se, sid_tgt=tgt_se, tau=tau)[0]
            return out[0, 0].data.cpu().float().numpy()


def get_engine(progress=None):
    """Load (downloading first if needed) the shared engine."""
    global _engine
    with _engine_lock:
        if _engine is None:
            ok, reason = availability()
            if not ok:
                raise CloneUnavailable(reason)
            if not model_ready():
                download_model(progress)
            _engine = CloneEngine()
        return _engine


# ----------------------------------------------------------------- pipeline
def render_clone(plan_segs, audio_by_idx, targets, out_mp3, tick, message):
    """Convert synthesized segments to their target voices and write one MP3.

    plan_segs   : list of ("text", chunk, edge_voice, target_key_or_None) / ("pause", seconds)
                  A segment with target_key=None is passed through unchanged.
    audio_by_idx: {segment index: mp3 bytes} for the text segments
    targets     : {target_key: {"ref_path": wav/audio path, "tau": float}}
    tick()      : called after every text segment (also raises if the job was cancelled)
    message(s)  : sets the status text shown to the user
    """
    import numpy as np
    import soundfile as sf

    message("Loading voice-clone model…")
    engine = get_engine(
        lambda done, total: message(
            f"Downloading voice-clone model… {done // (1024 * 1024)} MB"
            + (f" / {total // (1024 * 1024)} MB" if total else "")))
    sr = engine.sr

    with run_lock:
        message("Measuring the sample voice…")
        used = {s[3] for s in plan_segs if s[0] == "text" and s[3]}
        tgt_se = {k: engine.speaker_embedding(prepare_reference(targets[k]["ref_path"], sr)) for k in used}

        # Source embedding for each (base voice, target) pair, from that voice's first ~20 s.
        samples = {}
        for i, seg in enumerate(plan_segs):
            if seg[0] != "text" or not seg[3]:
                continue
            pair = (seg[2], seg[3])
            if sum(len(a) for a in samples.get(pair, [])) < 20 * sr:
                samples.setdefault(pair, []).append(decode_audio(audio_by_idx[i], sr))
        src_se = {p: engine.speaker_embedding(np.concatenate(arrs)) for p, arrs in samples.items()}
        del samples

        wav_path = out_mp3 + ".wav"
        message("Converting to the cloned voice…")
        try:
            with sf.SoundFile(wav_path, "w", samplerate=sr, channels=1, subtype="PCM_16") as wav:
                for i, seg in enumerate(plan_segs):
                    if seg[0] == "pause":
                        wav.write(np.zeros(int(sr * seg[1]), dtype=np.float32))
                        continue
                    audio = decode_audio(audio_by_idx.pop(i), sr)  # one chunk at a time
                    if seg[3]:
                        audio = engine.convert(audio, src_se[(seg[2], seg[3])], tgt_se[seg[3]],
                                               tau=targets[seg[3]]["tau"])
                    wav.write(np.clip(audio, -1.0, 1.0))
                    tick()
        except BaseException:
            if os.path.exists(wav_path):
                os.remove(wav_path)
            raise

    message("Encoding MP3…")
    proc = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", wav_path, "-codec:a", "libmp3lame",
         "-b:a", "96k", out_mp3],
        capture_output=True,
    )
    os.remove(wav_path)
    if proc.returncode != 0:
        raise CloneUnavailable("MP3 encoding failed: " + proc.stderr.decode(errors="ignore")[:200])
