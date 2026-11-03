from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    stopping = threading.Event()
    processes: list[subprocess.Popen] = []

    def stop(signum: int, frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        processes.append(subprocess.Popen(["/usr/sbin/cron", "-f"]))
        processes.append(subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers",
            ],
            user="appuser", group="appuser", extra_groups=(),
        ))
        while not stopping.wait(1):
            for process in processes:
                if process.poll() is not None:
                    logger.error("Container child process exited unexpectedly (code %s).", process.returncode)
                    return 1
        return 0
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                logger.error("Container child did not stop gracefully; terminating it.")
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
