"""Runs `phonon serve` and ends it when the backend that started it goes away.

macOS has no PR_SET_PDEATHSIG (transcribers/phonon.py uses it on Linux), so there the backend
starts this instead, with its own Python in isolated mode:

    python -I phonon_guard.py <backend pid> <program> <args...>

It runs the program as its child (same output, same environment), passes SIGTERM and SIGINT on,
and once a second checks that its parent is still the backend: when the backend has died, the
child is terminated (killed after 10 s). The guard exits with the child's status. The backend
starts it in a session of its own and kills that whole group when terminating is not enough.
Standard library only: it never imports mnemosyne.
"""

import os
import signal
import subprocess
import sys


def _status(code: int) -> int:
    return code if code >= 0 else 128 - code  # killed by signal N: 128 + N, like a shell


def main(argv: list[str]) -> int:
    parent = int(argv[1])
    child = subprocess.Popen(argv[2:])

    def forward(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    while True:
        try:
            return _status(child.wait(timeout=1.0))
        except subprocess.TimeoutExpired:
            pass
        if os.getppid() != parent:  # reparented: the backend is gone
            child.terminate()
            try:
                return _status(child.wait(timeout=10))
            except subprocess.TimeoutExpired:
                child.kill()
                return _status(child.wait())


if __name__ == "__main__":
    sys.exit(main(sys.argv))
