from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class WriterLock:
    """Hold an OS lock for the lifetime of a sync writer, including state startup."""

    def __init__(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        self.file: BinaryIO = (output_dir / ".sync-writer.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self.file.seek(0, os.SEEK_END) == 0:
                    self.file.write(b"\0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError(
                "Cannot acquire the persistent sync writer lock. Another app/job may be "
                "using OUTPUT_DIR, or the mount does not support locking."
            ) from None

    def close(self) -> None:
        self.file.close()
