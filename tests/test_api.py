"""HTTP API testleri: sahte STT + sahte düzenleyici, gerçek tuş gönderimi yok."""

import pytest
from fastapi.testclient import TestClient

from server import inject, main
from server.refine import RefineError

TOKEN = "t"
H = {"x-token": TOKEN}
CFG = {"inject": {"mode": "type", "trailing_space": True}}


class FakeTranscriber:
    class backend:
        name = "fake"

    def __init__(self, text):
        self.text = text

    async def transcribe(self, audio):
        return self.text, {"audio_s": 1.0, "stt_s": 0.1}


class FakeRefiner:
    model = "gemini-test"

    def __init__(self, fail=False):
        self.fail = fail

    async def refine(self, text, mode):
        if self.fail:
            raise RefineError("Gemini ücretsiz kota sınırı (429)")
        return f"[{mode}] temiz"


@pytest.fixture
def typed(monkeypatch):
    out = []
    monkeypatch.setattr(inject, "foreground_window", lambda: 42)
    monkeypatch.setattr(inject, "inject", lambda text, mode="type": out.append(text))
    monkeypatch.setattr(inject, "backspace", lambda n: out.append(("BS", n)))
    return out


def client(text="ııı merhaba merhaba", refiner=None, err=""):
    return TestClient(main.create_app(CFG, FakeTranscriber(text), TOKEN, refiner, err))


def test_raw_mode_types_transcript(typed):
    r = client(refiner=FakeRefiner()).post("/api/transcribe?mode=raw", content=b"x", headers=H).json()
    assert r["text"] == "ııı merhaba merhaba" and not r["refined"]
    assert typed == ["ııı merhaba merhaba "]


def test_clean_mode_types_refined_and_replace_restores_raw(typed):
    c = client(refiner=FakeRefiner())
    r = c.post("/api/transcribe?mode=clean", content=b"x", headers=H).json()
    assert r["refined"] and r["text"] == "[clean] temiz" and r["transcript"] == "ııı merhaba merhaba"
    rep = c.post("/api/replace", json={"chunk": r["chunk"], "text": r["transcript"]}, headers=H)
    assert rep.status_code == 200
    assert typed == ["[clean] temiz ", ("BS", len("[clean] temiz ")), "ııı merhaba merhaba "]
    # aynı parça ikinci kez değiştirilemez (artık en son parça değil)
    assert c.post("/api/replace", json={"chunk": r["chunk"], "text": "x"}, headers=H).status_code == 409


def test_refine_failure_falls_back_to_raw(typed):
    r = client(refiner=FakeRefiner(fail=True)).post("/api/transcribe?mode=short", content=b"x", headers=H).json()
    assert r["text"] == "ııı merhaba merhaba" and "429" in r["refine_error"]
    assert typed == ["ııı merhaba merhaba "]


def test_no_refiner_reports_reason(typed):
    c = client(refiner=None, err="GEMINI_API_KEY tanımlı değil")
    assert c.get("/api/ping", headers=H).json()["refine_model"] is None
    r = c.post("/api/transcribe?mode=clean", content=b"x", headers=H).json()
    assert r["refine_error"].startswith("GEMINI_API_KEY") and typed == ["ııı merhaba merhaba "]


def test_undo_and_auth(typed):
    c = client()
    assert c.post("/api/undo", headers={"x-token": "yanlis"}).status_code == 401
    c.post("/api/transcribe", content=b"x", headers=H)
    assert c.post("/api/undo", headers=H).json()["deleted"] == len("ııı merhaba merhaba ")


def test_every_api_endpoint_requires_token(typed):
    c = client()
    for method, path in [("get", "/api/ping"), ("post", "/api/transcribe"), ("post", "/api/type"),
                         ("post", "/api/replace"), ("post", "/api/key/enter"), ("post", "/api/undo")]:
        assert getattr(c, method)(path).status_code == 401, path
    assert typed == []


def test_oversized_audio_rejected(typed, monkeypatch):
    monkeypatch.setattr(main, "MAX_AUDIO_BYTES", 10)
    r = client().post("/api/transcribe", content=b"x" * 11, headers=H)
    assert r.status_code == 413 and typed == []


def test_setup_page_never_leaks_token(tmp_path):
    (tmp_path / "ca.crt").write_bytes(b"CA")
    c = TestClient(main.create_setup_app(tmp_path / "ca.crt", "https://192.168.0.2:8443/"))
    page = c.get("/").text
    assert "https://192.168.0.2:8443/" in page and "#t=" not in page
    assert c.post("/api/type", json={"text": "x"}).status_code in (404, 405)
