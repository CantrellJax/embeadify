"""Append one line to a file shared by parallel fake-tool processes, without losing or tearing lines.

Plain `open(path, "a")` is only atomic on POSIX. On Windows the C runtime emulates O_APPEND (seek to the end,
then write), so two processes appending at once overwrite each other: lines vanish or come back empty. The
fakes therefore serialise appends through an O_EXCL lock file, which is atomic on every platform. On
Windows a contended or delete-pending lock file raises PermissionError instead of FileExistsError, so both
mean "retry".
"""

import os
import time


def lock(path):
    """Take the exclusive lock beside `path` and return its file descriptor; spins until it is free."""
    lock_path = f"{path}.lock"
    while True:
        try:
            return lock_path, os.open(lock_path, os.O_CREAT | os.O_EXCL)
        except (FileExistsError, PermissionError):
            time.sleep(0.002)


def unlock(held):
    lock_path, fd = held
    os.close(fd)
    while True:
        try:
            os.unlink(lock_path)
            return
        except PermissionError:  # Windows: another process still has the handle open
            time.sleep(0.002)


def append_line(path, line, encoding="utf-8"):
    held = lock(str(path))
    try:
        with open(path, "ab") as handle:
            handle.write(line.rstrip("\n").encode(encoding) + b"\n")
    finally:
        unlock(held)
