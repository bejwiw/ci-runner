# -*- coding: utf-8 -*-
"""
WSS 交互式终端客户端

特性：
- 静默重连（最多3次，递增退避）
- 重连后恢复窗口尺寸
- Ctrl+4 退出终端
- Ctrl+O 复制干净屏幕
"""
import os
import sys
import time
import tty
import json
import struct
import fcntl
import termios
import threading
import urllib.request
import urllib.error

from cli import config

MAX_RECONNECT = 3
KEY_CTRL_4 = b"\x1c"
KEY_CTRL_O = b"\x0f"


def _get_term_size():
    try:
        return struct.unpack(
            "HHHH", fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0\0\0\0\0\0\0\0"))[:2]
    except Exception:
        return 35, 120


def _get_clean_screen(url, session):
    try:
        req = urllib.request.Request(
            url.rstrip("/") + f"/api/term/screen?session={session}",
            headers={"User-Agent": "Mozilla/5.0 (ghbox-cli)",
                     "Authorization": f"Bearer {config.TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
            if d.get("ok"):
                return d.get("screen", "")
    except Exception as e:
        sys.stderr.write(f"\r\n[terminal] {e}\r\n")
    return ""


def connect_terminal(host):
    """连接实例 WSS 终端"""
    url = f"https://{host}" if not host.startswith("http") else host
    session = config.load_session()
    state = {"force_exit": False}

    try:
        import socketio
    except ImportError:
        print("  需要安装 python-socketio: pip install python-socketio")
        return

    sio = socketio.Client(reconnection=False)

    @sio.on("output")
    def on_output(data):
        try:
            if isinstance(data, bytes):
                sys.stdout.buffer.write(data)
            else:
                sys.stdout.write(data)
            sys.stdout.flush()
        except Exception as e:
            sys.stderr.write(f"\r\n[terminal] {e}\r\n")

    @sio.on("exit")
    def on_exit(data):
        try:
            sio.disconnect()
        except Exception as e:
            sys.stderr.write(f"\r\n[terminal] {e}\r\n")

    @sio.event
    def connect():
        try:
            rows, cols = _get_term_size()
            sio.emit("resize", {"rows": rows, "cols": cols})
        except Exception as e:
            sys.stderr.write(f"\r\n[terminal] {e}\r\n")

    @sio.event
    def disconnect():
        pass

    def send_loop():
        try:
            while True:
                ch = os.read(0, 4096)
                if not ch:
                    break
                if KEY_CTRL_4 in ch:
                    state["force_exit"] = True
                    try:
                        sio.disconnect()
                    except Exception as e:
                        sys.stderr.write(f"\r\n[terminal] {e}\r\n")
                    break
                if KEY_CTRL_O in ch:
                    screen = _get_clean_screen(url, session)
                    if screen:
                        sys.stdout.write("\r\n" + screen + "\r\n")
                        sys.stdout.flush()
                    ch = ch.replace(KEY_CTRL_O, b"")
                    if not ch:
                        continue
                try:
                    sio.emit("input", ch)
                except Exception as e:
                    sys.stderr.write(f"\r\n[terminal] {e}\r\n")
        except Exception as e:
            sys.stderr.write(f"\r\n[terminal] {e}\r\n")
        finally:
            try:
                sio.disconnect()
            except Exception as e:
                sys.stderr.write(f"\r\n[terminal] {e}\r\n")

    def _connect():
        sio.connect(url, auth={"token": config.TOKEN, "session": session},
                    transports=["websocket"], wait_timeout=15)

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        try:
            _connect()
        except Exception as e:
            sys.stderr.write(f"\r\n[连接失败] {e}\r\n")
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            return
        threading.Thread(target=send_loop, daemon=True).start()
        while not state["force_exit"]:
            while sio.connected and not state["force_exit"]:
                time.sleep(0.5)
            if state["force_exit"]:
                break
            reconnected = False
            for attempt in range(1, MAX_RECONNECT + 1):
                if state["force_exit"]:
                    break
                time.sleep(attempt * 2)
                try:
                    _connect()
                    reconnected = True
                    break
                except Exception:
                    continue
            if not reconnected and not state["force_exit"]:
                state["force_exit"] = True
                sys.stderr.write(
                    f"\r\n[连接失败] 已重试 {MAX_RECONNECT} 次仍无法连接，退出终端\r\n")
                sys.stderr.flush()
    except KeyboardInterrupt:
        state["force_exit"] = True
    except Exception as e:
        state["force_exit"] = True
        sys.stderr.write(f"\r\n[终端错误] {e}\r\n")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if state["force_exit"]:
            try:
                sio.disconnect()
            except Exception as e:
                sys.stderr.write(f"\r\n[terminal] {e}\r\n")
