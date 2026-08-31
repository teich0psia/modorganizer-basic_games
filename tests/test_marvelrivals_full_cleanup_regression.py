from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from games.marvelrivals.full_deployment import FullDeploymentManager
from games.marvelrivals.temporary_deployment import DeploymentItem


class TestFullDeploymentCleanupRegression(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "game" / "MarvelGame" / "Marvel"
        self.root.mkdir(parents=True)
        self.mods = base / "mods"
        self.mods.mkdir()
        self.journal = base / "state" / "full.json"
        self.manager = FullDeploymentManager(
            self.root,
            self.journal,
            shipping_process_name="Marvel-Win64-Shipping.exe",
        )
        self.manager.shipping_running = lambda: False  # type: ignore[method-assign]
        self.addCleanup(self.manager.release_lock)

    def source(self, name: str, content: bytes) -> Path:
        path = self.mods / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_unmodified_dsound_copy_is_removed(self) -> None:
        source = self.source("root/dsound.dll", b"dll")
        journal = self.manager.deploy(
            [DeploymentItem(str(source), "Binaries/Win64/dsound.dll")]
        )
        destination = self.root / "Binaries" / "Win64" / "dsound.dll"

        self.assertTrue(destination.is_file())
        self.assertFalse(os.path.samefile(source, destination))
        self.assertTrue(self.manager.cleanup(journal=journal))
        self.assertFalse(destination.exists())
        self.assertFalse(self.journal.exists())

    def test_unmodified_pak_hardlink_is_removed_without_removing_source(self) -> None:
        source = self.source("pak/skin.pak", b"pak")
        journal = self.manager.deploy(
            [DeploymentItem(str(source), "Content/Paks/~Mods/skin.pak")]
        )
        destination = self.root / "Content" / "Paks" / "~Mods" / "skin.pak"

        self.assertTrue(destination.is_file())
        self.assertTrue(os.path.samefile(source, destination))
        self.assertTrue(self.manager.cleanup(journal=journal))
        self.assertFalse(destination.exists())
        self.assertEqual(source.read_bytes(), b"pak")
        self.assertFalse(self.journal.exists())


if __name__ == "__main__":
    unittest.main()
