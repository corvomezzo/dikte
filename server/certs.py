"""Yerel kök CA + sunucu sertifikası üretir.

iOS Safari mikrofon (getUserMedia) için güvenilir HTTPS ister. Self-signed bir sunucu
sertifikası yetmez; bu yüzden bir kök CA üretip iPhone'a profil olarak yüklüyoruz,
sunucu sertifikasını da o CA ile imzalıyoruz. LAN IP değişirse sunucu sertifikası
(CA aynı kalarak) yeniden üretilir, telefonda tekrar kurulum gerekmez.

iOS kuralları: sunucu sertifikası ≤ 825 gün, SAN zorunlu, EKU serverAuth zorunlu.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_DAYS = 3650
LEAF_DAYS = 800

# CA'nın imzalayabileceği adlar: yalnızca yerel/özel ağ adresleri. Telefon bu CA'ya "tam güven" verdiği
# için, ca.key sızsa bile kimse onunla internetteki bir site (ör. banka) için geçerli sertifika üretemesin.
PERMITTED_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
                  "100.64.0.0/10", "fc00::/7", "fe80::/10", "::1/128"]
PERMITTED_DNS = ["localhost", "local", "lan", "home.arpa"]


def lan_ips() -> list[str]:
    """Bu makinenin IPv4 adresleri (loopback/link-local hariç). Sanal adaptörler de gelir;
    sertifikaya fazladan IP koymanın zararı yok."""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except socket.gaierror:
        pass
    # Varsayılan rotanın kaynak adresi = asıl LAN IP'si
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


def primary_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def _load_or_create_ca(d: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    cert_p, key_p = d / "ca.crt", d / "ca.key"
    if cert_p.exists() and key_p.exists():
        cert = x509.load_pem_x509_certificate(cert_p.read_bytes())
        key = serialization.load_pem_private_key(key_p.read_bytes(), password=None)
        return cert, key  # type: ignore[return-value]

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, f"Dikte Yerel CA ({socket.gethostname()})"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Dikte"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - dt.timedelta(days=1))
        .not_valid_after(_now() + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=[x509.IPAddress(ipaddress.ip_network(n)) for n in PERMITTED_NETS]
                + [x509.DNSName(n) for n in PERMITTED_DNS],
                excluded_subtrees=None,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(key_p, key)
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert, key


def ensure_certs(d: Path, hosts: list[str]) -> tuple[Path, Path, Path]:
    """(ca.crt, server.crt, server.key) yollarını döner; gerekirse üretir."""
    d.mkdir(parents=True, exist_ok=True)
    ca_cert, ca_key = _load_or_create_ca(d)

    hosts = sorted(set(hosts) | {"127.0.0.1", "localhost"})
    meta_p, cert_p, key_p = d / "server.json", d / "server.crt", d / "server.key"
    if cert_p.exists() and key_p.exists() and meta_p.exists():
        meta = json.loads(meta_p.read_text())
        leaf = x509.load_pem_x509_certificate(cert_p.read_bytes())
        if meta.get("hosts") == hosts and leaf.not_valid_after_utc - _now() > dt.timedelta(days=30):
            return d / "ca.crt", cert_p, key_p

    sans: list[x509.GeneralName] = []
    for h in hosts:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            sans.append(x509.DNSName(h))

    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Dikte sunucusu")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - dt.timedelta(days=1))
        .not_valid_after(_now() + dt.timedelta(days=LEAF_DAYS))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(key_p, key)
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    meta_p.write_text(json.dumps({"hosts": hosts}))
    return d / "ca.crt", cert_p, key_p
