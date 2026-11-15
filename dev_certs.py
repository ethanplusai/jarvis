"""
dev_certs.py — make sure the self-signed localhost certificate exists.

The certs look optional (server.py serves plain HTTP without them) but are
not: `frontend/vite.config.ts` proxies `/api` and `/ws` to
`https://localhost:8340`, and `clap_listener.py` posts to
`https://127.0.0.1:8340`. Against a plain-HTTP backend the page still loads
from Vite, while every proxied request returns 500, so it reads as "the UI is
up but nothing works" with no error anywhere. Generating the pair on first
start removes that trap. `--no-ssl` on server.py still serves plain HTTP on
purpose.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("jarvis.dev_certs")

OPENSSL_ARGS = [
    "req", "-x509", "-newkey", "rsa:2048", "-days", "365", "-nodes",
    "-subj", "/CN=localhost",
]


def ensure_dev_certs(cert: Path, key: Path, *, timeout: float = 30.0) -> bool:
    """True if `cert` and `key` exist afterwards, generating them if needed."""
    if cert.exists() and key.exists():
        return True
    openssl = shutil.which("openssl")
    if not openssl:
        log.error("openssl not found; cannot create cert.pem/key.pem")
        return False
    try:
        r = subprocess.run(
            [openssl, *OPENSSL_ARGS, "-keyout", str(key), "-out", str(cert)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.error(f"openssl failed to run: {e}")
        return False
    if r.returncode != 0:
        log.error("openssl failed (%s): %s", r.returncode,
                  r.stderr.decode(errors="replace").strip()[:300])
        return False
    ok = cert.exists() and key.exists()
    if ok:
        log.info(f"created self-signed localhost certificate: {cert.name}, {key.name}")
    return ok
