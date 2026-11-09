"""Store an optional DeepSeek credential as a Windows-user-bound DPAPI blob."""

from __future__ import annotations

import ctypes
import getpass
import os
from pathlib import Path
import secrets
import stat

from .af012_runtime import DATA_ROOT


_MAX_KEY_BYTES = 4_096
_MAX_BLOB_BYTES = 16_384
_DEFAULT_PATH = DATA_ROOT / "deepseek-key.dpapi"
_REPARSE_POINT = 0x400


class _DataBlob(ctypes.Structure):
    _fields_ = (("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_byte)))


def _valid_key(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8", "ignore")) > _MAX_KEY_BYTES
            or value != value.strip()
            or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value)):
        raise ValueError("Credential must be non-empty, bounded ASCII text without whitespace.")
    return value


def _dpapi(payload: bytes, *, protect: bool) -> bytes:
    """Protect/unprotect bounded bytes for the current Windows user only."""
    if os.name != "nt":
        raise RuntimeError("DPAPI credentials are supported only on Windows.")
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_BLOB_BYTES:
        raise ValueError("DPAPI payload is empty or exceeds its size limit.")

    source_buffer = (ctypes.c_byte * len(payload)).from_buffer_copy(payload)
    source = _DataBlob(len(payload), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    output = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    operation.restype = ctypes.c_int
    operation.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_wchar_p,
                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_uint32, ctypes.POINTER(_DataBlob)]
    try:
        if protect:
            succeeded = operation(ctypes.byref(source), "AgentForge DeepSeek credential",
                                  None, None, None, 0, ctypes.byref(output))
        else:
            succeeded = operation(ctypes.byref(source), None, None, None, None, 0,
                                  ctypes.byref(output))
        if not succeeded or not output.pbData or not 1 <= output.cbData <= _MAX_BLOB_BYTES:
            action = "protect" if protect else "unprotect"
            raise RuntimeError(f"Windows DPAPI could not {action} the credential.")
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if output.pbData:
            kernel32.LocalFree(output.pbData)


class CredentialStore:
    """Resolve env overrides or atomically maintain an encrypted local secret."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        supplied = Path(path) if path is not None else _DEFAULT_PATH
        target = Path(os.path.abspath(supplied))
        try:
            root = DATA_ROOT.resolve(strict=False)
            parent = target.parent.resolve(strict=False)
            parent.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            raise ValueError("Credential path must stay inside the private data directory.") from None
        if target.name in {"", ".", ".."}:
            raise ValueError("Credential path is invalid.")
        self.path = parent / target.name
        self._check_file()

    def _check_file(self, *, required: bool = False) -> bool:
        if self.path.is_symlink():
            raise ValueError("Credential file cannot be a symbolic link or reparse point.")
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            if required:
                raise ValueError("Stored credential cannot be read.") from None
            return False
        except OSError:
            raise ValueError("Stored credential cannot be read.") from None
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT):
            raise ValueError("Credential file cannot be a symbolic link or reparse point.")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Credential file must be an ordinary, unshared file.")
        return True

    def resolve(self, environ: dict[str, str] | None = None) -> tuple[str | None, str]:
        env = os.environ if environ is None else environ
        environment_key = env.get("DEEPSEEK_API_KEY")
        if environment_key is not None:
            return _valid_key(environment_key), "environment"
        value = self.get()
        return (value, "dpapi") if value is not None else (None, "none")

    def status(self, environ: dict[str, str] | None = None) -> dict[str, object]:
        env = os.environ if environ is None else environ
        environment_key = env.get("DEEPSEEK_API_KEY")
        if environment_key is not None:
            _valid_key(environment_key)
            return {"configured": True, "source": "environment",
                    "stored_credential_present": self._check_file()}
        present = self._check_file()
        return {"configured": present, "source": "dpapi" if present else "none",
                "stored_credential_present": present}

    def get(self) -> str | None:
        if not self._check_file():
            return None
        try:
            encrypted = self.path.read_bytes()
        except OSError:
            raise ValueError("Stored credential cannot be read.") from None
        if not encrypted or len(encrypted) > _MAX_BLOB_BYTES:
            raise ValueError("Stored credential cannot be read.")
        try:
            plaintext = _dpapi(encrypted, protect=False)
            value = plaintext.decode("ascii")
            return _valid_key(value)
        except (UnicodeError, ValueError, TypeError):
            raise ValueError("Stored credential cannot be read.") from None
        except RuntimeError:
            raise ValueError("Stored credential cannot be read.") from None

    def set(self, value: str) -> None:
        key = _valid_key(value)
        encrypted = _dpapi(key.encode("ascii"), protect=True)
        if not isinstance(encrypted, bytes) or not encrypted or len(encrypted) > _MAX_BLOB_BYTES:
            raise RuntimeError("Windows DPAPI returned an invalid credential payload.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._check_file()
        temporary = self.path.parent / f".deepseek-{secrets.token_hex(12)}.tmp"
        descriptor = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_file()
            if self.path.is_symlink():
                raise ValueError("Credential file cannot be a symbolic link or reparse point.")
            os.replace(temporary, self.path)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def set_interactive(self, input_fn=None) -> None:
        reader = input_fn or getpass.getpass
        self.set(reader("DeepSeek API key (input hidden): "))

    def remove(self) -> bool:
        if not self._check_file():
            return False
        self.path.unlink()
        return True


__all__ = ["CredentialStore"]
