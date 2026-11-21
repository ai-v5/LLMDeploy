"""Read an upstream credential without placing it in arguments or files."""
import ctypes
import os
from ctypes import wintypes


class _FileTime(ctypes.Structure):
    _fields_ = [('low', wintypes.DWORD), ('high', wintypes.DWORD)]


class _Credential(ctypes.Structure):
    _fields_ = [
        ('flags', wintypes.DWORD), ('type', wintypes.DWORD),
        ('target', wintypes.LPWSTR), ('comment', wintypes.LPWSTR),
        ('last_written', _FileTime), ('blob_size', wintypes.DWORD),
        ('blob', ctypes.POINTER(ctypes.c_ubyte)), ('persist', wintypes.DWORD),
        ('attribute_count', wintypes.DWORD), ('attributes', ctypes.c_void_p),
        ('target_alias', wintypes.LPWSTR), ('username', wintypes.LPWSTR),
    ]


def read_windows_credential(target):
    if os.name != 'nt':
        raise OSError('Windows Credential Manager is unavailable')
    api = ctypes.WinDLL('advapi32', use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(_Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    ptr = ctypes.POINTER(_Credential)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(ptr)):
        raise OSError('Upstream credential is unavailable')
    try:
        blob = ctypes.string_at(ptr.contents.blob, ptr.contents.blob_size)
        encoding = 'utf-16-le' if len(blob) > 1 and blob[1] == 0 else 'utf-8'
        value = blob.decode(encoding).rstrip('\x00')
        if not value or '\r' in value or '\n' in value:
            raise ValueError('Invalid upstream credential')
        return value
    finally:
        api.CredFree(ptr)
