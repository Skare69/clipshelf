"""Local speech-to-text for videos without TikTok captions (DB-free).

transcribe(video_path) decodes the first audio stream with ffmpeg (fixed
argument array, local files only) into 16 kHz mono float32 PCM, then runs
whisper.cpp (pywhispercpp) in a child process: `python -m
clipshelf.transcription <model> <pcm>`. The child keeps the native library
out of the worker process, so a crash or hang costs one bounded run, not the
coordinator. The model is a ggml file baked into the image and named by
CLIPSHELF_WHISPER_MODEL; nothing is ever downloaded, and the child gets a
minimal environment without proxy or server secrets.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MODEL_ENV = "CLIPSHELF_WHISPER_MODEL"
TRANSCRIBE_TIMEOUT = 1800   # seconds, whole child run
DECODE_TIMEOUT = 300        # seconds, ffmpeg audio decode
AUDIO_MAX_SECONDS = 900     # same bound as acquisition.VIDEO_MAX_DURATION
TEXT_MAX = 20000            # same cap as acquisition._caption_text
WARNING_PREFIX = "local transcription "  # every worker warning of this feature
MARKER = "local_asr"  # source metadata key: "failed" / "no-speech" attempt outcome
REPO_ROOT = Path(__file__).resolve().parent.parent


class TranscriptionError(Exception):
    """Transcription unavailable or failed; str is a short visible reason."""


def _tail(data):
    return (data or b"").decode("utf-8", errors="replace").strip()[-300:]


def transcribe(video_path, timeout=TRANSCRIBE_TIMEOUT):
    """Whitespace-normalised transcript of the video's speech, capped at
    TEXT_MAX; "" when whisper hears no speech. Raises TranscriptionError."""
    model = os.environ.get(MODEL_ENV, "").strip()
    if not model or not os.path.isfile(model):
        raise TranscriptionError(
            "local transcription unavailable: no Whisper model file (CLIPSHELF_WHISPER_MODEL)")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise TranscriptionError("local transcription unavailable: ffmpeg not installed")
    with tempfile.TemporaryDirectory(prefix="clipshelf-asr-") as tmp:
        pcm = os.path.join(tmp, "audio.f32")
        _decode(ffmpeg, os.path.abspath(video_path), pcm)
        return _run_child(model, pcm, tmp, timeout)


def _decode(ffmpeg, video_path, pcm):
    argv = [ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file",
            "-i", video_path, "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-ac", "1", "-ar", "16000", "-t", str(AUDIO_MAX_SECONDS),
            "-f", "f32le", "-y", pcm]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=DECODE_TIMEOUT,
                              shell=False, check=False)
    except subprocess.TimeoutExpired:
        raise TranscriptionError(f"audio decode timed out after {DECODE_TIMEOUT}s") from None
    except OSError as exc:
        raise TranscriptionError(f"audio decode could not start: {exc}") from None
    err = _tail(proc.stderr)
    if proc.returncode != 0:
        if "matches no streams" in err or "does not contain any stream" in err:
            raise TranscriptionError("no audio track")
        raise TranscriptionError(f"audio decode failed: {err or proc.returncode}")
    if not os.path.isfile(pcm) or os.path.getsize(pcm) == 0:
        raise TranscriptionError("no audio track")


def _run_child(model, pcm, home, timeout):
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": home, "LANG": "C.UTF-8"}
    argv = [sys.executable, "-m", "clipshelf.transcription", model, pcm]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, shell=False,
                              check=False, cwd=str(REPO_ROOT), env=env)
    except subprocess.TimeoutExpired:
        raise TranscriptionError(f"transcription timed out after {timeout}s") from None
    except OSError as exc:
        raise TranscriptionError(f"transcription could not start: {exc}") from None
    if proc.returncode != 0:
        raise TranscriptionError(
            f"transcription failed (exit {proc.returncode}): {_tail(proc.stderr)}")
    # The last stdout line is ours; a native library may print before it.
    lines = (proc.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
    try:
        data = json.loads(lines[-1])
    except (IndexError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise TranscriptionError(f"transcription returned no result: {_tail(proc.stderr)}")
    return " ".join(data["text"].split())[:TEXT_MAX]


def main(argv=None):
    """Child entry: <model> <pcm> -> one JSON line {"text": ...} on stdout."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python -m clipshelf.transcription <model> <pcm>", file=sys.stderr)
        return 2
    model_path, pcm_path = argv
    import numpy as np
    from pywhispercpp.model import Model

    audio = np.fromfile(pcm_path, dtype=np.float32)
    model = Model(model_path, redirect_whispercpp_logs_to=None, print_progress=False,
                  n_threads=min(4, os.cpu_count() or 1))
    segments = model.transcribe(audio, language="auto")
    text = " ".join(segment.text for segment in segments)
    sys.stdout.write(json.dumps({"text": text}) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
