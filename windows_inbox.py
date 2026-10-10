"""Bounded AF_UNIX delivery via Winsock on Python builds without AF_UNIX."""
import ctypes as c
import os
import time


def send(path: str, payload: bytes, timeout=5):
    if os.name != "nt":
        raise OSError("Windows inbox transport is unavailable")
    encoded = os.fsencode(path)
    if len(encoded) >= 108:
        raise OSError("Inbox socket path is too long")
    ws = c.WinDLL("ws2_32")
    socket_type = c.c_size_t
    class Address(c.Structure):
        _fields_ = [("family", c.c_ushort), ("path", c.c_char * 108)]
    class FDSet(c.Structure):
        _fields_ = [("count", c.c_uint), ("sockets", socket_type * 64)]
    class Timeval(c.Structure):
        _fields_ = [("seconds", c.c_long), ("microseconds", c.c_long)]
    ws.socket.restype = socket_type
    ws.socket.argtypes = [c.c_int, c.c_int, c.c_int]
    ws.connect.argtypes = [socket_type, c.c_void_p, c.c_int]
    ws.ioctlsocket.argtypes = [socket_type, c.c_long, c.POINTER(c.c_ulong)]
    ws.send.argtypes = [socket_type, c.c_void_p, c.c_int, c.c_int]
    ws.closesocket.argtypes = [socket_type]
    ws.getsockopt.argtypes = [socket_type, c.c_int, c.c_int, c.c_void_p, c.POINTER(c.c_int)]
    ws.select.argtypes = [c.c_int, c.c_void_p, c.c_void_p, c.c_void_p, c.POINTER(Timeval)]
    data = c.create_string_buffer(512)
    if ws.WSAStartup(0x202, data):
        raise OSError("Winsock initialization failed")
    sock = socket_type(-1).value
    deadline = time.monotonic() + max(.01, min(timeout, 30))
    def error():
        number = ws.WSAGetLastError()
        return OSError(number, f"Inbox transport failed ({number})")
    def writable():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Inbox delivery timed out")
        write, fail = FDSet(), FDSet()
        write.count = fail.count = 1
        write.sockets[0] = fail.sockets[0] = sock
        interval = Timeval(int(remaining), int(remaining % 1 * 1_000_000))
        result = ws.select(0, None, c.byref(write), c.byref(fail), c.byref(interval))
        if result == 0:
            raise TimeoutError("Inbox delivery timed out")
        if result < 0:
            raise error()
        status, length = c.c_int(), c.c_int(c.sizeof(c.c_int))
        if ws.getsockopt(sock, 0xFFFF, 0x1007, c.byref(status), c.byref(length)):
            raise error()
        if status.value:
            raise OSError(status.value, "Inbox connection failed")
    try:
        sock = ws.socket(1, 1, 0)
        if sock == socket_type(-1).value:
            raise error()
        enabled = c.c_ulong(1)
        if ws.ioctlsocket(sock, c.c_long(0x8004667E).value, c.byref(enabled)):
            raise error()
        address = Address(1, encoded)
        if ws.connect(sock, c.byref(address), c.sizeof(address)):
            if ws.WSAGetLastError() not in (10035, 10036):
                raise error()
            writable()
        sent = 0
        while sent < len(payload):
            writable()
            chunk = payload[sent:]
            count = ws.send(sock, chunk, len(chunk), 0)
            if count < 0 and ws.WSAGetLastError() == 10035:
                continue
            if count <= 0:
                raise error()
            sent += count
    finally:
        if sock != socket_type(-1).value:
            ws.closesocket(sock)
        ws.WSACleanup()
