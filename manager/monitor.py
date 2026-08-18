# -*- coding: utf-8 -*-
"""
健康监控 + 自动恢复（manager 侧）

- 周期巡检实例健康（HTTP 探活）
- 连续失败自动重启实例
- 账号被封自动清理
"""
import time
import threading
import urllib.request

import config
import log
from core import ghapi
from manager import store, accounts

logger = log.setup_logger("monitor")

_fail_counts = {}
_restart_counts = {}
MAX_RESTART = 5


def check_health(host):
    try:
        req = urllib.request.Request(
            f"https://{host}/api/health",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def _restart_instance(inst):
    n = _restart_counts.get(inst["id"], 0)
    if n >= MAX_RESTART:
        logger.error(f"实例 {inst['id']} 连续重启 {n} 次仍失败，标记 failed 不再重启")
        inst["status"] = "failed"
        return False
    delay = min(2 ** n, 300)
    logger.warning(f"实例 {inst['id']} 第 {n+1}/{MAX_RESTART} 次重启，等待 {delay}s")
    time.sleep(delay)
    account = next((a for a in accounts.load_accounts()
                    if a["name"] == inst.get("account")), None)
    if not account:
        return False
    repo = account.get("repo") or config.REPO
    url = f"{ghapi.API_BASE}/repos/{repo}/actions/workflows/{config.WORKER_WORKFLOW}/dispatches"
    ghapi.gh_request("POST", url, token=account.get("token"),
                     data={"ref": "main", "inputs": {"INSTANCE_ID": inst["id"]}})
    _restart_counts[inst["id"]] = n + 1
    logger.info(f"实例 {inst['id']} 已触发第 {n+1} 次重启")
    return True


def _auto_cleanup_account(account):
    logger.warning(f"账号 {account['name']} 被封，自动清理")
    insts = store.load_instances()
    for inst in insts:
        if inst.get("account") == account.get("name") and not inst.get("closed"):
            _restart_counts.pop(inst["id"], None)
            try:
                store.close_instance(inst["id"])
            except Exception as e:
                logger.error(f"关闭 {inst['id']} 失败: {e}")
    accounts.remove_account(account["name"])


def health_monitor_loop():
    while True:
        time.sleep(60)
        try:
            insts = store.load_instances()
            changed = False
            for inst in insts:
                if inst.get("closed"):
                    continue
                host = inst.get("hostname")
                if not host:
                    continue
                if check_health(host):
                    _fail_counts[inst["id"]] = 0
                    _restart_counts.pop(inst["id"], None)
                    if inst.get("status") != "running":
                        inst["status"] = "running"
                        inst["last_seen"] = time.time()
                        changed = True
                else:
                    if inst.get("status") != "running":
                        # restarting状态超时(>10min)：检查账号是否被封
                        if inst.get("status") == "restarting":
                            restart_time = inst.get("last_seen", 0)
                            if isinstance(restart_time, (int, float)) and restart_time and time.time() - restart_time > 600:
                                logger.warning(f"实例 {inst['id']} 重启超时(>10min)，检查账号状态")
                                account = next((a for a in accounts.load_accounts()
                                                if a["name"] == inst.get("account")), None)
                                if account and ghapi.check_account_suspended(account.get("token")):
                                    logger.warning(f"账号 {account['name']} 已被封禁，自动清理")
                                    _auto_cleanup_account(account)
                                    _fail_counts[inst["id"]] = 0
                                    changed = True
                        continue
                    last_seen = inst.get("last_seen", 0)
                    if isinstance(last_seen, (int, float)) and last_seen and time.time() - last_seen < 180:
                        _fail_counts[inst["id"]] = 0
                        continue
                    n = _fail_counts.get(inst["id"], 0) + 1
                    _fail_counts[inst["id"]] = n
                    logger.warning(f"实例 {inst['id']} 失败 {n}/3")
                    if n >= 3:
                        account = next((a for a in accounts.load_accounts()
                                        if a["name"] == inst.get("account")), None)
                        if account and ghapi.check_account_suspended(account.get("token")):
                            _auto_cleanup_account(account)
                        else:
                            _restart_instance(inst)
                            inst["status"] = "restarting"
                            inst["last_seen"] = time.time()
                        _fail_counts[inst["id"]] = 0
                        changed = True
            if changed:
                store.save_instances(insts)
        except Exception as e:
            logger.error(f"巡检异常: {e}")


def start_monitors():
    threading.Thread(target=health_monitor_loop, daemon=True).start()
    logger.info("健康监控已启动")
