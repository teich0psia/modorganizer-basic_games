from __future__ import annotations

import time
import unittest
from pathlib import Path

from games.marvelrivals.full_watcher import FullDeploymentShippingWatcher


class FakeManager:
    def __init__(self) -> None:
        self.cleanup_count = 0

    def cleanup(self) -> bool:
        self.cleanup_count += 1
        return True


class FakeProcess:
    def __init__(self, pid: int = 1234) -> None:
        self.pid = pid
        self.info = {
            "name": "Marvel-Win64-Shipping.exe",
            "exe": r"C:\Games\MarvelRivals\MarvelGame\Marvel\Binaries\Win64\Marvel-Win64-Shipping.exe",
            "create_time": time.time(),
        }

    def as_dict(self, attrs):
        return dict(self.info)


class SequenceWatcher(FullDeploymentShippingWatcher):
    def __init__(self, manager: FakeManager, sequence, **kwargs) -> None:
        super().__init__(manager, **kwargs)  # type: ignore[arg-type]
        self.sequence = iter(sequence)
        self.last = []

    def _matching_processes(self, name: str):
        try:
            self.last = next(self.sequence)
        except StopIteration:
            pass
        return self.last


class ImmediateWatcher(FullDeploymentShippingWatcher):
    def _run_guarded(self) -> None:
        return None


class FailingWatcher(FullDeploymentShippingWatcher):
    def _run_guarded(self) -> None:
        raise RuntimeError("synthetic watcher failure")


class TestFullDeploymentShippingWatcher(unittest.TestCase):
    def params(self, log):
        return dict(
            shipping_process_name="Marvel-Win64-Shipping.exe",
            shipping_executable_path=Path(
                r"C:\Games\MarvelRivals\MarvelGame\Marvel\Binaries\Win64\Marvel-Win64-Shipping.exe"
            ),
            session_started_at=time.time(),
            launch_timeout=0.05,
            poll_interval=0.001,
            log=log,
        )

    def test_shipping_detection_and_exit_cleanup(self) -> None:
        manager = FakeManager()
        process = FakeProcess()
        messages: list[str] = []
        watcher = SequenceWatcher(
            manager,
            [[], [process], [process], []],
            **self.params(messages.append),
        )

        watcher._run()

        self.assertTrue(watcher.shipping_seen)
        self.assertEqual(manager.cleanup_count, 1)
        self.assertTrue(any("thread entered" in message for message in messages))
        self.assertTrue(any("detected" in message for message in messages))
        self.assertTrue(any("exited" in message for message in messages))
        self.assertTrue(any("cleanup finished (success=True)" in message for message in messages))

    def test_start_logs_thread_state(self) -> None:
        manager = FakeManager()
        messages: list[str] = []
        watcher = ImmediateWatcher(manager, **self.params(messages.append))  # type: ignore[arg-type]

        watcher.start()
        assert watcher._thread is not None
        watcher._thread.join(timeout=1.0)

        self.assertFalse(watcher._thread.is_alive())
        self.assertTrue(any("starting temporary full deployment" in message for message in messages))
        self.assertTrue(any("thread started" in message for message in messages))
        self.assertTrue(any("thread entered" in message for message in messages))

    def test_watcher_exception_is_logged(self) -> None:
        manager = FakeManager()
        messages: list[str] = []
        watcher = FailingWatcher(manager, **self.params(messages.append))  # type: ignore[arg-type]

        watcher._run()

        self.assertEqual(manager.cleanup_count, 0)
        self.assertTrue(
            any(
                "watcher failed: RuntimeError: synthetic watcher failure" in message
                for message in messages
            )
        )

    def test_never_started_times_out_and_cleans(self) -> None:
        manager = FakeManager()
        messages: list[str] = []
        watcher = SequenceWatcher(
            manager,
            [[]],
            **{
                **self.params(messages.append),
                "launch_timeout": 0.01,
            },
        )

        watcher._run()

        self.assertFalse(watcher.shipping_seen)
        self.assertEqual(manager.cleanup_count, 1)
        self.assertTrue(any("timed out" in message for message in messages))
        self.assertTrue(any("timeout cleanup finished" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
