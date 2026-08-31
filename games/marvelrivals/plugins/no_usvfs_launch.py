from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QDir
from PyQt6.QtWidgets import QMessageBox

import mobase

from ..usvfs_blacklist import UsvfsBlacklistError, blacklist_executable


class MarvelRivalsNoUsvfsLaunchPlugin(mobase.IPlugin):
    GameShortName = "MarvelRivals"
    LauncherName = "MarvelRivals_Launcher.exe"
    LaunchWithoutUsvfsSetting = "launch_without_usvfs"

    def __init__(self) -> None:
        mobase.IPlugin.__init__(self)
        self._organizer: mobase.IOrganizer | None = None
        self._blacklisted = False

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        return organizer.onAboutToRun(self._on_about_to_run)

    def name(self) -> str:
        return "Marvel Rivals No-USVFS Launch"

    def author(self) -> str:
        return "teich0psia"

    def description(self) -> str:
        return (
            "Experimental Marvel Rivals launch mode that prevents USVFS injection "
            "into the game launcher. The plugin stays active so it can receive MO2 "
            "launch callbacks; the launch mode itself is disabled by default. Restart "
            "Mod Organizer 2 after turning the launch mode off to restore VFS in the "
            "current session."
        )

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(0, 2, 0)

    def settings(self) -> list[mobase.PluginSetting]:
        return [
            mobase.PluginSetting(
                self.LaunchWithoutUsvfsSetting,
                "Launch Marvel Rivals without USVFS (experimental)",
                False,
            )
        ]

    def enabledByDefault(self) -> bool:
        organizer = self._organizer
        if organizer is None:
            return False
        game = organizer.managedGame()
        return game is not None and game.gameShortName() == self.GameShortName

    def _launch_mode_enabled(self) -> bool:
        organizer = self._organizer
        if organizer is None:
            return False
        return bool(
            organizer.pluginSetting(self.name(), self.LaunchWithoutUsvfsSetting)
        )

    def _target_launcher(self) -> Path | None:
        organizer = self._organizer
        if organizer is None:
            return None

        game = organizer.managedGame()
        if game is None or game.gameShortName() != self.GameShortName:
            return None

        return Path(game.gameDirectory().absoluteFilePath(self.LauncherName))

    def _is_target_launcher(self, binary: str) -> bool:
        launcher = self._target_launcher()
        if launcher is None:
            return False
        return os.path.abspath(binary).casefold() == os.path.abspath(launcher).casefold()

    def _on_about_to_run(self, binary: str, _cwd: QDir, _args: str) -> bool:
        if (
            not self._launch_mode_enabled()
            or not self._is_target_launcher(binary)
            or self._blacklisted
        ):
            return True

        try:
            library = blacklist_executable(
                QCoreApplication.applicationDirPath(), self.LauncherName
            )
        except UsvfsBlacklistError as error:
            QMessageBox.critical(None, "Marvel Rivals", str(error))
            return False

        self._blacklisted = True
        print(
            f"[Marvel Rivals] USVFS blacklist enabled for {self.LauncherName} "
            f"via {library}",
            file=sys.stderr,
        )
        return True
