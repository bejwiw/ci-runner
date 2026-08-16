# -*- coding: utf-8 -*-
"""
Manager 实例/账号/任务 API 路由（Blueprint）
"""
import time
import json
import functools
import threading
import urllib.request

from flask import Blueprint, request, jsonify

import config
import log
from core import ghapi
from manager import state, store, accounts, tasks, tunnels, monitor

logger = log.setup_logger("api")
bp = Blueprint("api", __name__)


# ==================== 认证 ====================
def _token():
    t = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not t:
        t = (request.args.get("token") or "").strip()
    if not t:
        d = request.get_json(silent=True) or {}
        t = (d.get("token") or "").strip()
    return t


def _authed():
    return bool(config.EXEC_TOKEN) and _token() == config.EXEC_TOKEN


def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not _authed():
            return jsonify(ok=False, error="未授权"), 401
        return f(*args, **kwargs)
    return wrapper


def _is_leader():
    return bool(state.leader and state.leader.is_leader)


# ==================== 账号 ====================
@bp.route("/api/accounts", methods=["GET"])
@require_auth
def list_accounts():
    return jsonify(ok=True, accounts=accounts.list_accounts())


@bp.route("/api/accounts", methods=["POST"])
@require_auth
def add_account():
    if not _is_leader():
        return jsonify(ok=False, error="备份节点"), 503
    d = request.get_json(silent=True) or {}
    task = tasks.add_task("add_account", {
        "name": d.get("name", ""), "token": d.get("token", ""),
        "repo": d.get("repo"), "max_concurrency": d.get("max_concurrency"),
    }, dedup_key=f"add_account:{d.get('name', '')}")
    return jsonify(ok=True, task_id=task["id"], msg="配置任务已入队")


@bp.route("/api/accounts/<name>", methods=["DELETE"])
@require_auth
def remove_account(name):
    if not _is_leader():
        return jsonify(ok=False, error="备份节点"), 503
    return jsonify(accounts.remove_account(name))


# ==================== 任务 ====================
@bp.route("/api/tasks", methods=["GET"])
@require_auth
def list_tasks():
    return jsonify(ok=True, tasks=tasks.load_tasks())


# ==================== 实例 ====================
@bp.route("/api/instances", methods=["POST"])
@require_auth
def create_instance():
    if not _is_leader():
        return jsonify(ok=False, error="备份节点"), 503
    d = request.get_json(silent=True) or {}
    account_name = (d.get("account") or "").strip() or None
    if account_name:
        accts = accounts.load_accounts()
        account = next((a for a in accts if a["name"] == account_name), None)
        if not account:
            return jsonify(ok=False, error=f"账号 {account_name} 不存在"), 404
    else:
        sel = accounts.select_best_account(workflow=config.WORKER_WORKFLOW)
        if not sel:
            return jsonify(ok=False, error="所有账号并发已满"), 409
        account = sel[0]
    inst_id = store.next_inst_id()
    hostname = f"{inst_id}.{config.BASE_DOMAIN}"
    try:
        accounts.sync_fork(account)
        time.sleep(2)
    except Exception as e:
        logger.warning(f"fork 异常: {e}")
    try:
        tunnel_id, tunnel_token = tunnels.create_tunnel(hostname)
    except Exception as e:
        return jsonify(ok=False, error=f"创建隧道失败: {e}"), 500
    mcp_enabled = d.get("mcp_enabled", True)
    mcp_hostname = f"mcp-{hostname}"
    mcp_tunnel_id = ""
    mcp_ttoken = ""
    if mcp_enabled:
        try:
            mcp_tunnel_id, mcp_ttoken = tunnels.create_mcp_tunnel(mcp_hostname)
        except Exception as e:
            logger.warning(f"MCP 隧道失败: {e}")
    else:
        logger.info(f"MCP 未启用 (mcp_enabled=false)")
    inst_cfg = {
        "inst_id": inst_id, "hostname": hostname,
        "tunnel_token": tunnel_token, "tunnel_id": tunnel_id,
        "mcp_hostname": mcp_hostname, "mcp_tunnel_token": mcp_ttoken,
        "mcp_tunnel_id": mcp_tunnel_id,
        "account": account["name"], "account_repo": account["repo"],
        "mcp_enabled": d.get("mcp_enabled", True),
    }
    store.save_instance_config(inst_id, inst_cfg)
    try:
        run_id = _trigger_worker(account, inst_id)
    except Exception as e:
        tunnels.delete_tunnel(tunnel_id, hostname)
        return jsonify(ok=False, error=f"触发 worker 失败: {e}"), 500
    inst = {
        "id": inst_id, "hostname": hostname,
        "account": account["name"], "account_repo": account["repo"],
        "tunnel_id": tunnel_id, "mcp_hostname": mcp_hostname,
        "mcp_tunnel_id": mcp_tunnel_id, "run_id": run_id,
        "status": "starting", "url": f"https://{hostname}",
        "mcp_url": f"https://{mcp_hostname}" if mcp_ttoken else None,
        "closed": False, "created_at": time.time(),
    }
    store.add_instance(inst)
    logger.info(f"实例 {inst_id} 创建中: https://{hostname}")
    return jsonify(ok=True, instance=inst, msg=f"实例 {inst_id} 创建中")


def _trigger_worker(account, inst_id):
    repo = account["repo"]
    url = f"{ghapi.API_BASE}/repos/{repo}/actions/workflows/{config.WORKER_WORKFLOW}/dispatches"
    status, d = ghapi.gh_request("POST", url, token=account["token"],
                                 data={"ref": "main", "inputs": {"INSTANCE_ID": inst_id}})
    if status not in (200, 204):
        raise RuntimeError(f"触发失败: {status} {d}")
    time.sleep(4)
    status, d = ghapi.gh_request("GET",
        f"{ghapi.API_BASE}/repos/{repo}/actions/runs?per_page=10&event=workflow_dispatch",
        token=account["token"])
    if status == 200:
        for r in d.get("workflow_runs", []):
            # 只找 worker workflow 的 run，避免误取 manager 等其他 workflow
            if r.get("name") == "worker" or "worker" in (r.get("path") or ""):
                return r["id"]
    return None


@bp.route("/api/instances", methods=["GET"])
@require_auth
def list_instances():
    insts = store.list_instances()
    return jsonify(ok=True, instances=[i for i in insts if not i.get("closed")])


@bp.route("/api/instances/<inst_id>", methods=["GET"])
@require_auth
def get_instance(inst_id):
    inst = store.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    return jsonify(ok=True, instance=inst)


@bp.route("/api/instances/<inst_id>", methods=["DELETE"])
@require_auth
def close_instance(inst_id):
    if not _is_leader():
        return jsonify(ok=False, error="备份节点"), 503
    inst = store.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    account = next((a for a in accounts.load_accounts()
                    if a["name"] == inst.get("account")), None)
    if account and inst.get("run_id"):
        try:
            _c_status, _ = ghapi.gh_request("POST",
                f"{ghapi.API_BASE}/repos/{account['repo']}/actions/runs/{inst['run_id']}/cancel",
                token=account["token"])
            if _c_status not in (200, 202):
                logger.warning(f"取消 run {inst['run_id']} 失败: HTTP {_c_status}")
            else:
                logger.info(f"已取消 run {inst['run_id']}")
        except Exception as e:
            logger.warning(f"取消 run {inst['run_id']} 异常: {e}")
    store.delete_instance_config(inst_id)
    if inst.get("tunnel_id"):
        tunnels.delete_tunnel(inst["tunnel_id"], inst.get("hostname", ""))
    if inst.get("mcp_tunnel_id"):
        tunnels.delete_tunnel(inst["mcp_tunnel_id"], inst.get("mcp_hostname", ""))
    # 异步清理备份数据，不阻塞 close 响应（大量分片时可能较慢）
    threading.Thread(target=store.purge_instance_data, args=(inst_id,), daemon=True).start()
    state.worker_heartbeats.pop(inst_id, None)
    store.close_instance(inst_id)
    return jsonify(ok=True, msg=f"实例 {inst_id} 已关闭")


@bp.route("/api/instances/<inst_id>/report", methods=["POST"])
def instance_report(inst_id):
    """worker 上报（含自愈，修复旧项目 404 bug）"""
    if not _authed():
        return jsonify(ok=False, error="未授权"), 401
    if not _is_leader():
        return jsonify(ok=False, error="备份节点"), 503
    d = request.get_json(silent=True) or {}
    # 存储worker上报的S3统计到worker_heartbeats
    # 不覆盖 job_id 和 heartbeat（leader 选举专用字段，由 /api/worker/heartbeat 维护）
    _s3 = d.get("s3", {})
    _pa = _s3.get("a_ops", 0)
    _pb = _s3.get("b_ops", 0)
    _storage = _s3.get("storage_mb", 0)
    _existing = state.worker_heartbeats.get(inst_id, {})
    _existing.update({
        "last_seen": time.time(),
        "s3": _s3,
        "procs": d.get("procs", 0),
        "disk_pct": d.get("disk_pct", 0),
        "a_ops": _pa,
        "b_ops": _pb,
        "storage_mb": _storage,
    })
    state.worker_heartbeats[inst_id] = _existing
    # 更新 run_id（worker每次上报携带当前 GITHUB_RUN_ID，续命后自动更新）
    _run_id = d.get("run_id", "")
    if _run_id:
        store.update_instance(inst_id, run_id=_run_id)
    # 累积到worker_stats（次数累积，存储实时，历史每次更新）
    _wstats = store.get_worker_stats(inst_id)
    # 用 worker 上报的 S3Pool 累积值（含恢复 B类操作），不用 pending 累加
    _wstats["a_count_total"] = _s3.get("a_ops_total", _wstats.get("a_count_total", 0))
    _wstats["b_count_total"] = _s3.get("b_ops_total", _wstats.get("b_count_total", 0))
    _wstats["storage_mb"] = _storage
    _wstats["last_backup"] = time.time()
    _wstats["last_seen"] = time.time()
    _bh = _s3.get("backup_history", [])
    if _bh:
        _wstats["backup_history"] = _bh
    store.save_worker_stats(inst_id, _wstats)
    # 同步累积统计到内存 heartbeats（供 s3/status 直接读取，不用查 S3）
    _existing["a_count_total"] = _wstats.get("a_count_total", 0)
    _existing["b_count_total"] = _wstats.get("b_count_total", 0)
    state.worker_heartbeats[inst_id] = _existing
    inst = store.get_instance(inst_id)
    if not inst:
        cfg = store.load_instance_config(inst_id)
        if cfg:
            inst = store.get_or_create_instance(inst_id, cfg)
            logger.info(f"实例 {inst_id} 已自愈恢复")
        else:
            return jsonify(ok=False, error=f"实例 {inst_id} 不存在且无配置"), 404
    # 防御：重启中不改状态（worker 收到 /api/shutdown 后会设 shutting_down 停止上报，
    # 但可能有在途请求，双保险）
    if inst.get("status") == "restarting":
        store.update_instance(inst_id, url=d.get("url", inst.get("url", "")))
    else:
        store.update_instance(inst_id, status="running",
                              url=d.get("url", inst.get("url", "")))
    monitor._fail_counts.pop(inst_id, None)
    return jsonify(ok=True)


@bp.route("/api/instances/<inst_id>/exec", methods=["POST"])
@require_auth
def instance_exec(inst_id):
    inst = store.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    host = inst.get("hostname")
    if not host:
        return jsonify(ok=False, error="实例无域名"), 404
    d = request.get_json(silent=True) or {}
    cmd = (d.get("cmd") or "").strip()
    if not cmd:
        return jsonify(ok=False, error="命令为空"), 400
    timeout = max(1, min(int(d.get("timeout", 30)), 600))
    payload = json.dumps({"token": config.EXEC_TOKEN, "cmd": cmd,
                          "timeout": timeout}).encode()
    url = f"https://{host}/api/exec"
    try:
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (ghbox-manager)"})
        with urllib.request.urlopen(req, timeout=timeout + 15) as r:
            return jsonify(ok=True, result=json.loads(r.read().decode()))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


# ==================== 代理 API（实时转发到 worker）====================
@bp.route("/api/instances/<inst_id>/logs")
@require_auth
def instance_logs(inst_id):
    """代理查看worker日志（实时）"""
    inst = store.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    host = inst.get("hostname")
    if not host:
        return jsonify(ok=False, error="实例无域名"), 404
    limit = request.args.get("limit", "300")
    keyword = request.args.get("keyword", "")
    url = f"https://{host}/api/logs?limit={limit}"
    if keyword:
        url += f"&keyword={keyword}"
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {config.EXEC_TOKEN}",
            "User-Agent": "Mozilla/5.0 (ghbox-manager)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return jsonify(json.loads(r.read().decode()))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


@bp.route("/api/instances/<inst_id>/processes")
@require_auth
def instance_processes(inst_id):
    """代理查看worker进程列表（实时）"""
    inst = store.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    host = inst.get("hostname")
    if not host:
        return jsonify(ok=False, error="实例无域名"), 404
    url = f"https://{host}/api/processes"
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {config.EXEC_TOKEN}",
            "User-Agent": "Mozilla/5.0 (ghbox-manager)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return jsonify(json.loads(r.read().decode()))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


@bp.route("/api/instances/<inst_id>/resource")
@require_auth
def instance_resource(inst_id):
    """代理查看worker资源状态（实时）"""
    inst = store.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    host = inst.get("hostname")
    if not host:
        return jsonify(ok=False, error="实例无域名"), 404
    url = f"https://{host}/api/resource"
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {config.EXEC_TOKEN}",
            "User-Agent": "Mozilla/5.0 (ghbox-manager)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return jsonify(json.loads(r.read().decode()))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


# ==================== 优雅重启 ====================
@bp.route("/api/instances/<inst_id>/restart", methods=["POST"])
@require_auth
def restart_instance(inst_id):
    """优雅重启实例：通知实例备份→退出→触发新workflow→监控"""
    if not _is_leader():
        return jsonify(ok=False, error="备份节点"), 503
    inst = store.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    if inst.get("status") == "restarting":
        return jsonify(ok=False, error="正在重启中"), 409
    store.update_instance(inst_id, status="restarting")
    threading.Thread(target=_restart_worker, args=(inst_id, inst), daemon=True).start()
    return jsonify(ok=True, msg=f"实例 {inst_id} 正在重启", status="restarting")


@bp.route("/api/instances/<inst_id>/shutdown-complete", methods=["POST"])
def shutdown_complete(inst_id):
    """实例备份完成通知"""
    if not _authed():
        return jsonify(ok=False, error="未授权"), 401
    state.shutdown_notifications[inst_id] = time.time()
    logger.info(f"{inst_id} 备份完成通知")
    return jsonify(ok=True)


def _restart_worker(inst_id, inst):
    """重启流程：通知实例关闭→等待→触发新workflow→监控health"""
    host = inst.get("hostname", "")
    # 1. 通知实例优雅关闭
    shutdown_ok = False
    if host:
        try:
            from core.utils import http_request
            url = f"https://{host}/api/shutdown"
            status, _ = http_request(url, method="POST",
                data=json.dumps({"token": config.EXEC_TOKEN}).encode(),
                headers={"Content-Type": "application/json"},
                timeout=30, retries=1)
            if status == 200:
                shutdown_ok = True
                logger.info(f"{inst_id} 优雅关闭已通知")
            else:
                logger.warning(f"{inst_id} shutdown 返回 {status}")
        except Exception as e:
            logger.warning(f"{inst_id} 通知失败: {e}")

    # 2. 等待实例退出（收到通知 或 60秒超时 或 实例不可达）
    if shutdown_ok:
        for _ in range(60):
            if inst_id in state.shutdown_notifications:
                logger.info(f"{inst_id} 备份完成，准备触发新 worker")
                break
            # 检查实例是否已退出
            try:
                req = urllib.request.Request(f"https://{host}/api/health",
                    headers={"User-Agent": "Mozilla/5.0"})
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                # 实例已不可达
                break
            time.sleep(1)
        state.shutdown_notifications.pop(inst_id, None)

    # 3. 触发新 workflow
    account = next((a for a in accounts.load_accounts()
                    if a["name"] == inst.get("account")), None)
    if not account:
        logger.error(f"{inst_id} 找不到账号")
        store.update_instance(inst_id, status="restart_failed")
        return
    repo = account.get("repo") or config.REPO
    url = f"{ghapi.API_BASE}/repos/{repo}/actions/workflows/{config.WORKER_WORKFLOW}/dispatches"
    ghapi.gh_request("POST", url, token=account.get("token"),
                     data={"ref": "main", "inputs": {"INSTANCE_ID": inst_id}})
    logger.info(f"{inst_id} 已触发新 worker")

    # 4. 监控新实例启动（每10秒，最多5分钟）
    import time as _t
    _t.sleep(30)  # 等新实例启动
    for attempt in range(30):
        try:
            req = urllib.request.Request(f"https://{host}/api/health",
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    store.update_instance(inst_id, status="running")
                    logger.info(f"{inst_id} 重启成功")
                    return
        except Exception:
            pass
        _t.sleep(10)
    # 超时
    store.update_instance(inst_id, status="restart_failed")
    logger.error(f"{inst_id} 重启超时（5分钟未恢复），需手动处理")
