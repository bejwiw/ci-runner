# -*- coding: utf-8 -*-
"""
Manager 实例/账号/任务 API 路由（Blueprint）
"""
import time
import json
import functools
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
        logger.warning(f"[create] fork 异常: {e}")
    try:
        tunnel_id, tunnel_token = tunnels.create_tunnel(hostname)
    except Exception as e:
        return jsonify(ok=False, error=f"创建隧道失败: {e}"), 500
    mcp_hostname = f"mcp-{hostname}"
    mcp_tunnel_id = ""
    mcp_ttoken = ""
    try:
        mcp_tunnel_id, mcp_ttoken = tunnels.create_mcp_tunnel(mcp_hostname)
    except Exception as e:
        logger.warning(f"[create] MCP 隧道失败: {e}")
    inst_cfg = {
        "inst_id": inst_id, "hostname": hostname,
        "tunnel_token": tunnel_token, "tunnel_id": tunnel_id,
        "mcp_hostname": mcp_hostname, "mcp_tunnel_token": mcp_ttoken,
        "mcp_tunnel_id": mcp_tunnel_id,
        "account": account["name"], "account_repo": account["repo"],
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
    logger.info(f"[create] 实例 {inst_id} 创建中: https://{hostname}")
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
        f"{ghapi.API_BASE}/repos/{repo}/actions/runs?per_page=1",
        token=account["token"])
    return d["workflow_runs"][0]["id"] if status == 200 and d.get("workflow_runs") else None


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
        ghapi.gh_request("POST",
            f"{ghapi.API_BASE}/repos/{account['repo']}/actions/runs/{inst['run_id']}/cancel",
            token=account["token"])
    store.delete_instance_config(inst_id)
    if inst.get("tunnel_id"):
        tunnels.delete_tunnel(inst["tunnel_id"], inst.get("hostname", ""))
    if inst.get("mcp_tunnel_id"):
        tunnels.delete_tunnel(inst["mcp_tunnel_id"], inst.get("mcp_hostname", ""))
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
    _s3 = d.get("s3", {})
    state.worker_heartbeats[inst_id] = {
        "job_id": d.get("job_id", ""),
        "last_seen": time.time(),
        "s3": _s3,
        "procs": d.get("procs", 0),
        "disk_pct": d.get("disk_pct", 0),
        "a_ops": _s3.get("a_ops", 0),
        "b_ops": _s3.get("b_ops", 0),
        "storage_mb": _s3.get("storage_mb", 0),
    }
    inst = store.get_instance(inst_id)
    if not inst:
        cfg = store.load_instance_config(inst_id)
        if cfg:
            inst = store.get_or_create_instance(inst_id, cfg)
            logger.info(f"[report] 实例 {inst_id} 已自愈恢复")
        else:
            return jsonify(ok=False, error=f"实例 {inst_id} 不存在且无配置"), 404
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
