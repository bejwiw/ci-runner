# -*- coding: utf-8 -*-
"""
Leader 锁

- backend="release"：基于 GitHub Releases leader.json 心跳（manager 用）
- backend="manager"：基于 manager HTTP 心跳（worker 用，不占 GitHub 配额）
"""
import os
import time
import json
import uuid
import threading
import urllib.request

import config
import log
from core import releases

logger = log.setup_logger("lock")

JOB_ID = uuid.uuid4().hex[:8]


class LeaderLock:
    def __init__(self, backend="release", instance_id=None, token=None):
        self.backend = backend
        self.instance_id = instance_id or config.INSTANCE_ID
        self.token = token
        self.job_id = JOB_ID
        self.is_leader = False
        self.fail_count = 0
        self._on_promote = None
        self.mgr_host = config.MANAGER_HOST or "ghvps2.kekeke.cc.cd"

    def _read_heartbeat(self):
        try:
            if self.backend == "release":
                blob = releases.download_asset(config.ASSET_LEADER, token=self.token)
                if not blob:
                    return None
                return json.loads(blob.decode())
            url = (f"https://{self.mgr_host}/api/worker/leader"
                   f"?inst_id={self.instance_id}&job_id={self.job_id}")
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {config.EXEC_TOKEN}",
                "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read().decode())
                if not d.get("ok"):
                    return None
                # 有别的活跃 leader → 返回"别人的心跳"防止 acquire
                if d.get("has_leader") and d.get("leader_job") != self.job_id:
                    age = d.get("leader_age", 0)
                    return {"job_id": d.get("leader_job"),
                            "heartbeat": time.time() - max(0, age)}
                # 自己是 leader → 返回自己的心跳
                if d.get("is_leader"):
                    return {"job_id": self.job_id, "heartbeat": time.time()}
            return None
        except Exception:
            return None

    def _write_heartbeat(self):
        try:
            if self.backend == "release":
                data = json.dumps({"job_id": JOB_ID, "heartbeat": time.time()}).encode()
                releases.upload_asset(config.ASSET_LEADER, data, token=self.token)
                return True
            url = f"https://{self.mgr_host}/api/worker/heartbeat"
            payload = json.dumps({
                "inst_id": self.instance_id, "job_id": self.job_id,
                "version": config.CURRENT_SHA or "unknown",
            }).encode()
            req = urllib.request.Request(url, data=payload, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.EXEC_TOKEN}",
                "User-Agent": "Mozilla/5.0"})
            urllib.request.urlopen(req, timeout=10)
            self.fail_count = 0
            return True
        except Exception:
            self.fail_count += 1
            return False

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
            logger.error(f"[lock] acquire 异常: {e}")
            self.is_leader = True
            return True

    def heartbeat_loop(self):
        while True:
            if not self.is_leader:
                return
            time.sleep(config.HEARTBEAT_INTERVAL)
            try:
                self._write_heartbeat()
            except Exception:
                pass

    def follower_loop(self, on_promote=None):
        self._on_promote = on_promote
        while True:
            if self.is_leader:
                return
            time.sleep(config.HEARTBEAT_INTERVAL)
            try:
                if self.backend == "release":
                    leader = self._read_heartbeat()
                    now = time.time()
                    if not leader or (now - leader.get("heartbeat", 0)) >= config.HEARTBEAT_TIMEOUT:
                        if self.acquire():
                            self._fire_promote()
                            return
                else:
                    # 先检查是否已有别的活跃 leader，有则等它过期
                    d = self._read_heartbeat()
                    if d and d.get("job_id") != JOB_ID:
                        continue
                    # 无 leader 或自己曾是 leader，写心跳抢占
                    ok = self._write_heartbeat()
                    if ok:
                        d = self._read_heartbeat()
                        if d and d.get("job_id") == JOB_ID:
                            self.is_leader = True
                            logger.info(f"[lock] follower 升级为 leader: {self.job_id}")
                            self._fire_promote()
                            return
                    elif self.fail_count >= 5:
                        self.is_leader = True
                        logger.warning("[lock] manager 不可达，降级为 leader")
                        self._fire_promote()
                        return
            except Exception:
                pass

    def _fire_promote(self):
        if self._on_promote:
            try:
                self._on_promote()
            except Exception as e:
                logger.error(f"[lock] 升级回调异常: {e}")
