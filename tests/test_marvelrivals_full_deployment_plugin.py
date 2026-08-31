from __future__ import annotations

import importlib
import sys
import types
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakePluginSetting:
    def __init__(self, key: str, description: str, default: bool) -> None:
        self.key = key
        self.description = description
        self.default = default


class FakeVersionInfo:
    def __init__(self, *parts: int) -> None:
        self.parts = parts


class FakeIPlugin:
    def __init__(self) -> None:
        pass


class FakeIFileTree:
    DIRECTORY = 1


mobase = cast(Any, types.ModuleType("mobase"))
mobase.IPlugin = FakeIPlugin
mobase.IOrganizer = object
mobase.IFileTree = FakeIFileTree
mobase.PluginSetting = FakePluginSetting
mobase.VersionInfo = FakeVersionInfo
sys.modules["mobase"] = mobase

qtcore = cast(Any, types.ModuleType("PyQt6.QtCore"))


class FakeQDir:
    pass


qtcore.QDir = FakeQDir
qtwidgets = cast(Any, types.ModuleType("PyQt6.QtWidgets"))


class FakeMessageBox:
    calls: list[tuple[Any, ...]] = []

    @classmethod
    def critical(cls, *args: Any) -> None:
        cls.calls.append(args)


qtwidgets.QMessageBox = FakeMessageBox
sys.modules["PyQt6"] = types.ModuleType("PyQt6")
sys.modules["PyQt6.QtCore"] = qtcore
sys.modules["PyQt6.QtWidgets"] = qtwidgets

# Keep this test focused on plugin orchestration. Backend behavior has its own tests.
full_backend = cast(Any, types.ModuleType("games.marvelrivals.full_deployment"))


class FakeFullDeploymentManager:
    pass


full_backend.FullDeploymentManager = FakeFullDeploymentManager
sys.modules["games.marvelrivals.full_deployment"] = full_backend

temporary = cast(Any, types.ModuleType("games.marvelrivals.temporary_deployment"))


class FakeDeploymentError(RuntimeError):
    pass


class FakeDeploymentItem:
    def __init__(self, source: str, relative_path: str) -> None:
        self.source = source
        self.relative_path = relative_path


class FakeShippingProcessWatcher:
    pass


temporary.DeploymentError = FakeDeploymentError
temporary.DeploymentItem = FakeDeploymentItem
temporary.ShippingProcessWatcher = FakeShippingProcessWatcher
temporary.is_managed_mod_source = lambda _source, _mods: True
temporary.is_process_elevated = lambda: True
sys.modules["games.marvelrivals.temporary_deployment"] = temporary

module = cast(
    Any, importlib.import_module("games.marvelrivals.plugins.full_deployment")
)


class FakeDirectory:
    def __init__(self, root: str) -> None:
        self.root = root

    def absoluteFilePath(self, value: str) -> str:
        return str(Path(self.root) / Path(value))

    def absolutePath(self) -> str:
        return self.root


class FakeGame:
    GameBinary = "MarvelGame/Marvel/Binaries/Win64/Marvel-Win64-Shipping.exe"

    def __init__(self, short_name: str = "MarvelRivals") -> None:
        self.short_name = short_name

    def gameShortName(self) -> str:
        return self.short_name

    def gameDirectory(self) -> FakeDirectory:
        return FakeDirectory("/game")

    def dataDirectory(self) -> FakeDirectory:
        return FakeDirectory("/game/MarvelGame/Marvel")


class FakeOrganizer:
    def __init__(
        self,
        *,
        full: bool = False,
        root: bool = False,
        no_usvfs: bool = False,
        no_usvfs_plugin: bool = True,
        game: FakeGame | None = None,
    ) -> None:
        self.full = full
        self.root = root
        self.no_usvfs = no_usvfs
        self.no_usvfs_plugin = no_usvfs_plugin
        self.game = game or FakeGame()
        self.callback: Callable[..., bool] | None = None

    def onAboutToRun(self, callback: Callable[..., bool]) -> bool:
        self.callback = callback
        return True

    def managedGame(self) -> FakeGame:
        return self.game

    def isPluginEnabled(self, plugin_name: str) -> bool:
        if plugin_name == "Marvel Rivals No-USVFS Launch":
            return self.no_usvfs_plugin
        return True

    def pluginSetting(self, plugin_name: str, key: str) -> bool:
        if key == "temporary_full_deployment":
            return self.full
        if (
            plugin_name == "Marvel Rivals Support Plugin"
            and key == "temporary_root_deployment"
        ):
            return self.root
        if (
            plugin_name == "Marvel Rivals No-USVFS Launch"
            and key == "launch_without_usvfs"
        ):
            return self.no_usvfs
        return False


class TestFullDeploymentPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = module.MarvelRivalsFullDeploymentPlugin()
        FakeMessageBox.calls = []
        self.original_elevated = module.is_process_elevated
        self.addCleanup(setattr, module, "is_process_elevated", self.original_elevated)

    def initialize(self, organizer: FakeOrganizer) -> None:
        # Avoid filesystem stale-recovery work in orchestration tests.
        self.plugin._recover_stale_deployment = lambda: None
        self.assertTrue(self.plugin.init(organizer))

    def test_plugin_active_for_marvel_but_full_mode_default_off(self) -> None:
        organizer = FakeOrganizer()
        self.initialize(organizer)
        self.assertTrue(self.plugin.enabledByDefault())
        settings = self.plugin.settings()
        self.assertEqual(len(settings), 1)
        self.assertEqual(settings[0].key, "temporary_full_deployment")
        self.assertFalse(settings[0].default)

    def test_plugin_inactive_by_default_for_other_games(self) -> None:
        organizer = FakeOrganizer(game=FakeGame(short_name="OtherGame"))
        self.initialize(organizer)
        self.assertFalse(self.plugin.enabledByDefault())

    def test_disabled_setting_leaves_existing_behavior_unchanged(self) -> None:
        organizer = FakeOrganizer(full=False, root=True, no_usvfs=False)
        self.initialize(organizer)
        self.assertTrue(
            self.plugin._on_about_to_run(
                "/game/MarvelRivals_Launcher.exe", FakeQDir(), ""
            )
        )
        self.assertEqual(FakeMessageBox.calls, [])

    def test_full_mode_rejects_legacy_root_deployment_overlap(self) -> None:
        organizer = FakeOrganizer(full=True, root=True, no_usvfs=True)
        self.initialize(organizer)
        self.assertFalse(
            self.plugin._on_about_to_run(
                "/game/MarvelRivals_Launcher.exe", FakeQDir(), ""
            )
        )
        self.assertEqual(len(FakeMessageBox.calls), 1)

    def test_full_mode_requires_no_usvfs_launch(self) -> None:
        organizer = FakeOrganizer(full=True, root=False, no_usvfs=False)
        self.initialize(organizer)
        self.assertFalse(
            self.plugin._on_about_to_run(
                "/game/MarvelRivals_Launcher.exe", FakeQDir(), ""
            )
        )
        self.assertEqual(len(FakeMessageBox.calls), 1)

    def test_full_mode_requires_no_usvfs_plugin_to_be_active(self) -> None:
        organizer = FakeOrganizer(
            full=True, root=False, no_usvfs=True, no_usvfs_plugin=False
        )
        self.initialize(organizer)
        self.assertFalse(
            self.plugin._on_about_to_run(
                "/game/MarvelRivals_Launcher.exe", FakeQDir(), ""
            )
        )
        self.assertEqual(len(FakeMessageBox.calls), 1)

    def test_non_elevated_launch_defers_without_writes(self) -> None:
        organizer = FakeOrganizer(full=True, root=False, no_usvfs=True)
        self.initialize(organizer)
        module.is_process_elevated = lambda: False
        self.plugin._get_manager = lambda: self.fail("manager must not be created")
        self.assertTrue(
            self.plugin._on_about_to_run(
                "/game/MarvelRivals_Launcher.exe", FakeQDir(), ""
            )
        )

    def test_other_executable_is_ignored(self) -> None:
        organizer = FakeOrganizer(full=True, root=False, no_usvfs=True)
        self.initialize(organizer)
        self.assertTrue(self.plugin._on_about_to_run("/game/Other.exe", FakeQDir(), ""))
        self.assertEqual(FakeMessageBox.calls, [])


if __name__ == "__main__":
    unittest.main()
