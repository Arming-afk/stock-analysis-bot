#!/usr/bin/env python
"""Encrypt the report so a public URL discloses nothing without the passphrase.

    python tools/encrypt_report.py --in report.json --out latest.enc

The host (Cloudflare Pages, any static host) only ever sees ciphertext. The
browser derives the key from a passphrase the user types once and decrypts in
memory, so access control does not depend on the host enforcing anything.

Format is chosen to be decryptable by WebCrypto with no library:

    PBKDF2-HMAC-SHA256(passphrase, salt, iterations) -> 256-bit key
    AES-256-GCM(key, iv) -> ciphertext with a 128-bit tag appended

WebCrypto's AES-GCM expects the tag appended to the ciphertext, which is
exactly what AESGCM.encrypt() here produces, so the two sides agree without
any repacking.

Threat model, stated plainly: anyone can fetch the ciphertext, so the only
thing standing between them and the data is the passphrase. Use a long random
one. PBKDF2 at the iteration count below makes guessing expensive, not
impossible — it does not rescue a weak passphrase.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
from pathlib import Path

ITERATIONS = 600_000
SALT_BYTES = 16
IV_BYTES = 12  # 96-bit nonce, the size AES-GCM is defined for
VERSION = 1


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def encrypt(plaintext: bytes, passphrase: str, iterations: int = ITERATIONS) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt = secrets.token_bytes(SALT_BYTES)
    iv = secrets.token_bytes(IV_BYTES)

    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
    ).derive(passphrase.encode("utf-8"))

    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)

    return {
        "v": VERSION,
        "kdf": "PBKDF2-SHA256",
        "iterations": iterations,
        "cipher": "AES-GCM",
        "salt": _b64(salt),
        "iv": _b64(iv),
        "ct": _b64(ciphertext),
    }


def decrypt(envelope: dict, passphrase: str) -> bytes:
    """Round-trip check. The browser is the real consumer."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt = base64.b64decode(envelope["salt"])
    iv = base64.b64decode(envelope["iv"])
    ciphertext = base64.b64decode(envelope["ct"])

    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=int(envelope["iterations"]),
    ).derive(passphrase.encode("utf-8"))

    return AESGCM(key).decrypt(iv, ciphertext, None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Encrypt a report for a public host")
    parser.add_argument("--in", dest="src", required=True)
    parser.add_argument("--out", dest="dst", required=True)
    parser.add_argument("--iterations", type=int, default=ITERATIONS)
    args = parser.parse_args()

    passphrase = os.getenv("DASHBOARD_PASSPHRASE")
    if not passphrase:
        print("DASHBOARD_PASSPHRASE is not set", file=sys.stderr)
        return 1
    if len(passphrase) < 12:
        print("DASHBOARD_PASSPHRASE is shorter than 12 characters — refusing", file=sys.stderr)
        return 1

    plaintext = Path(args.src).read_bytes()
    envelope = encrypt(plaintext, passphrase, args.iterations)

    # Never ship something that cannot be read back.
    if decrypt(envelope, passphrase) != plaintext:
        print("round-trip verification failed — refusing to write", file=sys.stderr)
        return 1

    Path(args.dst).write_text(json.dumps(envelope), encoding="utf-8")
    print(
        f"encrypted {len(plaintext):,} bytes -> {args.dst} "
        f"({args.iterations:,} PBKDF2 iterations, round-trip verified)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
