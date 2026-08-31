from __future__ import annotations

import ctypes
import tempfile
import unittest
from pathlib import Path

from games.marvelrivals.usvfs_blacklist import (
    UsvfsBlacklistError,
    blacklist_executable,
    usvfs_library_name,
)


class FakeFunction:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.argtypes = None
        self.restype = object()

    def __call__(self, value: str) -> None:
        self.calls.append(value)


class FakeLibrary:
    def __init__(self) -> None:
        self.usvfsBlacklistExecutable = FakeFunction()


class TestUsvfsBlacklist(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.application_dir = Path(self.temp.name)
        self.library_path = self.application_dir / usvfs_library_name()
        self.library_path.write_bytes(b"test")

    def test_selects_library_for_process_bitness(self) -> None:
        self.assertEqual(usvfs_library_name(8), "usvfs_x64.dll")
        self.assertEqual(usvfs_library_name(4), "usvfs_x86.dll")
        with self.assertRaises(UsvfsBlacklistError):
            usvfs_library_name(16)

    def test_registers_exact_executable_name(self) -> None:
        library = FakeLibrary()
        loaded: list[str] = []

        def loader(path: str):
            loaded.append(path)
            return library

        result = blacklist_executable(
            self.application_dir,
            "MarvelRivals_Launcher.exe",
            loader=loader,
        )

        self.assertEqual(result, self.library_path)
        self.assertEqual(loaded, [str(self.library_path)])
        self.assertEqual(
            library.usvfsBlacklistExecutable.calls,
            ["MarvelRivals_Launcher.exe"],
        )
        self.assertEqual(
            library.usvfsBlacklistExecutable.argtypes,
            [ctypes.c_wchar_p],
        )
        self.assertIsNone(library.usvfsBlacklistExecutable.restype)

    def test_missing_library_is_rejected(self) -> None:
        self.library_path.unlink()
        with self.assertRaises(UsvfsBlacklistError):
            blacklist_executable(
                self.application_dir,
                "MarvelRivals_Launcher.exe",
                loader=lambda _path: FakeLibrary(),
            )

    def test_missing_export_is_rejected(self) -> None:
        with self.assertRaises(UsvfsBlacklistError):
            blacklist_executable(
                self.application_dir,
                "MarvelRivals_Launcher.exe",
                loader=lambda _path: object(),
            )

    def test_loader_failure_is_wrapped(self) -> None:
        def loader(_path: str):
            raise OSError("synthetic load failure")

        with self.assertRaises(UsvfsBlacklistError):
            blacklist_executable(
                self.application_dir,
                "MarvelRivals_Launcher.exe",
                loader=loader,
            )

    def test_path_like_blacklist_entry_is_rejected(self) -> None:
        for value in (
            "C:/Games/MarvelRivals_Launcher.exe",
            "Marvel/MarvelRivals_Launcher.exe",
            r"Marvel\MarvelRivals_Launcher.exe",
            "",
            ".",
            "..",
        ):
            with self.subTest(value=value), self.assertRaises(UsvfsBlacklistError):
                blacklist_executable(
                    self.application_dir,
                    value,
                    loader=lambda _path: FakeLibrary(),
                )


if __name__ == "__main__":
    unittest.main()
