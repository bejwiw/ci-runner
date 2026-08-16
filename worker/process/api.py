# -*- coding: utf-8 -*-
"""
进程管理 API（Flask Blueprint）

- GET    /api/processes                 列出持久化进程
- POST   /api/processes/snapshot        手动触发快照
- POST   /api/processes/<name>/restart  重启进程
- POST   /api/processes/<name>/stop     停止进程
- POST   /api/processes/<name>/start    启动进程
- GET    /api/processes/<name>/log      查看进程日志
"""
import functools

from flask import Blueprint, request, jsonify

import config
import log

bp = Blueprint("process_api", __name__)
_manager = None


def init_process_api(manager):
    global _manager
    _manager = manager


def _token():
    t = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not t:
        t = (request.args.get("token") or "").strip()
    if not t:
        d = request.get_json(silent=True) or {}
        t = (d.get("token") or "").strip()
    return t


def _authed(data=None):
    token = ""
    if isinstance(data, dict):
        token = data.get("token", "")
    if not token:
        token = _token()
    return bool(config.EXEC_TOKEN) and token == config.EXEC_TOKEN


@bp.route("/api/processes", methods=["GET"])
def list_processes():
    if not _authed({}):
        return jsonify(ok=False, error="未授权"), 401
    if not _manager:
        return jsonify(ok=False, error="进程管理器未初始化"), 500
    return jsonify(ok=True, processes=_manager.list_processes())


@bp.route("/api/processes/snapshot", methods=["POST"])
def snapshot():
    data = request.get_json(silent=True) or {}
    if not _authed(data):
        return jsonify(ok=False, error="未授权"), 401
    if not _manager:
        return jsonify(ok=False, error="未初始化"), 500
    saved = _manager.snapshot(reason="manual")
    return jsonify(ok=True, saved=saved, msg=f"快照完成，持久化 {saved} 个进程")


@bp.route("/api/processes/adopt", methods=["POST"])
def adopt():
    """托管项目：检查是否已托管，没有则检查进程/隧道是否在运行，在运行则返回错误让用户手动处理"""
    data = request.get_json(silent=True) or {}
    if not _authed(data):
        return jsonify(ok=False, error="未授权"), 401
    if not _manager:
        return jsonify(ok=False, error="进程管理器未初始化"), 500
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="name 必填"), 400
    # 1. 检查是否已托管（PID 文件 + 进程存活）
    from worker.process import config as pconfig
    from core import utils
    pid = pconfig.read_pid_file(name)
    if pid and utils.is_alive(pid):
        return jsonify(ok=True, msg=f"{name} 已托管 (pid={pid})")
    # 2. 检查进程是否在运行（用户可能手动启动了）
    import subprocess
    try:
        r = subprocess.run(f"pgrep -f '{name}'", shell=True, capture_output=True,
                           text=True, timeout=5, executable="/bin/bash")
        if r.stdout.strip():
            pids = r.stdout.strip().split("\n")
            return jsonify(ok=False, error=f"进程 {name} 正在运行 (pid={','.join(pids)})，请先手动停止再托管"), 409
    except Exception as e:
        import log
        lg = log.setup_logger("proc.api")
        lg.debug(f"adopt pgrep 异常: {e}")
    # 3. 检查配置文件是否存在
    configs = pconfig.scan_configs()
    if name not in configs:
        return jsonify(ok=False, error=f"配置文件不存在: {name} 的 ghvps.json 未找到"), 404
    # 4. 启动进程（自动托管）
    ok = _manager.start(name)
    if ok:
        return jsonify(ok=True, msg=f"{name} 已托管并启动")
    else:
        return jsonify(ok=False, error=f"启动 {name} 失败，请检查日志"), 500


@bp.route("/api/processes/<name>/restart", methods=["POST"])
def restart(name):
    data = request.get_json(silent=True) or {}
    if not _authed(data):
        return jsonify(ok=False, error="未授权"), 401
    ok = _manager.restart(name) if _manager else False
    return jsonify(ok=ok, msg="已重启" if ok else "重启失败"), (200 if ok else 500)


@bp.route("/api/processes/<name>/stop", methods=["POST"])
def stop(name):
    data = request.get_json(silent=True) or {}
    if not _authed(data):
        return jsonify(ok=False, error="未授权"), 401
    ok, msg = _manager.stop(name) if _manager else (False, "未初始化")
    return jsonify(ok=ok, msg=msg), (200 if ok else 500)


@bp.route("/api/processes/<name>/start", methods=["POST"])
def start(name):
    data = request.get_json(silent=True) or {}
    if not _authed(data):
        return jsonify(ok=False, error="未授权"), 401
    ok = _manager.start(name) if _manager else False
    return jsonify(ok=ok, msg="已启动" if ok else "启动失败"), (200 if ok else 500)


@bp.route("/api/processes/<name>/log", methods=["GET"])
def proc_log(name):
    if not _authed({}):
        return jsonify(ok=False, error="未授权"), 401
    if not _manager:
        return jsonify(ok=False, error="未初始化"), 500
    limit = max(10, min(int(request.args.get("limit", 200)), 2000))
    return jsonify(ok=True, name=name, lines=_manager.get_process_log(name, limit=limit))
