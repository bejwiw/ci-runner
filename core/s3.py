# -*- coding: utf-8 -*-
"""
S3 多账号存储池（Tigris）

架构：
  账号0 (bootstrap桶)  → 存元数据：路由表、计数器、实例清单、配置、任务队列
  账号1~N (数据桶)     → 哈希分散存实例数据：db、files、processes

防死循环：所有重试最多3次，fallback最多3个账号，遍历查找最多5个账号。
防刷额度：路由表和计数器在内存中维护，最少60秒持久化一次到账号0。
"""
import os
import json
import time
import hashlib
import threading

import log

logger = log.setup_logger("s3")

# 常量
A_LIMIT = 9000
B_LIMIT = 90000
STORAGE_LIMIT = 4_500_000_000
STATE_KEY = "meta/state.json"
ACCOUNTS_KEY = "meta/s3-accounts.txt"
MAX_FALLBACK = 3
MAX_SCAN = 5
MAX_RETRIES = 3


def _parse_accounts(text):
    """解析 AWS profiles 格式文本 → 账号列表"""
    accounts = []
    current = {}
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("[profile"):
            if current and "aws_access_key_id" in current:
                accounts.append(current)
            current = {}
        elif "=" in line and not line.startswith("#"):
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if key == "aws_access_key_id":
                current["access_key"] = val
            elif key == "aws_secret_access_key":
                current["secret_key"] = val
        elif line.startswith("# bucket:"):
            current["bucket"] = line.split(":", 1)[1].strip()
    if current and "aws_access_key_id" in current:
        accounts.append(current)
    return accounts


class S3Pool:
    """S3 多账号存储池"""

    def __init__(self, bootstrap_creds, endpoint, region):
        parts = bootstrap_creds.split("|")
        self._bootstrap = {
            "access_key": parts[0],
            "secret_key": parts[1],
            "bucket": parts[2] if len(parts) > 2 else "kkk001",
        }
        self.endpoint = endpoint
        self.region = region
        self._accounts = []
        self._routing = {}
        self._counters = {}
        self._lock = threading.RLock()
        self._clients = {}
        self._last_state_save = 0
        self._initialized = False

    def init(self):
        try:
            raw = self._get_client(0).get(ACCOUNTS_KEY, prefix="")
            if raw:
                self._accounts = _parse_accounts(raw.decode("utf-8"))
                logger.info(f"[s3] 加载了 {len(self._accounts)} 个数据账号")
            else:
                logger.error("[s3] bootstrap 桶中无账号列表")
                return False
        except Exception as e:
            logger.error(f"[s3] 下载账号列表失败: {e}")
            return False
        now = time.gmtime()
        current_month = now.tm_year * 12 + now.tm_mon
        for i in range(len(self._accounts)):
            self._counters[i] = {
                "a_count": 0, "b_count": 0, "used_bytes": 0,
                "status": "active", "month": current_month,
            }
        self._load_state()
        self._initialized = True
        return True

    def is_ready(self):
        return self._initialized

    def _get_client(self, idx):
        if idx in self._clients:
            return self._clients[idx]
        if idx == 0:
            acct = self._bootstrap
        else:
            acct = self._accounts[idx - 1]
        client = _S3Client(
            access_key=acct["access_key"],
            secret_key=acct["secret_key"],
            bucket=acct["bucket"],
            endpoint=self.endpoint,
            region=self.region,
        )
        self._clients[idx] = client
        return client

    def _hash_idx(self, key):
        h = int(hashlib.md5(key.encode()).hexdigest(), 16)
        return (h % len(self._accounts)) + 1

    def _is_writable(self, idx):
        c = self._counters.get(idx, {})
        if c.get("status") != "active":
            return False
        if c.get("a_count", 0) >= A_LIMIT:
            return False
        if c.get("used_bytes", 0) >= STORAGE_LIMIT:
            return False
        return True

    def _select_write_account(self, key, exclude_list=None):
        """选写入账号，exclude_list 是要排除的账号索引列表"""
        exclude_list = exclude_list or []
        with self._lock:
            route = self._routing.get(key)
            if route and route["account"] not in exclude_list:
                idx = route["account"]
                if self._is_writable(idx):
                    return idx
            idx = self._hash_idx(key)
            if idx not in exclude_list and self._is_writable(idx):
                return idx
            candidates = [i for i in range(1, len(self._accounts) + 1)
                          if i not in exclude_list and self._is_writable(i)]
            if candidates:
                return min(candidates, key=lambda i: self._counters[i]["a_count"])
        return None

    def put(self, key, data):
        if not self._initialized:
            return False
        account_idx = self._select_write_account(key)
        if account_idx is None:
            logger.error(f"[s3] 无可用账号写入 {key}")
            return False
        for attempt in range(MAX_RETRIES):
            try:
                if self._get_client(account_idx).put(key, data):
                    with self._lock:
                        self._counters[account_idx]["a_count"] += 1
                        self._counters[account_idx]["used_bytes"] += len(data)
                        self._routing[key] = {
                            "account": account_idx,
                            "size": len(data),
                            "updated": time.time(),
                        }
                    return True
            except Exception as e:
                logger.warning(f"[s3] 写入 {key} 到账号{account_idx} 失败(第{attempt+1}次): {e}")
        return self._put_fallback(key, data, exclude_list=[account_idx])

    def _put_fallback(self, key, data, exclude_list):
        """fallback 写入（最多尝试 MAX_FALLBACK 个其他账号）"""
        for _ in range(MAX_FALLBACK):
            account_idx = self._select_write_account(key, exclude_list=exclude_list)
            if account_idx is None:
                break
            exclude_list.append(account_idx)
            try:
                if self._get_client(account_idx).put(key, data):
                    with self._lock:
                        self._counters[account_idx]["a_count"] += 1
                        self._counters[account_idx]["used_bytes"] += len(data)
                        self._routing[key] = {
                            "account": account_idx,
                            "size": len(data),
                            "updated": time.time(),
                        }
                    logger.info(f"[s3] fallback 写入 {key} 到账号{account_idx}")
                    return True
            except Exception as e:
                logger.warning(f"[s3] fallback 写入 {key} 到账号{account_idx} 失败: {e}")
        logger.error(f"[s3] {key} 所有 fallback 都失败")
        return False

    def get(self, key):
        if not self._initialized:
            return None
        with self._lock:
            route = self._routing.get(key)
        if route:
            idx = route["account"]
            try:
                data = self._get_client(idx).get(key)
                if data is not None:
                    with self._lock:
                        self._counters[idx]["b_count"] += 1
                    return data
            except Exception as e:
                logger.warning(f"[s3] 从账号{idx}读取 {key} 失败: {e}")
        idx = self._hash_idx(key)
        try:
            data = self._get_client(idx).get(key)
            if data is not None:
                with self._lock:
                    self._counters[idx]["b_count"] += 1
                    self._routing[key] = {
                        "account": idx, "size": len(data), "updated": time.time(),
                    }
                return data
        except Exception as e:
            logger.warning(f"[s3] 从账号{idx}(哈希)读取 {key} 失败: {e}")
        for offset in range(1, MAX_SCAN + 1):
            alt_idx = ((idx + offset - 1) % len(self._accounts)) + 1
            if alt_idx == idx:
                continue
            try:
                data = self._get_client(alt_idx).get(key)
                if data is not None:
                    with self._lock:
                        self._counters[alt_idx]["b_count"] += 1
                        self._routing[key] = {
                            "account": alt_idx, "size": len(data), "updated": time.time(),
                        }
                    logger.info(f"[s3] 从账号{alt_idx}遍历找到 {key}")
                    return data
            except Exception:
                continue
        return None

    def delete(self, key):
        if not self._initialized:
            return False
        with self._lock:
            route = self._routing.get(key)
        if not route:
            return True
        idx = route["account"]
        try:
            self._get_client(idx).delete(key)
            with self._lock:
                self._counters[idx]["a_count"] += 1
                self._routing.pop(key, None)
            return True
        except Exception as e:
            logger.warning(f"[s3] 删除 {key} 失败: {e}")
            return False

    def put_meta(self, key, data):
        try:
            return self._get_client(0).put(key, data, prefix="")
        except Exception as e:
            logger.error(f"[s3] 写入元数据 {key} 失败: {e}")
            return False

    def get_meta(self, key):
        try:
            return self._get_client(0).get(key, prefix="")
        except Exception as e:
            logger.warning(f"[s3] 读取元数据 {key} 失败: {e}")
            return None

    def put_meta_json(self, key, obj):
        return self.put_meta(key, json.dumps(obj, ensure_ascii=False).encode())

    def get_meta_json(self, key, default=None):
        raw = self.get_meta(key)
        if raw is None:
            return default
        try:
            return json.loads(raw.decode())
        except Exception:
            return default

    def save_state(self):
        if not self._initialized:
            return
        now = time.time()
        if now - self._last_state_save < 60:
            return
        self._last_state_save = now
        self._check_monthly_reset()
        with self._lock:
            state = {
                "routing": self._routing,
                "counters": {str(k): v for k, v in self._counters.items()},
                "updated": now,
            }
        try:
            self.put_meta_json(STATE_KEY, state)
            logger.info(f"[s3] 状态已持久化: {len(self._routing)} 路由, "
                        f"{len(self._counters)} 账号")
        except Exception as e:
            logger.warning(f"[s3] 状态持久化失败: {e}")

    def _load_state(self):
        state = self.get_meta_json(STATE_KEY)
        if not state:
            logger.info("[s3] 无持久化状态，从零开始")
            return
        with self._lock:
            self._routing = state.get("routing", {})
            saved_counters = state.get("counters", {})
            for idx_str, c in saved_counters.items():
                idx = int(idx_str)
                if idx < len(self._accounts):
                    existing = self._counters.get(idx, {})
                    existing["a_count"] = c.get("a_count", 0)
                    existing["b_count"] = c.get("b_count", 0)
                    existing["used_bytes"] = c.get("used_bytes", 0)
                    existing["status"] = c.get("status", "active")
                    existing["month"] = c.get("month", 0)
                    self._counters[idx] = existing
        logger.info(f"[s3] 加载了 {len(self._routing)} 路由, {len(self._counters)} 账号状态")

    def _check_monthly_reset(self):
        now = time.gmtime()
        current_month = now.tm_year * 12 + now.tm_mon
        with self._lock:
            for idx, c in self._counters.items():
                if c["month"] != current_month:
                    c["a_count"] = 0
                    c["b_count"] = 0
                    c["status"] = "active"
                    c["month"] = current_month
                    logger.info(f"[s3] 账号{idx} 月度重置")

    def get_status(self):
        if not self._initialized:
            return {"ready": False}
        with self._lock:
            active = sum(1 for c in self._counters.values()
                         if c.get("status") == "active")
            total_a = sum(c.get("a_count", 0) for c in self._counters.values())
            total_b = sum(c.get("b_count", 0) for c in self._counters.values())
            total_storage = sum(c.get("used_bytes", 0) for c in self._counters.values())
        return {
            "ready": True,
            "total_accounts": len(self._accounts),
            "active_accounts": active,
            "routing_entries": len(self._routing),
            "total_a_ops": total_a,
            "total_b_ops": total_b,
            "total_storage_mb": round(total_storage / (1024 * 1024), 1),
        }


class _S3Client:
    """单个 S3 账号的客户端（boto3 封装）"""

    def __init__(self, access_key, secret_key, bucket, endpoint, region):
        self.bucket = bucket
        self._client = None
        self._access_key = access_key
        self._secret_key = secret_key
        self._endpoint = endpoint
        self._region = region

    def _ensure_client(self):
        if self._client is not None:
            return
        import boto3
        from botocore.config import Config
        self._client = boto3.client(
            "s3",
            endpoint_url=self._endpoint,
            region_name=self._region,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            config=Config(
                retries={"max_attempts": 0},
                connect_timeout=10,
                read_timeout=60,
                max_pool_connections=5,
            ),
        )

    def put(self, key, data, prefix="ghbox"):
        self._ensure_client()
        full_key = f"{prefix}/{key}" if prefix else key
        self._client.put_object(Bucket=self.bucket, Key=full_key, Body=data)
        return True

    def get(self, key, prefix="ghbox"):
        self._ensure_client()
        full_key = f"{prefix}/{key}" if prefix else key
        from botocore.exceptions import ClientError
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=full_key)
            return resp["Body"].read()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def delete(self, key, prefix="ghbox"):
        self._ensure_client()
        full_key = f"{prefix}/{key}" if prefix else key
        from botocore.exceptions import ClientError
        try:
            self._client.delete_object(Bucket=self.bucket, Key=full_key)
            return True
        except ClientError:
            return False
