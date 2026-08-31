from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable

from .temporary_deployment import (
    JOURNAL_VERSION,
    DeploymentConflictError,
    DeploymentError,
    DeploymentItem,
    DeploymentJournal,
    JournalItem,
    TemporaryDeploymentManager,
    _is_link_or_junction,
    _normalized_compare_path,
    sha256_file,
)


_HARDLINK_CONTENT_ROOTS = (
    "content/paks/~mods/",
    "content/movies/",
)
_HARDLINK_EXTENSIONS = {".pak", ".utoc", ".ucas", ".bk2"}
_HARDLINK_FINGERPRINT_PREFIX = "hardlink:"


def prefers_hardlink(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").casefold()
    if not any(normalized.startswith(root) for root in _HARDLINK_CONTENT_ROOTS):
        return False
    return Path(normalized).suffix.casefold() in _HARDLINK_EXTENSIONS


def _hardlink_fingerprint(path: Path) -> str:
    stat = path.stat()
    identity = (
        f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
    ).encode("utf-8")
    return _HARDLINK_FINGERPRINT_PREFIX + hashlib.sha256(identity).hexdigest()


class FullDeploymentManager(TemporaryDeploymentManager):
    def _hardlink_exclusive(
        self, source: Path, destination: Path, on_owned: Callable[[], None]
    ) -> None:
        os.link(source, destination)
        on_owned()

    def _materialize(
        self,
        source: Path,
        destination: Path,
        relative_path: str,
        on_owned: Callable[[], None],
    ) -> tuple[str, str]:
        if prefers_hardlink(relative_path):
            try:
                self._hardlink_exclusive(source, destination, on_owned)
            except FileExistsError:
                raise
            except OSError as error:
                if destination.exists() or _is_link_or_junction(destination):
                    raise
                self._log(
                    f"hardlink unavailable for {relative_path}; "
                    f"falling back to copy: {error}"
                )
            else:
                return "hardlink", _hardlink_fingerprint(destination)

        self._copy_exclusive(source, destination, on_owned)
        return "copy", sha256_file(destination)

    def deploy(self, items: Iterable[DeploymentItem]) -> DeploymentJournal:
        plan = list(items)
        if not plan:
            raise DeploymentError("No enabled payloads were found.")
        if self.shipping_running():
            raise DeploymentConflictError(
                "Marvel Rivals is already running; temporary full deployment was "
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
                        "Another Marvel Rivals temporary full deployment session is "
                        "active."
                    )
                self._log(
                    f"recovering stale temporary full deployment session "
                    f"{existing_journal.session_id} before launch"
                )
                if not self.cleanup(journal=existing_journal):
                    raise DeploymentConflictError(
                        "A stale Marvel Rivals full deployment could not be cleaned up."
                    )
                self._acquire_lock()

            destinations: set[str] = set()
            journal_items: list[JournalItem] = []
            for item in plan:
                source = Path(item.source)
                if not source.is_file() or source.is_symlink():
                    raise DeploymentError(f"Mod source does not exist: {source}")
                destination = self._safe_destination(item.relative_path)
                key = _normalized_compare_path(destination)
                if key in destinations:
                    raise DeploymentConflictError(
                        f"Multiple payloads target the same file: {item.relative_path}"
                    )
                destinations.add(key)
                if destination.exists() or _is_link_or_junction(destination):
                    raise DeploymentConflictError(
                        "Temporary full deployment will not overwrite an existing "
                        f"file: {destination}"
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
            f"temporary full deployment session {journal.session_id} started "
            f"with {len(journal.items)} file(s)"
        )

        hardlinks = 0
        copies = 0
        try:
            for item in journal.items:
                destination = self._safe_destination(item.relative_path)
                self._prepare_directories(destination, journal.created_directories)
                self._write_journal(journal)

                def mark_owned(item: JournalItem = item) -> None:
                    item.owned = True
                    self._write_journal(journal)

                method, fingerprint = self._materialize(
                    Path(item.source),
                    destination,
                    item.relative_path,
                    mark_owned,
                )
                item.sha256 = fingerprint
                self._write_journal(journal)
                if method == "hardlink":
                    hardlinks += 1
                else:
                    copies += 1
        except Exception as error:
            self._log(f"temporary full deployment failed; rolling back: {error}")
            self.cleanup(journal=journal)
            raise DeploymentError(
                f"Temporary full deployment failed: {error}"
            ) from error

        self._log(
            f"temporary full deployment session {journal.session_id} completed "
            f"({hardlinks} hardlink(s), {copies} copy file(s))"
        )
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

            fingerprint = item.sha256 or ""
            if fingerprint.startswith(_HARDLINK_FINGERPRINT_PREFIX):
                source = Path(item.source)
                try:
                    same_file = (
                        source.is_file()
                        and not source.is_symlink()
                        and os.path.samefile(source, destination)
                    )
                except OSError:
                    same_file = False
                if not same_file:
                    self._log(f"leaving replaced deployed hardlink: {destination}")
                    clean = False
                    continue
                if keep_modified:
                    try:
                        current_fingerprint = _hardlink_fingerprint(destination)
                    except OSError:
                        clean = False
                        continue
                    if current_fingerprint != fingerprint:
                        self._log(
                            f"leaving externally modified deployed hardlink: "
                            f"{destination}"
                        )
                        clean = False
                        continue
            elif keep_modified and item.sha256 is not None:
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
                pass

        if clean:
            try:
                self.journal_path.unlink()
            except FileNotFoundError:
                pass
            self._log(
                f"temporary full deployment session {journal.session_id} cleaned up"
            )
            self.release_lock()
        else:
            self._write_journal(journal)
            self.release_lock()
        return clean
