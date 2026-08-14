# -*- coding: utf-8 -*-
"""
Manager 后台线程：自愈 / 续命 / 隧道 / MCP 补创建

注意：_pre_wake 和 _auto_update 在 app.py 中定义（因为它们涉及 os._exit）。
本模块只包含自愈循环、隧道启动、MCP 隧道补创建。
"""
import time
import threading
import subprocess

import config
import log
from core import ghapi, releases, crypto
from manager import state, store, accounts, tunnels

logger = log.setup_logger("background")


def start_background():
    """启动后台线程（仅在 leader 模式下调用）"""
    threading.Thread(target=_heal_loop, daemon=True).start()
    threading.Thread(target=_start_tunnel, daemon=True).start()
    logger.info("[bg] 后台线程已启动")


def _start_tunnel():
    if not config.TUNNEL_TOKEN:
        return
    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--no-autoupdate", "run", "--token", config.TUNNEL_TOKEN],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        logger.info(f"[tunnel] 管理隧道: https://{config.TUNNEL_HOST}")
        for line in proc.stdout:
            if "Registered tunnel connection" in line.strip():
                logger.info("[tunnel] 连接已注册")
    except Exception as e:
        logger.error(f"[tunnel] 启动失败: {e}")


def _heal_loop():
    """自愈循环：S3 状态持久化 + 实例清单自愈 + MCP 隧道补创建"""
    empty_retries = 0
    while True:
        time.sleep(120)
        try:
            if not state.leader or not state.leader.is_leader:
                continue
            # S3 状态持久化（单独 try，不阻塞后续逻辑）
            if state.s3pool:
                try:
                    state.s3pool.save_state()
                except Exception as e:
                    logger.warning(f"[heal] S3 状态持久化失败: {e}")
            # 实例清单自愈
            insts = store.list_instances()
            if not insts:
                empty_retries += 1
                if empty_retries >= 3:
                    logger.warning("[heal] 实例清单连续3次为空，触发自愈")
                    _self_heal_instances()
                    empty_retries = 0
            else:
                empty_retries = 0
                _ensure_mcp_tunnels(insts)
        except Exception as e:
            logger.error(f"[heal] 异常: {e}")


def _self_heal_instances():
    """从 Releases 扫描重建实例清单"""
    result = []
    for acc in accounts.load_accounts():
        repo = acc.get("repo") or config.REPO
        token = acc.get("token")
        if not token:
            continue
        try:
            rel = releases.get_release(token=token, repo=repo)
            if not rel:
                continue
            for asset in rel.get("assets", []):
                name = asset.get("name", "")
                if name.startswith("inst-") and name.endswith(".json.enc"):
                    blob = releases.download_asset(name, token=token, repo=repo)
                    if blob:
                        try:
                            cfg = crypto.decrypt_json(blob)
                            inst_id = cfg.get("inst_id")
                            if inst_id:
                                hostname = cfg.get("hostname") or f"{inst_id}.{config.BASE_DOMAIN}"
                                result.append({
                                    "id": inst_id, "hostname": hostname,
                                    "account": cfg.get("account", acc.get("name", "")),
                                    "account_repo": cfg.get("account_repo", repo),
                                    "tunnel_id": cfg.get("tunnel_id", ""),
                                    "status": "running", "url": f"https://{hostname}",
                                    "closed": False, "run_id": None,
                                })
                        except Exception as e:
                            logger.debug(f"[heal] 解密实例配置失败: {e}")
        except Exception as e:
            logger.warning(f"[heal] 扫描账号 {acc.get('name')} 失败: {e}")
    if result:
        store.save_instances(result)
        logger.info(f"[heal] 实例清单已重建，共 {len(result)} 个")


def _ensure_mcp_tunnels(insts):
    """为缺少 MCP 隧道的实例补创建"""
    changed = False
    for inst in insts:
        if inst.get("closed") or inst.get("mcp_tunnel_id"):
            continue
        hostname = inst.get("hostname", "")
        if not hostname:
            continue
        mcp_hostname = f"mcp-{hostname}"
        try:
            mcp_tid, mcp_ttoken = tunnels.create_mcp_tunnel(mcp_hostname)
            inst["mcp_hostname"] = mcp_hostname
            inst["mcp_tunnel_id"] = mcp_tid
            inst["mcp_url"] = f"https://{mcp_hostname}"
            changed = True
            logger.info(f"[mcp] 为 {inst['id']} 创建 MCP 隧道: {mcp_hostname}")
            account = next((a for a in accounts.load_accounts()
                           if a["name"] == inst.get("account")), None)
            if account:
                cfg = store.load_instance_config(inst["id"]) or {}
                cfg["mcp_hostname"] = mcp_hostname
                cfg["mcp_tunnel_token"] = mcp_ttoken
                cfg["mcp_tunnel_id"] = mcp_tid
                store.save_instance_config(inst["id"], cfg)
        except Exception as e:
            if "already have a tunnel" in str(e) or "1013" in str(e):
                pass
            else:
                logger.warning(f"[mcp] {inst['id']} 创建失败: {e}")
    if changed:
        store.save_instances(insts)
