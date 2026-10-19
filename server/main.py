"""Dikte sunucusu.

İki dinleyici açar:
- HTTPS (varsayılan 8443): telefon arayüzü + /api/* (token gerekli)
- HTTP  (varsayılan 8080): yalnızca kurulum sayfası ve CA sertifikası indirme

Çalıştırma: `uv run python -m server.main [--backend faster-whisper]`
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import secrets
import time
import tomllib
from pathlib import Path

import qrcode
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import certs, inject
from .refine import MODES, GeminiRefiner, RefineError, load_dotenv, make_refiner
from .stt import Transcriber, make_backend

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WEB = ROOT / "web"
MAX_AUDIO_BYTES = 50 * 1024 * 1024  # ~1 saatlik AAC; daha büyüğü bellek tüketme denemesi sayılır
MAX_TEXT_CHARS = 20_000
log = logging.getLogger("dikte")


def load_config() -> dict:
    return tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))


def load_token() -> str:
    p = DATA / "token.txt"
    if not p.exists():
        p.write_text(secrets.token_urlsafe(16))
    return p.read_text().strip()


def create_app(cfg: dict, transcriber: Transcriber, token: str,
               refiner: GeminiRefiner | None = None, refiner_error: str = "") -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    inj = cfg["inject"]
    history = inject.TypedHistory()
    typing_lock = asyncio.Lock()  # yazma ve geri alma birbirine karışmasın, geçmiş sırası doğru kalsın

    def check(req: Request) -> None:
        if not secrets.compare_digest(req.headers.get("x-token", ""), token):
            raise HTTPException(401, "geçersiz token")

    sep = " " if inj.get("trailing_space", True) else ""

    async def type_and_record(text: str) -> int:
        async with typing_lock:
            hwnd = inject.foreground_window()
            await asyncio.to_thread(inject.inject, text, inj.get("mode", "type"))
            return history.push(hwnd, text)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest")
    async def manifest() -> FileResponse:
        return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/icon.svg")
    async def icon() -> FileResponse:
        return FileResponse(WEB / "icon.svg", media_type="image/svg+xml")

    @app.get("/api/ping")
    async def ping(req: Request) -> dict:
        check(req)
        return {"ok": True, "backend": transcriber.backend.name,
                "refine_model": refiner.model if refiner else None, "refine_error": refiner_error}

    @app.post("/api/transcribe")
    async def transcribe(req: Request) -> JSONResponse:
        check(req)
        if int(req.headers.get("content-length") or 0) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "ses çok büyük")
        audio = await req.body()
        if not audio:
            raise HTTPException(400, "boş ses")
        if len(audio) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "ses çok büyük")
        mode = req.query_params.get("mode", "raw")
        if mode not in MODES:
            raise HTTPException(400, "bilinmeyen mod")
        try:
            text, info = await transcriber.transcribe(audio)
        except Exception as e:  # ffmpeg / backend hatasını telefona göster
            log.exception("transcribe hatası")
            raise HTTPException(500, str(e)) from e
        info.update(mode=mode, transcript=text, refined=False)
        if text and mode != "raw":
            if refiner is None:
                info["refine_error"] = refiner_error or "düzenleme kapalı"
            else:
                t = time.perf_counter()
                try:
                    out = await refiner.refine(text, mode)
                    info["refined"] = out != text
                    text = out
                except RefineError as e:  # kota/ağ/zaman aşımı: ham metinle devam
                    info["refine_error"] = str(e)
                    log.warning("düzenleme başarısız: %s", e)
                info["refine_s"] = round(time.perf_counter() - t, 3)
        chunk = 0
        if text and req.query_params.get("inject", "1") == "1":
            chunk = await type_and_record(text + sep)
        log.info("%s  %s", info, text)
        return JSONResponse({"text": text, "typed": bool(chunk), "chunk": chunk, **info})

    @app.post("/api/replace")
    async def replace(req: Request) -> JSONResponse:
        """Son yazılan parçayı başka metinle değiştirir (ör. düzenlenmiş → ham)."""
        check(req)
        body = await req.json()
        cid, text = int(body.get("chunk", 0)), str(body.get("text", ""))[:MAX_TEXT_CHARS]
        async with typing_lock:
            hwnd = inject.foreground_window()
            n = history.take_if_last(hwnd, cid)
            if n is None:
                return JSONResponse({"detail": "Bu dikte artık en son yazılan değil ya da pencere değişti"},
                                    status_code=409)
            await asyncio.to_thread(inject.backspace, n)
            await asyncio.to_thread(inject.inject, text + sep, inj.get("mode", "type"))
            new = history.push(hwnd, text + sep)
        return JSONResponse({"chunk": new})

    @app.post("/api/type")
    async def type_text(req: Request) -> dict:
        check(req)
        body = await req.json()
        text = str(body.get("text", ""))[:MAX_TEXT_CHARS]
        if text:
            await type_and_record(text)
        return {"ok": True}

    @app.post("/api/key/{key}")
    async def key(key: str, req: Request) -> dict:
        check(req)
        if key not in inject.SPECIAL_KEYS:
            raise HTTPException(400, "bilinmeyen tuş")
        async with typing_lock:
            hwnd = inject.foreground_window()
            await asyncio.to_thread(inject.press, key)
            if key == "backspace":
                history.on_backspace()
            else:
                history.push(hwnd, {"space": " ", "enter": "\n", "tab": "\t"}[key])
        return {"ok": True}

    @app.post("/api/undo")
    async def undo(req: Request) -> dict:
        """Son dikteyi (all=1 ise bu penceredeki tüm dikteleri) Backspace ile siler."""
        check(req)
        everything = req.query_params.get("all") == "1"
        async with typing_lock:
            hwnd = inject.foreground_window()
            n = history.take(hwnd, everything)
            if n:
                await asyncio.to_thread(inject.backspace, n)
        return {"deleted": n, "remaining": len(history.chunks)}

    return app


def create_setup_app(ca_path: Path, app_url: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    html = (WEB / "setup.html").read_text(encoding="utf-8").replace("{{APP_URL}}", app_url)

    @app.get("/")
    async def setup() -> HTMLResponse:
        return HTMLResponse(html)

    @app.get("/ca.crt")
    async def ca() -> Response:
        # iOS bu MIME türünü görünce "Profil indirildi" akışını başlatır
        return Response(ca_path.read_bytes(), media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="dikte-ca.crt"'})

    return app


def print_qr(url: str) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make()
    qr.print_ascii(invert=True)


async def amain(backend_override: str | None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    DATA.mkdir(exist_ok=True)
    srv = cfg["server"]
    hosts = srv.get("hosts") or certs.lan_ips()
    ip = srv["hosts"][0] if srv.get("hosts") else certs.primary_ip()
    ca_p, cert_p, key_p = certs.ensure_certs(DATA / "certs", hosts)
    token = load_token()

    backend = make_backend(cfg, backend_override)
    log.info("STT backend başlatılıyor: %s", backend.name)
    await backend.start()
    transcriber = Transcriber(backend)

    load_dotenv(ROOT / ".env")
    refiner, refiner_error = None, ""
    try:
        refiner = make_refiner(cfg)
        if refiner:
            await refiner.start()
            log.info("Düzenleme modeli: %s", refiner.model)
    except RefineError as e:  # anahtar yok/geçersiz: Ham mod yine çalışır
        refiner, refiner_error = None, str(e)
        log.warning("Düzenleme kapalı: %s", e)

    app_url = f"https://{ip}:{srv['https_port']}/#t={token}"
    setup_url = f"http://{ip}:{srv['http_port']}/"
    main_server = uvicorn.Server(uvicorn.Config(
        create_app(cfg, transcriber, token, refiner, refiner_error), host="0.0.0.0",
        port=srv["https_port"], ssl_certfile=str(cert_p), ssl_keyfile=str(key_p), log_level="warning"))
    setup_server = uvicorn.Server(uvicorn.Config(
        # Kurulum sayfası kimlik doğrulamasız HTTP'dir: token'lı adresi ASLA buraya koymayın
        create_setup_app(ca_p, f"https://{ip}:{srv['https_port']}/"), host="0.0.0.0", port=srv["http_port"], log_level="warning"))

    print("\n=== Dikte hazır ===")
    print(f"İlk kurulum (iPhone, sertifika): {setup_url}")
    print(f"Uygulama: {app_url}\n")
    print_qr(app_url)
    print("Ctrl+C ile kapatın.\n")
    try:
        await asyncio.gather(main_server.serve(), setup_server.serve())
    finally:
        with contextlib.suppress(Exception):
            await backend.stop()
        if refiner:
            with contextlib.suppress(Exception):
                await refiner.aclose()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["whispercpp", "faster-whisper"])
    args = ap.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain(args.backend))


if __name__ == "__main__":
    main()
