"""Konuşma → metin backend'leri.

Telefon (iOS Safari) sesi audio/mp4 (AAC) olarak yollar; ffmpeg ile 16 kHz mono
16-bit WAV'a çevrilir ve backend'e verilir.

- WhisperCppBackend: whisper.cpp `whisper-server.exe` (Vulkan → AMD GPU). Model bir kez
  belleğe yüklenir, alt süreç olarak yaşar; yerel HTTP ile konuşulur.
- FasterWhisperBackend: CPU yedeği; HF cache'teki Systran/faster-whisper-* modelleri.

Komut satırı: `uv run python -m server.stt ses.m4a [--backend faster-whisper]`
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_RATE = 16000
MIN_SECONDS = 0.3

# Whisper'ın sessizlik/gürültüde uydurduğu bilinen Türkçe kalıplar (çıktının tamamıysa atılır)
_HALLUCINATIONS = [
    r"altyaz[ıi].*",
    r"abone ol.*",
    r"izledi[ğg]iniz i[çc]in te[şs]ekk[üu]rler.*",
    r"(bir sonraki|ba[şs]ka bir) videoda g[öo]r[üu][şs]mek [üu]zere.*",
    r"m\.?\s?k\.?",
    r"\[.*\]|\(.*\)",
]
_HALLU_RE = re.compile(r"^\s*(?:" + "|".join(_HALLUCINATIONS) + r")\s*$", re.IGNORECASE)


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if not text or _HALLU_RE.match(text):
        return ""
    return text


def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg bulunamadı (PATH'te olmalı)")
    return exe


def to_wav(audio: bytes) -> bytes:
    """Herhangi bir ses formatını 16 kHz mono s16 WAV'a çevirir.

    ffmpeg pipe'a WAV yazarken başlığa uzunluğu geri yazamaz (geçersiz boyut kalır);
    bu yüzden ham PCM alıp başlığı burada yazıyoruz. Girdi geçici dosyadan okunur:
    MP4/M4A'da moov atomu sonda olabilir ve pipe'tan okunamaz.
    """
    fd, src = tempfile.mkstemp(prefix="dikte-", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        proc = subprocess.run(
            [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", src,
             "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"],
            capture_output=True, check=False,
        )
    finally:
        os.unlink(src)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg dönüştürme hatası: {proc.stderr.decode(errors='replace')[:300]}")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(proc.stdout)
    return buf.getvalue()


def _kill_with_parent(proc: subprocess.Popen) -> None:
    """Alt süreci bir Job Object'e koyar: bu süreç (Ctrl+C, çökme, pencere kapatma) ölünce
    whisper-server da ölür ve 8178 portunu açık bırakmaz."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in
                    ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    hproc = k32.OpenProcess(0x0001 | 0x0100, False, proc.pid)  # PROCESS_TERMINATE | SET_QUOTA
    k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(hproc))
    k32.CloseHandle(wintypes.HANDLE(hproc))
    _JOBS.append(job)  # handle süreç ömrü boyunca açık kalmalı


_JOBS: list = []


def wav_seconds(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav)) as w:
        return w.getnframes() / w.getframerate()


class Backend:
    name = "base"

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def transcribe_wav(self, wav: bytes) -> str:
        raise NotImplementedError


class WhisperCppBackend(Backend):
    name = "whispercpp"

    def __init__(self, exe: str, model: str, port: int, language: str, prompt: str,
                 threads: int = 6, flash_attn: bool = True) -> None:
        self.exe = (ROOT / exe).resolve()
        self.model = (ROOT / model).resolve()
        self.port = port
        self.language = language
        self.prompt = prompt
        self.threads = threads
        self.flash_attn = flash_attn
        self.proc: subprocess.Popen | None = None
        self.url = f"http://127.0.0.1:{port}"
        self.client = httpx.AsyncClient(timeout=120)

    async def start(self) -> None:
        if not self.exe.exists():
            raise RuntimeError(f"{self.exe} yok — önce scripts/build_whispercpp.ps1 çalıştırın")
        if not self.model.exists():
            raise RuntimeError(f"{self.model} yok — önce scripts/download_model.ps1 çalıştırın")
        # Göreli yol: kullanıcı klasöründeki ASCII olmayan karakterler (ör. "İ") C tarafında fopen'ı bozabilir
        model_arg = os.path.relpath(self.model, self.exe.parent)
        args = [str(self.exe), "-m", model_arg, "--host", "127.0.0.1",
                "--port", str(self.port), "-l", self.language, "-t", str(self.threads)]
        if self.flash_attn:
            args.append("-fa")
        log = open(ROOT / "data" / "whisper-server.log", "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(args, cwd=self.exe.parent, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        _kill_with_parent(self.proc)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("whisper-server kapandı, bkz. data/whisper-server.log")
            try:
                r = await self.client.get(f"{self.url}/health", timeout=2)
                if r.status_code == 200:
                    await self._warmup()
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.3)
        raise RuntimeError("whisper-server 120 sn içinde hazır olmadı")

    async def _warmup(self) -> None:
        """İlk çıkarım Vulkan pipeline'larını kurduğu için ~1.5 sn fazladan sürer; bunu açılışta öde."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(b"\0\0" * SAMPLE_RATE)
        await self.transcribe_wav(buf.getvalue())

    async def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        await self.client.aclose()

    async def transcribe_wav(self, wav: bytes) -> str:
        data = {"language": self.language, "response_format": "json", "temperature": "0.0",
                "temperature_inc": "0.2", "suppress_non_speech": "true"}
        if self.prompt:
            data["prompt"] = self.prompt
        r = await self.client.post(f"{self.url}/inference", files={"file": ("a.wav", wav, "audio/wav")},
                                   data=data)
        r.raise_for_status()
        return r.json().get("text", "")


class FasterWhisperBackend(Backend):
    name = "faster-whisper"

    def __init__(self, model: str, device: str, compute_type: str, language: str, prompt: str) -> None:
        self.model_name, self.device, self.compute_type = model, device, compute_type
        self.language, self.prompt = language, prompt
        self.model = None

    async def start(self) -> None:
        from faster_whisper import WhisperModel  # isteğe bağlı bağımlılık: uv sync --extra cpu

        self.model = await asyncio.to_thread(WhisperModel, self.model_name, device=self.device,
                                             compute_type=self.compute_type)

    async def transcribe_wav(self, wav: bytes) -> str:
        import numpy as np

        with wave.open(io.BytesIO(wav)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768

        def run() -> str:
            segments, _ = self.model.transcribe(pcm, language=self.language, beam_size=1,
                                                initial_prompt=self.prompt or None, vad_filter=True)
            return " ".join(s.text for s in segments)

        return await asyncio.to_thread(run)


def make_backend(cfg: dict, override: str | None = None) -> Backend:
    stt = cfg["stt"]
    kind = override or stt["backend"]
    if kind == "whispercpp":
        w = stt["whispercpp"]
        return WhisperCppBackend(w["exe"], w["model"], w["port"], stt["language"], stt.get("prompt", ""),
                                 w.get("threads", 6), w.get("flash_attn", True))
    if kind == "faster-whisper":
        f = stt["faster_whisper"]
        return FasterWhisperBackend(f["model"], f["device"], f["compute_type"], stt["language"],
                                    stt.get("prompt", ""))
    raise ValueError(f"bilinmeyen backend: {kind}")


class Transcriber:
    """Ses baytları → temizlenmiş metin. İstekleri sıraya koyar (GPU tek iş)."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.lock = asyncio.Lock()

    async def transcribe(self, audio: bytes) -> tuple[str, dict]:
        t0 = time.perf_counter()
        wav = await asyncio.to_thread(to_wav, audio)
        secs = wav_seconds(wav)
        if secs < MIN_SECONDS:
            return "", {"audio_s": secs, "skipped": "çok kısa"}
        t1 = time.perf_counter()
        async with self.lock:
            raw = await self.backend.transcribe_wav(wav)
        t2 = time.perf_counter()
        return clean_text(raw), {"audio_s": round(secs, 2), "convert_s": round(t1 - t0, 3),
                                 "stt_s": round(t2 - t1, 3), "raw": raw.strip()}


async def _cli() -> None:
    import argparse
    import tomllib

    ap = argparse.ArgumentParser(description="Ses dosyasını yazıya çevir (hız testi)")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--backend", choices=["whispercpp", "faster-whisper"])
    args = ap.parse_args()
    cfg = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    (ROOT / "data").mkdir(exist_ok=True)
    backend = make_backend(cfg, args.backend)
    t = time.perf_counter()
    await backend.start()
    print(f"[{backend.name}] hazır: {time.perf_counter() - t:.1f} sn")
    tr = Transcriber(backend)
    try:
        for f in args.files:
            text, info = await tr.transcribe(Path(f).read_bytes())
            print(f"{f}: {info}\n  → {text}")
    finally:
        await backend.stop()


if __name__ == "__main__":
    asyncio.run(_cli())
