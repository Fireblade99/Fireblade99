"""Generates the JWT key pair for the Qlik virtual proxy (replacement for the openssl command)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def generate_jwt_keys(out_dir: str, days: int = 730, common_name: str = "qlik-gateway", force: bool = False):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    key_path, cert_path = out / "qlik_jwt_private.pem", out / "qlik_jwt_public.crt"
    if key_path.exists() and not force:
        raise SystemExit(f"{key_path} already exists (use --force to overwrite)")

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path, cert.not_valid_after_utc


if __name__ == "__main__":
    import sys

    k, c, until = generate_jwt_keys(sys.argv[1] if len(sys.argv) > 1 else ".")
    print(f"private key: {k}\ncertificate: {c}\nvalid until: {until:%Y-%m-%d}")
