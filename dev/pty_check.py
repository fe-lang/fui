#!/usr/bin/env python3
"""End-to-end check of the `counter` example on a real pseudo-terminal.

Usage: dev/pty_check.py <path to the counter executable>

Runs the program under a pty of a set size, as a shell's foreground job, and
reads what it writes through a small terminal emulator. Checks that it
enters the alternate screen with its modes, that keys change what is drawn,
that a window size change redraws at the new size, that Ctrl+Z stops it
with the terminal restored and SIGCONT resumes it with a full redraw, that
a SIGTSTP from outside does the same, SIGTERM, Ctrl+C and `q` end it with
exit status 0 and the terminal restored, the terminal attributes after it
ended equal those before it started, and a hangup of the terminal ends it.
Prints a PASS or FAIL line per check and exits non-zero if any failed.
Standard library only.
"""

import fcntl
import os
import select
import signal
import struct
import sys
import termios
import time

TIMEOUT = 5.0
failures = 0


def report(ok, what):
    global failures
    print(("PASS  " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        failures += 1
    return ok


class Screen:
    """A grid of characters with the state a terminal keeps per screen."""

    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.cells = [[" "] * cols for _ in range(rows)]
        self.keyboard = []  # the kitty keyboard flag stack

    def resize(self, cols, rows):
        cells = [[" "] * cols for _ in range(rows)]
        for y in range(min(rows, self.rows)):
            for x in range(min(cols, self.cols)):
                cells[y][x] = self.cells[y][x]
        self.cols, self.rows, self.cells = cols, rows, cells

    def text(self):
        return "\n".join("".join(row).rstrip() for row in self.cells)


class Vt:
    """The part of xterm a full-screen program uses: text, cursor motion,
    erasure, scrolling at the bottom, the private modes and the kitty
    keyboard stack. Styles are read and ignored."""

    def __init__(self, cols, rows):
        self.main = Screen(cols, rows)
        self.alt = Screen(cols, rows)
        self.screen = self.main
        self.x = self.y = 0
        self.wrap = False
        self.saved = (0, 0)
        self.modes = {25: True}
        self.state = "ground"
        self.seq = ""
        self.utf8 = b""

    def resize(self, cols, rows):
        self.main.resize(cols, rows)
        self.alt.resize(cols, rows)
        self.x = min(self.x, cols - 1)
        self.y = min(self.y, rows - 1)
        self.wrap = False

    def mode(self, n):
        return self.modes.get(n, False)

    def feed(self, data):
        for b in data:
            self.byte(b)

    def byte(self, b):
        if self.state == "esc":
            if b == 0x5B:
                self.state, self.seq = "csi", ""
            elif b in (0x5D, 0x50, 0x5F, 0x5E, 0x58):
                self.state = "string"
            elif b == 0x37:
                self.saved, self.state = (self.x, self.y), "ground"
            elif b == 0x38:
                (self.x, self.y), self.state = self.saved, "ground"
            elif 0x20 <= b < 0x30:
                self.state = "esc_intermediate"
            else:
                self.state = "ground"
            return
        if self.state == "esc_intermediate":
            if b >= 0x30:
                self.state = "ground"
            return
        if self.state == "csi":
            if 0x40 <= b < 0x7F:
                self.csi(self.seq, chr(b))
                self.state = "ground"
            else:
                self.seq += chr(b)
            return
        if self.state in ("string", "string_esc"):
            if b == 0x07 or (self.state == "string_esc" and b == 0x5C):
                self.state = "ground"
            else:
                self.state = "string_esc" if b == 0x1B else "string"
            return
        if b == 0x1B:
            self.state = "esc"
        elif b == 0x0D:
            self.x, self.wrap = 0, False
        elif b == 0x0A:
            self.line_feed()
        elif b == 0x08:
            self.x, self.wrap = max(0, self.x - 1), False
        elif b == 0x09:
            self.x = min(self.screen.cols - 1, (self.x // 8 + 1) * 8)
        elif b >= 0x20 and b != 0x7F:
            self.utf8 += bytes([b])
            try:
                text = self.utf8.decode("utf-8")
            except UnicodeDecodeError:
                if len(self.utf8) >= 4:
                    self.utf8 = b""
                return
            self.utf8 = b""
            self.put(text)

    def put(self, ch):
        s = self.screen
        if s.cols == 0 or s.rows == 0:
            return
        if self.wrap:
            self.x, self.wrap = 0, False
            self.line_feed()
        s.cells[self.y][self.x] = ch
        if self.x + 1 < s.cols:
            self.x += 1
        else:
            self.wrap = True

    def line_feed(self):
        s = self.screen
        self.wrap = False
        if self.y + 1 < s.rows:
            self.y += 1
        elif s.rows > 0:
            s.cells.pop(0)
            s.cells.append([" "] * s.cols)

    def erase(self, y, x0, x1):
        for x in range(max(0, x0), min(self.screen.cols, x1)):
            self.screen.cells[y][x] = " "

    def csi(self, params, final):
        marker = params[:1] if params[:1] in ("?", ">", "<", "=") else ""
        body = params[len(marker):]
        nums = [int(p) if p.isdigit() else 0 for p in body.split(";")] if body else []

        def arg(i, default):
            return nums[i] if i < len(nums) and nums[i] != 0 else default

        s = self.screen
        if marker == "?" and final in "hl":
            for n in nums:
                self.set_mode(n, final == "h")
            return
        if final == "u" and marker in (">", "<", "="):
            if marker == ">":
                s.keyboard.append(arg(0, 0))
            elif marker == "<":
                for _ in range(arg(0, 1)):
                    if s.keyboard:
                        s.keyboard.pop()
            return
        if marker:
            return
        self.wrap = False
        if final in "Hf":
            self.y = min(arg(0, 1), s.rows) - 1
            self.x = min(arg(1, 1), s.cols) - 1
        elif final == "A":
            self.y = max(0, self.y - arg(0, 1))
        elif final == "B":
            self.y = min(s.rows - 1, self.y + arg(0, 1))
        elif final == "C":
            self.x = min(s.cols - 1, self.x + arg(0, 1))
        elif final == "D":
            self.x = max(0, self.x - arg(0, 1))
        elif final == "G":
            self.x = min(arg(0, 1), s.cols) - 1
        elif final == "d":
            self.y = min(arg(0, 1), s.rows) - 1
        elif final == "J":
            how = arg(0, 0)
            rows = range(s.rows) if how >= 2 else range(self.y + 1, s.rows) if how == 0 else range(self.y)
            for y in rows:
                self.erase(y, 0, s.cols)
            if how == 0:
                self.erase(self.y, self.x, s.cols)
            elif how == 1:
                self.erase(self.y, 0, self.x + 1)
        elif final == "K":
            how = arg(0, 0)
            if how == 0:
                self.erase(self.y, self.x, s.cols)
            elif how == 1:
                self.erase(self.y, 0, self.x + 1)
            else:
                self.erase(self.y, 0, s.cols)
        elif final == "X":
            self.erase(self.y, self.x, self.x + arg(0, 1))

    def set_mode(self, n, on):
        if n == 1049 and on != self.mode(1049):
            if on:
                self.saved = (self.x, self.y)
                self.screen = self.alt
                for y in range(self.alt.rows):
                    self.erase(y, 0, self.alt.cols)
            else:
                self.screen = self.main
                self.x, self.y = self.saved
        self.modes[n] = on

    def restored(self):
        """Whether the terminal is as a shell expects it."""
        return (
            self.mode(25)
            and not any(self.mode(n) for n in (1000, 1002, 1003, 1004, 1006, 1049, 2004, 2026))
            and not self.main.keyboard
            and not self.alt.keyboard
        )


def set_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Run:
    """The program under a new pty, in a session of this process's, as the
    foreground job of a shell would be."""

    def __init__(self, path, cols, rows):
        self.master, self.slave = os.openpty()
        set_size(self.slave, cols, rows)
        # This process leads the pty's session and stays in its own process
        # group, as a shell does, so the program's group is not orphaned and
        # Ctrl+Z can stop it.
        fcntl.ioctl(self.slave, termios.TIOCSCTTY, 0)
        self.before = termios.tcgetattr(self.slave)
        self.vt = Vt(cols, rows)
        self.output = b""
        self.pid = os.fork()
        if self.pid == 0:
            os.setpgid(0, 0)
            os.close(self.master)
            for fd in (0, 1, 2):
                os.dup2(self.slave, fd)
            if self.slave > 2:
                os.close(self.slave)
            signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            os.tcsetpgrp(0, os.getpid())
            signal.signal(signal.SIGTTOU, signal.SIG_DFL)
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
            os.execv(path, [path])
        os.setpgid(self.pid, self.pid)
        self.mark = 0

    def read(self, timeout=0.05):
        ready, _, _ = select.select([self.master], [], [], timeout)
        if not ready:
            return False
        try:
            data = os.read(self.master, 65536)
        except OSError:
            return False
        self.output += data
        self.vt.feed(data)
        return True

    def until(self, condition, timeout=TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            self.read()
        return condition()

    def settle(self, quiet=0.15):
        """Reads until nothing comes for `quiet` seconds."""
        while self.read(quiet):
            pass

    def since_mark(self):
        return self.output[self.mark:]

    def set_mark(self):
        self.mark = len(self.output)

    def send(self, data):
        os.write(self.master, data)

    def wait(self, flags=0, timeout=TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG | flags)
            if pid:
                return status
            self.read(0.02)
        return None

    def close(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        self.settle(0.05)
        os.close(self.master)
        os.close(self.slave)


CSI = b"\x1b["


def row_of(vt, text):
    for y, row in enumerate(vt.screen.cells):
        if text in "".join(row):
            return y
    return None


def check_exit(run, how):
    status = run.wait()
    report(
        status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0,
        f"{how} ends the program with exit status 0 (status {status})",
    )
    run.settle(0.1)
    report(CSI + b"?1049l" in run.since_mark() and CSI + b"?25h" in run.since_mark(),
           f"{how}: the restore sequence was written")
    report(run.vt.restored(), f"{how}: the terminal's modes are back, on the main screen")
    report(termios.tcgetattr(run.slave) == run.before,
           f"{how}: the terminal attributes equal those before the start")


def check_session(path):
    run = Run(path, 40, 10)
    try:
        started = run.until(lambda: row_of(run.vt, "Count: 0") is not None)
        report(started, "the first frame is drawn before any input")
        out = run.output
        report(out.startswith(CSI + b"?1049h"), "the output starts with the alternate screen (1049)")
        report(run.vt.mode(1049) and not run.vt.mode(25) and run.vt.mode(2004),
               "on the alternate screen, cursor hidden, bracketed paste on")
        raw = termios.tcgetattr(run.slave)
        report(raw[3] & termios.ICANON == 0 and raw[3] & termios.ECHO == 0, "the terminal is in raw mode")
        report(row_of(run.vt, "Count: 0") == 4 and run.vt.screen.text().splitlines()[-1].startswith("Up/+"),
               "the count is centered and the help on the last row at 40x10")

        run.send(b"++")
        report(run.until(lambda: row_of(run.vt, "Count: 2") is not None), "keys change what is drawn")
        run.send(b"\x1b[A")
        report(run.until(lambda: row_of(run.vt, "Count: 3") is not None), "an arrow key's sequence is one key")

        run.settle()
        set_size(run.master, 60, 15)
        run.vt.resize(60, 15)
        resized = run.until(
            lambda: row_of(run.vt, "Count: 3") == 7
            and run.vt.screen.cells[14][0:4] == list("Up/+")
            and "".join(run.vt.screen.cells[7]).index("Count") == 26
        )
        report(resized, "a window size change redraws at 60x15")

        run.settle()
        run.set_mark()
        run.send(b"\x1a")
        status = run.wait(os.WUNTRACED)
        report(status is not None and os.WIFSTOPPED(status), "Ctrl+Z stops the process")
        run.settle(0.1)
        report(CSI + b"?1049l" in run.since_mark() and run.vt.restored(),
               "stopped: the restore sequence was written and the modes are back")
        report(termios.tcgetattr(run.slave) == run.before, "stopped: the terminal attributes are restored")

        run.set_mark()
        os.kill(run.pid, signal.SIGCONT)
        resumed = run.until(lambda: run.vt.mode(1049) and row_of(run.vt, "Count: 3") == 7)
        report(resumed and CSI + b"?1049h" in run.since_mark(),
               "SIGCONT resumes it on the alternate screen with a full redraw")
        raw = termios.tcgetattr(run.slave)
        report(raw[3] & termios.ICANON == 0, "resumed: the terminal is in raw mode again")
        run.send(b"+")
        report(run.until(lambda: row_of(run.vt, "Count: 4") is not None), "resumed: keys still work")

        run.settle()
        run.set_mark()
        os.kill(run.pid, signal.SIGTSTP)
        status = run.wait(os.WUNTRACED)
        run.settle(0.1)
        report(status is not None and os.WIFSTOPPED(status) and run.vt.restored()
               and termios.tcgetattr(run.slave) == run.before,
               "SIGTSTP from outside stops it with the terminal restored")
        os.kill(run.pid, signal.SIGCONT)
        report(run.until(lambda: run.vt.mode(1049) and row_of(run.vt, "Count: 4") == 7),
               "SIGCONT resumes it with a full redraw")

        run.settle()
        run.set_mark()
        os.kill(run.pid, signal.SIGTERM)
        check_exit(run, "SIGTERM")
    finally:
        run.close()


def check_ctrl_c(path):
    run = Run(path, 40, 10)
    try:
        report(run.until(lambda: row_of(run.vt, "Count: 0") is not None), "second run: the first frame is drawn")
        run.settle()
        run.set_mark()
        run.send(b"\x03")
        check_exit(run, "Ctrl+C")
    finally:
        run.close()


def check_quit(path):
    run = Run(path, 40, 10)
    try:
        report(run.until(lambda: row_of(run.vt, "Count: 0") is not None), "third run: the first frame is drawn")
        run.settle()
        run.set_mark()
        run.send(b"q")
        check_exit(run, "q")
    finally:
        run.close()


def check_hangup(path):
    run = Run(path, 40, 10)
    report(run.until(lambda: row_of(run.vt, "Count: 0") is not None), "fourth run: the first frame is drawn")
    run.settle()
    # The terminal goes away, as when its window is closed.
    os.close(run.master)
    status = None
    deadline = time.monotonic() + TIMEOUT
    while status is None and time.monotonic() < deadline:
        pid, code = os.waitpid(run.pid, os.WNOHANG)
        status = code if pid else None
        time.sleep(0.02)
    report(status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0,
           f"a hangup ends the program with exit status 0 (status {status})")
    if status is None:
        os.kill(run.pid, signal.SIGKILL)
        os.waitpid(run.pid, 0)
    os.close(run.slave)


def in_session(check, path):
    """Runs `check` in a child that leads a new session, so it can own a new
    pty as its controlling terminal: the number of checks that failed."""
    pid = os.fork()
    if pid == 0:
        os.setsid()
        # Closing a pty hangs up its session's leader.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            check(path)
        finally:
            os._exit(min(failures, 100))
    _, status = os.waitpid(pid, 0)
    return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    path = os.path.abspath(sys.argv[1])
    failed = sum(in_session(check, path) for check in (check_session, check_ctrl_c, check_quit, check_hangup))
    print("all checks passed" if failed == 0 else f"{failed} checks failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
