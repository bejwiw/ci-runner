# -*- coding: utf-8 -*-
"""
S3 多账号存储池（Tigris）+ 一致性哈希

架构：
  账号0 (bootstrap桶)  → 只存账号列表 s3-accounts.txt（worker启动时下载1次）
  账号1~N (数据桶)     → 一致性哈希分散存所有数据（实例数据+元数据）

一致性哈希：增减账号只影响约1/N的数据位置，不需要全量迁移。
账号状态机：active → degraded(3次失败) → unavailable(5次失败) → 每5分钟探测恢复。
S3不加密（私有访问），Releases降级存储加密。
"""
import os
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import log
from core.hashring import HashRing

logger = log.setup_logger("s3")

# ==================== 常量 ====================
# Tigris 免费额度：每账号 5GB 永久存储，A类(PUT) 1万次/月，B类(GET) 10万次/月
S3_PREFIX = "ghbox"  # S3 key 前缀
A_LIMIT = 9000          # A类操作月度上限（预留余量）
B_LIMIT = 90000          # B类操作月度上限（预留余量）
STORAGE_LIMIT = 4_500_000_000  # 每账号存储上限 ~4.5GB（预留余量）
ACCOUNTS_KEY = "meta/s3-accounts.txt"
MAX_FALLBACK = 3
MAX_SCAN = 10
MAX_RETRIES = 3
LARGE_FILE_THRESHOLD = 50 * 1024 * 1024
CHUNK_SIZE = 10 * 1024 * 1024
CHUNK_CONCURRENCY = 20
# 动态并发参数
CONC_SMALL = 10
CONC_MEDIUM = 20
CONC_LARGE = 30
SMALL_FILE_BOUNDARY = 50 * 1024 * 1024
MEDIUM_FILE_BOUNDARY = 500 * 1024 * 1024
MAX_LIST_KEYS = 1000


def _dynamic_concurrency(file_size):
    """根据文件大小动态调整并发数"""
    if file_size < SMALL_FILE_BOUNDARY:
        return CONC_SMALL
    elif file_size < MEDIUM_FILE_BOUNDARY:
        return CONC_MEDIUM
    else:
        return CONC_LARGE
RECOVERY_INTERVAL = 300
DEGRADED_THRESHOLD = 3
UNAVAILABLE_THRESHOLD = 5


def _parse_accounts(text):
    """解析 AWS profiles 格式文本 → 账号列表"""
    accounts = []
    current = {}
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("[profile"):
            if current and "access_key" in current:
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
    if current and "access_key" in current:
        accounts.append(current)
    return accounts


class S3Pool:
    """S3 多账号存储池（一致性哈希 + 账号状态机）"""

    def __init__(self, bootstrap_creds, endpoint, region, owner="worker"):
        parts = bootstrap_creds.split("|")
        self._bootstrap = {
            "access_key": parts[0],
            "secret_key": parts[1],
            "bucket": parts[2] if len(parts) > 2 else "kkk001",
        }
        self.endpoint = endpoint
        self.region = region
        self._owner = owner
        self._accounts = []
        self._hash_ring = HashRing(virtual_nodes=150)
        self._counters = {}
        self._lock = threading.RLock()
        self._clients = {}
        self._initialized = False

    # ==================== 初始化 ====================
    def init(self):
        """从 bootstrap 桶下载账号列表 + 构建哈希环"""
        try:
            raw = self._get_client(0).get(ACCOUNTS_KEY, prefix="")
            if raw:
                self._accounts = _parse_accounts(raw.decode("utf-8"))
                logger.info(f"加载了 {len(self._accounts)} 个数据账号")
            else:
                logger.error("bootstrap 桶中无账号列表")
                return False
        except Exception as e:
            logger.error(f"下载账号列表失败: {e}")
            return False
        self._hash_ring.build(len(self._accounts))
        logger.info(f"哈希环构建完成: {self._hash_ring.size} 个虚拟节点")
        now = time.gmtime()
        current_month = now.tm_year * 12 + now.tm_mon
        for i in range(len(self._accounts)):
            self._counters[i] = {
                "a_count": 0, "b_count": 0, "used_bytes": 0,
                "status": "active", "fail_count": 0,
                "last_error": "", "last_error_time": 0,
                "last_success": 0, "month": current_month,
            }
        self._initialized = True
        self.load_state()
        # 刷新 used_bytes — 从S3 API查实际存储大小，纠正历史虚高
        self._refresh_storage_sizes()
        self.start_recovery()
        return True

    def is_ready(self):
        return self._initialized

    def _refresh_storage_sizes(self):
        """后台线程：从S3 API查询所有桶的实际存储大小，刷新 used_bytes。

        不阻塞启动，后台并发查询全部账号。put已修复差值计算，
        这里作为兜底纠正历史偏差。
        """
        threading.Thread(target=self._do_refresh_storage_sizes, daemon=True).start()

    def _do_refresh_storage_sizes(self):
        try:
            # 只查 used_bytes > 0 的桶（自己 owner 写入过的桶），
            # 避免把其他 owner 的数据算进本 owner 的计数器。
            # 配合 load_state 恢复 used_bytes，能纠正旧虚高值。
            with self._lock:
                candidates = [idx for idx, c in self._counters.items()
                              if idx > 0 and c.get("used_bytes", 0) > 0]
            if not candidates:
                logger.info("无 used_bytes>0 的桶，跳过刷新")
                return
            sizes = self.query_bucket_sizes(account_indices=candidates, concurrency=50)
            with self._lock:
                corrected = 0
                for idx, info in sizes.items():
                    if idx in self._counters:
                        actual = info.get("size", 0)
                        old = self._counters[idx]["used_bytes"]
                        if actual != old:
                            self._counters[idx]["used_bytes"] = actual
                            corrected += 1
            logger.info(f"used_bytes 已刷新 ({len(sizes)}桶, 纠正{corrected}个)")
        except Exception as e:
            logger.warning(f"刷新存储大小失败: {e}")

    def start_recovery(self):
        threading.Thread(target=self._recovery_loop, daemon=True).start()
        logger.info("恢复探测线程已启动")

    # ==================== S3 底层 ====================
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

    # ==================== 账号状态 ====================
    def _is_writable(self, idx):
        c = self._counters.get(idx, {})
        if c.get("status") == "unavailable":
            return False
        if c.get("a_count", 0) >= A_LIMIT:
            return False
        if c.get("used_bytes", 0) >= STORAGE_LIMIT:
            return False
        return True

    def _priority(self, idx):
        c = self._counters.get(idx, {})
        return 1 if c.get("status") == "degraded" else 0

    def _select_account(self, key, exclude=None):
        """用一致性哈希选账号。只返回原桶，不fallback到其他桶。

        修复Bug 2: put()和get()必须在同一个桶，否则put换桶后get读旧数据。
        如果原桶不可写，返回None，让调用方处理失败。
        """
        acct = self._hash_ring.get_account(key)
        if acct is None:
            return None
        if exclude and acct in exclude:
            return None
        if self._is_writable(acct):
            return acct
        return None

    def _record_failure(self, idx, error):
        with self._lock:
            c = self._counters.get(idx, {})
            if not c:
                return
            c["fail_count"] = c.get("fail_count", 0) + 1
            c["last_error"] = str(error)[:200]
            c["last_error_time"] = time.time()
            fc = c["fail_count"]
            old = c.get("status", "active")
            if fc >= UNAVAILABLE_THRESHOLD:
                c["status"] = "unavailable"
                if old != "unavailable":
                    logger.warning(f"账号{idx} → unavailable ({fc}次失败: {c['last_error'][:80]})")
            elif fc >= DEGRADED_THRESHOLD:
                c["status"] = "degraded"
                if old == "active":
                    logger.info(f"账号{idx} → degraded ({fc}次失败)")

    def _record_success(self, idx):
        with self._lock:
            c = self._counters.get(idx, {})
            if not c:
                return
            if c.get("fail_count", 0) > 0 or c.get("status") != "active":
                c["fail_count"] = 0
                c["status"] = "active"
            c["last_success"] = time.time()

    def _get_object_size(self, idx, key):
        """查询指定账号上key的实际大小（用于put时计算差值）"""
        try:
            client = self._get_client(idx)
            from botocore.exceptions import ClientError
            head = client._client.head_object(
                Bucket=client.bucket, Key=f"{S3_PREFIX}/{key}")
            return head.get("ContentLength", 0) or 0
        except ClientError:
            return 0
        except Exception:
            return 0

    # ==================== 写入 ====================
    def put(self, key, data):
        if not self._initialized:
            return False
        account_idx = self._select_account(key)
        if account_idx is None:
            logger.error(f"原账号不可写，拒绝写入 {key}")
            return False
        old_size = self._get_object_size(account_idx, key)
        for attempt in range(MAX_RETRIES):
            try:
                if self._get_client(account_idx).put(key, data):
                    with self._lock:
                        self._counters[account_idx]["a_count"] += 1
                        self._counters[account_idx]["used_bytes"] = max(0, self._counters[account_idx]["used_bytes"] + (len(data) - old_size))
                    self._record_success(account_idx)
                    return True
            except Exception as e:
                logger.warning(f"写入 {key} 到账号{account_idx} 失败(第{attempt+1}次): {e}")
                self._record_failure(account_idx, e)
        return False

    # ==================== 读取 ====================
    def get(self, key):
        if not self._initialized:
            return None
        account_idx = self._hash_ring.get_account(key)
        if account_idx is not None:
            if self._counters.get(account_idx, {}).get("status") != "unavailable":
                try:
                    data = self._get_client(account_idx).get(key)
                    if data is not None:
                        with self._lock:
                            self._counters[account_idx]["b_count"] += 1
                        self._record_success(account_idx)
                        return data
                except Exception as e:
                    logger.warning(f"从账号{account_idx}读取 {key} 失败: {e}")
                    self._record_failure(account_idx, e)
        nearby = self._hash_ring.get_nearby_accounts(key, MAX_SCAN)
        for alt_idx in nearby:
            if alt_idx == account_idx:
                continue
            if self._counters.get(alt_idx, {}).get("status") == "unavailable":
                continue
            try:
                data = self._get_client(alt_idx).get(key)
                if data is not None:
                    with self._lock:
                        self._counters[alt_idx]["b_count"] += 1
                    self._record_success(alt_idx)
                    logger.info(f"从账号{alt_idx}遍历找到 {key}")
                    return data
            except Exception as e:
                self._record_failure(alt_idx, e)
                continue
        return None

    # ==================== 删除 ====================
    def delete(self, key):
        if not self._initialized:
            return False
        account_idx = self._hash_ring.get_account(key)
        if account_idx is None:
            return True
        try:
            client = self._get_client(account_idx)
            # 先查对象大小用于回扣 used_bytes
            file_size = 0
            try:
                from botocore.exceptions import ClientError
                head = client._client.head_object(
                    Bucket=client.bucket, Key=f"{S3_PREFIX}/{key}")
                file_size = head.get("ContentLength", 0) or 0
            except ClientError as e:
                logger.debug(f"head_object {key} 失败: {e}")
            except Exception as e:
                logger.debug(f"head_object {key} 异常: {e}")
            client.delete(key)
            with self._lock:
                self._counters[account_idx]["a_count"] += 1
                if file_size > 0:
                    self._counters[account_idx]["used_bytes"] = max(
                        0, self._counters[account_idx]["used_bytes"] - file_size)
            self._record_success(account_idx)
            return True
        except Exception as e:
            logger.warning(f"删除 {key} 失败: {e}")
            self._record_failure(account_idx, e)
            return False


    # ==================== 兼容方法（哈希分散到数据账号）====================
    def get_meta_json(self, key, default=None):
        """读取JSON元数据（一致性哈希查找数据账号）"""
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return json.loads(raw.decode())
        except Exception:
            return default

    def put_meta_json(self, key, obj):
        """写入JSON元数据（一致性哈希分散到数据账号）"""
        return self.put(key, json.dumps(obj, ensure_ascii=False).encode())

    def get_meta(self, key):
        """读取原始元数据"""
        return self.get(key)

    def put_meta(self, key, data):
        """写入原始元数据"""
        return self.put(key, data)

    # ==================== bootstrap 桶（只读账号列表）====================
    def get_accounts_raw(self):
        """从 bootstrap 桶读取账号列表"""
        try:
            return self._get_client(0).get(ACCOUNTS_KEY, prefix="")
        except Exception as e:
            logger.error(f"读取账号列表失败: {e}")
            return None

    # ==================== 恢复探测 ====================
    def _recovery_loop(self):
        while not self._initialized:
            time.sleep(1)
        while True:
            time.sleep(RECOVERY_INTERVAL)
            try:
                self._probe_unavailable()
                self._check_monthly_reset()
                self.save_state()
            except Exception as e:
                logger.error(f"恢复探测异常: {e}")

    def _probe_unavailable(self):
        with self._lock:
            candidates = [idx for idx, c in self._counters.items()
                          if c.get("status") == "unavailable" and idx > 0]
        if not candidates:
            return
        logger.info(f"探测 {len(candidates)} 个 unavailable 账号")
        for idx in candidates:
            try:
                test_key = f"_healthcheck/{idx}"
                self._get_client(idx).put(test_key, b"ok")
                data = self._get_client(idx).get(test_key)
                if data == b"ok":
                    self._get_client(idx).delete(test_key)
                    self._record_success(idx)
                    logger.info(f"账号{idx} 恢复为 active")
            except Exception as e:
                with self._lock:
                    c = self._counters.get(idx, {})
                    c["last_error"] = str(e)[:200]
                    c["last_error_time"] = time.time()

    def _check_monthly_reset(self):
        now = time.gmtime()
        current_month = now.tm_year * 12 + now.tm_mon
        with self._lock:
            for idx, c in self._counters.items():
                if c["month"] != current_month:
                    c["a_count"] = 0
                    c["b_count"] = 0
                    c["status"] = "active"
                    c["fail_count"] = 0
                    c["month"] = current_month
                    logger.info(f"账号{idx} 月度重置")

    # ==================== 计数器持久化 ====================
    # 计数器持久化 key（按 owner 区分，防止 Manager/Worker 互相覆盖）

    def save_state(self):
        """持久化计数器到 bootstrap 桶（防止重启归零）"""
        if not self._initialized:
            return
        with self._lock:
            state = {str(idx): dict(c) for idx, c in self._counters.items()}
        try:
            data = json.dumps(state, ensure_ascii=False).encode()
            self._get_client(0).put(f"meta/s3-counters-{self._owner}.json", data)
            logger.info(f"计数器已持久化 ({len(state)} 账号)")
        except Exception as e:
            logger.warning(f"计数器持久化失败: {e}")

    def load_state(self):
        """从 bootstrap 桶恢复计数器"""
        try:
            raw = self._get_client(0).get(f"meta/s3-counters-{self._owner}.json")
            if not raw:
                return
            state = json.loads(raw.decode())
            now = time.gmtime()
            current_month = now.tm_year * 12 + now.tm_mon
            restored = 0
            for idx_str, saved in state.items():
                idx = int(idx_str)
                if idx not in self._counters:
                    continue
                c = self._counters[idx]
                # 跨月则不恢复计数，但恢复状态
                if saved.get("month") == current_month:
                    c["a_count"] = saved.get("a_count", 0)
                    c["b_count"] = saved.get("b_count", 0)
                # 恢复 used_bytes — put已修复差值计算，新数据精确，可放心恢复
                c["used_bytes"] = saved.get("used_bytes", 0)
                c["status"] = saved.get("status", "active")
                c["fail_count"] = saved.get("fail_count", 0)
                c["last_error"] = saved.get("last_error", "")
                c["last_error_time"] = saved.get("last_error_time", 0)
                c["last_success"] = saved.get("last_success", 0)
                c["month"] = saved.get("month", current_month)
                restored += 1
            logger.info(f"恢复 {restored} 个账号计数器")
        except Exception as e:
            logger.warning(f"恢复计数器失败: {e}")


    # ==================== 分片存储（大文件）====================
    def put_file(self, key, file_path):
        """从磁盘文件上传。>=50MB分片并发，<50MB直接上传。"""
        if not self._initialized:
            return False
        file_size = os.path.getsize(file_path)
        if file_size < LARGE_FILE_THRESHOLD:
            with open(file_path, "rb") as f:
                data = f.read()
            return self.put(key, data)
        return self._put_file_chunked(key, file_path, file_size)

    def _put_file_chunked(self, key, file_path, file_size):
        """分片并发上传大文件"""
        num_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        logger.info(f"分片上传 {key}: {file_size}B -> {num_chunks}块")

        def upload_chunk(idx):
            offset = idx * CHUNK_SIZE
            size = min(CHUNK_SIZE, file_size - offset)
            chunk_key = f"{key}.chunk{idx}"
            with open(file_path, "rb") as f:
                f.seek(offset)
                data = f.read(size)
            account = self._select_account(chunk_key)
            if account is None:
                return idx, None
            old_size = self._get_object_size(account, chunk_key)
            for attempt in range(MAX_RETRIES):
                try:
                    if self._get_client(account).put(chunk_key, data):
                        with self._lock:
                            self._counters[account]["a_count"] += 1
                            self._counters[account]["used_bytes"] = max(0, self._counters[account]["used_bytes"] + (len(data) - old_size))
                        self._record_success(account)
                        return idx, account
                except Exception as e:
                    self._record_failure(account, e)
                    # 不重新选桶（设计意图：put和get必须在同一桶），继续用原桶重试
            return idx, None

        workers = _dynamic_concurrency(file_size)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(upload_chunk, range(num_chunks)))
        for idx, account in results:
            if account is None:
                logger.error(f"分片{idx}上传失败, {key}整体失败")
                return False
        locations = [{"chunk": idx, "account": acct} for idx, acct in results]
        manifest = json.dumps({
            "chunks": num_chunks, "chunk_size": CHUNK_SIZE,
            "total_size": file_size, "locations": locations,
        }).encode()
        self.put(f"{key}.manifest", manifest)
        logger.info(f"分片上传完成: {key} ({num_chunks}块)")
        return True

    def get_to_file(self, key, file_path):
        """下载到磁盘文件。先查单文件（最新版本），没有再查清单走分片。"""
        if not self._initialized:
            return False
        # 先查单文件（如果存在就是最新版本，优先使用）
        data = self.get(key)
        if data is not None:
            with open(file_path, "wb") as f:
                f.write(data)
            return True
        # 单文件不存在，查清单走分片下载（大文件分片存储）
        manifest_data = self.get(f"{key}.manifest")
        if manifest_data is None:
            return False
        manifest = json.loads(manifest_data)
        return self._get_chunked_to_file(key, manifest, file_path)

    def _get_chunked_to_file(self, key, manifest, file_path):
        """分片并发下载写磁盘"""
        num_chunks = manifest["chunks"]
        total_size = manifest["total_size"]
        chunk_size = manifest["chunk_size"]
        locations = manifest["locations"]
        logger.info(f"分片下载 {key}: {total_size}B -> {num_chunks}块")
        with open(file_path, "wb") as f:
            f.truncate(total_size)

        def download_chunk(loc):
            idx = loc["chunk"]
            account = loc["account"]
            chunk_key = f"{key}.chunk{idx}"
            if self._counters.get(account, {}).get("status") != "unavailable":
                try:
                    data = self._get_client(account).get(chunk_key)
                    if data is not None:
                        with self._lock:
                            self._counters[account]["b_count"] += 1
                        self._record_success(account)
                        return idx, data
                except Exception as e:
                    self._record_failure(account, e)
            nearby = self._hash_ring.get_nearby_accounts(chunk_key, MAX_SCAN)
            for alt in nearby:
                if alt == account:
                    continue
                if self._counters.get(alt, {}).get("status") == "unavailable":
                    continue
                try:
                    data = self._get_client(alt).get(chunk_key)
                    if data is not None:
                        with self._lock:
                            self._counters[alt]["b_count"] += 1
                        self._record_success(alt)
                        return idx, data
                except Exception as e:
                    self._record_failure(alt, e)
            return idx, None

        workers = _dynamic_concurrency(total_size)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(download_chunk, locations))
        for idx, data in results:
            if data is None:
                logger.error(f"分片{idx}下载失败")
                return False
            offset = idx * chunk_size
            with open(file_path, "r+b") as f:
                f.seek(offset)
                f.write(data)
        logger.info(f"分片下载完成: {key} ({num_chunks}块)")
        return True


    def get_storage_size(self, key):
        """查询key在S3中的实际存储大小（恢复前调用）"""
        if not self._initialized:
            return 0
        # 先查manifest
        manifest_data = self.get(f"{key}.manifest")
        if manifest_data:
            import json
            manifest = json.loads(manifest_data)
            return manifest.get("total_size", 0)
        # 非分片文件，查key本身
        data = self.get(key)
        return len(data) if data else 0

    def query_bucket_sizes(self, account_indices=None, concurrency=20):
        """并发查询多个桶的存储大小和对象数（用S3兼容API list_objects_v2）"""
        if not self._initialized:
            return {}
        if account_indices is None:
            account_indices = list(range(min(20, len(self._accounts))))
        results = {}
        def _query(idx):
            try:
                client = self._get_client(idx)
                bucket = self._accounts[idx - 1]["bucket"] if idx > 0 else self._bootstrap["bucket"]
                total = 0
                count = 0
                token = None
                # 分页查询直到 IsTruncated=False，避免漏掉超1000个对象
                while True:
                    kwargs = {"Bucket": bucket, "MaxKeys": MAX_LIST_KEYS}
                    if token:
                        kwargs["ContinuationToken"] = token
                    resp = client._client.list_objects_v2(**kwargs)
                    total += sum(obj.get("Size", 0) for obj in resp.get("Contents", []))
                    count += resp.get("KeyCount", 0)
                    if resp.get("IsTruncated"):
                        token = resp.get("NextContinuationToken")
                    else:
                        break
                return idx, {"bucket": bucket, "size": total, "count": count}
            except Exception as e:
                return idx, {"bucket": "?", "size": 0, "count": 0, "error": str(e)[:100]}
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            for idx, info in executor.map(_query, account_indices):
                results[idx] = info
        return results

    # ==================== 状态查询 ====================
    def get_status(self):
        if not self._initialized:
            return {"ready": False}
        with self._lock:
            active = sum(1 for c in self._counters.values()
                         if c.get("status") == "active")
            degraded = sum(1 for c in self._counters.values()
                           if c.get("status") == "degraded")
            unavailable = sum(1 for c in self._counters.values()
                              if c.get("status") == "unavailable")
            total_a = sum(c.get("a_count", 0) for c in self._counters.values())
            total_b = sum(c.get("b_count", 0) for c in self._counters.values())
            total_storage = sum(c.get("used_bytes", 0) for c in self._counters.values())
        return {
            "ready": True,
            "total_accounts": len(self._accounts),
            "active_accounts": active,
            "degraded_accounts": degraded,
            "unavailable_accounts": unavailable,
            "total_a_ops": total_a,
            "total_b_ops": total_b,
            "total_storage_mb": round(total_storage / (1024 * 1024), 1),
            "hash_ring_size": self._hash_ring.size,
        }

    def get_health(self):
        if not self._initialized:
            return {"ready": False}
        s = self.get_status()
        return {
            "ready": s["ready"],
            "active": s["active_accounts"],
            "degraded": s["degraded_accounts"],
            "unavailable": s["unavailable_accounts"],
            "total": s["total_accounts"],
        }

    def get_account_status(self, limit=50):
        if not self._initialized:
            return {"accounts": [], "total": 0}
        with self._lock:
            all_accts = []
            for idx, c in sorted(self._counters.items()):
                if idx == 0:
                    continue
                if c.get("status") == "active" and c.get("fail_count", 0) == 0:
                    continue
                all_accts.append({
                    "idx": idx,
                    "status": c.get("status", "active"),
                    "a_count": c.get("a_count", 0),
                    "b_count": c.get("b_count", 0),
                    "used_mb": round(c.get("used_bytes", 0) / (1024 * 1024), 1),
                    "fail_count": c.get("fail_count", 0),
                    "last_error": c.get("last_error", "")[:100],
                    "last_error_time": c.get("last_error_time", 0),
                    "last_success": c.get("last_success", 0),
                })
        return {
            "accounts": all_accts[:limit],
            "total_non_active": len(all_accts),
            "total": len(self._accounts),
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
                retries={"max_attempts": 3},
                connect_timeout=10,
                read_timeout=60,
                max_pool_connections=5,
            ),
        )

    def put(self, key, data, prefix=S3_PREFIX):
        self._ensure_client()
        full_key = f"{prefix}/{key}" if prefix else key
        resp = self._client.put_object(Bucket=self.bucket, Key=full_key, Body=data)
        if resp and resp.get("ETag"):
            return True
        return False

    def get(self, key, prefix=S3_PREFIX):
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

    def delete(self, key, prefix=S3_PREFIX):
        self._ensure_client()
        full_key = f"{prefix}/{key}" if prefix else key
        from botocore.exceptions import ClientError
        try:
            self._client.delete_object(Bucket=self.bucket, Key=full_key)
            return True
        except ClientError:
            return False
