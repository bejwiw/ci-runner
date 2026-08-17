# -*- coding: utf-8 -*-
"""
Leader 锁：多个进程间唯一定义"谁负责写"。

存储后端两种：
- storage：跨机器共享心跳文件（S3 主 / Releases 降级），manager 用
- manager：通过 manager HTTP 查询心跳，worker 用（不占 GitHub 配额）

选举规则：心跳 90s(HEARTBEAT_TIMEOUT) 无更新 → 视为 leader 失效，可抢占。
"""
import time
import json
import uuid
import threading

import config
import log
from core import releases
from core.utils import http_request

logger = log.setup_logger("lock")

JOB_ID = uuid.uuid4().hex[:8]
LEADER_S3_KEY = "meta/leader.json"


class LeaderLock:
    def __init__(self, backend="storage", instance_id=None, token=None, s3pool=None):
        self.backend = backend
        self.instance_id = instance_id or config.INSTANCE_ID
        self.token = token
        self.s3pool = s3pool
        self.job_id = JOB_ID
        self.is_leader = False
        self.fail_count = 0
        self._on_promote = None
        self.mgr_host = config.MANAGER_HOST or "ghvps2.kekeke.cc.cd"

    # ==================== 读取 ====================
    def _read_heartbeat(self):
        try:
            if self.backend == "manager":
                return self._read_http()
            return self._read_storage()
        except Exception as e:
            logger.debug(f"read_heartbeat 异常: {e}")
            return None

    def _read_storage(self):
        """读取共享心跳：S3 优先（0 配额），S3 不可用回退 Releases。"""
        result = None
        if self.s3pool and self.s3pool.is_ready():
            blob = self.s3pool.get(LEADER_S3_KEY)
            if blob:
                return json.loads(blob.decode())
            return None  # S3 正常但无文件 = 无 leader，不回退 Releases
        if self.backend != "storage":
            return None
        blob = releases.download_asset(config.ASSET_LEADER, token=self.token)
        return json.loads(blob.decode()) if blob else None

    def _read_http(self):
        url = (f"https://{self.mgr_host}/api/worker/leader"
               f"?inst_id={self.instance_id}&job_id={self.job_id}")
        status, raw = http_request(url, method="GET",
                                   headers={"Authorization": f"Bearer {config.EXEC_TOKEN}"},
                                   timeout=10, retries=2)
        if status != 200 or not raw:
            return None
        d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        if not d.get("ok"):
            return None
        if d.get("has_leader") and d.get("leader_job") != self.job_id:
            age = d.get("leader_age", 0)
            return {"job_id": d.get("leader_job"),
                    "heartbeat": time.time() - max(0, age)}
        if d.get("is_leader"):
            return {"job_id": self.job_id, "heartbeat": time.time()}
        return None

    # ==================== 写入 ====================
    def _write_heartbeat(self):
        try:
            if self.backend == "manager":
                return self._write_http()
            return self._write_storage()
        except Exception as e:
            self.fail_count += 1
            logger.debug(f"write_heartbeat 异常: {e}")
            return False

    def _write_storage(self):
        data = json.dumps({"job_id": self.job_id, "heartbeat": time.time()}).encode()
        if self.s3pool and self.s3pool.is_ready():
            ok = self.s3pool.put(LEADER_S3_KEY, data)
            self.fail_count = 0 if ok else self.fail_count + 1
            return ok
        if self.backend != "storage":
            return False
        releases.upload_asset(config.ASSET_LEADER, data, token=self.token)
        self.fail_count = 0
        return True

    def _write_http(self):
        url = f"https://{self.mgr_host}/api/worker/heartbeat"
        payload = json.dumps({
            "inst_id": self.instance_id, "job_id": self.job_id,
            "version": config.CURRENT_SHA or "unknown",
        }).encode()
        status, _ = http_request(url, method="POST", data=payload,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {config.EXEC_TOKEN}"},
                                 timeout=10, retries=2)
        if status == 200:
            self.fail_count = 0
            return True
        self.fail_count += 1
        logger.debug(f"heartbeat 写入失败: status={status}")
        return False

    # ==================== 选举 ====================
    def acquire(self):
        try:
            leader = self._read_heartbeat()
            now = time.time()
            if (leader and leader.get("job_id") != JOB_ID
                    and (now - leader.get("heartbeat", 0)) < config.HEARTBEAT_TIMEOUT):
                self.is_leader = False
                return False
            self.is_leader = True
            self._write_heartbeat()
            return True
        except Exception as e:
            logger.error(f"acquire 异常: {e}")
            self.is_leader = True
            return True

    def heartbeat_loop(self):
        while self.is_leader:
            time.sleep(config.HEARTBEAT_INTERVAL)
            try:
                self._write_heartbeat()
            except Exception as e:
                logger.debug(f"heartbeat_loop 写入异常: {e}")

    def follower_loop(self, on_promote=None):
        self._on_promote = on_promote
        while not self.is_leader:
            time.sleep(config.HEARTBEAT_INTERVAL)
            try:
                if self._try_promote():
                    return
            except Exception as e:
                logger.debug(f"follower_loop 异常: {e}")

    def _try_promote(self):
        if self.backend == "manager":
            return self._try_promote_http()
        leader = self._read_heartbeat()
        now = time.time()
        if leader and (now - leader.get("heartbeat", 0)) < config.HEARTBEAT_TIMEOUT:
            return False
        if self.acquire():
            self._fire_promote()
            return True
        return False

    def _try_promote_http(self):
        d = self._read_heartbeat()
        if d and d.get("job_id") != JOB_ID:
            return False
        if not self._write_heartbeat():
            if self.fail_count >= 5:
                self.is_leader = True
                logger.warning("manager 不可达，降级为 leader")
                self._fire_promote()
                return True
            return False
        d = self._read_heartbeat()
        if d and d.get("job_id") == JOB_ID:
            self.is_leader = True
            logger.info(f"follower 升级为 leader: {self.job_id}")
            self._fire_promote()
            return True
        return False

    def _fire_promote(self):
        if self._on_promote:
            try:
                self._on_promote()
            except Exception as e:
                logger.error(f"升级回调异常: {e}")