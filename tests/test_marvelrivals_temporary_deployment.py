from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from games.marvelrivals.temporary_deployment import (
    DeploymentConflictError,
    DeploymentError,
    DeploymentItem,
    ShippingProcessWatcher,
    TemporaryDeploymentManager,
    UnsafePathError,
)


class TestTemporaryDeploymentManager(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "game" / "MarvelGame" / "Marvel"
        self.root.mkdir(parents=True)
        self.sources = base / "mods"
        self.sources.mkdir()
        self.journal = base / "state" / "journal.json"
        self.manager = TemporaryDeploymentManager(
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

    def deploy(self, *entries: tuple[Path, str]):
        return self.manager.deploy(
            DeploymentItem(str(source), relative) for source, relative in entries
        )

    def test_deploy_one_file_and_cleanup(self) -> None:
        source = self.source("dsound.dll")
        self.deploy((source, "Binaries/Win64/dsound.dll"))
        destination = self.root / "Binaries/Win64/dsound.dll"
        self.assertEqual(destination.read_bytes(), b"payload")
        self.assertTrue(self.journal.exists())
        self.assertTrue(self.manager.cleanup())
        self.assertFalse(destination.exists())
        self.assertFalse(self.journal.exists())

    def test_nested_deployment(self) -> None:
        source = self.source("SigeonLoader.asi")
        self.deploy((source, "Binaries/Win64/plugins/SigeonLoader.asi"))
        destination = self.root / "Binaries/Win64/plugins/SigeonLoader.asi"
        self.assertTrue(destination.exists())
        self.assertTrue(self.manager.cleanup())
        self.assertFalse((self.root / "Binaries/Win64/plugins").exists())

    def test_multiple_files(self) -> None:
        files = [
            (self.source("dsound.dll", b"dll"), "Binaries/Win64/dsound.dll"),
            (
                self.source("MarvelRivalsUTOCSignatureBypass.asi", b"bypass"),
                "Binaries/Win64/plugins/MarvelRivalsUTOCSignatureBypass.asi",
            ),
            (
                self.source("SigeonLoader.asi", b"loader"),
                "Binaries/Win64/plugins/SigeonLoader.asi",
            ),
            (
                self.source("SigeonLoader.ini", b"ini"),
                "Binaries/Win64/plugins/SigeonLoader.ini",
            ),
        ]
        self.deploy(*files)
        for source, relative in files:
            self.assertEqual(
                (self.root / Path(relative.replace("/", os.sep))).read_bytes(),
                source.read_bytes(),
            )
        self.assertTrue(self.manager.cleanup())

    def test_existing_destination_aborts_without_overwrite(self) -> None:
        source = self.source("dsound.dll", b"mod")
        destination = self.root / "Binaries/Win64/dsound.dll"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"user")
        with self.assertRaises(DeploymentConflictError):
            self.deploy((source, "Binaries/Win64/dsound.dll"))
        self.assertEqual(destination.read_bytes(), b"user")
        self.assertFalse(self.journal.exists())

    def test_partial_failure_rolls_back_owned_files(self) -> None:
        first = self.source("first.dll", b"one")
        second = self.source("second.asi", b"two")
        original = self.manager._copy_exclusive
        calls = 0

        def failing_copy(source: Path, destination: Path, on_owned):
            nonlocal calls
            calls += 1
            if calls == 2:
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                os.close(descriptor)
                on_owned()
                raise OSError("synthetic failure")
            return original(source, destination, on_owned)

        self.manager._copy_exclusive = failing_copy  # type: ignore[method-assign]
        with self.assertRaises(DeploymentError):
            self.deploy(
                (first, "Binaries/Win64/first.dll"),
                (second, "Binaries/Win64/plugins/second.asi"),
            )
        self.assertFalse((self.root / "Binaries/Win64/first.dll").exists())
        self.assertFalse((self.root / "Binaries/Win64/plugins/second.asi").exists())
        self.assertFalse(self.journal.exists())

    def test_destination_race_is_not_deleted(self) -> None:
        source = self.source("dsound.dll", b"mod")
        destination = self.root / "Binaries/Win64/dsound.dll"
        original = self.manager._copy_exclusive

        def racing_copy(src: Path, dst: Path, on_owned):
            dst.write_bytes(b"other")
            return original(src, dst, on_owned)

        self.manager._copy_exclusive = racing_copy  # type: ignore[method-assign]
        with self.assertRaises(DeploymentError):
            self.deploy((source, "Binaries/Win64/dsound.dll"))
        self.assertEqual(destination.read_bytes(), b"other")

    def test_invalid_journal_releases_session_lock(self) -> None:
        source = self.source("dsound.dll")
        self.journal.parent.mkdir(parents=True)
        self.journal.write_text("not json", encoding="utf-8")
        with self.assertRaises(DeploymentError):
            self.deploy((source, "Binaries/Win64/dsound.dll"))
        self.assertFalse(self.manager.lock_path.exists())

    def test_unsafe_path_releases_session_lock(self) -> None:
        source = self.source("x")
        with self.assertRaises(UnsafePathError):
            self.deploy((source, "Binaries/Win64/../../outside.dll"))
        self.assertFalse(self.manager.lock_path.exists())

    def test_journal_failure_after_exclusive_create_rolls_back(self) -> None:
        source = self.source("dsound.dll")
        original = self.manager._write_journal
        calls = 0

        def failing_write(journal):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("synthetic journal failure")
            return original(journal)

        self.manager._write_journal = failing_write  # type: ignore[method-assign]
        with self.assertRaises(DeploymentError):
            self.deploy((source, "Binaries/Win64/dsound.dll"))
        self.assertFalse((self.root / "Binaries/Win64/dsound.dll").exists())
        self.assertFalse(self.manager.lock_path.exists())

    def test_cleanup_is_idempotent(self) -> None:
        source = self.source("dsound.dll")
        self.deploy((source, "Binaries/Win64/dsound.dll"))
        self.assertTrue(self.manager.cleanup())
        self.assertTrue(self.manager.cleanup())

    def test_stale_recovery(self) -> None:
        source = self.source("dsound.dll")
        self.deploy((source, "Binaries/Win64/dsound.dll"))
        self.manager.release_lock()
        self.assertTrue(self.manager.recover_stale())
        self.assertFalse((self.root / "Binaries/Win64/dsound.dll").exists())
        self.assertFalse(self.journal.exists())

    def test_missing_deployed_file_is_cleanup_success(self) -> None:
        source = self.source("dsound.dll")
        self.deploy((source, "Binaries/Win64/dsound.dll"))
        (self.root / "Binaries/Win64/dsound.dll").unlink()
        self.assertTrue(self.manager.cleanup())
        self.assertFalse(self.journal.exists())

    def test_externally_modified_file_is_preserved(self) -> None:
        source = self.source("dsound.dll", b"mod")
        self.deploy((source, "Binaries/Win64/dsound.dll"))
        destination = self.root / "Binaries/Win64/dsound.dll"
        destination.write_bytes(b"changed")
        self.assertFalse(self.manager.cleanup())
        self.assertEqual(destination.read_bytes(), b"changed")
        self.assertTrue(self.journal.exists())

    def test_duplicate_session_is_rejected(self) -> None:
        source = self.source("dsound.dll")
        self.deploy((source, "Binaries/Win64/dsound.dll"))
        other = TemporaryDeploymentManager(
            self.root,
            self.journal,
            shipping_process_name="Marvel-Win64-Shipping.exe",
        )
        other.shipping_running = lambda: False  # type: ignore[method-assign]
        with self.assertRaises(DeploymentConflictError):
            other.deploy(
                [DeploymentItem(str(source), "Binaries/Win64/other-dsound.dll")]
            )
        self.assertTrue((self.root / "Binaries/Win64/dsound.dll").exists())
        self.assertTrue(self.manager.cleanup())

    def test_shipping_already_running_rejects_deployment(self) -> None:
        source = self.source("dsound.dll")
        self.manager.shipping_running = lambda: True  # type: ignore[method-assign]
        with self.assertRaises(DeploymentConflictError):
            self.deploy((source, "Binaries/Win64/dsound.dll"))

    def test_shipping_running_prevents_stale_recovery(self) -> None:
        source = self.source("dsound.dll")
        self.deploy((source, "Binaries/Win64/dsound.dll"))
        self.manager.release_lock()
        self.manager.shipping_running = lambda: True  # type: ignore[method-assign]
        self.assertFalse(self.manager.recover_stale())
        self.assertTrue((self.root / "Binaries/Win64/dsound.dll").exists())

    def test_journal_for_other_installation_is_rejected(self) -> None:
        source = self.source("dsound.dll")
        self.deploy((source, "Binaries/Win64/dsound.dll"))
        self.manager.release_lock()
        payload = json.loads(self.journal.read_text(encoding="utf-8"))
        payload["deployment_root"] = str(self.root.parent / "other")
        self.journal.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(DeploymentError):
            self.manager.recover_stale()

    def test_empty_plan_is_rejected(self) -> None:
        with self.assertRaises(DeploymentError):
            self.manager.deploy([])

    def test_case_insensitive_duplicate_destination_is_rejected(self) -> None:
        first = self.source("first.asi")
        second = self.source("second.asi")
        with self.assertRaises(DeploymentConflictError):
            self.deploy(
                (first, "Binaries/Win64/plugins/Test.asi"),
                (second, "binaries/win64/PLUGINS/test.ASI"),
            )

    def test_path_traversal_is_rejected(self) -> None:
        source = self.source("x")
        with self.assertRaises(UnsafePathError):
            self.deploy((source, "Binaries/Win64/../../outside.dll"))

    def test_absolute_path_is_rejected(self) -> None:
        source = self.source("x")
        with self.assertRaises(UnsafePathError):
            self.deploy((source, "C:/Windows/System32/x.dll"))

    def test_unc_path_is_rejected(self) -> None:
        source = self.source("x")
        with self.assertRaises(UnsafePathError):
            self.deploy((source, "//server/share/x.dll"))

    def test_ads_path_is_rejected(self) -> None:
        source = self.source("x")
        with self.assertRaises(UnsafePathError):
            self.deploy((source, "Binaries/Win64/dsound.dll:stream"))

    def test_reserved_windows_name_is_rejected(self) -> None:
        source = self.source("x")
        with self.assertRaises(UnsafePathError):
            self.deploy((source, "Binaries/Win64/CON.dll"))

    def test_symlink_escape_is_rejected(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        binaries = self.root / "Binaries"
        binaries.mkdir()
        try:
            (binaries / "Win64").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation not permitted")
        source = self.source("x")
        with self.assertRaises(UnsafePathError):
            self.deploy((source, "Binaries/Win64/x.dll"))
        self.assertFalse((outside / "x.dll").exists())


class FakeManager:
    def __init__(self) -> None:
        self.cleanup_count = 0

    def cleanup(self) -> bool:
        self.cleanup_count += 1
        return True


class SequenceWatcher(ShippingProcessWatcher):
    def __init__(self, manager: FakeManager, launcher, shipping, **kwargs):
        super().__init__(manager, **kwargs)  # type: ignore[arg-type]
        self.launcher_sequence = iter(launcher)
        self.shipping_sequence = iter(shipping)
        self.last_launcher = []
        self.last_shipping = []

    def _matching_processes(self, name: str):
        if name == self.launcher_process_name:
            try:
                self.last_launcher = next(self.launcher_sequence)
            except StopIteration:
                pass
            return self.last_launcher
        try:
            self.last_shipping = next(self.shipping_sequence)
        except StopIteration:
            pass
        return self.last_shipping


class TestShippingProcessWatcher(unittest.TestCase):
    def watcher(self, launcher, shipping, **overrides):
        manager = FakeManager()
        params = dict(
            launcher_process_name="MarvelRivals_Launcher.exe",
            shipping_process_name="Marvel-Win64-Shipping.exe",
            session_started_at=time.time(),
            launch_start_timeout=0.03,
            launch_timeout=0.12,
            launcher_exit_grace=0.03,
            poll_interval=0.005,
        )
        params.update(overrides)
        return manager, SequenceWatcher(manager, launcher, shipping, **params)

    def test_shipping_exit_is_final_cleanup_condition(self) -> None:
        manager, watcher = self.watcher(
            launcher=[[object()], [], [], []],
            shipping=[[], [object()], [object()], []],
        )
        watcher._run()
        self.assertEqual(manager.cleanup_count, 1)

    def test_launcher_exit_does_not_cleanup_while_shipping_runs(self) -> None:
        manager, watcher = self.watcher(
            launcher=[[object()], [], [], [], []],
            shipping=[[], [object()], [object()], [object()], []],
        )
        watcher.notify_launcher_finished()
        watcher._run()
        self.assertEqual(manager.cleanup_count, 1)

    def test_launcher_abort_cleans_after_grace(self) -> None:
        manager, watcher = self.watcher(
            launcher=[[object()], [], [], [], [], [], [], []],
            shipping=[[], [], [], [], [], [], [], []],
        )
        watcher.notify_launcher_finished()
        watcher._run()
        self.assertEqual(manager.cleanup_count, 1)

    def test_launcher_never_starts_cleans_on_start_timeout(self) -> None:
        manager, watcher = self.watcher(
            launcher=[[], [], [], [], [], [], [], []],
            shipping=[[], [], [], [], [], [], [], []],
        )
        watcher._run()
        self.assertEqual(manager.cleanup_count, 1)


if __name__ == "__main__":
    unittest.main()
