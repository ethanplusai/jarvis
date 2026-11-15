"""dev_certs.ensure_dev_certs: generate the localhost pair once, never clobber it.

`openssl` is faked at the subprocess seam, like every other binary here.
"""
import subprocess
from types import SimpleNamespace

import dev_certs


def _fake_openssl(calls, *, rc=0, write=True):
    def run(args, **kwargs):
        calls.append(args)
        if write and rc == 0:
            key = args[args.index("-keyout") + 1]
            cert = args[args.index("-out") + 1]
            open(key, "w").write("key")
            open(cert, "w").write("cert")
        return SimpleNamespace(returncode=rc, stderr=b"boom")
    return run


def test_existing_pair_is_left_alone(tmp_path, monkeypatch):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_text("mine"); key.write_text("mine")
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_openssl(calls))
    assert dev_certs.ensure_dev_certs(cert, key) is True
    assert calls == []
    assert cert.read_text() == "mine"


def test_missing_pair_is_generated_for_localhost(tmp_path, monkeypatch):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    calls = []
    monkeypatch.setattr(dev_certs.shutil, "which", lambda name: "/usr/bin/openssl")
    monkeypatch.setattr(subprocess, "run", _fake_openssl(calls))
    assert dev_certs.ensure_dev_certs(cert, key) is True
    assert len(calls) == 1
    assert "/CN=localhost" in calls[0]
    assert cert.exists() and key.exists()


def test_openssl_failure_reports_false(tmp_path, monkeypatch):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    monkeypatch.setattr(dev_certs.shutil, "which", lambda name: "/usr/bin/openssl")
    monkeypatch.setattr(subprocess, "run", _fake_openssl([], rc=1))
    assert dev_certs.ensure_dev_certs(cert, key) is False


def test_no_openssl_reports_false(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_certs.shutil, "which", lambda name: None)
    assert dev_certs.ensure_dev_certs(tmp_path / "c", tmp_path / "k") is False
