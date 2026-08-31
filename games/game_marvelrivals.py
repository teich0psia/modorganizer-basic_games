import json
import os
import shutil
import sys
from enum import IntEnum, auto
from functools import cached_property
from pathlib import Path
from typing import TypedDict

from PyQt6.QtCore import QDir, QFileInfo, Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import mobase

from ..basic_game import BasicGame
from .marvelrivals.temporary_deployment import (
    DeploymentError,
    DeploymentItem,
    ShippingProcessWatcher,
    TemporaryDeploymentManager,
    is_managed_mod_source,
    is_process_elevated,
)
from .unreal_tabs.constants import DEFAULT_UE4SS_MODS, UE4SSModInfo
from .unreal_tabs.manage_paks.widget import PaksTabWidget
from .unreal_tabs.manage_ue4ss.widget import UE4SSTabWidget


class Content(IntEnum):
    UCAS = auto()
    UTOC = auto()
    PAK = auto()
    UE4SS = auto()
    DLL = auto()
    BK2 = auto()


class MarvelRivalsModDataContent(mobase.ModDataContent):
    GAMECONTENTS: list[tuple[Content, str, str, bool] | tuple[Content, str, str]] = [
        (Content.UCAS, "UCAS", ":/MO/gui/content/geometries"),
        (Content.UTOC, "UTOC", ":/MO/gui/content/inifile"),
        (Content.PAK, "PAK", ":/MO/gui/content/geometries"),
        (Content.UE4SS, "UE4SS", ":/MO/gui/content/script"),
        (Content.DLL, "DLL", ":/MO/gui/content/skse"),
        (Content.BK2, "Video", ":/MO/gui/content/modgroup"),
    ]

    def getAllContents(self) -> list[mobase.ModDataContent.Content]:
        return [
            mobase.ModDataContent.Content(id, name, icon, *filter_only)
            for id, name, icon, *filter_only in self.GAMECONTENTS
        ]

    def walkContent(self, path: str, entry: mobase.FileTreeEntry):
        if entry.isFile():
            match entry.suffix().casefold():
                case "utoc":
                    self.contents.append(Content.UTOC)
                case "ucas":
                    self.contents.append(Content.UCAS)
                case "pak":
                    self.contents.append(Content.PAK)
                case "lua":
                    self.contents.append(Content.UE4SS)
                case "dll":
                    self.contents.append(Content.DLL)
                case "bk2":
                    self.contents.append(Content.BK2)
                case _:
                    pass
        return mobase.IFileTree.WalkReturn.CONTINUE

    def getContentsFor(self, filetree: mobase.IFileTree) -> list[int]:
        self.contents: list[int] = []
        filetree.walk(self.walkContent, "/")
        return list(self.contents)


class ModDetectionCandidate(TypedDict):
    trees: list[mobase.IFileTree | mobase.FileTreeEntry]
    name: str
    display: str
    destination: str
    installtype: str


class MarvelRivalsModDataChecker(mobase.ModDataChecker):
    def __init__(self, organizer: mobase.IOrganizer):
        super().__init__()
        self.organizer: mobase.IOrganizer = organizer
        self.modDetectionCandidates: list[ModDetectionCandidate] = []
        self.processedBasenames: set[str] = set()  # Track already-grouped files
        self.category_groups: dict[str, list[mobase.FileTreeEntry]] = {}

    def sanitizeFolderName(self, name: str) -> str:
        invalid_chars = '+&<>:"|?*\\/'
        for char in invalid_chars:
            name = name.replace(char, "")
        name = "".join(c for c in name if ord(c) >= 32)
        name = name.rstrip(". ")
        if not name:
            name = "Mod"
        return name

    def hasLooseInstallableFiles(self, filetree: mobase.IFileTree) -> bool:
        for entry in filetree:
            if entry.isFile() and entry.suffix().casefold() in {
                "pak",
                "utoc",
                "ucas",
                "bk2",
            }:
                return True
        return False

    def dataLooksValid(
        self, filetree: mobase.IFileTree
    ) -> mobase.ModDataChecker.CheckReturn:
        GameDataUE4SSMods = getattr(
            self.organizer.managedGame(), "GameDataUE4SSRoot", ""
        )
        GameDataPakMods = getattr(self.organizer.managedGame(), "GameDataPakMods", "")
        GameDataMovieMods = getattr(
            self.organizer.managedGame(), "GameDataMovieMods", ""
        )

        if self.hasLooseInstallableFiles(filetree):
            return mobase.ModDataChecker.FIXABLE

        if filetree.exists(GameDataPakMods, mobase.IFileTree.DIRECTORY):
            return mobase.ModDataChecker.VALID
        if filetree.exists(GameDataMovieMods, mobase.IFileTree.DIRECTORY):
            return mobase.ModDataChecker.VALID
        if filetree.exists(GameDataUE4SSMods, mobase.IFileTree.DIRECTORY):
            return mobase.ModDataChecker.VALID
        return mobase.ModDataChecker.FIXABLE

    def moveTreeContent(
        self,
        installtype: str,
        entries: list[mobase.IFileTree | mobase.FileTreeEntry],
        targettree: mobase.IFileTree,
        destination: str,
    ) -> None:
        if installtype == "virtual":
            for entry in entries:
                targettree.move(entry, destination, mobase.IFileTree.MERGE)
        elif installtype == "os":
            entry = entries[0]
            if isinstance(entry, mobase.IFileTree):
                mod_name_val = entry.name()
                mod_path = os.path.join(self.organizer.modsPath(), mod_name_val)
                insideMods = os.path.join(mod_path, destination)
                os.makedirs(insideMods, exist_ok=True)

                destination_root = (
                    destination.replace("\\", "/").split("/", 1)[0].casefold()
                )

                for subentry in entry:
                    mod_file = subentry.name()

                    if subentry.isDir() and mod_file.casefold() == destination_root:
                        continue

                    src = os.path.join(mod_path, mod_file)
                    dst = os.path.join(mod_path, destination, mod_file)
                    shutil.move(src, dst)
            return None

    def addModDetectionCandidate(
        self,
        trees: list[mobase.IFileTree | mobase.FileTreeEntry],
        name: str,
        category: str,
        destination: str,
        installtype: str,
    ) -> None:
        tree_name = self.sanitizeFolderName(trees[0].name() if trees else "Unknown")

        self.modDetectionCandidates.append(
            {
                "trees": trees,
                "name": tree_name,
                "display": f"{name} ({category})",
                "destination": destination,
                "installtype": installtype,
            }
        )

    def showModDetectionDialog(self) -> set[int] | None:
        if not self.modDetectionCandidates:
            return set()

        dialog = QDialog()
        dialog.setWindowTitle("Found Mods")

        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Select the mods to install:"))

        listWidget = QListWidget()
        listWidget.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        for candidate in self.modDetectionCandidates:
            item = QListWidgetItem(candidate["display"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            listWidget.addItem(item)

        layout.addWidget(listWidget)

        selectButtons = QHBoxLayout()
        selectAllButton = QPushButton("Select All")
        selectNoneButton = QPushButton("Select None")
        selectAllButton.clicked.connect(  # type: ignore # type: ignore
            lambda: self.setDialogSelection(listWidget, True)
        )
        selectNoneButton.clicked.connect(  # type: ignore # type: ignore
            lambda: self.setDialogSelection(listWidget, False)
        )
        selectButtons.addWidget(selectAllButton)
        selectButtons.addWidget(selectNoneButton)
        layout.addLayout(selectButtons)

        buttonBox = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttonBox.accepted.connect(lambda: dialog.accept())  # type: ignore
        buttonBox.rejected.connect(lambda: dialog.reject())  # type: ignore
        layout.addWidget(buttonBox)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None

        selectedIndexes: set[int] = set()
        for index in range(listWidget.count()):
            item = listWidget.item(index)
            if (
                isinstance(item, QListWidgetItem)
                and item.checkState() == Qt.CheckState.Checked
            ):
                selectedIndexes.add(index)

        return selectedIndexes

    def setDialogSelection(self, listWidget: QListWidget, select: bool) -> None:
        state = Qt.CheckState.Checked if select else Qt.CheckState.Unchecked
        for index in range(listWidget.count()):
            item = listWidget.item(index)
            if isinstance(item, QListWidgetItem):
                item.setCheckState(state)

    def collectModCandidates(
        self,
        path: str,
        entry: mobase.FileTreeEntry,
        installtype: str = "virtual",
    ):
        category = None
        entryext = "None"
        basename = "Unknown"
        GameDataUE4SSRootDir = getattr(
            self.organizer.managedGame(), "GameDataUE4SSRoot", ""
        )
        GameDataUE4SSModsDir = GameDataUE4SSRootDir + "/Mods"
        GameDataPakModsDir = getattr(
            self.organizer.managedGame(), "GameDataPakMods", ""
        )
        GameDataMovieModsDir = getattr(
            self.organizer.managedGame(), "GameDataMovieMods", ""
        )

        if installtype == "os" and isinstance(entry, mobase.IFileTree):
            for subentry in entry:
                entryext = os.path.splitext(subentry.name())[1].removeprefix(".")
                basename = os.path.splitext(subentry.name())[0]
        else:
            entryext = entry.suffix().casefold()
            basename = os.path.splitext(entry.name())[0]

        if isinstance(entry, mobase.IFileTree) and entry.isDir():
            if entry.exists("ue4ss.dll", mobase.IFileTree.FILE) or entry.exists(
                "dsound.dll", mobase.IFileTree.FILE
            ):
                category = "Root"
            elif entry.exists(
                "Scripts", mobase.IFileTree.DIRECTORY
            ) and not entry.exists("ue4ss.dll", mobase.IFileTree.FILE):
                disallowedFolders = {"mods"}
                tree_path = entry.path()
                tree_path_lower = tree_path.replace("\\", "/").casefold()
                if not disallowedFolders & set(tree_path_lower.split("/")):
                    category = "UE4SS"

        # Check single file for correct extentions
        match entryext:
            case "pak" | "utoc" | "ucas":
                category = "Paks"
            case "bk2":
                category = "Movie"
            case _:
                pass

        if category is not None:
            basename = basename + " " + category

            if basename not in self.category_groups:
                self.category_groups[basename] = []

            self.category_groups[basename].append(entry)

            # Add grouped entries as candidates
            for basename, entries in self.category_groups.items():
                if basename not in self.processedBasenames:
                    self.processedBasenames.add(basename)
                    sanitized_name = self.sanitizeFolderName(entries[0].name())

                    if category == "UE4SS":
                        destination = GameDataUE4SSModsDir + "/"
                    elif category == "Root":
                        destination = GameDataUE4SSRootDir + "/"
                    elif category == "Paks":
                        destination = GameDataPakModsDir + "/"
                    elif category == "Movie":
                        destination = GameDataMovieModsDir + "/"
                    else:
                        destination = "/"

                    if installtype == "os":
                        # Single file/entry
                        self.addModDetectionCandidate(
                            [entry],
                            sanitized_name,
                            f"{category} Mod",
                            destination,
                            installtype,
                        )
                    else:
                        candidate_entries = entries

                        if category == "Root":
                            candidate_entries: list[mobase.FileTreeEntry] = []
                            for root_entry in entries:
                                if (
                                    isinstance(root_entry, mobase.IFileTree)
                                    and root_entry.isDir()
                                ):
                                    candidate_entries.extend(list(root_entry))
                                else:
                                    candidate_entries.append(root_entry)

                        self.addModDetectionCandidate(
                            candidate_entries,
                            sanitized_name,
                            f"{category} Mod",
                            destination,
                            installtype,
                        )

        return mobase.IFileTree.WalkReturn.CONTINUE

    def fix(self, filetree: mobase.IFileTree) -> mobase.IFileTree | None:
        self.modDetectionCandidates = []
        self.processedBasenames = set()
        self.category_groups = {}
        UnZippedInstallation = False
        newtree = filetree.createOrphanTree("Fixed Tree")

        if filetree.name() != "":
            # Initial Check on Main Directory
            self.collectModCandidates("/", filetree, installtype="os")
            UnZippedInstallation = True
        else:
            # Initial Check on Main Directory
            self.collectModCandidates("/", filetree)
            filetree.walk(self.collectModCandidates, "/")

        if len(self.modDetectionCandidates) == 1:
            selectedIndexes = {0}
        else:
            selectedIndexes = self.showModDetectionDialog()
            if selectedIndexes is None:
                return None

        if not UnZippedInstallation:
            filetree = newtree

        for index in selectedIndexes:
            candidate = self.modDetectionCandidates[index]
            self.moveTreeContent(
                candidate["installtype"],
                candidate["trees"],
                filetree,
                candidate["destination"],
            )

        return filetree


class MarvelRivalsGame(BasicGame):
    Name = "Marvel Rivals Support Plugin"
    Author = "ModWorkshop"
    Version = "1"
    GameName = "Marvel Rivals"
    GameLauncher = "MarvelRivals_Launcher.exe"
    GameShortName = "MarvelRivals"
    GameSteamId = 2767030
    GameBinary = "MarvelGame/Marvel/Binaries/Win64/Marvel-Win64-Shipping.exe"
    GameDataPath = "MarvelGame/Marvel"
    GameDataUE4SSRoot = "Binaries/Win64"
    GameDataPakMods = "Content/Paks/~Mods"
    GameDataMovieMods = "Content/Movies"
    GameDocumentsDirectory = "%LOCALAPPDATA%/MarvelGame/Saved/Config/Windows"
    GameSavesDirectory = "%LOCALAPPDATA%/MarvelGame/Saved/SaveGames"
    GameSaveExtension = "sav"
    TemporaryRootDeploymentSetting = "temporary_root_deployment"
    _main_window: QMainWindow
    _ue4ss_tab: UE4SSTabWidget
    _paks_tab: PaksTabWidget
    _temporary_manager: TemporaryDeploymentManager | None
    _temporary_watcher: ShippingProcessWatcher | None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        super().init(organizer)
        self._temporary_manager = None
        self._temporary_watcher = None
        self.dataChecker = MarvelRivalsModDataChecker(organizer)
        self._register_feature(self.dataChecker)
        self._register_feature(MarvelRivalsModDataContent())
        organizer.onUserInterfaceInitialized(self.initTab)
        if not organizer.onAboutToRun(self._on_about_to_run):
            print(
                "Failed to register Marvel Rivals onAboutToRun callback!",
                file=sys.stderr,
            )
            return False
        self._recover_stale_deployment()
        return True

    def settings(self) -> list[mobase.PluginSetting]:
        return [
            mobase.PluginSetting(
                self.TemporaryRootDeploymentSetting,
                "Use Temporary Root Deployment instead of Forced Load",
                False,
            )
        ]

    def _log_temporary_deployment(self, message: str) -> None:
        print(f"[Marvel Rivals] {message}", file=sys.stderr)

    def _temporary_deployment_enabled(self) -> bool:
        organizer = getattr(self, "_organizer", None)
        if organizer is None:
            return False
        return bool(
            organizer.pluginSetting(self.name(), self.TemporaryRootDeploymentSetting)
        )

    def _launcher_path(self) -> Path:
        return Path(self.gameDirectory().absoluteFilePath(self.GameLauncher))

    def _is_launcher(self, binary: str) -> bool:
        return os.path.abspath(binary).casefold() == os.path.abspath(
            self._launcher_path()
        ).casefold()

    def _get_temporary_manager(self) -> TemporaryDeploymentManager:
        if self._temporary_manager is None:
            journal = (
                Path(self._organizer.pluginDataPath())
                / "marvelrivals"
                / "temporary_root_deployment.json"
            )
            self._temporary_manager = TemporaryDeploymentManager(
                Path(self.dataDirectory().absolutePath()),
                journal,
                shipping_process_name=QFileInfo(self.binaryName()).fileName(),
                shipping_executable_path=Path(
                    self.gameDirectory().absoluteFilePath(self.GameBinary)
                ),
                log=self._log_temporary_deployment,
            )
        return self._temporary_manager

    def _recover_stale_deployment(self) -> None:
        if not self.isInstalled() or not self.dataDirectory().exists():
            return
        try:
            self._get_temporary_manager().recover_stale()
        except DeploymentError as error:
            self._log_temporary_deployment(f"stale recovery failed: {error}")

    def _discover_root_payloads(self) -> list[DeploymentItem]:
        tree = self._organizer.virtualFileTree()
        if not isinstance(tree, mobase.IFileTree):
            return []

        root_entry = tree.find(self.GameDataUE4SSRoot, mobase.IFileTree.DIRECTORY)
        if not isinstance(root_entry, mobase.IFileTree):
            return []

        data_root = Path(self.dataDirectory().absolutePath())
        mods_root = Path(self._organizer.modsPath())
        items: list[DeploymentItem] = []

        def collect(current: mobase.IFileTree, prefix: str = "") -> None:
            for entry in current:
                name = entry.name()
                relative = f"{prefix}/{name}" if prefix else name

                if entry.isDir():
                    if not prefix and name.casefold() == "mods":
                        continue
                    if isinstance(entry, mobase.IFileTree):
                        collect(entry, relative)
                    continue

                if not entry.isFile():
                    continue

                virtual_path = f"{self.GameDataUE4SSRoot}/{relative}".replace(
                    "\\", "/"
                )
                source_value = self._organizer.resolvePath(virtual_path)
                if not source_value:
                    raise DeploymentError(
                        f"Could not resolve Root mod source: {virtual_path}"
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

        collect(root_entry)
        return items

    def _show_deployment_error(self, error: DeploymentError) -> None:
        QMessageBox.critical(
            getattr(self, "_main_window", None),
            "Marvel Rivals",
            str(error),
        )

    def _on_about_to_run(self, binary: str, _cwd: QDir, _args: str) -> bool:
        if not self._is_launcher(binary) or not self._temporary_deployment_enabled():
            return True

        # MO2 reports ERROR_ELEVATION_REQUIRED only after onAboutToRun. Deferring all
        # physical writes here ensures the non-elevated instance cannot leave a partial
        # deployment behind when MO2 restarts itself as administrator.
        if not is_process_elevated():
            self._log_temporary_deployment(
                "MO2 is not elevated; deferring temporary Root deployment"
            )
            return True

        manager = self._get_temporary_manager()
        try:
            if not manager.recover_stale():
                raise DeploymentError(
                    "An existing Marvel Rivals deployment session is still active."
                )
            items = self._discover_root_payloads()
            if not items:
                self._log_temporary_deployment("no enabled Root payloads found")
                return True

            journal = manager.deploy(items)
            watcher = ShippingProcessWatcher(
                manager,
                shipping_process_name=QFileInfo(self.binaryName()).fileName(),
                shipping_executable_path=Path(
                    self.gameDirectory().absoluteFilePath(self.GameBinary)
                ),
                session_started_at=journal.started_at,
                log=self._log_temporary_deployment,
            )
            self._temporary_watcher = watcher
            watcher.start()
            return True
        except DeploymentError as error:
            self._show_deployment_error(error)
            return False

    def initTab(self, main_window: QMainWindow):
        if self._organizer.managedGame() != self:
            return
        self._main_window = main_window
        tab_widget: QTabWidget = main_window.findChild(QTabWidget, "tabWidget")
        if not tab_widget or not tab_widget.findChild(QWidget, "espTab"):
            return
        self._ue4ss_tab = UE4SSTabWidget(main_window, self._organizer)
        plugin_tab = tab_widget.findChild(QWidget, "espTab")
        tab_index = tab_widget.indexOf(plugin_tab) + 1
        if not tab_widget.isTabVisible(tab_widget.indexOf(plugin_tab)):
            tab_index += 1
        tab_widget.insertTab(tab_index, self._ue4ss_tab, "UE4SS")
        self._paks_tab = PaksTabWidget(main_window, self._organizer)
        tab_index += 1
        tab_widget.insertTab(tab_index, self._paks_tab, "Paks")

    def executables(self):
        return [
            mobase.ExecutableInfo(
                "Marvel Rivals",
                QFileInfo(self.gameDirectory().absoluteFilePath(self.GameLauncher)),
            )
        ]

    @cached_property
    def baseDlls(self) -> set[str]:
        base_dir = Path(self.gameDirectory().absolutePath())
        return {str(f.relative_to(base_dir)) for f in base_dir.glob("*.dll")}

    def executableForcedLoads(self) -> list[mobase.ExecutableForcedLoadSetting]:
        try:
            efls = super().executableForcedLoads()
        except AttributeError:
            efls = []
        if self._temporary_deployment_enabled():
            return efls

        libs: set[str] = set()
        tree: mobase.IFileTree | mobase.FileTreeEntry | None = (
            self._organizer.virtualFileTree()
        )
        if type(tree) is not mobase.IFileTree:
            return efls
        for e in tree:
            relpath = e.pathFrom(tree)
            if relpath and e.hasSuffix("dll") and relpath not in self.baseDlls:
                libs.add(relpath)

        shipping_binary = QFileInfo(self.binaryName()).fileName()
        return efls + [
            mobase.ExecutableForcedLoadSetting(shipping_binary, lib).withEnabled(True)
            for lib in libs
        ]

    def writeDefaultMods(self, profile: QDir):
        ue4ss_mods_txt = QFileInfo(profile.absoluteFilePath("mods.txt"))
        ue4ss_mods_json = QFileInfo(profile.absoluteFilePath("mods.json"))
        if not ue4ss_mods_txt.exists():
            with open(ue4ss_mods_txt.absoluteFilePath(), "w") as mods_txt:
                for mod in DEFAULT_UE4SS_MODS:
                    mods_txt.write(f"{mod['mod_name']} : 1\n")
        if not ue4ss_mods_json.exists():
            mods_data: list[UE4SSModInfo] = []
            for mod in DEFAULT_UE4SS_MODS:
                mods_data.append({"mod_name": mod["mod_name"], "mod_enabled": True})
            with open(ue4ss_mods_json.absoluteFilePath(), "w") as mods_json:
                mods_json.write(json.dumps(mods_data, indent=4))

    def iniFiles(self):
        return ["GameUserSettings.ini", "Engine.ini", "Input.ini"]

    def initializeProfile(self, directory: QDir, settings: mobase.ProfileSetting):
        self.writeDefaultMods(directory)

        base_data_dir = self.dataDirectory().absolutePath()

        paksDirectory = QDir(base_data_dir + "/" + self.GameDataPakMods)
        ue4ssDirectory = QDir(base_data_dir + "/" + self.GameDataUE4SSRoot + "/Mods")
        movieDirectory = QDir(base_data_dir + "/" + self.GameDataMovieMods)

        if not paksDirectory.exists():
            os.makedirs(paksDirectory.absolutePath())
        if not ue4ssDirectory.exists():
            os.makedirs(ue4ssDirectory.absolutePath())
        if not movieDirectory.exists():
            os.makedirs(movieDirectory.absolutePath())
        super().initializeProfile(directory, settings)
