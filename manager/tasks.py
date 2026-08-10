# -*- coding: utf-8 -*-
"""
任务持久化与执行（manager 侧）

- 任务持久化（通过 store.py）
- 后台串行执行器 + 自动重试
- 任务去重 / 超时 / 历史清理
"""
import time
import uuid
import threading

import config
import log
from manager import store

logger = log.setup_logger("tasks")

MAX_RETRIES = 3
RETRY_DELAY = [5, 15, 45]
TASK_TIMEOUT = 900
MAX_HISTORY = 50

_handlers = {}
_lock = threading.Lock()


def register_handler(task_type):
    def decorator(fn):
        _handlers[task_type] = fn
        return fn
    return decorator


def load_tasks():
    return store.load_tasks()


def save_tasks(tasks):
    store.save_tasks(tasks)


def add_task(task_type, params, dedup_key=None):
    with _lock:
        tasks = load_tasks()
        if dedup_key:
            for t in tasks:
                if (t["type"] == task_type
                        and t.get("dedup_key") == dedup_key
                        and t["status"] in ("pending", "running")):
                    logger.info(f"[task] 任务已存在，跳过: {t['id']}")
                    return t
        task = {
            "id": f"t-{int(time.time())}-{uuid.uuid4().hex[:6]}",
            "type": task_type,
            "params": params,
            "dedup_key": dedup_key,
            "status": "pending",
            "retries": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
            "started_at": None,
            "error": "",
        }
        tasks.append(task)
        _trim_history(tasks)
        save_tasks(tasks)
        logger.info(f"[task] 添加任务 {task['id']} ({task_type})")
        return task


def update_task(task_id, **kw):
    with _lock:
        tasks = load_tasks()
        for t in tasks:
            if t["id"] == task_id:
                for k, v in kw.items():
                    t[k] = v
                t["updated_at"] = time.time()
                break
        save_tasks(tasks)


def _trim_history(tasks):
    if len(tasks) > MAX_HISTORY:
        tasks.sort(key=lambda t: t.get("created_at", 0), reverse=True)
        del tasks[MAX_HISTORY:]


def get_pending_tasks():
    tasks = load_tasks()
    return [t for t in tasks if t["status"] in ("pending", "running")]


def _execute(task):
    handler = _handlers.get(task["type"])
    if not handler:
        update_task(task["id"], status="failed", error="无处理器")
        return
    task["started_at"] = time.time()
    update_task(task["id"], status="running", started_at=task["started_at"])
    retries = task.get("retries", 0)
    try:
        handler(task["params"], task)
        update_task(task["id"], status="done", error="")
        logger.info(f"[task] {task['id']} 完成")
    except Exception as e:
        retries += 1
        if retries <= MAX_RETRIES:
            delay = RETRY_DELAY[min(retries - 1, len(RETRY_DELAY) - 1)]
            update_task(task["id"], status="pending", retries=retries, error=str(e))
            logger.warning(f"[task] {task['id']} 失败(第{retries}次): {e}，{delay}s后重试")
            time.sleep(delay)
        else:
            update_task(task["id"], status="failed", retries=retries, error=str(e))
            logger.error(f"[task] {task['id']} 最终失败: {e}")


def _worker_loop():
    while True:
        try:
            tasks = load_tasks()
            pending = [t for t in tasks if t["status"] == "pending"]
            for t in tasks:
                if (t["status"] == "running" and t.get("started_at")
                        and time.time() - t["started_at"] > TASK_TIMEOUT):
                    update_task(t["id"], status="failed", error="任务超时")
                    logger.error(f"[task] {t['id']} 超时")
            if pending:
                _execute(pending[0])
            else:
                time.sleep(2)
        except Exception as e:
            logger.error(f"[task] 执行器异常: {e}")
            time.sleep(5)


def start_worker():
    threading.Thread(target=_worker_loop, daemon=True).start()
    logger.info("[task] 任务执行器已启动")


def recover_pending():
    tasks = load_tasks()
    changed = False
    for t in tasks:
        if t["status"] in ("pending", "running", "failed"):
            if t["status"] == "failed" and t.get("retries", 0) >= MAX_RETRIES:
                continue
            t["status"] = "pending"
            t["started_at"] = None
            changed = True
    if changed:
        save_tasks(tasks)
        logger.info(f"[task] 已恢复未完成任务")
