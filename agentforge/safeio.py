"""读取任何字节前，先验证已打开的 Windows 源码句柄所对应的路径。"""

import os
from pathlib import Path
import stat


def open_source(root: Path, path: Path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("Source is not an ordinary file.")
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise

    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    ]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    # Windows 打开文件时使用的标志：GENERIC_READ、FILE_SHARE_READ、OPEN_EXISTING、OPEN_REPARSE_POINT。
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    owned = True
    try:
        class AttributeTagInfo(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

        attributes = AttributeTagInfo()
        if not kernel.GetFileInformationByHandleEx(
            handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes.attributes & (0x400 | 0x10):
            raise OSError("Source handle is a reparse point or directory.")
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise OSError("Cannot validate the final source handle path.")
        final = buffer.value
        if final.startswith("\\\\?\\UNC\\"):
            final = "\\\\" + final[8:]
        elif final.startswith("\\\\?\\"):
            final = final[4:]
        if os.path.normcase(os.path.commonpath((str(root), final))) != os.path.normcase(str(root)):
            raise OSError("Opened source handle is outside the repository.")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        owned = False
        try:
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        if owned:
            kernel.CloseHandle(handle)

