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
