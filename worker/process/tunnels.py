# -*- coding: utf-8 -*-
"""
CF隧道持久化管理

ghvps.json的tunnels字段管理CF隧道的启动和持久化。
scanner排除所有cloudflared进程，隧道由tunnels字段专门管理。

备份时自动复制凭证文件到持久化目录（随机命名防止冲突，每次清空重建）。
恢复时自动启动cloudflared，记录PID到known字典。
monitor_loop检测隧道崩溃并自动重启。
"""
import os
import uuid
import shutil
import signal
import subprocess
import time

import config
import log
from core import utils

logger = log.setup_logger("tunnel")

TUNNELS_SUBDIR = "tunnels"


def copy_tunnel_files(cfg, proc_name, base_dir):
    """备份时复制凭证文件到持久化目录，修改ghvps.json中的路径

    每次备份清空tunnels目录重建，确保只保留当前配置的文件。
    """
    tunnels = cfg.get("tunnels") or []
    if not tunnels:
        return cfg

    tunnels_dir = os.path.join(base_dir, proc_name, TUNNELS_SUBDIR)
    # 清空旧文件
    if os.path.isdir(tunnels_dir):
        for f in os.listdir(tunnels_dir):
            try:
                os.remove(os.path.join(tunnels_dir, f))
            except Exception as e:
                logger.debug(f"[tunnel] 复制凭证失败: {e}")
    os.makedirs(tunnels_dir, exist_ok=True)

    for tunnel in tunnels:
        if tunnel.get("type") != "credentials":
            continue

        # 复制 credentials_file
        creds = tunnel.get("credentials_file", "")
        if creds and os.path.exists(creds):
            ext = os.path.splitext(creds)[1]
            dest = os.path.join(tunnels_dir, uuid.uuid4().hex[:12] + ext)
            try:
                shutil.copy2(creds, dest)
                tunnel["credentials_file"] = dest
                logger.info(f"[tunnel] 复制凭证 {os.path.basename(creds)} → {os.path.basename(dest)}")
            except Exception as e:
                logger.warning(f"[tunnel] 复制凭证失败 {creds}: {e}")
        elif creds:
            logger.warning(f"[tunnel] 凭证文件不存在: {creds}")

        # 复制 config_file
        cfile = tunnel.get("config_file", "")
        if cfile and os.path.exists(cfile):
            ext = os.path.splitext(cfile)[1]
            dest = os.path.join(tunnels_dir, uuid.uuid4().hex[:12] + ext)
            try:
                shutil.copy2(cfile, dest)
                tunnel["config_file"] = dest
                logger.info(f"[tunnel] 复制配置 {os.path.basename(cfile)} → {os.path.basename(dest)}")
            except Exception as e:
                logger.warning(f"[tunnel] 复制配置失败 {cfile}: {e}")
        elif cfile:
            logger.warning(f"[tunnel] 配置文件不存在: {cfile}")

    return cfg


def start_tunnels(cfg, known_entry, proc_name):
    """启动隧道，记录PID到 known_entry['tunnel_pids']（字典 {隧道名: PID}）"""
    tunnels = cfg.get("tunnels") or []
    if not tunnels:
        return

    # 检查隧道名唯一性
    seen = set()
    valid = []
    for t in tunnels:
        name = t.get("name", "")
        if not name or name in seen:
            logger.warning(f"[tunnel] 跳过重复或无名的隧道: {name}")
            continue
        seen.add(name)
        valid.append(t)

    if "tunnel_pids" not in known_entry:
        known_entry["tunnel_pids"] = {}

    for tunnel in valid:
        name = tunnel.get("name", "")
        ttype = tunnel.get("type", "")

        # 已在运行则跳过
        existing = known_entry["tunnel_pids"].get(name)
        if existing and utils.is_alive(existing):
            continue

        # 构建启动命令
        if ttype == "token":
            token = tunnel.get("token", "")
            if not token:
                logger.warning(f"[tunnel] {proc_name}/{name} token为空，跳过")
                continue
            cmd = f"cloudflared tunnel --no-autoupdate run --token {token}"
        elif ttype == "credentials":
            creds = tunnel.get("credentials_file", "")
            cfile = tunnel.get("config_file", "")
            if not creds or not os.path.exists(creds):
                logger.warning(f"[tunnel] {proc_name}/{name} 凭证文件不存在，跳过")
                continue
            if not cfile or not os.path.exists(cfile):
                logger.warning(f"[tunnel] {proc_name}/{name} 配置文件不存在，跳过")
                continue
            tid = tunnel.get("tunnel_id", "")
            cmd = f"cloudflared tunnel --no-autoupdate --config {cfile} run"
            if tid:
                cmd += f" {tid}"
        else:
            logger.warning(f"[tunnel] {proc_name}/{name} 未知类型: {ttype}，跳过")
            continue

        # 启动
        log_path = os.path.join(config.LOGS_DIR, f"{proc_name}-{name}.log")
        try:
            logf = open(log_path, "ab")
        except Exception:
            logf = subprocess.DEVNULL

        try:
            proc = subprocess.Popen(
                cmd, shell=True, stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=True, executable="/bin/bash")
            known_entry["tunnel_pids"][name] = proc.pid
            token_disp = (tunnel.get("token", "")[:10] + "...") if tunnel.get("token") else ""
            logger.info(f"[tunnel] {proc_name}/{name} 启动 (pid={proc.pid}, type={ttype}, token={token_disp})")
        except Exception as e:
            logger.error(f"[tunnel] {proc_name}/{name} 启动失败: {e}")

    if not known_entry.get("tunnel_pids"):
        known_entry.pop("tunnel_pids", None)


def stop_tunnels(known_entry):
    """停止所有隧道进程"""
    pids = known_entry.get("tunnel_pids", {})
    for name, pid in list(pids.items()):
        if pid and utils.is_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(1)
                if utils.is_alive(pid):
                    os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError) as e:
                logger.debug(f"[tunnel] 停止进程失败: {e}")
        logger.info(f"[tunnel] 停止 {name} (pid={pid})")
    known_entry.pop("tunnel_pids", None)


def get_tunnel_status(cfg, known_entry):
    """获取隧道状态列表（用于list_processes）"""
    tunnels = cfg.get("tunnels") or []
    if not tunnels:
        return []

    pids = known_entry.get("tunnel_pids", {})
    result = []
    for tunnel in tunnels:
        name = tunnel.get("name", "")
        ttype = tunnel.get("type", "unknown")
        pid = pids.get(name)
        running = bool(pid and utils.is_alive(pid))
        info = {
            "name": name,
            "type": ttype,
            "running": running,
            "pid": pid if running else None,
        }
        if ttype == "token":
            info["port"] = tunnel.get("port")
        elif ttype == "credentials":
            info["tunnel_id"] = tunnel.get("tunnel_id", "")
        result.append(info)
    return result


def check_and_restart(known_entry, proc_name):
    """检查隧道崩溃并重启（monitor_loop调用）"""
    pids = known_entry.get("tunnel_pids", {})
    if not pids:
        return

    crashed = False
    for name, pid in list(pids.items()):
        if pid and not utils.is_alive(pid):
            logger.warning(f"[tunnel] {proc_name}/{name} 崩溃 (pid={pid})，准备重启")
            crashed = True

    if crashed:
        cfg = known_entry.get("config") or {}
        known_entry.pop("tunnel_pids", None)
        start_tunnels(cfg, known_entry, proc_name)
