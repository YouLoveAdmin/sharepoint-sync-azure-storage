"""Standalone launcher injected into the converter image by the job template."""
import json
import os
import signal
import subprocess
import time
from pathlib import Path


def main():
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    control = Path(os.environ["SYNC_CONTROL_DIR"])
    server = subprocess.Popen([
        "gunicorn", "--bind", "0.0.0.0:5001", "--workers", "1",
        "--timeout", "300", "--access-logfile", "-", "--error-logfile", "-", "app:app",
    ])
    try:
        while not stopped:
            if server.poll() is not None:
                (control / "converter-failed").write_text(str(server.returncode))
                return server.returncode or 1
            done = control / "worker-done.json"
            if done.exists():
                code = json.loads(done.read_text())["exit_code"]
                print("Converter shutdown: sync worker completed.", flush=True)
                return code
            time.sleep(1)
        return 1
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    raise SystemExit(main())
