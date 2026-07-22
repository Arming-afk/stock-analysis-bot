#!/usr/bin/env python
"""Generate a VAPID key pair for Web Push, once, and print it for .env.

    python tools/gen_vapid.py

Keep the private key secret. Regenerating it invalidates every existing push
subscription — the phone has to re-enable alerts.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.output.push import generate_vapid_keys  # noqa: E402


def main() -> int:
    try:
        public, private = generate_vapid_keys()
    except ImportError:
        print("needs the `cryptography` package:  pip install pywebpush", file=sys.stderr)
        return 1

    print("Add these two lines to your .env:\n")
    print(f"VAPID_PUBLIC_KEY={public}")
    print(f"VAPID_PRIVATE_KEY={private}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
