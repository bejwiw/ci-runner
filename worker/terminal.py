# -*- coding: utf-8 -*-
"""
WSS 交互式终端（PTY + pyte 屏幕模拟 + 断线无缝）

- pty.fork 启动 sudo -i（免密 root）
- 断线不关 fd（保留 bash，重连无缝）
- pyte 模拟屏幕，支持干净屏幕复制
- 空闲会话自动清理
"""
import os
import pty
import time
import fcntl
import signal
import struct
import termios
import threading

import pyte

import config
import log

logger = log.setup_logger("terminal")

SESSIONS = {}
_lock = threading.Lock()


class Session:
    def __init__(self, key, cols=120, rows=35):
        self.key = key
        self.cols = cols
        self.rows = rows
        self.pid, self.fd = self._spawn()
        self.last_active = time.time()
        self.attached = True
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)

    @staticmethod
    def _spawn():
        pid, fd = pty.fork()
        if pid == 0:
            env = os.environ.copy()
            env["LANG"] = "C.UTF-8"
            env["LC_ALL"] = "C.UTF-8"
            env["TERM"] = "xterm-256color"
            env["GHBOX_PERSIST_DIR"] = config.FILES_DIR
            os.execvpe("sudo", ["sudo", "-i"], env)
        return pid, fd

    def feed(self, data: bytes):
        try:
            self.stream.feed(data.decode("utf-8", errors="replace"))
        except Exception as e:
            logger.debug(f"PTY操作失败: {e}")

    def get_screen(self):
        try:
            return "\n".join(self.screen.display)
        except Exception:
            return ""

    def read_output(self, chunk=8192):
        try:
            return os.read(self.fd, chunk)
        except OSError:
            return None

    def write_input(self, data: bytes):
        try:
            os.write(self.fd, data)
            self.last_active = time.time()
        except OSError as e:
            logger.debug(f"PTY IO失败: {e}")

    def resize(self, rows, cols):
        try:
            self.rows, self.cols = rows, cols
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
            self.screen.resize(rows, cols)
        except Exception as e:
            logger.debug(f"PTY操作失败: {e}")

    def destroy(self):
        try:
            os.kill(self.pid, signal.SIGHUP)
            time.sleep(0.2)
            os.kill(self.pid, signal.SIGKILL)
        except Exception as e:
            logger.debug(f"PTY操作失败: {e}")
        try:
            os.close(self.fd)
        except Exception as e:
            logger.debug(f"PTY操作失败: {e}")


def get_or_create_session(session_key):
    with _lock:
        sess = SESSIONS.get(session_key)
        if sess:
            sess.attached = True
            sess.last_active = time.time()
            return sess
        sess = Session(session_key)
        SESSIONS[session_key] = sess
        return sess


def detach_session(session_key):
    with _lock:
        sess = SESSIONS.get(session_key)
        if sess:
            sess.attached = False
            sess.last_active = time.time()


def get_screen(session_key):
    sess = SESSIONS.get(session_key)
    return sess.get_screen() if sess else ""


def cleanup_loop():
    while True:
        time.sleep(30)
        now = time.time()
        with _lock:
            stale = [k for k, s in SESSIONS.items()
                     if not s.attached and (now - s.last_active) > config.SESSION_TTL]
            for k in stale:
                SESSIONS.pop(k).destroy()
                logger.info(f"会话过期: {k}")


def start_cleanup():
    threading.Thread(target=cleanup_loop, daemon=True).start()
    logger.info("清理线程已启动")
