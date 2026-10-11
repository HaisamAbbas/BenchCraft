"""Small handle-relative filesystem helpers for hardened Windows workspace I/O."""

from __future__ import annotations

import ctypes
import errno
import os
from collections.abc import Iterator
from ctypes import wintypes
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4
FILE_READ_ATTRIBUTES = 0x80
FILE_LIST_DIRECTORY = 0x1
FILE_TRAVERSE = 0x20
FILE_ADD_FILE = 0x2
FILE_DELETE_ACCESS = 0x10000
SYNCHRONIZE = 0x00100000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_OPEN = 1
FILE_CREATE = 2
FILE_DIRECTORY_FILE = 0x1
FILE_NON_DIRECTORY_FILE = 0x40
FILE_SYNCHRONOUS_IO_NONALERT = 0x20
FILE_OPEN_REPARSE_POINT = 0x00200000
OBJ_CASE_INSENSITIVE = 0x40
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_OBJECT_NAME_COLLISION = 0xC0000035
STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
ERROR_NO_MORE_FILES = 18
FILE_BASIC_INFO_CLASS = 0
FILE_STANDARD_INFO_CLASS = 1
FILE_ID_EXTD_DIRECTORY_INFO_CLASS = 19
FILE_ID_INFO_CLASS = 18
FILE_DISPOSITION_INFO_CLASS = 4
_ENUM_BUFFER_SIZE = 64 * 1024


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", wintypes.LPVOID),
        ("SecurityQualityOfService", wintypes.LPVOID),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class _FileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
    ]


class _FileStandardInfo(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", wintypes.DWORD),
        ("DeletePending", wintypes.BOOLEAN),
        ("Directory", wintypes.BOOLEAN),
    ]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_ulonglong), ("FileId", ctypes.c_ubyte * 16)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("CreationTime", wintypes.FILETIME),
        ("LastAccessTime", wintypes.FILETIME),
        ("LastWriteTime", wintypes.FILETIME),
        ("VolumeSerialNumber", wintypes.DWORD),
        ("FileSizeHigh", wintypes.DWORD),
        ("FileSizeLow", wintypes.DWORD),
        ("NumberOfLinks", wintypes.DWORD),
        ("FileIndexHigh", wintypes.DWORD),
        ("FileIndexLow", wintypes.DWORD),
    ]


class _FileIdExtdDirectoryInfo(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.DWORD),
        ("FileIndex", wintypes.DWORD),
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("AllocationSize", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
        ("FileNameLength", wintypes.DWORD),
        ("EaSize", wintypes.DWORD),
        ("ReparsePointTag", wintypes.DWORD),
        ("FileId", ctypes.c_ubyte * 16),
        ("FileName", wintypes.WCHAR * 1),
    ]


@dataclass(frozen=True)
class HandleInfo:
    attributes: int
    is_directory: bool
    size: int
    link_count: int
    volume_id: int
    file_id: int


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    attributes: int
    size: int
    file_id: int


@cache
def _apis() -> tuple[Any, Any, Any, Any, Any, Any]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    ]
    nt_create_file.restype = ctypes.c_long
    nt_set_information = ntdll.NtSetInformationFile
    nt_set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    nt_set_information.restype = ctypes.c_long
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    return create_file, close_handle, get_info, nt_create_file, set_info, nt_set_information


def _raise_last_error(message: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{message}: {ctypes.FormatError(code).strip()}")


@cache
def _flush_file_buffers_api() -> Any:
    flush = ctypes.WinDLL("kernel32", use_last_error=True).FlushFileBuffers
    flush.argtypes = [wintypes.HANDLE]
    flush.restype = wintypes.BOOL
    return flush


def flush_directory(directory_handle: int) -> None:
    """Flush a writable directory handle after publishing a durable entry."""
    if not _flush_file_buffers_api()(wintypes.HANDLE(directory_handle)):
        raise _raise_last_error("could not flush workspace directory metadata")


def _raise_ntstatus(status: int, message: str) -> OSError:
    ntdll = ctypes.WinDLL("ntdll")
    convert = ntdll.RtlNtStatusToDosError
    convert.argtypes = [ctypes.c_long]
    convert.restype = wintypes.ULONG
    code = int(convert(status))
    return OSError(code, f"{message}: {ctypes.FormatError(code).strip()}")


def _extended_path(absolute: str) -> str:
    """Convert a Win32 absolute path to the extended form without rewriting it."""
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def open_directory(path: Path, expected: os.stat_result, *, writable: bool = False) -> int:
    create_file, close_handle, _, _, _, _ = _apis()
    absolute = os.path.abspath(path)
    extended = _extended_path(absolute)
    handle = create_file(
        extended,
        FILE_LIST_DIRECTORY
        | FILE_READ_ATTRIBUTES
        | FILE_TRAVERSE
        | SYNCHRONIZE
        | ((FILE_ADD_FILE | GENERIC_WRITE) if writable else 0),
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        3,
        0x02000000 | FILE_OPEN_REPARSE_POINT,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise _raise_last_error(f"could not open workspace directory {path}")
    try:
        info = handle_info(handle)
        if (
            not info.is_directory
            or info.attributes & FILE_ATTRIBUTE_REPARSE_POINT
            or not identity_matches_stat(handle, info, expected)
        ):
            raise OSError("workspace directory changed while it was being opened")
        return int(handle)
    except BaseException:
        close_handle(handle)
        raise


def open_relative(
    directory_handle: int,
    name: str,
    *,
    directory: bool,
    read: bool = False,
    write: bool = False,
    create: bool = False,
    allow_missing: bool = False,
) -> int | None:
    if not name or "\\" in name or "/" in name or name in {".", ".."}:
        raise ValueError("relative Windows filename must be one path component")
    _, _, _, nt_create_file, _, _ = _apis()
    encoded_size = len(name.encode("utf-16-le"))
    unicode_name = _UnicodeString(encoded_size, encoded_size + 2, name)
    object_attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        wintypes.HANDLE(directory_handle),
        ctypes.pointer(unicode_name),
        OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    desired_access = FILE_READ_ATTRIBUTES | SYNCHRONIZE
    if directory:
        desired_access |= FILE_LIST_DIRECTORY | FILE_TRAVERSE
    if read:
        desired_access |= GENERIC_READ
    if write:
        desired_access |= GENERIC_WRITE | FILE_DELETE_ACCESS
    options = FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT
    options |= FILE_DIRECTORY_FILE if directory else FILE_NON_DIRECTORY_FILE
    disposition = FILE_CREATE if create else FILE_OPEN
    output = wintypes.HANDLE()
    io_status = _IoStatusBlock()
    status = (
        int(
            nt_create_file(
                ctypes.byref(output),
                desired_access,
                ctypes.byref(object_attributes),
                ctypes.byref(io_status),
                None,
                0,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                disposition,
                options,
                None,
                0,
            )
        )
        & 0xFFFFFFFF
    )
    if status in {STATUS_OBJECT_NAME_NOT_FOUND, STATUS_OBJECT_PATH_NOT_FOUND} and allow_missing:
        return None
    if status == STATUS_OBJECT_NAME_COLLISION:
        raise FileExistsError(errno.EEXIST, f"workspace entry {name!r} already exists")
    if status & 0x80000000:
        raise _raise_ntstatus(status, f"could not open workspace entry {name!r}")
    if output.value is None:
        raise OSError("Windows returned success without a workspace entry handle")
    return int(output.value)


def handle_info(handle: int) -> HandleInfo:
    _, _, get_info, _, _, _ = _apis()
    basic = _FileBasicInfo()
    standard = _FileStandardInfo()
    identity = _FileIdInfo()
    for information_class, structure in (
        (FILE_BASIC_INFO_CLASS, basic),
        (FILE_STANDARD_INFO_CLASS, standard),
        (FILE_ID_INFO_CLASS, identity),
    ):
        if not get_info(
            handle, information_class, ctypes.byref(structure), ctypes.sizeof(structure)
        ):
            raise _raise_last_error("could not inspect workspace entry handle")
    file_id = int.from_bytes(bytes(identity.FileId), "little")
    return HandleInfo(
        attributes=int(basic.FileAttributes),
        is_directory=bool(standard.Directory),
        size=int(standard.EndOfFile),
        link_count=int(standard.NumberOfLinks),
        volume_id=int(identity.VolumeSerialNumber),
        file_id=file_id,
    )


@cache
def _legacy_handle_information_api() -> Any:
    get_info = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)]
    get_info.restype = wintypes.BOOL
    return get_info


def identity_matches_stat(
    handle: int, info: HandleInfo, expected: os.stat_result
) -> bool:
    """Compare a native file ID to ``stat`` using one complete identity format.

    Windows Python versions/filesystems may populate ``st_ino`` from either the
    128-bit FILE_ID_INFO API or the older 64-bit file-index API. Never combine the
    volume from one representation with the file ID from the other.
    """
    if (info.volume_id, info.file_id) == (expected.st_dev, expected.st_ino):
        return True
    legacy = _ByHandleFileInformation()
    if not _legacy_handle_information_api()(
        wintypes.HANDLE(handle), ctypes.byref(legacy)
    ):
        raise _raise_last_error("could not identify workspace entry handle")
    file_index = (int(legacy.FileIndexHigh) << 32) | int(legacy.FileIndexLow)
    return (int(legacy.VolumeSerialNumber), file_index) == (
        expected.st_dev,
        expected.st_ino,
    )


def iter_directory(handle: int) -> Iterator[DirectoryEntry]:
    _, _, get_info, _, _, _ = _apis()
    buffer = ctypes.create_string_buffer(_ENUM_BUFFER_SIZE)
    while True:
        if not get_info(
            handle,
            FILE_ID_EXTD_DIRECTORY_INFO_CLASS,
            buffer,
            _ENUM_BUFFER_SIZE,
        ):
            error = ctypes.get_last_error()
            if error == ERROR_NO_MORE_FILES:
                return
            raise _raise_last_error("could not enumerate pinned workspace directory")
        offset = 0
        while True:
            info = ctypes.cast(
                ctypes.addressof(buffer) + offset,
                ctypes.POINTER(_FileIdExtdDirectoryInfo),
            ).contents
            if info.FileNameLength % 2:
                raise OSError("Windows returned a malformed directory entry")
            name_address = (
                ctypes.addressof(buffer) + offset + _FileIdExtdDirectoryInfo.FileName.offset
            )
            name = ctypes.wstring_at(name_address, info.FileNameLength // 2)
            if name not in {".", ".."}:
                yield DirectoryEntry(
                    name=name,
                    attributes=int(info.FileAttributes),
                    size=max(0, int(info.EndOfFile)),
                    file_id=int.from_bytes(bytes(info.FileId), "little"),
                )
            next_offset = int(info.NextEntryOffset)
            if not next_offset:
                break
            if (
                next_offset < _FileIdExtdDirectoryInfo.FileName.offset
                or offset + next_offset >= _ENUM_BUFFER_SIZE
            ):
                raise OSError("Windows returned invalid directory entry offsets")
            offset += next_offset


def handle_to_fd(handle: int, *, write: bool = False) -> int:
    import msvcrt

    flags = os.O_BINARY | (os.O_WRONLY if write else os.O_RDONLY)
    return msvcrt.open_osfhandle(handle, flags)


def rename_relative(file_handle: int, parent_handle: int, name: str, *, replace: bool) -> None:
    _, _, _, _, _, nt_set_information = _apis()
    encoded = name.encode("utf-16-le")
    name_offset = _FileRenameInfo.FileName.offset
    size = ctypes.sizeof(_FileRenameInfo) + len(encoded)
    buffer = ctypes.create_string_buffer(size)
    info = ctypes.cast(buffer, ctypes.POINTER(_FileRenameInfo)).contents
    info.ReplaceIfExists = bool(replace)
    info.RootDirectory = wintypes.HANDLE(parent_handle)
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded, len(encoded))
    io_status = _IoStatusBlock()
    status = (
        int(
            nt_set_information(
                file_handle,
                ctypes.byref(io_status),
                buffer,
                size,
                10,  # FileRenameInformation
            )
        )
        & 0xFFFFFFFF
    )
    if status & 0x80000000:
        raise _raise_ntstatus(status, f"could not publish workspace file as {name!r}")


def delete_handle(file_handle: int) -> None:
    _, _, _, _, set_info, _ = _apis()
    disposition = _FileDispositionInfo(True)
    if not set_info(
        file_handle,
        FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise _raise_last_error("could not remove temporary workspace file")


class _FileRenameInfo(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", wintypes.BOOL),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
        ("FileName", wintypes.WCHAR * 1),
    ]


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOLEAN)]


def close_handle(handle: int) -> None:
    _, close, _, _, _, _ = _apis()
    if not close(handle):
        raise _raise_last_error("could not close workspace directory handle")
