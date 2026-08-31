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


mobase = cast(Any, types.ModuleType("mobase"))
mobase.IPlugin = FakeIPlugin
mobase.IOrganizer = object
mobase.PluginSetting = FakePluginSetting
mobase.VersionInfo = FakeVersionInfo
sys.modules["mobase"] = mobase

qtcore = cast(Any, types.ModuleType("PyQt6.QtCore"))


class FakeQCoreApplication:
    @staticmethod
    def applicationDirPath() -> str:
        return r"C:\Modding\MO2"


class FakeQDir:
    pass


qtcore.QCoreApplication = FakeQCoreApplication
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

module = cast(
    Any, importlib.import_module("games.marvelrivals.plugins.no_usvfs_launch")
)


class FakeGameDirectory:
    def __init__(self, root: str) -> None:
        self.root = root

    def absoluteFilePath(self, name: str) -> str:
        return str(Path(self.root) / name)


class FakeGame:
    def __init__(self, short_name: str = "MarvelRivals", root: str = "/game") -> None:
        self.short_name = short_name
        self.root = root

    def gameShortName(self) -> str:
        return self.short_name

    def gameDirectory(self) -> FakeGameDirectory:
        return FakeGameDirectory(self.root)


class FakeOrganizer:
    def __init__(self, *, setting: bool = False, game: FakeGame | None = None) -> None:
        self.setting = setting
        self.game = game or FakeGame()
        self.callback: Callable[..., bool] | None = None

    def onAboutToRun(self, callback: Callable[..., bool]) -> bool:
        self.callback = callback
        return True

    def managedGame(self) -> FakeGame:
        return self.game

    def pluginSetting(self, _plugin_name: str, _key: str) -> bool:
        return self.setting


class TestNoUsvfsPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = module.MarvelRivalsNoUsvfsLaunchPlugin()
        self.calls: list[tuple[str, str]] = []
        original_blacklist = module.blacklist_executable
        self.addCleanup(setattr, module, "blacklist_executable", original_blacklist)
        FakeMessageBox.calls = []

        def fake_blacklist(application_dir: str, executable_name: str) -> Path:
            self.calls.append((application_dir, executable_name))
            return Path(application_dir) / "usvfs_x64.dll"

        module.blacklist_executable = fake_blacklist

    def test_plugin_active_by_default_for_marvel_but_launch_mode_default_off(self) -> None:
        organizer = FakeOrganizer()
        self.plugin.init(organizer)
        self.assertTrue(self.plugin.enabledByDefault())
        settings = self.plugin.settings()
        self.assertEqual(len(settings), 1)
        self.assertEqual(settings[0].key, "launch_without_usvfs")
        self.assertFalse(settings[0].default)

    def test_plugin_inactive_by_default_for_other_games(self) -> None:
        organizer = FakeOrganizer(game=FakeGame(short_name="OtherGame"))
        self.plugin.init(organizer)
        self.assertFalse(self.plugin.enabledByDefault())

    def test_init_registers_callback(self) -> None:
        organizer = FakeOrganizer()
        self.assertTrue(self.plugin.init(organizer))
        self.assertIsNotNone(organizer.callback)

    def test_disabled_setting_does_nothing(self) -> None:
        organizer = FakeOrganizer(setting=False)
        self.plugin.init(organizer)
        self.assertTrue(
            self.plugin._on_about_to_run(
                "/game/MarvelRivals_Launcher.exe", FakeQDir(), ""
            )
        )
        self.assertEqual(self.calls, [])

    def test_enabled_setting_blacklists_target_launcher(self) -> None:
        organizer = FakeOrganizer(setting=True)
        self.plugin.init(organizer)
        self.assertTrue(
            self.plugin._on_about_to_run(
                "/game/MarvelRivals_Launcher.exe", FakeQDir(), ""
            )
        )
        self.assertEqual(
            self.calls,
            [(r"C:\Modding\MO2", "MarvelRivals_Launcher.exe")],
        )

    def test_enabled_setting_ignores_other_executable(self) -> None:
        organizer = FakeOrganizer(setting=True)
        self.plugin.init(organizer)
        self.assertTrue(self.plugin._on_about_to_run("/game/Other.exe", FakeQDir(), ""))
        self.assertEqual(self.calls, [])

    def test_enabled_setting_ignores_other_game(self) -> None:
        organizer = FakeOrganizer(
            setting=True, game=FakeGame(short_name="OtherGame")
        )
        self.plugin.init(organizer)
        self.assertTrue(
            self.plugin._on_about_to_run(
                "/game/MarvelRivals_Launcher.exe", FakeQDir(), ""
            )
        )
        self.assertEqual(self.calls, [])

    def test_blacklist_is_idempotent_within_session(self) -> None:
        organizer = FakeOrganizer(setting=True)
        self.plugin.init(organizer)
        for _ in range(2):
            self.assertTrue(
                self.plugin._on_about_to_run(
                    "/game/MarvelRivals_Launcher.exe", FakeQDir(), ""
                )
            )
        self.assertEqual(len(self.calls), 1)

    def test_blacklist_error_cancels_launch(self) -> None:
        organizer = FakeOrganizer(setting=True)
        self.plugin.init(organizer)

        def fail_blacklist(_application_dir: str, _executable_name: str) -> Path:
            raise module.UsvfsBlacklistError("synthetic failure")

        module.blacklist_executable = fail_blacklist
        self.assertFalse(
            self.plugin._on_about_to_run(
                "/game/MarvelRivals_Launcher.exe", FakeQDir(), ""
            )
        )
        self.assertEqual(len(FakeMessageBox.calls), 1)


if __name__ == "__main__":
    unittest.main()
