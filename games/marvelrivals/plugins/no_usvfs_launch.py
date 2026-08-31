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
            "into the game launcher. Disabled by default. Restart Mod Organizer 2 "
            "after disabling it to restore normal VFS launch behavior."
        )

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(0, 1, 0)

    def settings(self) -> list[mobase.PluginSetting]:
        return []

    def enabledByDefault(self) -> bool:
        return False

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
        if not self._is_target_launcher(binary) or self._blacklisted:
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
