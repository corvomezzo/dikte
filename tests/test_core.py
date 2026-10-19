import ssl
import sys

import pytest
from cryptography import x509

from server import certs, inject
from server.stt import clean_text


def _units(events):
    return [e.u.ki.wScan for e in events if not (e.u.ki.dwFlags & inject.KEYEVENTF_KEYUP)]


def test_turkish_chars_are_unicode_units():
    assert _units(inject.build_inputs("çğış")) == [ord(c) for c in "çğış"]


def test_newline_becomes_enter_and_surrogates_split():
    ev = inject.build_inputs("a\n😀")
    downs = [e for e in ev if not (e.u.ki.dwFlags & inject.KEYEVENTF_KEYUP)]
    assert downs[1].u.ki.wVk == inject.VK_RETURN
    assert [d.u.ki.wScan for d in downs[2:]] == [0xD83D, 0xDE00]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows yapısı")
def test_input_struct_size_matches_win64():
    import ctypes
    assert ctypes.sizeof(inject.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_history_undo_last_and_all():
    h = inject.TypedHistory()
    h.push(1, "Merhaba dünya. ")
    h.push(1, "\n")
    h.push(1, "İkinci 😀 ")
    assert h.take(1, everything=False) == len("İkinci 😀 ")  # emoji tek karakter sayılır
    assert h.take(1, everything=True) == len("Merhaba dünya. ") + 1
    assert h.take(1, everything=True) == 0


def test_history_refuses_other_window_and_tracks_backspace():
    h = inject.TypedHistory()
    h.push(1, "birinci ")
    h.push(2, "ikinci ")
    assert h.take(1, everything=True) == 0  # son parça başka pencerede: dokunma
    h.on_backspace()
    assert h.take(2, everything=True) == len("ikinci ") - 1
    assert h.take(1, everything=False) == len("birinci ")


def test_history_replace_only_last_chunk():
    h = inject.TypedHistory()
    a = h.push(1, "ilk ")
    b = h.push(1, "ikinci ")
    assert h.take_if_last(1, a) is None  # en son değil
    assert h.take_if_last(2, b) is None  # pencere değişti
    assert h.take_if_last(1, b) == len("ikinci ")


def test_pick_model_prefers_newest_stable_flash_lite():
    from server.refine import pick_model
    names = ["models/gemini-2.5-flash", "models/gemini-2.5-flash-lite", "models/gemini-3.5-flash-lite",
             "models/gemini-3.5-flash-lite-preview-09", "models/gemini-3.5-flash-lite-001",
             "models/gemini-3.5-flash-tts", "models/gemma-3-27b-it"]
    assert pick_model(names) == "gemini-3.5-flash-lite"
    assert pick_model(["models/gemini-3-flash", "models/gemini-2.5-flash"]) == "gemini-3-flash"


@pytest.mark.parametrize("raw,expected", [
    ("  Merhaba,   dünya. ", "Merhaba, dünya."),
    ("Altyazı M.K.", ""),
    ("Abone olmayı unutmayın!", ""),
    ("İzlediğiniz için teşekkürler.", ""),
    ("[Müzik]", ""),
    ("Teşekkürler, yarın görüşürüz.", "Teşekkürler, yarın görüşürüz."),
])
def test_clean_text(raw, expected):
    assert clean_text(raw) == expected


def test_certs_chain_and_sans(tmp_path):
    ca_p, cert_p, key_p = certs.ensure_certs(tmp_path, ["192.168.0.12"])
    leaf = x509.load_pem_x509_certificate(cert_p.read_bytes())
    ca = x509.load_pem_x509_certificate(ca_p.read_bytes())
    leaf.verify_directly_issued_by(ca)
    ips = [str(i) for i in leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
           .value.get_values_for_type(x509.IPAddress)]
    assert "192.168.0.12" in ips
    assert (leaf.not_valid_after_utc - leaf.not_valid_before_utc).days <= 825  # iOS sınırı
    ssl.create_default_context().load_cert_chain(cert_p, key_p)

    # Aynı CA korunur, IP değişince yalnızca sunucu sertifikası yenilenir
    ca_before = ca_p.read_bytes()
    certs.ensure_certs(tmp_path, ["10.0.0.5"])
    assert ca_p.read_bytes() == ca_before


def test_ca_cannot_sign_public_names(tmp_path):
    """ca.key sızsa bile internet adları için üretilen sertifika doğrulanmamalı (NameConstraints)."""
    import datetime as dt
    import ipaddress

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

    ca_p, cert_p, _ = certs.ensure_certs(tmp_path, ["192.168.0.12"])
    ca = x509.load_pem_x509_certificate(ca_p.read_bytes())
    ca_key = serialization.load_pem_private_key((tmp_path / "ca.key").read_bytes(), password=None)
    verifier = lambda name: PolicyBuilder().store(Store([ca])).build_server_verifier(name)  # noqa: E731

    verifier(x509.IPAddress(ipaddress.ip_address("192.168.0.12"))).verify(
        x509.load_pem_x509_certificate(cert_p.read_bytes()), [])

    def forge(san):
        key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.timezone.utc)
        return (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")]))
                .issuer_name(ca.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
                .add_extension(x509.SubjectAlternativeName([san]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                               critical=False)
                .sign(ca_key, hashes.SHA256()))

    for san in (x509.DNSName("bank.example.com"), x509.IPAddress(ipaddress.ip_address("8.8.8.8"))):
        name = san if isinstance(san, x509.IPAddress) else x509.DNSName(san.value)
        with pytest.raises(VerificationError):
            verifier(name).verify(forge(san), [])
