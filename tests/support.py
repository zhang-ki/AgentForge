import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from agentforge.config import PROJECT_ROOT

TMP_ROOT = PROJECT_ROOT / "tests" / ".tmp"
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "synthetic_repo"


class LocalTestCase(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="case-", dir=TMP_ROOT)
        self.sandbox = Path(self.temporary.name)
        self.root = self.sandbox / "repository"
        self.root.mkdir()
        self.addCleanup(self.cleanup_local)

    def cleanup_local(self):
        resolved = Path(self.temporary.name).resolve()
        if not resolved.is_relative_to(TMP_ROOT.resolve()) or not resolved.is_relative_to(PROJECT_ROOT):
            raise AssertionError("Refusing cleanup outside the project test directory.")
        self.temporary.cleanup()
        try:
            TMP_ROOT.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # Another test-owned directory may still be alive.
            pass

    def write(self, relative, content="example\n"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path


def snapshot(root):
    entries = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.stat()
        entries[relative] = {
            "type": "directory" if path.is_dir() else "file",
            "size": None if path.is_dir() else info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "sha256": None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    return {"entries": entries, "manifest_sha256": digest}


def make_junction(link: Path, target: Path):
    """Create a test-owned NTFS junction using Windows APIs, without a shell."""
    if os.name != "nt":
        raise OSError("Windows junctions are unavailable on this platform.")
    import ctypes
    from ctypes import wintypes
    import struct

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    link.mkdir()
    handle = kernel.CreateFileW(str(link), 0x40000000, 0, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        link.rmdir()
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        substitute = ("\\??\\" + str(target.resolve())).encode("utf-16-le")
        display = str(target.resolve()).encode("utf-16-le")
        paths = substitute + b"\0\0" + display + b"\0\0"
        data = struct.pack("<LHHHHHH", 0xA0000003, 8 + len(paths), 0,
                           0, len(substitute), len(substitute) + 2, len(display)) + paths
        buffer = ctypes.create_string_buffer(data)
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(handle, 0x000900A4, buffer, len(data), None, 0,
                                      ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(handle)
        link.rmdir()
        raise
    else:
        kernel.CloseHandle(handle)

