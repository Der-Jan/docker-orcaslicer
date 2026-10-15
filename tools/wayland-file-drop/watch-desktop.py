#!/usr/bin/env python3
"""Drop completed Selkies desktop uploads at the center of one Wayland output."""
import argparse
import ctypes
import fcntl
import logging
import os
from pathlib import Path
import select
import signal
import stat
import struct
import subprocess
import sys
import time

EVENT = struct.Struct("iIII")
MOVED_FROM, MOVED_TO, OVERFLOW, DELETE_SELF, MOVE_SELF = 0x40, 0x80, 0x4000, 0x400, 0x800
SUPPORTED = {".stl", ".obj", ".step", ".stp", ".3mf"}
LOG = logging.getLogger("desktop-drop")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(os.environ.get("FILE_MANAGER_PATH", str(Path.home() / "Desktop"))))
    parser.add_argument("--helper", type=Path, default=Path(__file__).resolve().with_name("wayland-file-drop"))
    parser.add_argument("--project-helper", type=Path, help="prepare uploads in a project folder before dropping")
    parser.add_argument("--once", action="store_true", help="stop after the first supported completed upload")
    parser.add_argument("--dry-run", action="store_true", help="log eligible uploads without moving the pointer or importing")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [desktop-drop] %(message)s")
    directory = args.directory.expanduser().resolve(strict=True)
    helper = args.helper.resolve(strict=True)
    if not directory.is_dir() or not os.access(helper, os.X_OK):
        parser.error("directory and executable helper must exist")

    # one watcher per upload directory; never replay files already present
    lock = open(directory / ".wayland-file-drop.lock", "a+", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("a watcher is already running for this directory")
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()) + "\n")
    lock.flush()

    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1")
    if libc.inotify_add_watch(fd, os.fsencode(directory), MOVED_FROM | MOVED_TO | DELETE_SELF | MOVE_SELF) < 0:
        os.close(fd)
        raise OSError(ctypes.get_errno(), "inotify_add_watch")
    stopped = False
    child = None

    def stop(signum, frame):
        nonlocal stopped
        stopped = True
        if child is not None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    moves = {}
    LOG.info("Watching completed STL/OBJ/STEP/3MF uploads in %s; target is screen center", directory)
    try:
        while not stopped:
            if not select.select([fd], [], [], 1)[0]:
                continue
            data = os.read(fd, 65536)
            offset = 0
            ready = []
            while offset < len(data):
                _, mask, cookie, length = EVENT.unpack_from(data, offset)
                offset += EVENT.size
                name = os.fsdecode(data[offset:offset + length].split(b"\0", 1)[0])
                offset += length
                if mask & OVERFLOW:
                    moves.clear()
                    ready.clear()
                    LOG.error("Watch queue overflowed; skipped files must be opened manually")
                    continue
                if mask & (DELETE_SELF | MOVE_SELF):
                    LOG.error("Watched directory disappeared or moved; stopping")
                    stopped = True
                    break
                if mask & MOVED_FROM and name.startswith(".selkies-upload-") and name.endswith(".part"):
                    moves[cookie] = time.monotonic()
                if mask & MOVED_TO and cookie in moves:
                    del moves[cookie]
                    path = directory / name
                    if name.startswith(".") or name.lower().endswith(".gcode.3mf") or path.suffix.lower() not in SUPPORTED:
                        continue
                    try:
                        entry = path.lstat()
                        if stat.S_ISREG(entry.st_mode) and entry.st_size:
                            ready.append((path, entry))
                    except FileNotFoundError:
                        LOG.warning("Completed upload disappeared: %s", name)
            moves = {cookie: ts for cookie, ts in moves.items() if time.monotonic() - ts < 60}
            for path, entry in ready:
                if stopped:
                    break
                try:
                    current = path.lstat()
                    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns):
                        LOG.warning("Upload changed before drop; skipped %s", path.name)
                        continue
                    LOG.info("%s %s", "Would drop" if args.dry_run else "Dropping", path.name)
                    if not args.dry_run:
                        command = [str(helper), str(path)]
                        if args.project_helper:
                            command = [sys.executable, str(args.project_helper), '--helper', str(helper), str(path)]
                        child = subprocess.Popen(command)
                        try:
                            result = child.wait(timeout=480 if args.project_helper else 30)
                        except subprocess.TimeoutExpired:
                            child.terminate()
                            try:
                                child.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                child.kill()
                                child.wait()
                            result = 1
                        finally:
                            child = None
                        if result:
                            LOG.warning("Drop not confirmed for %s; inspect the plate before retrying manually", path.name)
                        else:
                            LOG.info("Drop completed for %s", path.name)
                    if args.once:
                        stopped = True
                        break
                except OSError as error:
                    LOG.warning("Skipped %s: %s", path.name, error)
    finally:
        os.close(fd)
        lock.close()
    LOG.info("Watcher stopped")


if __name__ == "__main__":
    main()
