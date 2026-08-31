from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtCore import QDir
from PyQt6.QtWidgets import QMessageBox

import mobase

from ..full_deployment import FullDeploymentManager
from ..full_watcher import FullDeploymentShippingWatcher
from ..temporary_deployment import (
    DeploymentError,
    DeploymentItem,
    is_managed_mod_source,
    is_process_elevated,
)


class MarvelRivalsFullDeploymentPlugin(mobase.IPlugin):
    GameShortName = "MarvelRivals"
    LauncherName = "MarvelRivals_Launcher.exe"
    FullDeploymentSetting = "temporary_full_deployment"
    RootPluginName = "Marvel Rivals Support Plugin"
    RootDeploymentSetting = "temporary_root_deployment"
    NoUsvfsPluginName = "Marvel Rivals No-USVFS Launch"
    NoUsvfsSetting = "launch_without_usvfs"
    DeploymentRoots = (
        "Binaries/Win64",
        "Content/Paks/~Mods",
        "Content/Movies",
    )

    def __init__(self) -> None:
        mobase.IPlugin.__init__(self)
        self._organizer: mobase.IOrganizer | None = None
        self._manager: FullDeploymentManager | None = None
        self._watcher: FullDeploymentShippingWatcher | None = None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        if not organizer.onAboutToRun(self._on_about_to_run):
            return False
        self._recover_stale_deployment()
        return True

    def name(self) -> str:
        return "Marvel Rivals Temporary Full Deployment"

    def author(self) -> str:
        return "teich0psia"

    def description(self) -> str:
        return (
            "Experimental physical deployment for all supported Marvel Rivals mod "
            "files. "
            "Large PAK/UTOC/UCAS/BK2 payloads use hardlinks when possible and fall "
            "back to copies."
        )

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(0, 2, 0)

    def settings(self) -> list[mobase.PluginSetting]:
        return [
            mobase.PluginSetting(
                self.FullDeploymentSetting,
                "Physically deploy all supported Marvel Rivals mods (experimental)",
                False,
            )
        ]

    def enabledByDefault(self) -> bool:
        return self._managed_game() is not None

    def _managed_game(self) -> mobase.IPluginGame | None:
        organizer = self._organizer
        if organizer is None:
            return None
        game = organizer.managedGame()
        if game is None or game.gameShortName() != self.GameShortName:
            return None
        return game

    def _mode_enabled(self) -> bool:
        organizer = self._organizer
        if organizer is None:
            return False
        return bool(organizer.pluginSetting(self.name(), self.FullDeploymentSetting))

    def _root_deployment_enabled(self) -> bool:
        organizer = self._organizer
        if organizer is None:
            return False
        return bool(
            organizer.pluginSetting(self.RootPluginName, self.RootDeploymentSetting)
        )

    def _no_usvfs_enabled(self) -> bool:
        organizer = self._organizer
        if organizer is None:
            return False
        return organizer.isPluginEnabled(self.NoUsvfsPluginName) and bool(
            organizer.pluginSetting(self.NoUsvfsPluginName, self.NoUsvfsSetting)
        )

    def _launcher_path(self) -> Path | None:
        game = self._managed_game()
        if game is None:
            return None
        return Path(game.gameDirectory().absoluteFilePath(self.LauncherName))

    def _is_target_launcher(self, binary: str) -> bool:
        launcher = self._launcher_path()
        if launcher is None:
            return False
        return os.path.abspath(binary).casefold() == os.path.abspath(
            launcher
        ).casefold()

    def _game_binary_path(self) -> Path:
        game = self._managed_game()
        if game is None:
            raise DeploymentError("Marvel Rivals is not the managed game.")
        game_binary = getattr(game, "GameBinary", "")
        if not game_binary:
            raise DeploymentError("Marvel Rivals Shipping executable is unavailable.")
        return Path(game.gameDirectory().absoluteFilePath(game_binary))

    def _data_root(self) -> Path:
        game = self._managed_game()
        if game is None:
            raise DeploymentError("Marvel Rivals is not the managed game.")
        return Path(game.dataDirectory().absolutePath())

    def _get_manager(self) -> FullDeploymentManager:
        if self._manager is None:
            organizer = self._organizer
            if organizer is None:
                raise DeploymentError("Mod Organizer is unavailable.")
            journal = (
                Path(organizer.pluginDataPath())
                / "marvelrivals"
                / "temporary_full_deployment.json"
            )
            shipping_path = self._game_binary_path()
            self._manager = FullDeploymentManager(
                self._data_root(),
                journal,
                shipping_process_name=shipping_path.name,
                shipping_executable_path=shipping_path,
                log=self._log,
            )
        return self._manager

    def _log(self, message: str) -> None:
        print(f"[Marvel Rivals] {message}", file=sys.stderr)

    def _recover_stale_deployment(self) -> None:
        game = self._managed_game()
        if game is None:
            return
        try:
            data_root = self._data_root()
            if not data_root.exists():
                return
            self._get_manager().recover_stale()
        except DeploymentError as error:
            self._log(f"full deployment stale recovery failed: {error}")

    def _collect_tree(
        self,
        tree: mobase.IFileTree,
        virtual_root: str,
        items: list[DeploymentItem],
    ) -> None:
        organizer = self._organizer
        if organizer is None:
            return
        mods_root = Path(organizer.modsPath())
        data_root = self._data_root()

        def collect(current: mobase.IFileTree, prefix: str = "") -> None:
            for entry in current:
                name = entry.name()
                relative = f"{prefix}/{name}" if prefix else name
                if entry.isDir():
                    if isinstance(entry, mobase.IFileTree):
                        collect(entry, relative)
                    continue
                if not entry.isFile():
                    continue

                virtual_path = f"{virtual_root}/{relative}".replace("\\", "/")
                source_value = organizer.resolvePath(virtual_path)
                if not source_value:
                    raise DeploymentError(
                        f"Could not resolve mod source: {virtual_path}"
                    )
                source = Path(source_value)
                if not is_managed_mod_source(source, mods_root):
                    continue

                destination = data_root.joinpath(*virtual_path.split("/"))
                if os.path.abspath(source).casefold() == os.path.abspath(
                    destination
                ).casefold():
                    continue
                items.append(DeploymentItem(str(source), virtual_path))

        collect(tree)

    def _discover_payloads(self) -> list[DeploymentItem]:
        organizer = self._organizer
        if organizer is None:
            return []
        tree = organizer.virtualFileTree()
        if not isinstance(tree, mobase.IFileTree):
            return []

        items: list[DeploymentItem] = []
        for virtual_root in self.DeploymentRoots:
            root_entry = tree.find(virtual_root, mobase.IFileTree.DIRECTORY)
            if isinstance(root_entry, mobase.IFileTree):
                self._collect_tree(root_entry, virtual_root, items)
        return items

    def _show_error(self, error: DeploymentError) -> None:
        QMessageBox.critical(None, "Marvel Rivals", str(error))

    def _validate_mode_dependencies(self) -> None:
        if self._root_deployment_enabled():
            raise DeploymentError(
                "Temporary Full Deployment already includes Root files. Disable "
                "'Use Temporary Root Deployment instead of Forced Load' first."
            )
        if not self._no_usvfs_enabled():
            raise DeploymentError(
                "Temporary Full Deployment currently requires 'Launch Marvel Rivals "
                "without USVFS (experimental)' to be enabled."
            )

    def _on_about_to_run(self, binary: str, _cwd: QDir, _args: str) -> bool:
        if not self._mode_enabled() or not self._is_target_launcher(binary):
            return True

        try:
            self._validate_mode_dependencies()
        except DeploymentError as error:
            self._show_error(error)
            return False

        if not is_process_elevated():
            self._log("MO2 is not elevated; deferring temporary full deployment")
            return True

        manager = self._get_manager()
        try:
            if not manager.recover_stale():
                raise DeploymentError(
                    "An existing Marvel Rivals full deployment session is still active."
                )
            items = self._discover_payloads()
            if not items:
                self._log("no enabled payloads found")
                return True

            journal = manager.deploy(items)
            shipping_path = self._game_binary_path()
            watcher = FullDeploymentShippingWatcher(
                manager,
                shipping_process_name=shipping_path.name,
                shipping_executable_path=shipping_path,
                session_started_at=journal.started_at,
                log=self._log,
            )
            self._watcher = watcher
            watcher.start()
            return True
        except DeploymentError as error:
            self._show_error(error)
            return False
