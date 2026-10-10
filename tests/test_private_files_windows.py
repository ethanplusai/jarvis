"""JARVIS's private files are private on Windows too.

`ensure_tool_token` creates the token at mode 0600 and `backup` chmods the
archive — and on Windows both are inert: the files inherit the folder's ACL,
and on this machine that granted `NT AUTHORITY\\Authenticated Users` Modify
on the token and the database. `data_paths.restrict_to_owner` is the
Windows answer (icacls: inheritance off, owner + SYSTEM + Administrators
only), applied to the data directory at startup and to the token and every
archive as they are made; `preflight` says so when it has not happened.

Measured 2026-09-24: the first version ran icacls with `/T`, which applies
the directory grant `(OI)(CI)F` to every file in the subtree — and icacls
silently drops a grant with inheritance flags on a file, leaving the file
with an empty DACL that admits nobody. The lockout probe then fired and the
whole thing rolled back on every start, on every account. The restriction
is applied to the directory alone now; Windows propagates it to everything
under it that inherits, and the few things that do not are restricted on
their own.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import data_paths
import preflight

WINDOWS = sys.platform == "win32"
_windows_only = pytest.mark.skipif(not WINDOWS, reason="icacls is the Windows ACL tool")
_posix_only = pytest.mark.skipif(WINDOWS, reason="POSIX modes have no meaning on Windows")

BROAD = ("Authenticated Users", "Everyone", "BUILTIN\\Users")
SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
AUTHENTICATED_USERS = "S-1-5-11"
NETWORK_SERVICE = "S-1-5-20"


def _icacls(path: Path, *args: str) -> str:
    result = subprocess.run(["icacls", str(path), *args], capture_output=True, text=True, timeout=30)
    return result.stdout + result.stderr


@pytest.fixture
def tmp_path(tmp_path):
    """On Windows, a folder that inherits the way a real data folder does.

    pytest makes its temp folders with `os.mkdir(..., 0o700)`, which CPython
    implements on Windows with the descriptor `D:P(...)`: protected, but
    without SE_DACL_AUTO_INHERITED. What is created under it carries the
    entries as inherited (`ID`) in a DACL not marked auto-inherited, and
    icacls is not consistent about that across Windows builds. Measured on
    the GitHub Windows runner, 2026-10-10: it shows no `(I)` there and
    `/inheritance:r` keeps the entries as if explicit, so every test of
    "inheritance off" failed; on Windows 11 26200 the same SDDL reads as
    `(I)`. A folder whose ACL icacls itself wrote (`D:PAI`) behaves the same
    on both, as a profile or repository folder does.
    """
    if not WINDOWS:
        return tmp_path
    root = tmp_path / "acl-root"
    root.mkdir()
    sid = data_paths._current_sid()
    _icacls(root, "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "/grant:r", f"*{SYSTEM}:(OI)(CI)F",
            "/grant:r", f"*{ADMINISTRATORS}:(OI)(CI)F")
    return root


def _readable(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            f.read()
        return True
    except OSError:
        return False


def _tree(root: Path) -> list[Path]:
    (root / "jarvis.db").write_bytes(b"db")
    (root / "jarvis").mkdir()
    (root / "jarvis" / "tool-token").write_text("secret", encoding="utf-8")
    (root / "jarvis" / "memory").mkdir()
    (root / "jarvis" / "memory" / "2026-09-24.md").write_text("a memory", encoding="utf-8")
    (root / "backups").mkdir()
    (root / "backups" / "jarvis-2026-09-24.tar.gz").write_bytes(b"PK")
    return [p for p in root.rglob("*") if p.is_file()]


# --- restrict_to_owner --------------------------------------------------------

@_windows_only
def test_a_restricted_directory_no_longer_admits_every_authenticated_user(tmp_path):
    private = tmp_path / "data"
    private.mkdir()
    (private / "tool-token").write_text("secret", encoding="utf-8")
    assert "(I)" in _icacls(private), "precondition: a fresh directory inherits the ACL above it"

    assert data_paths.restrict_to_owner(private) is True

    listing = _icacls(private)
    assert "(I)" not in listing, f"inheritance is off; every ACE is explicit:\n{listing}"
    assert not any(b in listing for b in BROAD), listing
    # The RUNNING account keeps access — by SID, which is what survives a
    # service token whose name resolves elsewhere.
    sid = data_paths._current_sid()
    assert sid and sid.startswith("S-1-")
    assert sid in listing or os.environ["USERNAME"].lower() in listing.lower(), listing
    assert SYSTEM in listing or "SYSTEM" in listing
    assert data_paths.foreign_entries(private) == []
    assert (private / "tool-token").read_text(encoding="utf-8") == "secret", "and still readable by the owner"


@_windows_only
def test_every_file_under_the_directory_stays_readable_and_becomes_private(tmp_path):
    """The bug: `/T` gave every file an empty DACL. Nothing under the root
    may lose the owner, and nothing under it may keep a stranger."""
    root = tmp_path / "data"
    root.mkdir()
    files = _tree(root)
    # Not every machine's temp folder is broad (this one's is owner-only);
    # the breadth the bug needs is staged explicitly, on the root, inherited.
    _icacls(root, "/grant", f"*{AUTHENTICATED_USERS}:(OI)(CI)M")
    assert data_paths.foreign_entries(root), "precondition: the tree inherits a broad ACL"

    assert data_paths.restrict_to_owner(root) is True

    unreadable = [p for p in files if not _readable(p)]
    assert unreadable == [], f"locked out of: {unreadable}"
    assert data_paths.foreign_entries(root) == []


@_windows_only
def test_a_protected_descendant_with_a_broad_acl_is_restricted_on_its_own(tmp_path):
    """Propagation stops at an object whose inheritance is off. Such an
    object must be restricted by itself — with a file grant for a file."""
    root = tmp_path / "data"
    root.mkdir()
    _tree(root)
    stray_dir = root / "restored"
    stray_dir.mkdir()
    (stray_dir / "notes.md").write_text("restored from somewhere", encoding="utf-8")
    stray_file = root / "backups" / "old.tar.gz"
    stray_file.write_bytes(b"PK")
    sid = data_paths._current_sid()
    _icacls(stray_dir, "/inheritance:r", "/grant:r", f"*{AUTHENTICATED_USERS}:(OI)(CI)F", "/grant:r", f"*{sid}:(OI)(CI)F")
    _icacls(stray_file, "/inheritance:r", "/grant:r", f"*{AUTHENTICATED_USERS}:F", "/grant:r", f"*{sid}:F")
    assert {p for p, _ in data_paths.foreign_entries(root)} >= {stray_dir, stray_file}

    assert data_paths.restrict_to_owner(root) is True

    assert data_paths.foreign_entries(root) == []
    assert _readable(stray_dir / "notes.md")
    assert _readable(stray_file)


@_windows_only
def test_a_restricted_file_is_restricted_on_its_own(tmp_path):
    archive = tmp_path / "jarvis.zip"
    archive.write_bytes(b"PK")
    assert data_paths.restrict_to_owner(archive) is True
    listing = _icacls(archive)
    assert "(I)" not in listing and not any(b in listing for b in BROAD), listing
    assert archive.read_bytes() == b"PK"


@_windows_only
def test_a_lockout_is_rolled_back_only_where_the_restriction_was_applied(tmp_path, monkeypatch):
    """The probe that opens a file afterwards is the safety net for an
    account that genuinely cannot hold access (a restricted token). Rolling
    back must undo what this call did — the root, and any descendant it
    restricted — and nothing else: a token that was already private stays
    private."""
    root = tmp_path / "data"
    root.mkdir()
    _tree(root)
    token = root / "jarvis" / "tool-token"
    assert data_paths.restrict_to_owner(token) is True
    assert "(I)" not in _icacls(token)

    monkeypatch.setattr(data_paths, "_still_accessible", lambda path: False)
    assert data_paths.restrict_to_owner(root) is False

    assert "(I)" in _icacls(root), "the root inherits again"
    assert _readable(root / "jarvis.db")
    token_listing = _icacls(token)
    assert "(I)" not in token_listing and not any(b in token_listing for b in BROAD), token_listing
    assert token.read_text(encoding="utf-8") == "secret"


@_posix_only
def test_restrict_to_owner_is_the_chmod_everyone_already_had(tmp_path):
    private = tmp_path / "data"
    private.mkdir()
    f = private / "tool-token"
    f.write_text("secret", encoding="utf-8")
    assert data_paths.restrict_to_owner(private) is True
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert data_paths.restrict_to_owner(f) is True
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_restrict_to_owner_never_raises_for_a_missing_path(tmp_path):
    assert data_paths.restrict_to_owner(tmp_path / "nope") is False


def test_the_data_root_is_hardened_at_startup_and_the_token_when_made(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(data_paths, "restrict_to_owner", lambda p: calls.append(Path(p)) or True)
    data_paths.harden_private_root()
    data_paths.ensure_tool_token()
    assert tmp_path.resolve() in [c.resolve() for c in calls]
    assert data_paths.tool_token_path().resolve() in [c.resolve() for c in calls]


def test_the_server_hardens_before_it_checks():
    """Order matters: the check would warn on every first start otherwise."""
    src = (Path(__file__).parent.parent / "server.py").read_text(encoding="utf-8")
    assert src.index("data_paths.harden_private_root") < src.index("preflight.run_checks()")


def test_backups_are_restricted_as_they_are_made():
    src = (Path(__file__).parent.parent / "maintenance.py").read_text(encoding="utf-8")
    assert "restrict_to_owner(destination)" in src


def test_the_restriction_never_recurses_with_icacls():
    """`/T` is the bug: it hands a directory grant to every file. The
    subtree is covered by inheritance from the root, and by restricting
    the protected stragglers one at a time."""
    src = (Path(__file__).parent.parent / "data_paths.py").read_text(encoding="utf-8")
    assert '"/T"' not in src


ORPHAN = "S-1-5-21-1-2-3-4567"      # an account that never existed here


def _grant_orphan(path: Path, inheritable: bool) -> None:
    """An explicit entry for a SID this machine cannot resolve — what a
    deleted account, or a folder copied from another machine, leaves behind.
    icacls refuses to touch such a SID in either direction; .NET does not."""
    flags = "ContainerInherit,ObjectInherit" if inheritable else "None"
    script = (f"$acl = Get-Acl -LiteralPath '{path}'; "
              f"$sid = New-Object System.Security.Principal.SecurityIdentifier('{ORPHAN}'); "
              f"$rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'Read', '{flags}', 'None', 'Allow'); "
              f"$acl.AddAccessRule($rule); Set-Acl -LiteralPath '{path}' -AclObject $acl")
    # Windows PowerShell 5.1, so without the PSModulePath a PowerShell 7
    # parent hands down (a GitHub Actions step, a pwsh terminal): with it,
    # 5.1 tries to load 7's Microsoft.PowerShell.Security and has no Get-Acl.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   capture_output=True, text=True, timeout=60, check=True, env=env)


@_windows_only
def test_an_orphaned_sid_inherited_from_above_does_not_stop_the_restriction(tmp_path):
    """The live case: the data folder inherited entries for two accounts the
    machine could no longer name. Inheritance off is all it takes; asking
    icacls to remove them by SID is what failed."""
    above = tmp_path / "above"     # not tmp_path itself: Set-Acl on it wants SeSecurityPrivilege
    above.mkdir()
    _grant_orphan(above, inheritable=True)
    root = above / "data"
    root.mkdir()
    _tree(root)
    assert any(ORPHAN in names for _, names in data_paths.foreign_entries(root))

    assert data_paths.restrict_to_owner(root) is True

    assert data_paths.foreign_entries(root) == []
    assert _readable(root / "jarvis" / "tool-token")


@_windows_only
def test_an_explicit_entry_for_an_orphaned_sid_is_removed(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    _tree(root)
    _grant_orphan(root, inheritable=True)
    _grant_orphan(root / "jarvis" / "tool-token", inheritable=False)
    assert {p for p, names in data_paths.foreign_entries(root) if ORPHAN in names} >= {root, root / "jarvis" / "tool-token"}

    assert data_paths.restrict_to_owner(root) is True

    assert data_paths.foreign_entries(root) == []
    assert _readable(root / "jarvis" / "tool-token")


# --- reading a DACL without parsing icacls -----------------------------------

@_windows_only
def test_dacl_sids_are_the_principals_an_object_admits(tmp_path):
    import windows_acl
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    sids = windows_acl.dacl_sids(f)
    assert sids and all(s.startswith("S-1-") for s in sids), sids
    sid = windows_acl.current_user_sid()
    _icacls(f, "/inheritance:r", "/grant:r", f"*{sid}:F")
    assert windows_acl.dacl_sids(f) == [sid]


@_windows_only
def test_the_current_user_sid_is_what_whoami_says():
    import windows_acl
    whoami = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "whoami.exe")
    out = subprocess.run([whoami, "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15).stdout
    expected = [p.strip().strip('"') for p in out.strip().split(",")][-1]
    assert expected.startswith("S-1-")
    assert windows_acl.current_user_sid() == expected
    assert data_paths._current_sid() == expected


@_windows_only
def test_a_well_known_sid_gets_its_name_and_an_orphan_keeps_its_sid():
    import windows_acl
    assert windows_acl.principal_name(SYSTEM) not in ("", SYSTEM)
    orphan = "S-1-5-21-1-2-3-4567"
    assert windows_acl.principal_name(orphan) == orphan


@_windows_only
def test_restrict_and_reset_write_the_dacl_whole(tmp_path):
    import windows_acl
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    sid = windows_acl.current_user_sid()
    windows_acl.restrict(f, [sid, SYSTEM])
    listing = _icacls(f)
    assert "(I)" not in listing and "(F)" in listing, listing
    assert windows_acl.dacl_sids(f) == [sid, SYSTEM]
    assert _readable(f)
    windows_acl.reset(f)
    assert "(I)" in _icacls(f), "inheriting again"
    assert _readable(f)


@_windows_only
def test_a_deny_entry_is_not_an_admission(tmp_path):
    import windows_acl
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    sid = windows_acl.current_user_sid()
    _icacls(f, "/inheritance:r", "/grant:r", f"*{sid}:F", "/deny", f"*{AUTHENTICATED_USERS}:W")
    assert windows_acl.dacl_sids(f) == [sid]


@_windows_only
def test_foreign_entries_names_everyone_who_is_not_this_account(tmp_path):
    """Not only the three broad groups: any other account, group or orphaned
    SID on the ACL is somebody else. SYSTEM and Administrators belong to the
    machine and are allowed."""
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    sid = data_paths._current_sid()
    _icacls(f, "/inheritance:r", "/grant:r", f"*{sid}:F", "/grant:r", f"*{SYSTEM}:F",
            "/grant:r", f"*{ADMINISTRATORS}:F", "/grant:r", f"*{NETWORK_SERVICE}:R")
    entries = data_paths.foreign_entries(tmp_path)
    assert entries and any(p == f and "NETWORK SERVICE" in " ".join(names).upper()
                           for p, names in entries), entries
    _icacls(f, "/remove:g", f"*{NETWORK_SERVICE}")
    assert not any(p == f for p, _ in data_paths.foreign_entries(tmp_path))


@_windows_only
def test_an_explicit_entry_for_somebody_else_is_removed_not_kept(tmp_path):
    """`/inheritance:r` drops inherited entries and `/grant:r` replaces our
    own; an entry granted to another account explicitly survives both
    unless it is removed by SID."""
    private = tmp_path / "data"
    private.mkdir()
    (private / "tool-token").write_text("secret", encoding="utf-8")
    _icacls(private, "/grant", f"*{NETWORK_SERVICE}:(OI)(CI)R")
    assert data_paths.restrict_to_owner(private) is True
    assert data_paths.foreign_entries(private) == []
    assert (private / "tool-token").read_text(encoding="utf-8") == "secret"


# --- preflight ----------------------------------------------------------------

def _windows_check(monkeypatch, entries):
    """The check's wording, on any host. Both OS reads are faked — the ACL
    and the account's SID, which the remedy's icacls command names; the
    real SID comes from the Windows token API (`ctypes.WinDLL`), which only
    exists on Windows and is tested on its own above."""
    monkeypatch.setattr(preflight.sys, "platform", "win32")
    monkeypatch.setattr(data_paths, "foreign_entries", lambda root: entries)
    monkeypatch.setattr(data_paths, "_current_sid", lambda: "S-1-5-21-1-2-3-1001")
    return preflight._check_private_files_sync()


def test_a_broad_acl_on_the_data_directory_is_a_warning_with_the_command(monkeypatch):
    root = data_paths.data_dir()
    check = _windows_check(monkeypatch, [(root, ["NT AUTHORITY\\Authenticated Users"])])
    assert check.status == preflight.STATUS_WARN
    assert check.name == "private_files"
    assert "Authenticated Users" in check.message
    assert "icacls" in check.remedy
    # A switch, not a substring: macOS's temp path is /var/folders/../T/..
    assert "/t" not in check.remedy.lower().split(), "the recursive command is the lockout"
    assert "(OI)(CI)" in check.remedy, "the directory grant, so what is under it inherits"
    assert '"*S-1-5-21-1-2-3-1001:(OI)(CI)F"' in check.remedy, "granted to this account by SID"


def test_a_readable_file_under_the_data_directory_is_a_warning_naming_it(monkeypatch):
    root = data_paths.data_dir()
    stray = root / "backups" / "old.tar.gz"
    check = _windows_check(monkeypatch, [(stray, ["Everyone"])])
    assert check.status == preflight.STATUS_WARN
    assert "old.tar.gz" in check.message and "Everyone" in check.message


def test_an_owner_only_tree_is_ok(monkeypatch):
    check = _windows_check(monkeypatch, [])
    assert check.status == preflight.STATUS_OK


def test_an_unreadable_acl_is_a_warning_not_a_crash(monkeypatch):
    monkeypatch.setattr(preflight.sys, "platform", "win32")
    def boom(root):
        raise OSError("access denied")
    monkeypatch.setattr(data_paths, "foreign_entries", boom)
    assert preflight._check_private_files_sync().status == preflight.STATUS_WARN


def test_off_windows_the_check_reads_the_mode_bits(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight.sys, "platform", "linux")
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(preflight, "_posix_mode", lambda path: 0o755)
    assert preflight._check_private_files_sync().status == preflight.STATUS_WARN
    monkeypatch.setattr(preflight, "_posix_mode", lambda path: 0o700)
    assert preflight._check_private_files_sync().status == preflight.STATUS_OK


def test_the_check_runs_at_startup():
    assert preflight._check_private_files_sync in preflight._SYNC_CHECKS


@_windows_only
def test_the_live_check_passes_on_a_hardened_directory(monkeypatch, tmp_path):
    """End to end, no seams: harden a directory the way the server does at
    start, then run the check the server runs next."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    _tree(tmp_path)
    _icacls(tmp_path, "/grant", f"*{AUTHENTICATED_USERS}:(OI)(CI)M")
    assert preflight._check_private_files_sync().status == preflight.STATUS_WARN
    assert data_paths.harden_private_root() is True
    check = preflight._check_private_files_sync()
    assert check.status == preflight.STATUS_OK, check.message
