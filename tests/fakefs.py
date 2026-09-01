"""A minimal fake filesystem for exercising sysfs-walking checks.

Checks read /sys and /proc directly, so covering their branches means
simulating those trees. Patching pathlib globally is fragile; this patches only
the handful of Path methods the checks use, dispatching on the path itself.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest import mock


@contextmanager
def fake_fs(tree: dict):
    """Simulate a filesystem.

    `tree` maps absolute paths to either a string (file contents) or a list of
    child names (directory). Anything not present does not exist.
    """
    files = {k: v for k, v in tree.items() if isinstance(v, str)}
    dirs = {k.rstrip("/"): list(v) for k, v in tree.items()
            if isinstance(v, (list, tuple))}

    def exists(self):
        s = str(self)
        return s in files or s in dirs

    def is_dir(self):
        return str(self) in dirs

    def read_text(self, *a, **k):
        s = str(self)
        if s in files:
            return files[s]
        raise FileNotFoundError(s)

    def read_bytes(self):
        return read_text(self).encode()

    def iterdir(self):
        s = str(self)
        if s not in dirs:
            raise FileNotFoundError(s)
        return [Path(s) / name for name in dirs[s]]

    # Every path the tree implies, including children named only in a
    # directory listing. Needed because checks glob nested patterns such as
    # "card*-*/status", which must match across a directory boundary.
    known = set(files) | set(dirs)
    for parent, children in dirs.items():
        known.update(f"{parent}/{c}" for c in children)

    def glob(self, pattern):
        import fnmatch
        base = str(self).rstrip("/")
        prefix = base + "/"
        out = []
        for p in sorted(known):
            if not p.startswith(prefix):
                continue
            if fnmatch.fnmatch(p[len(prefix):], pattern):
                out.append(Path(p))
        return out

    def resolve(self, *a, **k):
        return self

    def stat_(self, *a, **k):
        return mock.MagicMock(st_size=len(files.get(str(self), "")),
                              st_mtime=0)

    with mock.patch.object(Path, "exists", exists), \
         mock.patch.object(Path, "is_dir", is_dir), \
         mock.patch.object(Path, "read_text", read_text), \
         mock.patch.object(Path, "read_bytes", read_bytes), \
         mock.patch.object(Path, "iterdir", iterdir), \
         mock.patch.object(Path, "glob", glob), \
         mock.patch.object(Path, "resolve", resolve), \
         mock.patch.object(Path, "stat", stat_):
        yield
