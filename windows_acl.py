"""Read NTFS security the way the kernel evaluates it: by SID, from the DACL.

`icacls` prints principals by name in the system language and cannot tell a
path with a space in it from the principal that follows, so its listing is
not something to parse. It also refuses to grant or remove a SID the machine
cannot name — an account that was deleted, or a folder that came from
another machine — so it cannot clear such an entry either. These calls go
to advapi32 instead: SID strings in, SID strings out, the same on every
machine in every language, and a DACL written whole rather than edited.

Windows only. Importing this module elsewhere is harmless; calling it is not.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_ACCESS_ALLOWED_ACE_TYPE = 0
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_ACL_REVISION = 2
_FILE_ALL_ACCESS = 0x1F01FF
_OBJECT_INHERIT_ACE = 0x1
_CONTAINER_INHERIT_ACE = 0x2
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000

EVERYONE = "S-1-1-0"


class _ACL(ctypes.Structure):
    _fields_ = [("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte),
                ("AclSize", ctypes.c_ushort), ("AceCount", ctypes.c_ushort),
                ("Sbz2", ctypes.c_ushort)]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte),
                ("AceSize", ctypes.c_ushort)]


class _ACCESS_ALLOWED_ACE(ctypes.Structure):
    _fields_ = [("Header", _ACE_HEADER), ("Mask", ctypes.c_uint32),
                ("SidStart", ctypes.c_uint32)]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]


_libs: dict[str, ctypes.WinDLL] = {}


def _advapi32():
    if "advapi32" not in _libs:
        lib = ctypes.WinDLL("advapi32", use_last_error=True)
        lib.GetNamedSecurityInfoW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.POINTER(_ACL)), ctypes.POINTER(ctypes.POINTER(_ACL)),
            ctypes.POINTER(ctypes.c_void_p)]
        lib.GetNamedSecurityInfoW.restype = ctypes.c_uint32
        lib.GetAce.argtypes = [ctypes.POINTER(_ACL), ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        lib.GetAce.restype = ctypes.c_int
        lib.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        lib.ConvertSidToStringSidW.restype = ctypes.c_int
        lib.ConvertStringSidToSidW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.ConvertStringSidToSidW.restype = ctypes.c_int
        lib.LookupAccountSidW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
        lib.LookupAccountSidW.restype = ctypes.c_int
        lib.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        lib.OpenProcessToken.restype = ctypes.c_int
        lib.GetTokenInformation.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32)]
        lib.GetTokenInformation.restype = ctypes.c_int
        lib.InitializeAcl.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        lib.InitializeAcl.restype = ctypes.c_int
        lib.AddAccessAllowedAceEx.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        lib.AddAccessAllowedAceEx.restype = ctypes.c_int
        lib.GetLengthSid.argtypes = [ctypes.c_void_p]
        lib.GetLengthSid.restype = ctypes.c_uint32
        lib.SetNamedSecurityInfoW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.SetNamedSecurityInfoW.restype = ctypes.c_uint32
        _libs["advapi32"] = lib
    return _libs["advapi32"]


def _kernel32():
    if "kernel32" not in _libs:
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        lib.LocalFree.argtypes = [ctypes.c_void_p]
        lib.LocalFree.restype = ctypes.c_void_p
        lib.GetCurrentProcess.argtypes = []
        lib.GetCurrentProcess.restype = ctypes.c_void_p
        lib.CloseHandle.argtypes = [ctypes.c_void_p]
        lib.CloseHandle.restype = ctypes.c_int
        _libs["kernel32"] = lib
    return _libs["kernel32"]


def _sid_string(psid) -> str:
    out = ctypes.c_wchar_p()
    if not _advapi32().ConvertSidToStringSidW(psid, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return out.value or ""
    finally:
        _kernel32().LocalFree(ctypes.cast(out, ctypes.c_void_p))


def security(path) -> tuple[str | None, list[str]]:
    """The owner of `path` and the SIDs its DACL admits, in ACL order without
    repeats. Deny entries admit nobody and are left out. A missing DACL means
    Windows admits everyone, and is reported as exactly that.

    Raises OSError when the security descriptor cannot be read.
    """
    adv, k32 = _advapi32(), _kernel32()
    owner = ctypes.c_void_p()
    dacl = ctypes.POINTER(_ACL)()
    descriptor = ctypes.c_void_p()
    rc = adv.GetNamedSecurityInfoW(
        str(path), _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if rc != 0:
        raise OSError(0, ctypes.FormatError(rc).strip(), str(path), rc)
    try:
        owner_sid = _sid_string(owner) if owner else None
        if not dacl:
            return owner_sid, [EVERYONE]
        admitted: list[str] = []
        for index in range(dacl.contents.AceCount):
            ace = ctypes.c_void_p()
            if not adv.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.cast(ace, ctypes.POINTER(_ACE_HEADER)).contents
            if header.AceType != _ACCESS_ALLOWED_ACE_TYPE:
                continue
            sid = _sid_string(ace.value + _ACCESS_ALLOWED_ACE.SidStart.offset)
            if sid not in admitted:
                admitted.append(sid)
        return owner_sid, admitted
    finally:
        k32.LocalFree(descriptor)


def dacl_sids(path) -> list[str]:
    """The SIDs `path` admits — see `security`."""
    return security(path)[1]


def current_user_sid() -> str:
    """The SID of the account this process runs as, from its own token."""
    adv, k32 = _advapi32(), _kernel32()
    token = ctypes.c_void_p()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = ctypes.c_uint32()
        adv.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token, _TOKEN_USER, buffer, needed.value, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.cast(buffer, ctypes.POINTER(_SID_AND_ATTRIBUTES)).contents
        return _sid_string(user.Sid)
    finally:
        k32.CloseHandle(token)


def principal_name(sid: str) -> str:
    """`DOMAIN\\Name` for a SID this machine can resolve; the SID itself for
    one it cannot (an account that no longer exists, or never did here)."""
    adv, k32 = _advapi32(), _kernel32()
    psid = ctypes.c_void_p()
    if not adv.ConvertStringSidToSidW(sid, ctypes.byref(psid)):
        return sid
    try:
        name_len, domain_len, use = ctypes.c_uint32(0), ctypes.c_uint32(0), ctypes.c_uint32(0)
        adv.LookupAccountSidW(None, psid, None, ctypes.byref(name_len), None, ctypes.byref(domain_len), ctypes.byref(use))
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
            return sid
        name = ctypes.create_unicode_buffer(name_len.value)
        domain = ctypes.create_unicode_buffer(domain_len.value)
        if not adv.LookupAccountSidW(None, psid, name, ctypes.byref(name_len), domain,
                                     ctypes.byref(domain_len), ctypes.byref(use)):
            return sid
        return f"{domain.value}\\{name.value}" if domain.value else name.value
    finally:
        k32.LocalFree(psid)


def restrict(path, sids: list[str]) -> None:
    """Give `path` a DACL of its own — inheritance off — that grants full
    control to exactly `sids` and nobody else. On a directory the entries
    are inheritable, so what is created under it is covered, and Windows
    carries them to what already inherits from it; on a file they are
    plain, because an entry with inheritance flags on a file is inert.

    Raises OSError when a SID is malformed or the descriptor cannot be set.
    """
    adv, k32 = _advapi32(), _kernel32()
    path = Path(path)
    flags = (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE) if path.is_dir() else 0
    psids: list[ctypes.c_void_p] = []
    try:
        for sid in sids:
            psid = ctypes.c_void_p()
            if not adv.ConvertStringSidToSidW(sid, ctypes.byref(psid)):
                raise ctypes.WinError(ctypes.get_last_error())
            psids.append(psid)
        size = ctypes.sizeof(_ACL) + sum(
            ctypes.sizeof(_ACCESS_ALLOWED_ACE) - ctypes.sizeof(ctypes.c_uint32) + adv.GetLengthSid(p)
            for p in psids)
        acl = ctypes.create_string_buffer(size)
        if not adv.InitializeAcl(acl, size, _ACL_REVISION):
            raise ctypes.WinError(ctypes.get_last_error())
        for psid in psids:
            if not adv.AddAccessAllowedAceEx(acl, _ACL_REVISION, flags, _FILE_ALL_ACCESS, psid):
                raise ctypes.WinError(ctypes.get_last_error())
        _set_dacl(path, acl, _PROTECTED_DACL_SECURITY_INFORMATION)
    finally:
        for psid in psids:
            k32.LocalFree(psid)


def reset(path) -> None:
    """Back to inheriting from the folder above, with no entries of its own
    — what `icacls /reset` does. Not recursive: resetting a directory
    reaches what inherits from it on its own."""
    size = ctypes.sizeof(_ACL)
    acl = ctypes.create_string_buffer(size)
    if not _advapi32().InitializeAcl(acl, size, _ACL_REVISION):
        raise ctypes.WinError(ctypes.get_last_error())
    _set_dacl(Path(path), acl, _UNPROTECTED_DACL_SECURITY_INFORMATION)


def _set_dacl(path: Path, acl, protection: int) -> None:
    rc = _advapi32().SetNamedSecurityInfoW(
        str(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION | protection, None, None, acl, None)
    if rc != 0:
        raise OSError(0, ctypes.FormatError(rc).strip(), str(path), rc)
