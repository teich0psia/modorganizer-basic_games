from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterable, Protocol

import psutil


JOURNAL_VERSION = 1
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class DeploymentError(RuntimeError):
    pass


class DeploymentConflictError(DeploymentError):
    pass


class UnsafePathError(DeploymentError):
    pass


@dataclass(frozen=True)
class DeploymentItem:
    source: str
    relative_path: str


@dataclass
class JournalItem:
    source: str
    relative_path: str
    destination: str
    sha256: str | None = None
    owned: bool = False


@dataclass
class DeploymentJournal:
    version: int
    session_id: str
    owner_pid: int
    owner_started_at: float
    deployment_root: str
    started_at: float
    items: list[JournalItem]
    created_directories: list[str]


class DeploymentLogger(Protocol):
    def __call__(self, message: str) -> None: ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_process_elevated() -> bool:
    if os.name != "nt":
        return True

    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _validate_windows_relative_path(value: str) -> PureWindowsPath:
    if not value or "\x00" in value:
        raise UnsafePathError("Root mod contains an invalid empty path.")

    normalized = value.replace("/", "\\")
    path = PureWindowsPath(normalized)
    if path.is_absolute() or path.drive or path.root:
        raise UnsafePathError(f"Root mod path must be relative: {value}")

    if any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafePathError(f"Root mod path is not safe: {value}")

    for part in path.parts:
        if ":" in part:
            raise UnsafePathError(f"Root mod path contains a drive or stream: {value}")
        if part.endswith((" ", ".")):
            raise UnsafePathError(f"Root mod path has an invalid Windows name: {value}")
        stem = part.split(".", 1)[0].upper()
        if stem in WINDOWS_RESERVED_NAMES:
            raise UnsafePathError(
                f"Root mod path uses a reserved Windows name: {value}"
            )

    return path


def _same_path(left: Path, right: Path) -> bool:
    return os.path.abspath(left).casefold() == os.path.abspath(right).casefold()


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


class TemporaryDeploymentManager:
    def __init__(
        self,
        deployment_root: Path,
        journal_path: Path,
        *,
        shipping_process_name: str,
        log: DeploymentLogger | None = None,
    ) -> None:
        self.deployment_root = deployment_root.resolve()
        self.journal_path = journal_path
        self.lock_path = journal_path.with_suffix(journal_path.suffix + ".lock")
        self.shipping_process_name = shipping_process_name.casefold()
        self.owner_started_at = psutil.Process(os.getpid()).create_time()
        self._log = log or (lambda _message: None)
        self._lock_owned = False

    def _write_journal(self, journal: DeploymentJournal) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.journal_path.with_suffix(self.journal_path.suffix + ".tmp")
        payload = asdict(journal)
        with temp_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, self.journal_path)

    def _read_journal(self) -> DeploymentJournal | None:
        try:
            raw = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError) as error:
            raise DeploymentError(
                f"Temporary deployment journal is invalid: {error}"
            ) from error

        try:
            items = [JournalItem(**item) for item in raw["items"]]
            journal = DeploymentJournal(
                version=int(raw["version"]),
                session_id=str(raw["session_id"]),
                owner_pid=int(raw["owner_pid"]),
                owner_started_at=float(raw["owner_started_at"]),
                deployment_root=str(raw["deployment_root"]),
                started_at=float(raw["started_at"]),
                items=items,
                created_directories=[str(path) for path in raw["created_directories"]],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DeploymentError(
                f"Temporary deployment journal is invalid: {error}"
            ) from error

        if journal.version != JOURNAL_VERSION:
            raise DeploymentError(
                f"Unsupported temporary deployment journal version: {journal.version}"
            )
        if not _same_path(Path(journal.deployment_root), self.deployment_root):
            raise DeploymentError(
                "Temporary deployment journal belongs to a different "
                "Marvel Rivals installation."
            )
        return journal

    def _acquire_lock(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            try:
                raw_owner = json.loads(self.lock_path.read_text(encoding="utf-8"))
                owner_pid = int(raw_owner["pid"])
                owner_started_at = float(raw_owner["started_at"])
            except (OSError, ValueError, TypeError, KeyError):
                owner_pid = -1
                owner_started_at = -1.0

            if self._process_identity_alive(owner_pid, owner_started_at):
                raise DeploymentConflictError(
                    "Another Marvel Rivals temporary deployment session is active."
                )

            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise DeploymentConflictError(
                    "A stale Marvel Rivals deployment lock could not be removed."
                ) from error
            return self._acquire_lock()

        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {"pid": os.getpid(), "started_at": self.owner_started_at},
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        self._lock_owned = True

    @staticmethod
    def _process_identity_alive(pid: int, started_at: float) -> bool:
        if pid <= 0 or started_at < 0:
            return False
        try:
            process = psutil.Process(pid)
            return abs(process.create_time() - started_at) < 1.0
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return psutil.pid_exists(pid)

    def release_lock(self) -> None:
        if not self._lock_owned:
            return
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self._lock_owned = False

    def shipping_running(self) -> bool:
        for process in psutil.process_iter(["name"]):
            try:
                name = process.info.get("name")
                if name and name.casefold() == self.shipping_process_name:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def _safe_destination(self, relative_path: str) -> Path:
        windows_path = _validate_windows_relative_path(relative_path)
        destination = self.deployment_root.joinpath(*windows_path.parts)

        # Resolve existing ancestors one by one. This rejects junction/symlink escapes
        # without requiring the destination itself to exist.
        current = self.deployment_root
        root_real = self.deployment_root.resolve()
        for part in windows_path.parts[:-1]:
            current = current / part
            if current.exists() or current.is_symlink():
                try:
                    current_real = current.resolve(strict=True)
                except OSError as error:
                    raise UnsafePathError(
                        f"Cannot validate Root mod destination: {relative_path}"
                    ) from error
                try:
                    current_real.relative_to(root_real)
                except ValueError as error:
                    raise UnsafePathError(
                        f"Root mod path escapes the game directory: {relative_path}"
                    ) from error
                if _is_link_or_junction(current):
                    raise UnsafePathError(
                        f"Root mod path uses a symbolic link or junction: "
                        f"{relative_path}"
                    )

        try:
            destination.parent.resolve(strict=False).relative_to(root_real)
        except ValueError as error:
            raise UnsafePathError(
                f"Root mod path escapes the game directory: {relative_path}"
            ) from error
        return destination

    def _prepare_directories(self, destination: Path, created: list[str]) -> None:
        missing: list[Path] = []
        current = destination.parent
        while not current.exists() and not _same_path(current, self.deployment_root):
            missing.append(current)
            current = current.parent

        if _is_link_or_junction(current):
            raise UnsafePathError(f"Deployment parent is a link: {current}")
        try:
            current.resolve(strict=True).relative_to(self.deployment_root)
        except (OSError, ValueError) as error:
            raise UnsafePathError(
                f"Deployment parent escapes the game directory: {current}"
            ) from error

        for directory in reversed(missing):
            try:
                directory.mkdir()
            except FileExistsError:
                if not directory.is_dir() or _is_link_or_junction(directory):
                    raise UnsafePathError(f"Deployment parent is unsafe: {directory}")
            else:
                created.append(str(directory))

    def _copy_exclusive(
        self, source: Path, destination: Path, on_owned: Callable[[], None]
    ) -> None:
        source_stream = source.open("rb")
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o666,
            )
        except Exception:
            source_stream.close()
            raise

        try:
            try:
                on_owned()
            except Exception:
                os.close(descriptor)
                raise
            with os.fdopen(descriptor, "wb") as destination_stream:
                shutil.copyfileobj(
                    source_stream, destination_stream, length=1024 * 1024
                )
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
        finally:
            source_stream.close()

    def deploy(self, items: Iterable[DeploymentItem]) -> DeploymentJournal:
        plan = list(items)
        if not plan:
            raise DeploymentError("No enabled Root payloads were found.")
        if self.shipping_running():
            raise DeploymentConflictError(
                "Marvel Rivals is already running; temporary Root deployment was "
                "not started."
            )

        self._acquire_lock()
        try:
            existing_journal = self._read_journal()
            if existing_journal is not None:
                if (
                    existing_journal.owner_pid != os.getpid()
                    and self._process_identity_alive(
                        existing_journal.owner_pid, existing_journal.owner_started_at
                    )
                ):
                    raise DeploymentConflictError(
                        "Another Marvel Rivals temporary deployment session is active."
                    )
                self._log(
                    f"recovering stale temporary deployment session "
                    f"{existing_journal.session_id} before launch"
                )
                if not self.cleanup(journal=existing_journal):
                    raise DeploymentConflictError(
                        "A stale Marvel Rivals Root deployment could not be cleaned up."
                    )
                self._acquire_lock()

            destinations: set[str] = set()
            journal_items: list[JournalItem] = []
            for item in plan:
                source = Path(item.source)
                if not source.is_file() or source.is_symlink():
                    raise DeploymentError(f"Root mod source does not exist: {source}")
                destination = self._safe_destination(item.relative_path)
                key = os.path.abspath(destination).casefold()
                if key in destinations:
                    raise DeploymentConflictError(
                        f"Multiple Root payloads target the same file: "
                        f"{item.relative_path}"
                    )
                destinations.add(key)
                if destination.exists() or _is_link_or_junction(destination):
                    raise DeploymentConflictError(
                        f"Temporary deployment will not overwrite an existing file: "
                        f"{destination}"
                    )
                journal_items.append(
                    JournalItem(
                        source=str(source),
                        relative_path=item.relative_path,
                        destination=str(destination),
                    )
                )

            journal = DeploymentJournal(
                version=JOURNAL_VERSION,
                session_id=uuid.uuid4().hex,
                owner_pid=os.getpid(),
                owner_started_at=self.owner_started_at,
                deployment_root=str(self.deployment_root),
                started_at=time.time(),
                items=journal_items,
                created_directories=[],
            )
            self._write_journal(journal)
        except Exception:
            self.release_lock()
            raise
        self._log(
            f"temporary root deployment session {journal.session_id} started "
            f"with {len(journal.items)} file(s)"
        )

        try:
            for item in journal.items:
                destination = self._safe_destination(item.relative_path)
                self._prepare_directories(destination, journal.created_directories)
                self._write_journal(journal)

                def mark_owned(item: JournalItem = item) -> None:
                    item.owned = True
                    self._write_journal(journal)

                self._copy_exclusive(Path(item.source), destination, mark_owned)
                item.sha256 = sha256_file(destination)
                self._write_journal(journal)
        except Exception as error:
            self._log(f"temporary root deployment failed; rolling back: {error}")
            self.cleanup(journal=journal)
            raise DeploymentError(
                f"Temporary Root deployment failed: {error}"
            ) from error

        self._log(f"temporary root deployment session {journal.session_id} completed")
        return journal

    def cleanup(
        self,
        *,
        journal: DeploymentJournal | None = None,
        keep_modified: bool = True,
    ) -> bool:
        if journal is None:
            journal = self._read_journal()
        if journal is None:
            self.release_lock()
            return True

        clean = True
        for item in reversed(journal.items):
            if not item.owned:
                continue
            destination = self._safe_destination(item.relative_path)
            if not destination.exists() and not _is_link_or_junction(destination):
                continue
            if _is_link_or_junction(destination) or not destination.is_file():
                clean = False
                continue

            if keep_modified and item.sha256 is not None:
                try:
                    current_hash = sha256_file(destination)
                except OSError:
                    clean = False
                    continue
                if current_hash != item.sha256:
                    self._log(
                        f"leaving externally modified deployed file: {destination}"
                    )
                    clean = False
                    continue

            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                clean = False

        for raw_directory in reversed(journal.created_directories):
            directory = Path(raw_directory)
            if _is_link_or_junction(directory):
                clean = False
                continue
            try:
                directory.resolve(strict=False).relative_to(self.deployment_root)
            except ValueError:
                clean = False
                continue
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                # Non-empty directories are not owned wholesale by this session.
                pass

        if clean:
            try:
                self.journal_path.unlink()
            except FileNotFoundError:
                pass
            self._log(
                f"temporary root deployment session {journal.session_id} cleaned up"
            )
            self.release_lock()
        else:
            self._write_journal(journal)
            self.release_lock()
        return clean

    def recover_stale(self) -> bool:
        if not self.journal_path.exists():
            return True
        if self.shipping_running():
            self._log(
                "stale journal found while Marvel Rivals is running; "
                "leaving it in place"
            )
            return False

        try:
            self._acquire_lock()
        except DeploymentConflictError:
            return False

        try:
            journal = self._read_journal()
            if journal is None:
                self.release_lock()
                return True
            if (
                journal.owner_pid != os.getpid()
                and self._process_identity_alive(
                    journal.owner_pid, journal.owner_started_at
                )
            ):
                self.release_lock()
                return False

            self._log(
                f"recovering stale temporary deployment session {journal.session_id}"
            )
            return self.cleanup(journal=journal)
        except Exception:
            self.release_lock()
            raise


class ShippingProcessWatcher:
    def __init__(
        self,
        manager: TemporaryDeploymentManager,
        *,
        launcher_process_name: str,
        shipping_process_name: str,
        session_started_at: float,
        launch_start_timeout: float = 45.0,
        launch_timeout: float = 15.0 * 60.0,
        launcher_exit_grace: float = 60.0,
        poll_interval: float = 1.0,
        log: DeploymentLogger | None = None,
    ) -> None:
        self.manager = manager
        self.launcher_process_name = launcher_process_name.casefold()
        self.shipping_process_name = shipping_process_name.casefold()
        self.session_started_at = session_started_at
        self.launch_start_timeout = launch_start_timeout
        self.launch_timeout = launch_timeout
        self.launcher_exit_grace = launcher_exit_grace
        self.poll_interval = poll_interval
        self._log = log or (lambda _message: None)
        self._launcher_finished = threading.Event()
        self._thread: threading.Thread | None = None

    def notify_launcher_finished(self) -> None:
        self._launcher_finished.set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="MarvelRivalsTemporaryDeployment",
            daemon=True,
        )
        self._thread.start()

    def _matching_processes(self, name: str) -> list[psutil.Process]:
        matches: list[psutil.Process] = []
        for process in psutil.process_iter(["name", "create_time"]):
            try:
                process_name = process.info.get("name")
                created = process.info.get("create_time")
                if (
                    process_name
                    and process_name.casefold() == name
                    and isinstance(created, (int, float))
                    and created >= self.session_started_at - 2.0
                ):
                    matches.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return matches

    def _run(self) -> None:
        started = time.monotonic()
        launcher_seen = False
        shipping_seen = False
        launcher_disappeared_at: float | None = None

        while True:
            now = time.monotonic()
            elapsed = now - started
            launchers = self._matching_processes(self.launcher_process_name)
            shipping = self._matching_processes(self.shipping_process_name)

            if launchers:
                if not launcher_seen:
                    self._log("Marvel Rivals launcher detected")
                launcher_seen = True
                launcher_disappeared_at = None

            if shipping:
                if not shipping_seen:
                    self._log("Marvel-Win64-Shipping.exe detected")
                shipping_seen = True

            if shipping_seen:
                if shipping:
                    time.sleep(self.poll_interval)
                    continue
                self._log(
                    "Marvel-Win64-Shipping.exe exited; cleaning temporary Root payloads"
                )
                self.manager.cleanup()
                return

            if elapsed >= self.launch_timeout:
                self._log(
                    "Marvel Rivals launch timed out; cleaning temporary Root payloads"
                )
                self.manager.cleanup()
                return

            if not launcher_seen and elapsed >= self.launch_start_timeout:
                self._log(
                    "Marvel Rivals launcher was not detected; cleaning temporary "
                    "Root payloads"
                )
                self.manager.cleanup()
                return

            launcher_finished = self._launcher_finished.is_set()
            if launcher_seen and not launchers:
                if launcher_disappeared_at is None:
                    launcher_disappeared_at = now
                # onFinishedRun is only an abort hint here. Shipping.exe remains the
                # final cleanup condition once it has been observed. A grace window also
                # covers launchers that exit shortly before spawning the game process.
                if (
                    launcher_finished
                    and now - launcher_disappeared_at >= self.launcher_exit_grace
                ):
                    self._log(
                        "Marvel Rivals launcher exited without starting the game; "
                        "cleaning temporary Root payloads"
                    )
                    self.manager.cleanup()
                    return

            time.sleep(self.poll_interval)
