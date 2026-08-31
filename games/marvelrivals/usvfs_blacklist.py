from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Any, Callable


class UsvfsBlacklistError(RuntimeError):
    pass


LibraryLoader = Callable[[str], Any]


def usvfs_library_name(pointer_size: int | None = None) -> str:
    size = ctypes.sizeof(ctypes.c_void_p) if pointer_size is None else pointer_size
    if size == 8:
        return "usvfs_x64.dll"
    if size == 4:
        return "usvfs_x86.dll"
    raise UsvfsBlacklistError(f"Unsupported process pointer size: {size}")


def blacklist_executable(
    application_dir: str | Path,
    executable_name: str,
    *,
    loader: LibraryLoader | None = None,
) -> Path:
    if (
        not executable_name
        or executable_name in {".", ".."}
        or "/" in executable_name
        or "\\" in executable_name
        or ":" in executable_name
    ):
        raise UsvfsBlacklistError("Executable blacklist entry must be a file name")

    library_path = Path(application_dir) / usvfs_library_name()
    if not library_path.is_file():
        raise UsvfsBlacklistError(f"USVFS library not found: {library_path}")

    if loader is None:
        if os.name != "nt":
            raise UsvfsBlacklistError("USVFS is only available on Windows")
        loader = ctypes.WinDLL

    try:
        library = loader(str(library_path))
    except OSError as error:
        raise UsvfsBlacklistError(
            f"Could not load USVFS library: {library_path}"
        ) from error

    try:
        blacklist = library.usvfsBlacklistExecutable
    except AttributeError as error:
        raise UsvfsBlacklistError(
            f"USVFS blacklist export is unavailable: {library_path}"
        ) from error

    blacklist.argtypes = [ctypes.c_wchar_p]
    blacklist.restype = None
    blacklist(executable_name)
    return library_path
