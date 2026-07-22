"""Encryption envelope. The browser half is verified against Node's WebCrypto
separately; these cover the Python side and the properties the scheme relies on."""

from __future__ import annotations

import base64
import json
import sys

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))

pytest.importorskip("cryptography")

from encrypt_report import ITERATIONS, decrypt, encrypt  # noqa: E402

PLAIN = json.dumps(
    {"portfolio_value": 98765.43, "tickers": [{"ticker": "MSFT", "signal": "SELL"}]}
).encode()
PASS = "correct-horse-battery-staple-42"


def test_round_trip():
    assert decrypt(encrypt(PLAIN, PASS), PASS) == PLAIN


def test_wrong_passphrase_raises_rather_than_returning_garbage():
    envelope = encrypt(PLAIN, PASS)
    with pytest.raises(Exception):
        decrypt(envelope, PASS + "x")


def test_tampering_is_detected():
    """AES-GCM authenticates: a flipped byte must fail, not decrypt to noise."""
    envelope = encrypt(PLAIN, PASS)
    raw = bytearray(base64.b64decode(envelope["ct"]))
    raw[5] ^= 0xFF
    envelope["ct"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(Exception):
        decrypt(envelope, PASS)


def test_no_plaintext_survives_in_the_envelope():
    blob = json.dumps(encrypt(PLAIN, PASS))
    for needle in ("MSFT", "portfolio_value", "98765"):
        assert needle not in blob


def test_salt_and_iv_are_fresh_every_run():
    """Reusing an AES-GCM nonce under one key is catastrophic; never cache these."""
    a, b = encrypt(PLAIN, PASS), encrypt(PLAIN, PASS)
    assert a["salt"] != b["salt"]
    assert a["iv"] != b["iv"]
    assert a["ct"] != b["ct"]


def test_envelope_carries_what_the_browser_needs():
    envelope = encrypt(PLAIN, PASS)
    assert envelope["kdf"] == "PBKDF2-SHA256"
    assert envelope["cipher"] == "AES-GCM"
    assert envelope["iterations"] == ITERATIONS
    assert len(base64.b64decode(envelope["iv"])) == 12  # AES-GCM nonce size
    assert len(base64.b64decode(envelope["salt"])) == 16


def test_iteration_count_is_not_weakened_by_accident():
    assert ITERATIONS >= 600_000
