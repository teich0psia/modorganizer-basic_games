from __future__ import annotations

import time
from pathlib import Path

import psutil

from .temporary_deployment import (
    DeploymentLogger,
    ShippingProcessWatcher,
    TemporaryDeploymentManager,
    _process_identity,
)


class FullDeploymentShippingWatcher(ShippingProcessWatcher):
    def __init__(
        self,
        manager: TemporaryDeploymentManager,
        *,
        shipping_process_name: str,
        session_started_at: float,
        shipping_executable_path: Path | None = None,
        launch_timeout: float = 15.0 * 60.0,
        poll_interval: float = 1.0,
        launcher_process_name: str | None = None,
        launch_start_timeout: float | None = None,
        launcher_exit_grace: float | None = None,
        log: DeploymentLogger | None = None,
    ) -> None:
        super().__init__(
            manager,
            shipping_process_name=shipping_process_name,
            session_started_at=session_started_at,
            shipping_executable_path=shipping_executable_path,
            launch_timeout=launch_timeout,
            poll_interval=poll_interval,
            launcher_process_name=launcher_process_name,
            launch_start_timeout=launch_start_timeout,
            launcher_exit_grace=launcher_exit_grace,
            log=log,
        )
        self.shipping_seen = False
        self._last_wait_log = 0.0
        self._last_candidate_log = 0.0

    def start(self) -> None:
        self._log(
            "starting temporary full deployment Shipping watcher "
            f"(expected={self.shipping_executable_path})"
        )
        super().start()
        thread = self._thread
        self._log(
            "temporary full deployment Shipping watcher thread started "
            f"(alive={bool(thread and thread.is_alive())})"
        )

    def _matching_processes(self, name: str) -> list[psutil.Process]:
        matches = super()._matching_processes(name)
        if matches:
            return matches

        now = time.monotonic()
        if now - self._last_candidate_log < 5.0:
            return matches
        self._last_candidate_log = now

        candidates: list[str] = []
        for process in psutil.process_iter(["name", "exe", "create_time"]):
            try:
                info = process.info
                process_name = info.get("name")
                if (
                    not process_name
                    or process_name.casefold() != self.shipping_process_name
                ):
                    continue
                candidates.append(self._describe_process(process))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if candidates:
            self._log(
                "Shipping process name candidate did not match expected target "
                f"{self.shipping_executable_path}: {'; '.join(candidates)}"
            )
        elif now - self._last_wait_log >= 30.0:
            self._last_wait_log = now
            self._log(
                "temporary full deployment Shipping watcher is waiting "
                f"for {self.shipping_process_name}"
            )
        return matches

    def _run(self) -> None:
        self._log("temporary full deployment Shipping watcher thread entered")
        try:
            self._run_guarded()
        except Exception as error:
            self._log(
                "temporary full deployment Shipping watcher failed: "
                f"{type(error).__name__}: {error}"
            )

    def _run_guarded(self) -> None:
        started = time.monotonic()
        shipping_identity: tuple[int, float] | None = None

        while True:
            shipping = self._matching_processes(self.shipping_process_name)

            if self.shipping_seen and shipping_identity is not None:
                shipping = [
                    process
                    for process in shipping
                    if _process_identity(process) == shipping_identity
                ]

            if shipping:
                if not self.shipping_seen:
                    self.shipping_seen = True
                    shipping_identity = _process_identity(shipping[0])
                    self._log(
                        "Marvel-Win64-Shipping.exe detected by full deployment watcher "
                        f"({self._describe_process(shipping[0])})"
                    )
                time.sleep(self.poll_interval)
                continue

            if self.shipping_seen:
                self._log(
                    "Marvel-Win64-Shipping.exe exited; cleaning temporary full "
                    "deployment payloads"
                )
                cleaned = self.manager.cleanup()
                self._log(
                    "temporary full deployment cleanup finished "
                    f"(success={cleaned})"
                )
                return

            if time.monotonic() - started >= self.launch_timeout:
                self._log(
                    "Marvel Rivals launch timed out before Shipping.exe was detected; "
                    "cleaning temporary full deployment payloads"
                )
                cleaned = self.manager.cleanup()
                self._log(
                    "temporary full deployment timeout cleanup finished "
                    f"(success={cleaned})"
                )
                return

            time.sleep(self.poll_interval)
