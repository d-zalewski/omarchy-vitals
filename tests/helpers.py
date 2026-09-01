"""Shared test scaffolding.

Checks talk to the system through Context, so a fake Context is all that is
needed to drive every branch of every check without touching real hardware.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def cp(stdout="", returncode=0, stderr=""):
    """Build a CompletedProcess the way Context.run returns one."""
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class FakeContext:
    """Stands in for vitals.core.Context.

    Commands are matched by substring against the joined argv, so a test only
    has to name the distinctive part of a command it cares about.
    """

    def __init__(self, *, commands=None, files=None, paths=(), tools=(),
                 journal_kernel="", journal_all="", kconfig="",
                 session=None, minutes=1, stress_minutes=1, default=None):
        self.commands = commands or {}
        self.files = files or {}
        self.paths = set(paths)
        self.tools = set(tools)
        self._journal_kernel = journal_kernel
        self._journal_all = journal_all
        self._kconfig = kconfig
        self._session = session if session is not None else {}
        self.minutes = minutes
        self.stress_minutes = stress_minutes
        self.default = default if default is not None else cp("", 1)
        self.calls = []

    # -- command execution -------------------------------------------------
    def _lookup(self, cmd):
        key = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        self.calls.append(key)
        for pattern, result in self.commands.items():
            if pattern in key:
                return result() if callable(result) else result
        return self.default

    def run(self, cmd, timeout=60, check_rc=False):
        return self._lookup(cmd)

    def sudo(self, cmd, timeout=60):
        return self._lookup(cmd)

    def run_in_session(self, cmd, timeout=30):
        return self._lookup(cmd)

    # -- environment -------------------------------------------------------
    def have(self, binary):
        return binary in self.tools

    def path_exists(self, p):
        return p in self.paths

    def read(self, p, default=""):
        return self.files.get(p, default)

    def session_env(self):
        return self._session

    @property
    def journal_kernel(self):
        return self._journal_kernel

    @property
    def journal_all(self):
        return self._journal_all

    @property
    def kconfig(self):
        return self._kconfig

    def config_is_set(self, opt):
        return f"{opt}=y" in self._kconfig or f"{opt}=m" in self._kconfig

    def count_matches(self, text, pattern):
        import re
        return len(re.findall(pattern, text, re.IGNORECASE | re.MULTILINE))
