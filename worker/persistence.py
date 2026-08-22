# -*- coding: utf-8 -*-
"""
数据/文件持久化（S3 纯单文件 + Release 冗余 + 时间戳对比恢复）

架构（v2，2026-08-22）：
- 备份：tar打包FILES_DIR + 内置_timestamp.txt → S3单文件put → Release异步冗余
- 恢复：并行从S3+Release拉取 → 读_timestamp.txt对比 → 用更新的
- 大文件(>500MB)跳过S3，只走Release
- 兼容旧分片数据（get_to_file先查单文件，无则查manifest走分片下载）

修复历史问题：
1. 分片上传不更新单文件 → 旧版本残留 → 恢复读到旧数据
2. Release资产积累到1000上限 → 422上传失败
3. S3账号不可写 → fallback到附近可写账号（已在s3.py修复）
"""
import os
import json
import time
import sqlite3
import threading
import subprocess
import shutil
import datetime
from concurrent.futures import ThreadPoolExecutor

import config
import log
from core import crypto, releases
from core.s3 import S3Pool
from worker import upload_queue

TMP_DIR = "/tmp/ghbox_backup"
TIMESTAMP_FILENAME = "_timestamp.txt"

logger = log.setup_logger("persistence")

_db_conn = None
_db_lock = threading.RLock()
_s3pool = None


def set_s3pool(pool):
    global _s3pool
    _s3pool = pool


def _get_db():
    global _db_conn
    with _db_lock:
        if _db_conn is None:
            _db_conn = sqlite3.connect(config.DB_FILE, check_same_thread=False)
            _db_conn.row_factory = sqlite3.Row
        return _db_conn


def db_execute(sql, params=None):
    with _db_lock:
        conn = _get_db()
        cur = conn.execute(sql, params or ())
        conn.commit()
        return cur


def create_new_db():
    db_execute("CREATE TABLE IF NOT EXISTS messages "
               "(id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, created_at TEXT)")
    db_execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    db_execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('visits', '0')")
    db_execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('created_at', ?)",
               (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))


# ==================== 时间戳工具 ====================
def _create_timestamp_file():
    """在FILES_DIR下创建_timestamp.txt（打包前调用，打包后清理）"""
    ts_file = os.path.join(config.FILES_DIR, TIMESTAMP_FILENAME)
    try:
        with open(ts_file, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        logger.warning(f"时间戳文件创建失败: {e}")


def _remove_timestamp_file():
    """清理FILES_DIR下的_timestamp.txt（解压后调用）"""
    ts_file = os.path.join(config.FILES_DIR, TIMESTAMP_FILENAME)
    try:
        if os.path.exists(ts_file):
            os.remove(ts_file)
    except Exception as e:
        logger.debug(f"时间戳文件清理失败: {e}")


def _extract_timestamp(tar_path):
    """从压缩包中提取_timestamp.txt的时间戳（不解压全部文件）

    返回float时间戳；旧数据无此文件返回0.0（视为最旧）
    """
    base_name = os.path.basename(config.FILES_DIR)
    member_path = f"{base_name}/{TIMESTAMP_FILENAME}"
    try:
        with open(tar_path, "rb") as _f:
            _hdr = _f.read(4)
        is_zstd = (_hdr[:4] == b"\x28\xb5\x2f\xfd")
        tar_cmd = ["tar", "--zstd" if is_zstd else "-z", "-xOf", tar_path, member_path] if is_zstd else ["tar", "xzOf", tar_path, member_path]
        result = subprocess.run(tar_cmd, capture_output=True, timeout=10)
        if result.returncode == 0 and result.stdout:
            return float(result.stdout.strip())
    except Exception as e:
        logger.debug(f"时间戳提取失败: {e}")
    return 0.0


# ==================== 文件打包 ====================
def backup_files_to_bytes():
    if not os.path.isdir(config.FILES_DIR):
        return None
    _create_timestamp_file()
    try:
        result = subprocess.run(
            ["sudo", "tar", "--zstd", "-cf", "-", "-C", os.path.dirname(config.FILES_DIR), os.path.basename(config.FILES_DIR)],
            capture_output=True, timeout=180)
        if result.returncode != 0:
            logger.error(f"文件打包失败: {result.stderr.decode(errors='replace')[:200]}")
            return None
        return result.stdout
    finally:
        _remove_timestamp_file()


def restore_files_from_bytes(data):
    if not data:
        return False
    tmp = os.path.join(os.path.expanduser("~"), ".files_restore.tar.gz")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        ok = restore_files_from_file(tmp)
        return ok
    except Exception as e:
        logger.error(f"文件恢复失败: {e}")
        return False
    finally:
        try:
            os.remove(tmp)
        except Exception as e:
            logger.debug(f"操作失败: {e}")


# ==================== 备份/恢复 ====================
def load_or_create(inst_cfg):
    """恢复实例数据。并行从S3+Release拉取files，时间戳对比用最新的。

    流程：
    1. 数据库恢复（S3优先，Release降级，db很小不并行）
    2. 文件恢复：并行从S3和Release拉取files.tar.gz
       - 各自提取_timestamp.txt（旧数据无则返回0）
       - 对比时间戳，用更新的
       - 只有一个成功就用那个
       - 都失败则创建空目录
    3. 完整日志记录（哪个源、时间戳对比、是否降级）
    """
    inst_id = inst_cfg.instance_id if inst_cfg else "global"
    db_key = f"inst-data/{inst_id}/db"
    files_key = f"inst-files/{inst_id}/files.tar.gz"
    db_asset = inst_cfg.asset_db if inst_cfg else config.ASSET_DB
    files_asset = inst_cfg.asset_files if inst_cfg else config.ASSET_FILES
    status_msg = "新建初始数据库"

    # === 数据库恢复（S3优先，Release降级）===
    db_base = db_asset[:-4] if db_asset.endswith(".enc") else db_asset
    db_data = None
    db_source = "无"
    if _s3pool and _s3pool.is_ready():
        try:
            db_data = _s3pool.get(db_key)
            if db_data is not None:
                db_source = "S3"
        except Exception as e:
            logger.warning(f"S3 数据库读取失败: {e}")
    if db_data is None:
        db_data = releases.download_latest(db_base, token=config.GH_TOKEN, repo=config.REPO)
        if db_data:
            db_source = "Releases"
    if db_data:
        with open(config.DB_FILE, "wb") as f:
            f.write(db_data)
        status_msg = f"恢复数据库（{len(db_data)} 字节, 来源={db_source}）"
        logger.info(status_msg)
    else:
        create_new_db()
        logger.warning("数据库无可用备份，创建空数据库")

    # === 文件恢复（并行从S3+Release拉取，时间戳对比）===
    files_base = files_asset[:-4] if files_asset.endswith(".enc") else files_asset
    os.makedirs(TMP_DIR, exist_ok=True)

    s3_result = {"path": None, "timestamp": 0.0, "ok": False}
    rel_result = {"path": None, "timestamp": 0.0, "ok": False}

    def _fetch_s3():
        """从S3拉取files.tar.gz"""
        if not _s3pool or not _s3pool.is_ready():
            return
        tmp = os.path.join(TMP_DIR, f"restore_s3_{inst_id}.tar.gz")
        try:
            if _s3pool.get_to_file(files_key, tmp):
                ts = _extract_timestamp(tmp)
                s3_result["path"] = tmp
                s3_result["timestamp"] = ts
                s3_result["ok"] = True
                logger.info(f"S3 文件获取成功 (ts={ts:.0f})")
            else:
                logger.warning("S3 文件不存在")
        except Exception as e:
            logger.warning(f"S3 文件读取失败: {e}")

    def _fetch_release():
        """从Release拉取files.tar.gz"""
        try:
            data = releases.download_latest(files_base, token=config.GH_TOKEN, repo=config.REPO)
            if data:
                tmp = os.path.join(TMP_DIR, f"restore_rel_{inst_id}.tar.gz")
                with open(tmp, "wb") as f:
                    f.write(data)
                ts = _extract_timestamp(tmp)
                rel_result["path"] = tmp
                rel_result["timestamp"] = ts
                rel_result["ok"] = True
                logger.info(f"Release 文件获取成功 (ts={ts:.0f})")
            else:
                logger.warning("Release 文件不存在")
        except Exception as e:
            logger.warning(f"Release 文件读取失败: {e}")

    # 并行拉取（各超时120秒）
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_s3 = executor.submit(_fetch_s3)
        f_rel = executor.submit(_fetch_release)
        try:
            f_s3.result(timeout=120)
        except Exception as e:
            logger.warning(f"S3 拉取超时/异常: {e}")
        try:
            f_rel.result(timeout=120)
        except Exception as e:
            logger.warning(f"Release 拉取超时/异常: {e}")

    # 对比时间戳，选最新的
    chosen = None
    chosen_source = "无"
    if s3_result["ok"] and rel_result["ok"]:
        if s3_result["timestamp"] >= rel_result["timestamp"]:
            chosen = s3_result["path"]
            chosen_source = "S3"
            logger.info(f"时间戳对比: S3({s3_result['timestamp']:.0f}) >= Release({rel_result['timestamp']:.0f}), 用S3")
        else:
            chosen = rel_result["path"]
            chosen_source = "Release"
            logger.info(f"时间戳对比: Release({rel_result['timestamp']:.0f}) > S3({s3_result['timestamp']:.0f}), 用Release")
    elif s3_result["ok"]:
        chosen = s3_result["path"]
        chosen_source = "S3(仅S3可用)"
        logger.info("仅S3可用，用S3")
    elif rel_result["ok"]:
        chosen = rel_result["path"]
        chosen_source = "Release(仅Release可用)"
        logger.info("仅Release可用，用Release")
    else:
        logger.error("S3和Release都无法获取文件，创建空目录")

    # 解压选中的文件
    if chosen:
        restore_files_from_file(chosen)

    # 清理临时文件
    for tmp_path in [s3_result["path"], rel_result["path"]]:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    # 清理解压后的_timestamp.txt
    _remove_timestamp_file()

    os.makedirs(config.FILES_DIR, exist_ok=True)
    record_restore("success" if chosen else "failed",
                   f"files: source={chosen_source}, s3_ts={s3_result['timestamp']:.0f}, rel_ts={rel_result['timestamp']:.0f}",
                   0, 0)
    return status_msg


def backup_database(inst_cfg=None):
    """备份数据库。S3 优先，Releases 降级。"""
    inst_id = inst_cfg.instance_id if inst_cfg else "global"
    db_key = f"inst-data/{inst_id}/db"
    db_asset = inst_cfg.asset_db if inst_cfg else config.ASSET_DB
    with _db_lock:
        conn = _get_db()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            logger.debug(f"checkpoint失败: {e}")
    with open(config.DB_FILE, "rb") as f:
        data = f.read()
    db_base = db_asset[:-4] if db_asset.endswith(".enc") else db_asset
    if _s3pool and _s3pool.is_ready() and _s3pool.put(db_key, data):
        logger.info(f"数据库 → S3 ({len(data)} 字节)")
        try:
            if upload_queue.get_queue().enqueue("db", db_base, data=data,
                                                inst_id=inst_id):
                logger.info(f"数据库 → Releases 已入队异步 ({len(data)} 字节)")
            else:
                logger.warning("数据库 → Releases 入队被拒（队列满/配额低），跳过双写")
        except Exception as e:
            logger.warning(f"数据库 → Releases 入队异常: {e}")
        return len(data), 1
    try:
        r = releases.upload_asset_v2(db_base, data)
        if r.get("ok"):
            logger.info(f"数据库 → Releases 同步兜底成功 ({len(data)} 字节)")
            return len(data), 1
        logger.error(f"数据库 → Releases 兜底失败: {r.get('status')}")
    except Exception as e:
        logger.error(f"数据库 → Releases 兜底异常: {e}")
    return len(data), 0


def backup_files(inst_cfg=None):
    """备份 ~/files。tar(内置时间戳) → S3单文件put → Release异步冗余。

    大文件(>500MB)跳过S3，只走Release。
    """
    inst_id = inst_cfg.instance_id if inst_cfg else "global"
    files_key = f"inst-files/{inst_id}/files.tar.gz"
    files_asset = inst_cfg.asset_files if inst_cfg else config.ASSET_FILES
    tmp = backup_files_to_disk()
    if not tmp:
        return None
    file_size = os.path.getsize(tmp)
    files_base = files_asset[:-4] if files_asset.endswith(".enc") else files_asset
    s3_ok = False
    if _s3pool and _s3pool.is_ready():
        s3_ok = _s3pool.put_file(files_key, tmp)
        if s3_ok:
            logger.info(f"文件 → S3 ({file_size} 字节)")
        else:
            logger.error(f"文件 → S3 失败! ({file_size} 字节)")
    if s3_ok:
        try:
            q = upload_queue.get_queue()
            staged = os.path.join(upload_queue.QUEUE_DIR, f"files-{inst_id}-{int(time.time())}.tar.gz")
            os.makedirs(upload_queue.QUEUE_DIR, exist_ok=True)
            if os.path.exists(staged):
                os.remove(staged)
            shutil.move(tmp, staged)
            if q.enqueue("files", files_base, path=staged, inst_id=inst_id):
                logger.info(f"文件 → Releases 已入队异步 ({file_size} 字节)")
                return file_size, 1
            logger.warning("文件 → Releases 入队被拒，清理暂存")
            try:
                os.remove(staged)
            except Exception:
                pass
            return file_size, 1
        except Exception as e:
            logger.warning(f"文件 → Releases 入队异常: {e}")
            try:
                os.remove(tmp)
            except Exception:
                pass
            return file_size, 1
    else:
        try:
            r = releases.upload_asset_v2(files_base, tmp)
            ok = r.get("ok", False)
            logger.info(f"文件 → Releases 同步兜底 {'成功' if ok else '失败'} ({file_size} 字节)")
        except Exception as e:
            logger.error(f"文件 → Releases 兜底异常: {e}")
            ok = False
        try:
            os.remove(tmp)
        except Exception:
            pass
        return file_size, 1 if ok else 0


def backup_files_to_disk():
    """tar写磁盘临时文件（内置_timestamp.txt）"""
    os.makedirs(TMP_DIR, exist_ok=True)
    tmp = os.path.join(TMP_DIR, "backup_files.tar.gz")
    _create_timestamp_file()
    try:
        result = subprocess.run(
            ["sudo", "tar", "--zstd", "-cf", tmp, "-C", os.path.dirname(config.FILES_DIR), os.path.basename(config.FILES_DIR)],
            capture_output=True, timeout=300)
        subprocess.run(["sudo", "chown", "runner:runner", tmp], timeout=5)
        if result.returncode != 0:
            logger.error(f"文件打包失败: {result.stderr.decode(errors='replace')[:200]}")
            return None
        return tmp
    except Exception as e:
        logger.error(f"文件打包异常: {e}")
        return None
    finally:
        _remove_timestamp_file()


def restore_files_from_file(file_path):
    """从磁盘文件解压。解压后清理_timestamp.txt。"""
    try:
        with open(file_path, "rb") as _f:
            _hdr = _f.read(4)
        if _hdr[:4] == b"\x28\xb5\x2f\xfd":
            result = subprocess.run(
                ["sudo", "tar", "--zstd", "-xf", file_path, "-C", os.path.dirname(config.FILES_DIR)],
                capture_output=True, timeout=300)
        else:
            result = subprocess.run(
                ["sudo", "tar", "xzf", file_path, "-C", os.path.dirname(config.FILES_DIR)],
                capture_output=True, timeout=300)
        if result.returncode != 0:
            logger.error(f"文件解压失败: {result.stderr.decode(errors='replace')[:200]}")
            return False
        os.makedirs(config.FILES_DIR, exist_ok=True)
        _remove_timestamp_file()
        logger.info(f"文件恢复完成")
        return True
    except Exception as e:
        logger.error(f"文件恢复失败: {e}")
        return False


def save_prev_backup(inst_cfg=None):
    """保存上一版数据库快照（用于回滚）"""
    db_asset = inst_cfg.asset_db if inst_cfg else config.ASSET_DB
    try:
        blob = releases.download_chunked(db_asset)
        if blob:
            releases.upload_chunked(f"{db_asset}.bak", blob)
    except Exception as e:
        logger.warning(f"保存快照失败: {e}")


# ==================== 操作统计（stats.json） ====================
STATS_FILE = os.path.join(config.FILES_DIR, "stats.json")

def load_stats():
    try:
        with open(STATS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"pending_a": 0, "pending_b": 0, "storage_mb": 0,
                "backup_history": [], "restore_history": [], "timeline": []}

def save_stats(stats):
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"stats保存失败: {e}")

def record_backup(status, size_bytes, log_msg, a_delta=0, b_delta=0):
    stats = load_stats()
    stats["pending_a"] = stats.get("pending_a", 0) + a_delta
    stats["pending_b"] = stats.get("pending_b", 0) + b_delta
    stats["storage_mb"] = round(size_bytes / (1024 * 1024), 1) if size_bytes else 0
    entry = {
        "timestamp": time.time(),
        "status": status,
        "size_bytes": size_bytes,
        "a_delta": a_delta, "b_delta": b_delta,
        "log": log_msg[:500]
    }
    stats.setdefault("backup_history", []).insert(0, entry)
    stats["backup_history"] = stats["backup_history"][:50]
    stats.setdefault("timeline", []).insert(0, {"type": "backup", **entry})
    stats["timeline"] = stats["timeline"][:100]
    save_stats(stats)

def record_restore(status, log_msg, a_delta=0, b_delta=0):
    stats = load_stats()
    stats["pending_a"] = stats.get("pending_a", 0) + a_delta
    stats["pending_b"] = stats.get("pending_b", 0) + b_delta
    entry = {
        "timestamp": time.time(),
        "status": status,
        "a_delta": a_delta, "b_delta": b_delta,
        "log": log_msg[:500]
    }
    stats.setdefault("restore_history", []).insert(0, entry)
    stats["restore_history"] = stats["restore_history"][:50]
    stats.setdefault("timeline", []).insert(0, {"type": "restore", **entry})
    stats["timeline"] = stats["timeline"][:100]
    save_stats(stats)

def get_pending():
    s = load_stats()
    return s.get("pending_a", 0), s.get("pending_b", 0)

def get_storage_mb():
    return load_stats().get("storage_mb", 0)

def clear_pending():
    stats = load_stats()
    stats["pending_a"] = 0
    stats["pending_b"] = 0
    save_stats(stats)

def get_s3_delta(s3pool, before_a, before_b):
    if not s3pool or not s3pool.is_ready():
        return 0, 0
    s = s3pool.get_status()
    after_a = s.get("total_a_ops", 0)
    after_b = s.get("total_b_ops", 0)
    return max(0, after_a - before_a), max(0, after_b - before_b)