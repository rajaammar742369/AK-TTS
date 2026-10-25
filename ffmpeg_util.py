"""Locate an ffmpeg binary without requiring it to be on PATH.

Order: FFMPEG_PATH env var -> ffmpeg on PATH -> common Windows install folders
-> the binary bundled with the `imageio-ffmpeg` pip package.
"""
import glob
import os
import shutil


def _windows_candidates():
    pats = []
    local = os.environ.get("LOCALAPPDATA")
    if local:  # winget installs land here
        pats.append(os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "**", "bin", "ffmpeg.exe"))
        pats.append(os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
    pats += [r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
             os.path.join(os.path.expanduser("~"), "ffmpeg", "bin", "ffmpeg.exe")]
    for pat in pats:
        for hit in glob.glob(pat, recursive=True):
            if os.path.isfile(hit):
                return hit
    return None


def find_ffmpeg():
    env = os.environ.get("FFMPEG_PATH")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    if os.name == "nt":
        found = _windows_candidates()
        if found:
            return found
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return None


FFMPEG = find_ffmpeg()
