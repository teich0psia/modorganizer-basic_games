from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from games.marvelrivals.temporary_deployment import (
    ShippingProcessWatcher,
    _process_matches_target,
    is_managed_mod_source,
)


class FakeManager:
    def __init__(self) -> None:
        self.cleanup_count = 0

    def cleanup(self) -> bool:
        self.cleanup_count += 1
        return True


class FakeProcess:
    def __init__(
        self,
        pid: int,
        name: str | None,
        exe: str | None,
        created: float | None,
    ) -> None:
        self.pid = pid
        self.info = {"name": name, "exe": exe, "create_time": created}

    def as_dict(self, attrs):
        return dict(self.info)


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


class TestManagedModSource(unittest.TestCase):
    def test_mod_source_is_accepted_and_overwrite_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            mods = base / "mods"
            overwrite = base / "overwrite"
            game = base / "game"
            mod_file = mods / "UTOC" / "Binaries" / "Win64" / "dsound.dll"
            overwrite_file = overwrite / "Binaries" / "Win64" / "ccmini" / "LOCK"
            game_file = game / "Binaries" / "Win64" / "steam_api64.dll"
            for path in (mod_file, overwrite_file, game_file):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")

            self.assertTrue(is_managed_mod_source(mod_file, mods))
            self.assertFalse(is_managed_mod_source(overwrite_file, mods))
            self.assertFalse(is_managed_mod_source(game_file, mods))

    def test_prefix_sibling_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            mods = base / "mods"
            sibling = base / "mods-old" / "x.dll"
            mods.mkdir()
            sibling.parent.mkdir()
            sibling.write_bytes(b"x")
            self.assertFalse(is_managed_mod_source(sibling, mods))

    def test_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            mods = base / "mods"
            outside = base / "outside.dll"
            link = mods / "mod" / "link.dll"
            link.parent.mkdir(parents=True)
            outside.write_bytes(b"x")
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlink creation not permitted")
            self.assertFalse(is_managed_mod_source(link, mods))


class TestProcessMatching(unittest.TestCase):
    def test_executable_path_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            expected = base / "Marvel-Win64-Shipping.exe"
            other = base / "other" / "Marvel-Win64-Shipping.exe"
            expected.write_bytes(b"x")
            other.parent.mkdir()
            other.write_bytes(b"x")
            process = FakeProcess(
                10,
                "Marvel-Win64-Shipping.exe",
                str(other),
                time.time(),
            )
            self.assertFalse(
                _process_matches_target(
                    process,  # type: ignore[arg-type]
                    expected_name="marvel-win64-shipping.exe",
                    expected_executable=expected,
                )
            )

    def test_name_is_fallback_when_executable_is_unavailable(self) -> None:
        process = FakeProcess(
            11,
            "Marvel-Win64-Shipping.exe",
            None,
            time.time(),
        )
        self.assertTrue(
            _process_matches_target(
                process,  # type: ignore[arg-type]
                expected_name="marvel-win64-shipping.exe",
                expected_executable=Path("unused.exe"),
            )
        )

    def test_old_process_is_rejected(self) -> None:
        session = time.time()
        process = FakeProcess(
            12,
            "Marvel-Win64-Shipping.exe",
            None,
            session - 30,
        )
        self.assertFalse(
            _process_matches_target(
                process,  # type: ignore[arg-type]
                expected_name="marvel-win64-shipping.exe",
                expected_executable=None,
                min_created_at=session,
            )
        )


class TestShippingProcessWatcherRegression(unittest.TestCase):
    def watcher(self, launcher, shipping, **overrides):
        manager = FakeManager()
        params = dict(
            launcher_process_name="MarvelRivals_Launcher.exe",
            shipping_process_name="Marvel-Win64-Shipping.exe",
            session_started_at=time.time(),
            launch_start_timeout=0.001,
            launcher_exit_grace=0.001,
            launch_timeout=0.15,
            poll_interval=0.005,
        )
        params.update(overrides)
        return manager, SequenceWatcher(manager, launcher, shipping, **params)

    def test_launcher_exit_does_not_abort_delayed_shipping(self) -> None:
        manager, watcher = self.watcher(
            launcher=[[object()], [], [], [], [], [], [], []],
            shipping=[[], [], [], [], [], [object()], []],
        )
        messages: list[str] = []
        watcher._log = messages.append
        watcher.notify_launcher_finished()
        watcher._run()
        self.assertEqual(manager.cleanup_count, 1)
        self.assertTrue(any("detected" in message for message in messages))
        self.assertTrue(any("exited" in message for message in messages))

    def test_never_started_waits_for_overall_timeout(self) -> None:
        manager, watcher = self.watcher(
            launcher=[[object()], []],
            shipping=[[]],
            launch_timeout=0.03,
        )
        watcher.notify_launcher_finished()
        started = time.monotonic()
        watcher._run()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.025)
        self.assertEqual(manager.cleanup_count, 1)


if __name__ == "__main__":
    unittest.main()
