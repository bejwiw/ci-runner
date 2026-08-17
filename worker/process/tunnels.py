# -*- coding: utf-8 -*-
"""
CF隧道持久化管理

ghvps.json的tunnels字段管理CF隧道的启动和持久化。
scanner排除所有cloudflared进程，隧道由tunnels字段专门管理。

核心功能：
- 支持 ingress 规则自动生成 config.yml（规避 token 方式覆盖 ingress 的问题）
- token 方式：从 token 解析出 tunnel_id/secret，生成 credentials.json + config.yml
- credentials 方式：复制凭证文件，更新 config.yml 里的路径引用
- 恢复时验证文件存在，缺失则跳过并报日志
"""
import os
import json
import base64
import uuid
import shutil
import signal
import subprocess
import time

import config
import log
from core import utils

logger = log.setup_logger("proc.tunnels")

TUNNELS_SUBDIR = "tunnels"


# ==================== token 解析 + config.yml 生成 ====================

def _parse_token(token):
    """解析 cloudflared token，返回 (account_id, tunnel_id, tunnel_secret)"""
    try:
        decoded = base64.b64decode(token)
        d = json.loads(decoded)
        return d.get("a", ""), d.get("t", ""), d.get("s", "")
    except Exception as e:
        logger.warning(f"token 解析失败: {e}")
        return "", "", ""


def _generate_config_yml(tunnel_id, creds_path, ingress_rules, tunnels_dir):
    """从 ingress 规则生成 config.yml（不依赖 PyYAML，手动写 YAML）

    自动确保最后一条是 catch-all（无 hostname 的 service）。
    """
    config_path = os.path.join(tunnels_dir, uuid.uuid4().hex[:12] + ".yml")

    # 确保最后一条是 catch-all
    rules = list(ingress_rules)
    has_catchall = any(not r.get("hostname") for r in rules)
    if not has_catchall:
        rules.append({"service": "http_status:404"})

    lines = []
    if tunnel_id:
        lines.append(f"tunnel: {tunnel_id}")
    if creds_path:
        lines.append(f"credentials-file: {creds_path}")
    lines.append("ingress:")
    for rule in rules:
        hostname = rule.get("hostname", "")
        service = rule.get("service", "http_status:404")
        if hostname:
            lines.append(f"  - hostname: {hostname}")
            lines.append(f"    service: {service}")
        else:
            lines.append(f"  - service: {service}")
    # originRequest 默认配置（避免 cloudflared 警告）
    lines.append("originRequest:")
    lines.append("  noTLSVerify: true")

    with open(config_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return config_path


def _update_config_yml_credentials(config_path, new_creds_path):
    """更新 config.yml 里的 credentials-file 路径"""
    if not os.path.exists(config_path):
        return
    try:
        with open(config_path, "r") as f:
            content = f.read()
        import re
        content = re.sub(
            r"credentials-file:\s*.*",
            f"credentials-file: {new_creds_path}",
            content)
        with open(config_path, "w") as f:
            f.write(content)
        logger.debug(f"config.yml credentials-file 已更新 → {new_creds_path}")
    except Exception as e:
        logger.warning(f"更新 config.yml 失败: {e}")


# ==================== 备份时复制/生成凭证文件 ====================

def copy_tunnel_files(cfg, proc_name, base_dir):
    """备份时复制/生成凭证文件和配置文件到持久化目录

    对于有 ingress 规则的 tunnel：
      - token 方式：解析 token → 生成 credentials.json → 生成 config.yml（含 ingress）
      - credentials 方式：复制 credentials_file → 生成 config.yml（含 ingress）
    对于无 ingress 规则的 tunnel：
      - credentials 方式：复制 credentials_file + config_file，更新路径
      - token 方式：不复制任何文件（用 --token 方式启动）
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
                logger.debug(f"清理旧文件失败: {e}")
    os.makedirs(tunnels_dir, exist_ok=True)

    for tunnel in tunnels:
        ttype = tunnel.get("type", "")
        ingress_rules = tunnel.get("ingress", [])

        if ttype == "token" and ingress_rules:
            # token + ingress → 从 token 生成 credentials.json + config.yml
            token = tunnel.get("token", "")
            if not token:
                logger.warning(f"{proc_name}: token 为空，跳过")
                continue
            account, tunnel_id, secret = _parse_token(token)
            if not tunnel_id or not secret:
                logger.warning(f"{proc_name}: token 解析失败，跳过")
                continue
            # 生成 credentials.json
            creds_path = os.path.join(tunnels_dir, uuid.uuid4().hex[:12] + ".json")
            try:
                creds_data = {
                    "AccountTag": account,
                    "TunnelID": tunnel_id,
                    "TunnelSecret": secret,
                }
                with open(creds_path, "w") as f:
                    json.dump(creds_data, f)
                tunnel["credentials_file"] = creds_path
                tunnel["tunnel_id"] = tunnel_id
                logger.info(f"{proc_name}: 从 token 生成 credentials.json")
            except Exception as e:
                logger.warning(f"{proc_name}: 生成 credentials.json 失败: {e}")
                continue
            # 生成 config.yml
            config_path = _generate_config_yml(tunnel_id, creds_path, ingress_rules, tunnels_dir)
            tunnel["config_file"] = config_path
            logger.info(f"{proc_name}: 生成 config.yml（{len(ingress_rules)} 条 ingress）")

        elif ttype == "credentials" and ingress_rules:
            # credentials + ingress → 复制 credentials_file → 生成 config.yml
            creds = tunnel.get("credentials_file", "")
            if creds and os.path.exists(creds):
                ext = os.path.splitext(creds)[1]
                dest = os.path.join(tunnels_dir, uuid.uuid4().hex[:12] + ext)
                try:
                    shutil.copy2(creds, dest)
                    tunnel["credentials_file"] = dest
                    logger.info(f"{proc_name}: 复制凭证 → {os.path.basename(dest)}")
                except Exception as e:
                    logger.warning(f"{proc_name}: 复制凭证失败: {e}")
                    continue
            elif creds:
                logger.warning(f"{proc_name}: 凭证文件不存在: {creds}")
                continue
            # 生成 config.yml（用新路径 + ingress 规则）
            tid = tunnel.get("tunnel_id", "")
            config_path = _generate_config_yml(tid, tunnel.get("credentials_file", ""), ingress_rules, tunnels_dir)
            tunnel["config_file"] = config_path
            logger.info(f"{proc_name}: 生成 config.yml（{len(ingress_rules)} 条 ingress）")

        elif ttype == "credentials":
            # credentials 无 ingress → 复制 credentials_file + config_file，更新路径
            creds = tunnel.get("credentials_file", "")
            if creds and os.path.exists(creds):
                ext = os.path.splitext(creds)[1]
                dest = os.path.join(tunnels_dir, uuid.uuid4().hex[:12] + ext)
                try:
                    shutil.copy2(creds, dest)
                    tunnel["credentials_file"] = dest
                    logger.info(f"{proc_name}: 复制凭证 → {os.path.basename(dest)}")
                except Exception as e:
                    logger.warning(f"{proc_name}: 复制凭证失败: {e}")
            elif creds:
                logger.warning(f"{proc_name}: 凭证文件不存在: {creds}")

            cfile = tunnel.get("config_file", "")
            if cfile and os.path.exists(cfile):
                ext = os.path.splitext(cfile)[1]
                dest = os.path.join(tunnels_dir, uuid.uuid4().hex[:12] + ext)
                try:
                    shutil.copy2(cfile, dest)
                    tunnel["config_file"] = dest
                    # 更新 config.yml 里的 credentials-file 路径
                    if tunnel.get("credentials_file"):
                        _update_config_yml_credentials(dest, tunnel["credentials_file"])
                    logger.info(f"{proc_name}: 复制配置 → {os.path.basename(dest)}")
                except Exception as e:
                    logger.warning(f"{proc_name}: 复制配置失败: {e}")
            elif cfile:
                logger.warning(f"{proc_name}: 配置文件不存在: {cfile}")

        # token 无 ingress → 不复制任何文件（用 --token 方式启动）

    return cfg


# ==================== 启动/停止隧道 ====================

def _token_extract_tunnel_id(token):
    """从 CF 隧道 token 解码提取 tunnel_id（t 字段）

    CF token 是 base64 JSON：{"a": account_id, "t": tunnel_id, "s": secret}。
    所有 token 的 account_id 相同，不能用 token 前缀匹配（会互相误杀），
    必须用 t 字段（每个隧道唯一）。
    """
    if not token:
        return ""
    try:
        import base64
        payload = token
        padded = payload + "=" * (-len(payload) % 4)
        d = json.loads(base64.b64decode(padded))
        return d.get("t", "") or ""
    except Exception:
        return ""


def _kill_orphan_tunnels(proc_name, tunnel):
    """精准清理该隧道的孤儿 cloudflared 进程

    匹配依据（唯一标识）：
    - 配置里的 tunnel_id（显式指定）
    - 从 token 解码出的 tunnel_id（t 字段）
    绝不使用 token 前缀匹配——同一 account 下所有 token 前缀相同，会误杀主隧道。
    """
    import signal as _sig
    token = tunnel.get("token", "")
    tid = tunnel.get("tunnel_id", "") or _token_extract_tunnel_id(token)
    if not token and not tid:
        return
    try:
        for pid_str in os.listdir("/proc"):
            if not pid_str.isdigit():
                continue
            try:
                with open(f"/proc/{pid_str}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\x00", b" ").decode(errors="replace")
                if "cloudflared" not in cmd:
                    continue
                # 匹配唯一标识，绝不按 token 前缀（同账号下前缀相同会误杀）：
                # - config 方式：命令行含明文 tunnel_id
                # - token 方式：命令行含完整唯一 token（每隧道不同）
                hit = False
                if tid and tid in cmd:
                    hit = True
                elif token and len(token) > 30 and token in cmd:
                    hit = True
                if hit:
                    os.kill(int(pid_str), _sig.SIGKILL)
                    logger.info(f"{proc_name} 清理旧隧道进程 pid={pid_str}")
            except (ProcessLookupError, PermissionError):
                continue
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"清理孤儿隧道失败: {e}")


def start_tunnels(cfg, known_entry, proc_name):
    """启动隧道，记录PID到 known_entry['tunnel_pids']

    优先用 --config 方式启动（支持 ingress 规则）。
    无 config_file 时回退到 --token 方式。
    启动前先清理该隧道的孤儿进程，避免重复。
    """
    tunnels = cfg.get("tunnels") or []
    if not tunnels:
        return

    # 检查隧道名唯一性
    seen = set()
    valid = []
    for t in tunnels:
        name = t.get("name", "")
        if not name or name in seen:
            logger.warning(f"跳过重复或无名的隧道: {name}")
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

        # 启动前清理孤儿进程（进程被强杀后的残留 cloudflared）
        _kill_orphan_tunnels(proc_name, tunnel)

        # 优先用 config 方式（支持 ingress 规则，不会被覆盖）
        config_file = tunnel.get("config_file", "")
        token = tunnel.get("token", "")
        tid = tunnel.get("tunnel_id", "")

        if config_file and os.path.exists(config_file):
            # 验证 credentials_file 是否存在（config 方式需要）
            creds = tunnel.get("credentials_file", "")
            if creds and not os.path.exists(creds):
                logger.warning(f"{proc_name}/{name} 凭证文件不存在: {creds}，跳过")
                continue
            cmd = f"cloudflared tunnel --no-autoupdate --config {config_file} run"
            if tid:
                cmd += f" {tid}"
            logger.info(f"{proc_name}/{name} 用 config 方式启动 (config={os.path.basename(config_file)})")
        elif token:
            # 无 config_file，用 token 方式启动
            cmd = f"cloudflared tunnel --no-autoupdate run --token {token}"
            logger.info(f"{proc_name}/{name} 用 token 方式启动")
        else:
            logger.warning(f"{proc_name}/{name} 无 config_file 且无 token，跳过")
            continue

        # 启动
        log_path = os.path.join(config.LOGS_DIR, f"{proc_name}-{name}.log")
        try:
            logf = open(log_path, "ab")
        except Exception as e:
            logger.debug(f"打开日志文件失败: {e}")
            logf = subprocess.DEVNULL

        try:
            proc = subprocess.Popen(
                cmd, shell=True, stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=True, executable="/bin/bash")
            known_entry["tunnel_pids"][name] = proc.pid
            token_disp = (token[:10] + "...") if token else ""
            logger.info(f"{proc_name}/{name} 启动 (pid={proc.pid}, type={ttype}, token={token_disp})")
        except Exception as e:
            logger.error(f"{proc_name}/{name} 启动失败: {e}")

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
                logger.debug(f"停止进程失败: {e}")
        logger.info(f"停止 {name} (pid={pid})")
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
            "has_ingress": bool(tunnel.get("ingress")),
        }
        if ttype == "token":
            info["port"] = tunnel.get("port")
        if tunnel.get("tunnel_id"):
            info["tunnel_id"] = tunnel.get("tunnel_id")
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
            logger.warning(f"{proc_name}/{name} 崩溃 (pid={pid})，准备重启")
            crashed = True

    if crashed:
        cfg = known_entry.get("config") or {}
        known_entry.pop("tunnel_pids", None)
        start_tunnels(cfg, known_entry, proc_name)
