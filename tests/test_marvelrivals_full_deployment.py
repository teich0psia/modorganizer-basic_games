from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Callable

from games.marvelrivals.full_deployment import FullDeploymentManager, prefers_hardlink
from games.marvelrivals.temporary_deployment import (
    DeploymentConflictError,
    DeploymentItem,
    DeploymentJournal,
)


class TestFullDeploymentManager(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "game" / "MarvelGame" / "Marvel"
        self.root.mkdir(parents=True)
        self.sources = base / "mods"
        self.sources.mkdir()
        self.journal = base / "state" / "full.json"
        self.manager = FullDeploymentManager(
            self.root,
            self.journal,
            shipping_process_name="Marvel-Win64-Shipping.exe",
        )
        self.manager.shipping_running = lambda: False  # type: ignore[method-assign]
        self.addCleanup(self.manager.release_lock)

    def source(self, name: str, content: bytes = b"payload") -> Path:
        path = self.sources / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def deploy(self, *entries: tuple[Path, str]) -> DeploymentJournal:
        return self.manager.deploy(
            DeploymentItem(str(source), relative) for source, relative in entries
        )

    def test_hardlink_policy_is_limited_to_large_content_payloads(self) -> None:
        for path in (
            "Content/Paks/~Mods/skin.pak",
            "Content/Paks/~Mods/skin.utoc",
            "Content/Paks/~Mods/skin.ucas",
            "Content/Movies/intro.bk2",
        ):
            with self.subTest(path=path):
                self.assertTrue(prefers_hardlink(path))

        for path in (
            "Binaries/Win64/dsound.dll",
            "Binaries/Win64/Mods/config.json",
            "Binaries/Win64/Mods/embedded.pak",
            "Content/Paks/~Mods/readme.txt",
        ):
            with self.subTest(path=path):
                self.assertFalse(prefers_hardlink(path))

    def test_pak_is_hardlinked_and_root_file_is_copied(self) -> None:
        pak = self.source("skin.pak", b"pak")
        dll = self.source("dsound.dll", b"dll")
        journal = self.deploy(
            (pak, "Content/Paks/~Mods/skin.pak"),
            (dll, "Binaries/Win64/dsound.dll"),
        )
        pak_destination = self.root / "Content/Paks/~Mods/skin.pak"
        dll_destination = self.root / "Binaries/Win64/dsound.dll"

        self.assertTrue(os.path.samefile(pak, pak_destination))
        self.assertFalse(os.path.samefile(dll, dll_destination))
        hardlink_fingerprint = journal.items[0].sha256
        copy_hash = journal.items[1].sha256
        self.assertIsNotNone(hardlink_fingerprint)
        self.assertIsNotNone(copy_hash)
        assert hardlink_fingerprint is not None
        assert copy_hash is not None
        self.assertTrue(hardlink_fingerprint.startswith("hardlink:"))
        self.assertFalse(copy_hash.startswith("hardlink:"))
        self.assertTrue(self.manager.cleanup(journal=journal))

    def test_hardlink_failure_falls_back_to_copy(self) -> None:
        pak = self.source("skin.pak", b"pak")

        def fail_link(
            _source: Path,
            _destination: Path,
            _on_owned: Callable[[], None],
        ) -> None:
            raise OSError("synthetic hardlink failure")

        self.manager._hardlink_exclusive = fail_link  # type: ignore[method-assign]
        journal = self.deploy((pak, "Content/Paks/~Mods/skin.pak"))
        destination = self.root / "Content/Paks/~Mods/skin.pak"
        self.assertEqual(destination.read_bytes(), b"pak")
        self.assertFalse(os.path.samefile(pak, destination))
        fingerprint = journal.items[0].sha256
        self.assertIsNotNone(fingerprint)
        assert fingerprint is not None
        self.assertFalse(fingerprint.startswith("hardlink:"))
        self.assertTrue(self.manager.cleanup(journal=journal))

    def test_modified_hardlink_is_preserved(self) -> None:
        pak = self.source("skin.pak", b"pak")
        journal = self.deploy((pak, "Content/Paks/~Mods/skin.pak"))
        destination = self.root / "Content/Paks/~Mods/skin.pak"
        destination.write_bytes(b"changed")

        self.assertFalse(self.manager.cleanup(journal=journal))
        self.assertTrue(destination.exists())
        self.assertEqual(pak.read_bytes(), b"changed")

    def test_replaced_hardlink_is_preserved(self) -> None:
        pak = self.source("skin.pak", b"pak")
        journal = self.deploy((pak, "Content/Paks/~Mods/skin.pak"))
        destination = self.root / "Content/Paks/~Mods/skin.pak"
        destination.unlink()
        destination.write_bytes(b"replacement")

        self.assertFalse(self.manager.cleanup(journal=journal))
        self.assertEqual(destination.read_bytes(), b"replacement")
        self.assertTrue(self.journal.exists())

    def test_hardlink_is_preserved_if_mod_source_disappears(self) -> None:
        pak = self.source("skin.pak", b"pak")
        journal = self.deploy((pak, "Content/Paks/~Mods/skin.pak"))
        destination = self.root / "Content/Paks/~Mods/skin.pak"
        pak.unlink()

        self.assertFalse(self.manager.cleanup(journal=journal))
        self.assertEqual(destination.read_bytes(), b"pak")

    def test_modified_copy_is_preserved(self) -> None:
        dll = self.source("dsound.dll", b"dll")
        journal = self.deploy((dll, "Binaries/Win64/dsound.dll"))
        destination = self.root / "Binaries/Win64/dsound.dll"
        destination.write_bytes(b"changed")

        self.assertFalse(self.manager.cleanup(journal=journal))
        self.assertEqual(destination.read_bytes(), b"changed")

    def test_existing_destination_is_never_overwritten(self) -> None:
        pak = self.source("skin.pak", b"pak")
        destination = self.root / "Content/Paks/~Mods/skin.pak"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"existing")

        with self.assertRaises(DeploymentConflictError):
            self.deploy((pak, "Content/Paks/~Mods/skin.pak"))
        self.assertEqual(destination.read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
