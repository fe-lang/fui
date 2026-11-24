#!/usr/bin/env python3
"""End-to-end checks of the `gallery`, `panic` and `listen` examples on a
pseudo-terminal.

Usage: dev/pty_gallery.py <gallery executable> <panic executable> <listen executable>

Runs each program under a pty as `pty_check.py` runs the counter, with its
terminal emulator. For the gallery: the first frame turns on mouse reports,
keys switch tabs and move the table's selection, an SGR mouse click selects
a row and another a tab, a window size change redraws at the new size, and
`q` ends it with the terminal restored. For the panic example: a panic while
the app holds the terminal restores it (modes and attributes) before the
panic report, which shows after the restore sequence on the main screen.
For the listen example, which runs its own loop on a poller: a TCP
connection to the port in its title is listed, keys still work, and `q`
ends it with the terminal restored.
Prints a PASS or FAIL line per check and exits non-zero if any failed.
Standard library only.
"""

import os
import re
import socket
import sys
import termios

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pty_check  # noqa: E402
from pty_check import CSI, TIMEOUT, Run, check_exit, in_session, report, row_of  # noqa: E402


def click(run, x, y):
    """A press and release of the left button at the zero-based x and y."""
    run.send(CSI + f"<0;{x + 1};{y + 1}M".encode() + CSI + f"<0;{x + 1};{y + 1}m".encode())


def column_of(vt, y, text):
    return "".join(vt.screen.cells[y]).find(text)


def check_gallery(path):
    run = Run(path, 50, 12)
    try:
        report(run.until(lambda: row_of(run.vt, "> Block") is not None), "gallery: the first frame is drawn")
        report(run.vt.mode(1000) and run.vt.mode(1006), "gallery: mouse clicks are reported, in SGR form")
        run.send(CSI + b"C")
        report(run.until(lambda: row_of(run.vt, "Planets by order") is not None), "gallery: Right shows the table tab")
        run.send(CSI + b"B" + CSI + b"B")
        report(run.until(lambda: row_of(run.vt, "> Venus") is not None), "gallery: Down moves the table's selection")
        run.settle()
        earth = row_of(run.vt, "Earth")
        click(run, 5, earth)
        report(run.until(lambda: row_of(run.vt, "> Earth") == earth), "gallery: a click selects the row under it")
        run.send(b"s")
        report(run.until(lambda: row_of(run.vt, "Planets by moons") is not None), "gallery: s sorts the table")
        run.settle()
        click(run, column_of(run.vt, 0, "Form"), 0)
        report(run.until(lambda: row_of(run.vt, "Sign up") is not None), "gallery: a click on a tab selects it")
        run.send(b"Ada")
        report(run.until(lambda: row_of(run.vt, "Hello, Ada") is not None), "gallery: typing fills the form")
        run.settle()
        click(run, column_of(run.vt, 0, "Table"), 0)
        run.vt.resize(70, 16)
        pty_check.set_size(run.master, 70, 16)
        resized = run.until(
            lambda: run.vt.screen.text().splitlines()[-1].startswith("Up/Down select")
            and run.vt.screen.text().splitlines()[-1].rstrip().endswith("dark")
            and len(run.vt.screen.text().splitlines()) == 16
            and row_of(run.vt, "> Earth") is not None
        )
        report(resized, "gallery: a window size change redraws at 70x16")
        run.settle()
        run.set_mark()
        run.send(b"q")
        check_exit(run, "gallery: q")
        report(not run.vt.mode(1000) and not run.vt.mode(1006), "gallery: mouse reports are off again")
    finally:
        run.close()


def check_panic(path):
    run = Run(path, 40, 10)
    try:
        report(run.until(lambda: row_of(run.vt, "p panics") is not None), "panic: the first frame is drawn")
        run.settle()
        run.set_mark()
        run.send(b"p")
        status = run.wait()
        run.settle(0.2)
        out = run.since_mark()
        report(status is not None and not (os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0),
               f"panic: the process ends with a failure (status {status})")
        restore = out.find(CSI + b"?1049l")
        panic = out.find(b"panicked:")
        report(0 <= restore < panic, "panic: the report comes after the restore sequence")
        report(run.vt.restored(), "panic: the terminal's modes are back, on the main screen")
        report(row_of(run.vt, "panicked: index out of bounds") is not None,
               "panic: the report is on the main screen")
        report(termios.tcgetattr(run.slave) == run.before, "panic: the terminal attributes equal those before the start")
    finally:
        run.close()


def check_listen(path):
    run = Run(path, 50, 8)
    try:
        report(run.until(lambda: row_of(run.vt, "No connections yet") is not None), "listen: the first frame is drawn")
        title = re.search(r"127\.0\.0\.1:(\d+)", run.vt.screen.text())
        report(title is not None, "listen: the title shows the port")
        for _ in range(2):
            socket.create_connection(("127.0.0.1", int(title.group(1))), timeout=TIMEOUT).close()
        report(run.until(lambda: row_of(run.vt, "2 accepted") is not None and row_of(run.vt, "No connections") is None),
               "listen: the connections are listed")
        run.send(CSI + b"B")
        report(run.until(lambda: row_of(run.vt, "> 127.0.0.1") is not None),
               "listen: keys reach the list")
        run.settle()
        run.set_mark()
        run.send(b"q")
        check_exit(run, "listen: q")
    finally:
        run.close()


def main():
    if len(sys.argv) != 4:
        print(__doc__.strip().splitlines()[3], file=sys.stderr)
        return 2
    gallery, panic, listen = (os.path.abspath(p) for p in sys.argv[1:])
    failed = (
        in_session(check_gallery, gallery) + in_session(check_panic, panic) + in_session(check_listen, listen)
    )
    print("all checks passed" if failed == 0 else f"{failed} checks failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
