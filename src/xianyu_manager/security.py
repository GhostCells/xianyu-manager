from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return (
        _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))),
        buffer,
    )


def protect_secret(secret: str) -> bytes:
    if os.name != "nt":
        raise RuntimeError("API Key 加密仅支持 Windows")
    source, source_buffer = _blob(secret.encode("utf-8"))
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "Xianyu Manager SiliconFlow API Key",
        None,
        None,
        None,
        0,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)
        del source_buffer


def unprotect_secret(payload: bytes) -> str:
    if os.name != "nt":
        raise RuntimeError("API Key 解密仅支持 Windows")
    source, source_buffer = _blob(payload)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(output.pbData)
        del source_buffer


class SecretStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def has_secret(self) -> bool:
        return self.path.is_file() and self.path.stat().st_size > 0

    def save(self, secret: str) -> None:
        value = secret.strip()
        if not value:
            raise ValueError("API Key 不能为空")
        encrypted = protect_secret(value)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_bytes(encrypted)
        temporary.replace(self.path)

    def load(self) -> str:
        if not self.has_secret():
            return ""
        return unprotect_secret(self.path.read_bytes()).strip()

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
