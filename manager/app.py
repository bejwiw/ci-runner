# -*- coding: utf-8 -*-
"""
Manager Flask App + 认证 + 基础路由 + 启动入口
"""
import os
import time
import json
import functools
import threading
import subprocess

from flask import Flask, request, jsonify

import config
import log
from core import lock as core_lock
from core import status as core_status
from core import ghapi
from core.s3 import S3Pool
from manager import state, store, accounts, tasks, monitor
from manager.api_instances import bp as api_bp
from manager.background import start_background

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()
logger = log.setup_logger("manager")

log.clear_logs()


# ==================== 500 错误处理 ====================
@app.errorhandler(500)
def _handle_500(e):
    import traceback
    return jsonify(ok=False, error=str(e),
                   traceback=traceback.format_exc()[:2000]), 500


@app.errorhandler(Exception)
def _handle_error(e):
    import traceback
    from werkzeug.exceptions import HTTPException
    code = e.code if isinstance(e, HTTPException) else 500
    if code == 404:
        return jsonify(ok=False, error="Not Found"), 404
    return jsonify(ok=False, error=str(e),
                   traceback=traceback.format_exc()[:2000]), code


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


# ==================== 基础路由 ====================
@app.route("/api/health")
def health():
    return jsonify(ok=True, role="manager", job=core_lock.JOB_ID,
                   elapsed=core_status.elapsed(),
                   leader=state.leader.is_leader if state.leader else False)


@app.route("/api/status")
@require_auth
def status():
    accts = accounts.list_accounts()
    insts = store.list_instances()
    now = time.time()
    healthy = sum(1 for hb in state.worker_heartbeats.values()
                  if now - hb.get("last_seen", 0) < 180)
    return jsonify(ok=True, role="manager", job=core_lock.JOB_ID,
                   elapsed=core_status.elapsed(),
                   leader=state.leader.is_leader if state.leader else False,
                   accounts=accts, instances=insts,
                   worker_health={"online": healthy, "total": len(insts)})


@app.route("/api/overview")
@require_auth
def overview():
    accts = accounts.list_accounts()
    insts = store.list_instances()
    now = time.time()
    healthy = sum(1 for hb in state.worker_heartbeats.values()
                  if now - hb.get("last_seen", 0) < 180)
    quota = {}
    for acc in accounts.load_accounts():
        h, detail = ghapi.estimate_account_quota(acc)
        quota[acc["name"]] = {"health": round(h, 2), "detail": detail}
    all_tasks = tasks.load_tasks()
    task_stats = {}
    for t in all_tasks:
        s = t.get("status", "unknown")
        task_stats[s] = task_stats.get(s, 0) + 1
    s3_info = state.s3pool.get_status() if state.s3pool else {"ready": False}
    return jsonify(ok=True, role="manager", job=core_lock.JOB_ID,
                   elapsed=core_status.elapsed(),
                   leader=state.leader.is_leader if state.leader else False,
                   accounts=accts, instances=insts,
                   worker_health={"online": healthy, "total": len(insts)},
                   quota=quota, tasks=task_stats, s3=s3_info)


@app.route("/api/logs")
@require_auth
def logs():
    limit = max(10, min(int(request.args.get("limit", 300)), 2000))
    return jsonify(ok=True, logs=log.get_logs(
        limit=limit, level=request.args.get("level"),
        module=request.args.get("module"), keyword=request.args.get("keyword")),
        stats=log.get_stats())


# ==================== S3 存储管理 ====================
@app.route("/api/s3/status")
@require_auth
def s3_status():
    if not state.s3pool:
        return jsonify(ok=False, error="S3 未初始化"), 503
    mgr = state.s3pool.get_status()
    w_a = sum(hb.get("a_count_total", 0) for hb in state.worker_heartbeats.values())
    w_b = sum(hb.get("b_count_total", 0) for hb in state.worker_heartbeats.values())
    w_st = sum(hb.get("storage_mb", 0) for hb in state.worker_heartbeats.values())
    w_cnt = len(state.worker_heartbeats)
    return jsonify(ok=True,
        ready=mgr.get("ready", False),
        total_accounts=mgr.get("total_accounts", 0),
        active_accounts=mgr.get("active_accounts", 0),
        degraded_accounts=mgr.get("degraded_accounts", 0),
        unavailable_accounts=mgr.get("unavailable_accounts", 0),
        total_a_ops=mgr.get("total_a_ops", 0) + w_a,
        total_b_ops=mgr.get("total_b_ops", 0) + w_b,
        total_storage_mb=round(mgr.get("total_storage_mb", 0) + w_st, 1),
        hash_ring_size=mgr.get("hash_ring_size", 0),
        manager_a_ops=mgr.get("total_a_ops", 0),
        manager_b_ops=mgr.get("total_b_ops", 0),
        manager_storage_mb=mgr.get("total_storage_mb", 0),
        worker_count=w_cnt,
        worker_a_ops=w_a,
        worker_b_ops=w_b,
        worker_storage_mb=round(w_st, 1),
    )


@app.route("/api/s3/health")
@require_auth
def s3_health():
    if state.s3pool:
        return jsonify(ok=True, **state.s3pool.get_health())
    return jsonify(ok=False, error="S3 未初始化"), 503


@app.route("/api/s3/accounts")
@require_auth
def s3_accounts():
    if state.s3pool:
        return jsonify(ok=True, **state.s3pool.get_account_status())
    return jsonify(ok=False, error="S3 未初始化"), 503


@app.route("/api/s3/workers")
@require_auth
def s3_workers():
    """查看每个worker的S3状态（含累积统计）"""
    result = []
    for inst_id, hb in state.worker_heartbeats.items():
        s3 = hb.get("s3", {})
        wstats = store.get_worker_stats(inst_id)
        result.append({
            "instance": inst_id,
            "last_seen": hb.get("last_seen", 0),
            "active": s3.get("active", 0),
            "degraded": s3.get("degraded", 0),
            "unavailable": s3.get("unavailable", 0),
            "a_ops_total": wstats.get("a_count_total", 0),
            "b_ops_total": wstats.get("b_count_total", 0),
            "storage_mb": wstats.get("storage_mb", 0),
            "procs": hb.get("procs", 0),
            "disk_pct": hb.get("disk_pct", 0),
            "last_backup": wstats.get("last_backup", 0),
        })
    return jsonify(ok=True, workers=result, total=len(result))


# ==================== Worker 心跳 ====================
@app.route("/api/worker/heartbeat", methods=["POST"])
def worker_heartbeat():
    """worker leader 心跳：记录 job_id + heartbeat 时间戳"""
    if not _authed():
        return jsonify(ok=False, error="未授权"), 401
    d = request.get_json(silent=True) or {}
    inst_id = d.get("inst_id", "")
    if inst_id:
        _s3 = d.get("s3", {})
        _existing = state.worker_heartbeats.get(inst_id, {})
        _existing.update({
            "job_id": d.get("job_id", ""),
            "heartbeat": time.time(),
            "last_seen": time.time(),
            "version": d.get("version", "unknown"),
            "s3": _s3,
            "procs": d.get("procs", 0),
            "disk_pct": d.get("disk_pct", 0),
            "a_ops": _s3.get("a_ops", 0),
            "b_ops": _s3.get("b_ops", 0),
            "storage_mb": _s3.get("storage_mb", 0),
        })
        state.worker_heartbeats[inst_id] = _existing
    return jsonify(ok=True)


@app.route("/api/worker/leader")
def worker_leader():
    """返回完整 leader 状态，供 worker 判断是否有别的活跃 leader"""
    if not _authed():
        return jsonify(ok=False, error="未授权"), 401
    inst_id = request.args.get("inst_id", "")
    job_id = request.args.get("job_id", "")
    now = time.time()
    hb = state.worker_heartbeats.get(inst_id, {})
    leader_job = hb.get("job_id", "")
    heartbeat_ts = hb.get("heartbeat", 0)
    has_leader = bool(leader_job and heartbeat_ts
                      and (now - heartbeat_ts) < config.HEARTBEAT_TIMEOUT)
    is_ldr = has_leader and leader_job == job_id
    leader_age = int(now - heartbeat_ts) if heartbeat_ts else -1
    return jsonify(ok=True, is_leader=is_ldr, has_leader=has_leader,
                   leader_job=leader_job if has_leader else "",
                   leader_age=leader_age, current=hb)


# ==================== 任务处理器 ====================
@tasks.register_handler("add_account")
def _task_add_account(params, task):
    logger.info(f"[task] 处理账号添加: {params.get('name')}")
    res = accounts.auto_provision_account(
        params.get("name"), params.get("token"),
        repo=params.get("repo"), max_conc=params.get("max_concurrency"))
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "未知错误"))
    logger.info(f"[task] 账号 {params.get('name')} 配置完成")


# ==================== Worker统计API ====================
@app.route("/api/instances/<inst_id>/backup-history")
@require_auth
def backup_history(inst_id):
    stats = store.get_worker_stats(inst_id)
    return jsonify(ok=True, history=stats.get("backup_history", []))


@app.route("/api/instances/<inst_id>/restore-history")
@require_auth
def restore_history(inst_id):
    stats = store.get_worker_stats(inst_id)
    return jsonify(ok=True, history=stats.get("restore_history", []))


@app.route("/api/instances/<inst_id>/timeline")
@require_auth
def worker_timeline(inst_id):
    stats = store.get_worker_stats(inst_id)
    return jsonify(ok=True, timeline=stats.get("timeline", []))


@app.route("/api/instances/<inst_id>/stats")
@require_auth
def worker_stats_api(inst_id):
    stats = store.get_worker_stats(inst_id)
    hb = state.worker_heartbeats.get(inst_id, {})
    return jsonify(ok=True, **stats,
        last_seen=hb.get("last_seen", 0),
        procs=hb.get("procs", 0),
        disk_pct=hb.get("disk_pct", 0))


@app.route("/api/accounts/banned")
@require_auth
def banned_accounts():
    result = []
    for acc in accounts.load_accounts():
        name = acc.get("name", "")
        if acc.get("status") == "banned" or acc.get("banned"):
            result.append({
                "name": name,
                "repo": acc.get("repo", ""),
                "banned_at": acc.get("banned_at", ""),
                "reason": acc.get("banned_reason", "unknown"),
            })
    return jsonify(ok=True, banned=result, total=len(result))


# ==================== 启动入口 ====================
def run():
    state.leader = core_lock.LeaderLock(backend="release")
    state.leader.acquire()
    if state.leader.is_leader:
        threading.Thread(target=state.leader.heartbeat_loop, daemon=True).start()
        monitor.start_monitors()
        tasks.recover_pending()
        tasks.start_worker()
        start_background()
        logger.info("[boot] Leader 模式，所有服务已启动")
    else:
        def _on_promote():
            monitor.start_monitors()
            tasks.recover_pending()
            tasks.start_worker()
            start_background()
            logger.info("[boot] Follower 升级为 Leader")
        threading.Thread(target=state.leader.follower_loop,
                         args=(_on_promote,), daemon=True).start()

    bootstrap = os.environ.get("S3_BOOTSTRAP", "")
    if bootstrap:
        state.s3pool = S3Pool(bootstrap, config.S3_ENDPOINT, config.S3_REGION)
        if state.s3pool.init():
            store.set_s3pool(state.s3pool)
            logger.info("[boot] S3 池初始化成功")
        else:
            logger.error("[boot] S3 池初始化失败，降级 Releases")
    else:
        logger.warning("[boot] 无 S3_BOOTSTRAP，仅用 Releases")

    threading.Thread(target=_pre_wake, daemon=True).start()
    threading.Thread(target=_auto_update, daemon=True).start()

    log.request_logger(app)
    app.register_blueprint(api_bp)
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", config.PORT, app, threaded=True, use_reloader=False)


def _pre_wake():
    import time as _t
    done = False
    while True:
        if core_status.elapsed() >= config.PRE_WAKE_SECONDS and not done:
            done = True
            try:
                url = f"{ghapi.API_BASE}/repos/{config.REPO}/actions/workflows/{config.MANAGER_WORKFLOW}/dispatches"
                ghapi.gh_request("POST", url, data={"ref": "main"})
                logger.info(f"[prewake] 已预触发 ({core_status.elapsed()}s)")
            except Exception as e:
                logger.error(f"[prewake] 失败: {e}")
            break
        _t.sleep(60)


def _auto_update():
    import time as _t
    sha = config.CURRENT_SHA
    if not sha:
        return
    while True:
        _t.sleep(600)
        try:
            url = f"{ghapi.API_BASE}/repos/{config.MAIN_REPO}/commits/main"
            _, d = ghapi.gh_request("GET", url)
            latest = d.get("sha", "")
            if latest and latest != sha:
                logger.info(f"[update] 新版本 {latest[:10]}，重启")
                url2 = f"{ghapi.API_BASE}/repos/{config.REPO}/actions/workflows/{config.MANAGER_WORKFLOW}/dispatches"
                ghapi.gh_request("POST", url2, data={"ref": "main"})
                _t.sleep(60)
                os._exit(0)
        except Exception as e:
            logger.error(f"[update] 检查失败: {e}")
