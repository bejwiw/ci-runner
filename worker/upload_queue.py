# -*- coding: utf-8 -*-
"""
Releases 异步上传队列（v2）

设计：备份主流程只同步 S3（主存储），Releases 上传转异步队列，
不阻塞备份、不读内存（path 模式按需读）、失败重试、死信落盘、
优雅停机 flush + 待传清单落盘（重启续传）。

配额保护：剩余 <200 时暂停队列，避免 403 大爆发。
"""
import os
import json
import time
import queue
import shutil
import threading

import config
import log
from core import releases

logger = log.setup_logger("upload_queue")

QUEUE_DIR = os.path.join(config.FILES_DIR, ".backup_queue")


def pending_file():
    return os.path.join(QUEUE_DIR, "pending.json")


def dlq_file():
    return os.path.join(QUEUE_DIR, "dlq.json")
MAX_QUEUE = 64
MAX_WORKERS = 2
QUOTA_PAUSE_THRESHOLD = 200
FLUSH_TIMEOUT = 60


class ReleasesUploadQueue:
    def __init__(self, max_workers=MAX_WORKERS, maxsize=MAX_QUEUE):
        self.max_workers = max_workers
        self.q = queue.Queue(maxsize=maxsize)
        self._workers = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._stats = {
            "enqueued": 0, "ok": 0, "failed": 0, "skipped": 0,
            "dlq": 0, "processing": 0, "pending": 0,
            "last_error": "", "paused": False,
        }
        self._started = False

    # ==================== 生命周期 ====================
    def start(self):
        if self._started:
            return
        self._started = True
        os.makedirs(QUEUE_DIR, exist_ok=True)
        self._restore_pending()
        for i in range(self.max_workers):
            t = threading.Thread(target=self._worker_loop, name=f"rel-upload-{i}",
                                 daemon=True)
            t.start()
            self._workers.append(t)
        logger.info(f"Releases 上传队列已启动 ({self.max_workers} workers)")

    def stop(self, flush=True, timeout=FLUSH_TIMEOUT):
        """优雅停止：先 flush 队列，再停 workers。"""
        self._stop.set()
        if flush:
            self.flush(timeout=timeout)
        self._started = False
        logger.info("Releases 上传队列已停止")

    # ==================== 入队 ====================
    def enqueue(self, kind, base, path=None, data=None, inst_id=None,
                repo=None, token=None):
        """入队一个上传任务。

        kind: "db" | "files"
        base: 资产基名（如 inst-inst3.files）
        path: 文件路径（优先，避免读内存）
        data: bytes（小对象用）
        """
        if self._stop.is_set():
            logger.warning(f"队列已停止，拒绝新任务 {base}")
            return False
        # 配额保护：剩余过低时暂停（不阻塞，直接拒绝并记录）
        if self._quota_low():
            self._stats["skipped"] += 1
            logger.warning(f"配额过低，跳过 Releases 上传 {base}")
            return False
        task = {
            "kind": kind, "base": base, "path": path, "inst_id": inst_id,
            "repo": repo, "token": token,
            "ts": time.time(), "attempts": 0,
        }
        if data is not None:
            task["data_b64"] = data  # bytes（正字节，不编码，内存中）
        try:
            self.q.put_nowait(task)
            with self._lock:
                self._stats["enqueued"] += 1
                self._stats["pending"] = self.q.qsize()
            return True
        except queue.Full:
            with self._lock:
                self._stats["skipped"] += 1
                self._stats["last_error"] = "队列满，丢弃（Releases 可落后）"
            logger.warning(f"队列满({self.q.qsize()})，丢弃任务 {base}")
            return False

    # ==================== worker ====================
    def _worker_loop(self):
        while True:
            try:
                task = self.q.get(timeout=2)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
            # 等待配额保护解除
            if self._stop.is_set() and task is None:
                break
            try:
                self._process(task)
            finally:
                self.q.task_done()
            if self._stop.is_set() and self.q.empty():
                break

    def _process(self, task):
        with self._lock:
            self._stats["processing"] += 1
        t0 = time.time()
        try:
            result = self._upload_with_retry(task)
            if result.get("ok"):
                with self._lock:
                    self._stats["ok"] += 1
                logger.info(f"[队列] {task['base']} 上传成功 "
                            f"({result.get('size', 0)}B, {time.time()-t0:.1f}s)")
            else:
                self._to_dlq(task, result)
        except Exception as e:
            self._to_dlq(task, {"error": str(e)})
        finally:
            # 清理临时文件（文件任务）
            path = task.get("path")
            if path and path.startswith(QUEUE_DIR) and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
            with self._lock:
                self._stats["processing"] -= 1
                self._stats["pending"] = self.q.qsize()

    def _upload_with_retry(self, task):
        """上传任务（重试在 releases 内已做，这里做最终状态汇总）"""
        base = task["base"]
        repo = task.get("repo") or config.REPO
        token = task.get("token") or config.GH_TOKEN
        kind = task.get("kind")
        if task.get("path") and os.path.exists(task["path"]):
            # 大文件：直接 path → 分片 or 单传
            size = os.path.getsize(task["path"])
            if size > releases.SINGLE_UPLOAD_LIMIT:
                return releases.upload_chunked_v2(base, task["path"],
                                                  token=token, repo=repo)
            return releases.upload_asset_v2(base, task["path"],
                                            token=token, repo=repo)
        if task.get("data_b64") is not None:
            return releases.upload_asset_v2(base, task["data_b64"],
                                            token=token, repo=repo)
        raise ValueError(f"任务无数据: {task}")

    def _to_dlq(self, task, result):
        """死信：落盘 dlq.json + 统计。"""
        with self._lock:
            self._stats["failed"] += 1
            self._stats["dlq"] += 1
        entry = dict(task)
        entry.pop("data_b64", None)  # 不落盘大 bytes
        entry["error"] = result.get("error") or f"status={result.get('status')}"
        entry["failed_at"] = time.time()
        try:
            os.makedirs(QUEUE_DIR, exist_ok=True)
            with self._lock:
                dlq = []
                if os.path.exists(dlq_file()):
                    try:
                        with open(dlq_file()) as f:
                            dlq = json.load(f)
                    except Exception:
                        dlq = []
                dlq.append(entry)
                dlq = dlq[-50:]  # 只留最近 50 条
                with open(dlq_file(), "w") as f:
                    json.dump(dlq, f, indent=2)
        except Exception as e:
            logger.error(f"写死信失败: {e}")
        logger.error(f"[队列] {task['base']} 上传失败: {entry['error']}")

    # ==================== 待传清单 ====================
    def _restore_pending(self):
        """启动时恢复待传文件（上次停机前未传完的）"""
        if not os.path.exists(pending_file()):
            return
        try:
            with open(pending_file()) as f:
                tasks = json.load(f)
            for t in tasks:
                if os.path.exists(t.get("path", "")):
                    self.q.put_nowait(t)
            logger.info(f"恢复 {len(tasks)} 个待传任务")
        except Exception as e:
            logger.warning(f"恢复待传失败: {e}")
        try:
            os.remove(pending_file())
        except Exception:
            pass

    def _save_pending(self):
        """把队列中未处理的任务落盘（优雅停机时）"""
        items = []
        while True:
            try:
                items.append(self.q.get_nowait())
            except queue.Empty:
                break
        if not items:
            return
        try:
            os.makedirs(QUEUE_DIR, exist_ok=True)
            clean = []
            for t in items:
                c = dict(t)
                c.pop("data_b64", None)
                clean.append(c)
            with open(pending_file(), "w") as f:
                json.dump(clean, f, indent=2)
            logger.info(f"待传清单落盘 {len(clean)} 个任务")
        except Exception as e:
            logger.error(f"待传清单落盘失败: {e}")

    # ==================== flush ====================
    def flush(self, timeout=FLUSH_TIMEOUT):
        """阻塞等待队列清空（最多 timeout 秒）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.q.empty() and self._stats["processing"] == 0:
                return True
            time.sleep(1)
        logger.warning("flush 超时，剩余任务落盘待传")
        self._save_pending()
        return False

    # ==================== 配额保护 ====================
    def _quota_low(self):
        try:
            from core import ghapi
            remaining, _, _ = ghapi.check_rate_limit(force=False)
            if remaining < QUOTA_PAUSE_THRESHOLD:
                with self._lock:
                    self._stats["paused"] = True
                return True
            with self._lock:
                self._stats["paused"] = False
        except Exception:
            pass
        return False

    # ==================== 状态 ====================
    def status(self):
        with self._lock:
            s = dict(self._stats)
        s["pending"] = self.q.qsize()
        s["running"] = self._started and not self._stop.is_set()
        return s


# ==================== 全局单例 ====================
_queue_instance = None
_queue_lock = threading.Lock()


def get_queue():
    global _queue_instance
    with _queue_lock:
        if _queue_instance is None:
            _queue_instance = ReleasesUploadQueue()
        return _queue_instance


def start_queue():
    q = get_queue()
    q.start()
    return q


def stop_queue(flush=True):
    if _queue_instance:
        _queue_instance.stop(flush=flush)


def queue_status():
    if _queue_instance:
        return _queue_instance.status()
    return {"running": False, "enqueued": 0, "ok": 0, "failed": 0, "pending": 0}