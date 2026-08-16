#!/usr/bin/env python3
"""
验证 purge_instance_data 清理逻辑
在inst3上执行，清理inst1和inst2的残留S3+Releases数据
"""
import sys
import os
import json

# 设置代码路径
CODE_DIR = "/home/runner/work/ci-runner/ci-runner"
sys.path.insert(0, CODE_DIR)
os.chdir(CODE_DIR)

import config
import log
from core.s3 import S3Pool
from core import releases

log.setup_logger("purge")
logger = log.setup_logger("purge")

# 初始化S3Pool
bootstrap = os.environ.get("S3_BOOTSTRAP", "")
if not bootstrap:
    # 从环境变量文件读取
    for line in os.environ.get("GITHUB_ENV", "").split("\n"):
        if line.startswith("S3_BOOTSTRAP="):
            bootstrap = line.split("=", 1)[1]
            break

if not bootstrap:
    print("ERROR: S3_BOOTSTRAP 未找到")
    sys.exit(1)

print(f"S3_BOOTSTRAP: {bootstrap[:20]}...")
pool = S3Pool(bootstrap, config.S3_ENDPOINT, config.S3_REGION, owner="purge-test")
if not pool.init():
    print("ERROR: S3初始化失败")
    sys.exit(1)
print(f"S3初始化成功，{len(pool._accounts)}个账号")
print(f"清理前总存储: {pool.get_status()['total_storage_mb']:.1f}MB")

# 要清理的实例
TARGETS = ["inst1", "inst2"]

def purge_s3(pool, inst_id):
    """清理S3上的实例数据"""
    prefixes = [
        f"inst-data/{inst_id}",
        f"inst-files/{inst_id}",
        f"inst-proc/{inst_id}",
    ]
    deleted = 0
    for prefix in prefixes:
        # 查manifest
        manifest_data = None
        try:
            manifest_data = pool.get(f"{prefix}.manifest")
        except Exception:
            pass

        if manifest_data:
            try:
                m = json.loads(manifest_data)
                num_chunks = m.get("chunks", 0)
                locations = m.get("locations", [])
                print(f"  {prefix}: manifest找到, {num_chunks}块分片, {len(locations)}个位置")
                # 按locations精确删除
                for loc in locations:
                    chunk_idx = loc.get("chunk", 0)
                    chunk_key = f"{prefix}.chunk{chunk_idx}"
                    try:
                        if pool.delete(chunk_key):
                            deleted += 1
                            print(f"    删除分片 {chunk_key}")
                    except Exception as e:
                        print(f"    删除 {chunk_key} 失败: {e}")
                # 兜底
                if len(locations) < num_chunks:
                    for i in range(num_chunks):
                        try:
                            if pool.delete(f"{prefix}.chunk{i}"):
                                deleted += 1
                        except Exception:
                            pass
                # 删manifest
                try:
                    if pool.delete(f"{prefix}.manifest"):
                        deleted += 1
                        print(f"    删除manifest {prefix}.manifest")
                except Exception as e:
                    print(f"    删除manifest失败: {e}")
            except Exception as e:
                print(f"  {prefix}: 解析manifest失败: {e}")

        # 删单文件版本
        try:
            if pool.delete(prefix):
                deleted += 1
                print(f"  删除单文件 {prefix}")
        except Exception:
            pass

    return deleted

def purge_releases(inst_id):
    """清理Releases上的实例数据"""
    assets = [
        f"inst-{inst_id}.db.enc",
        f"inst-{inst_id}.files.tar.gz.enc",
        f"inst-{inst_id}.processes.tar.gz.enc",
        f"inst-{inst_id}.json.enc",
    ]
    deleted = 0
    for asset in assets:
        # 查分片manifest
        try:
            manifest_blob = releases.download_asset(f"{asset}.manifest")
            if manifest_blob:
                m = json.loads(manifest_blob.decode())
                parts = m.get("parts", 0)
                print(f"  {asset}: 分片manifest, {parts}片")
                for i in range(parts):
                    try:
                        releases.delete_asset(f"{asset}.part{i}")
                        deleted += 1
                    except Exception:
                        pass
        except Exception:
            pass
        # 删manifest
        try:
            releases.delete_asset(f"{asset}.manifest")
        except Exception:
            pass
        # 删单文件
        try:
            releases.delete_asset(asset)
            deleted += 1
            print(f"  删除 {asset}")
        except Exception as e:
            print(f"  删除 {asset} 失败: {e}")

    return deleted

# 执行清理
total_deleted = 0
for inst_id in TARGETS:
    print(f"\n===== 清理 {inst_id} =====")
    s3_del = purge_s3(pool, inst_id)
    print(f"  S3删除: {s3_del}个对象")
    rel_del = purge_releases(inst_id)
    print(f"  Releases删除: {rel_del}个资产")
    total_deleted += s3_del + rel_del

print(f"\n===== 清理完成 =====")
print(f"总共删除: {total_deleted}个对象")
# 刷新used_bytes
pool._refresh_storage_sizes()
status = pool.get_status()
print(f"清理后总存储: {status['total_storage_mb']:.1f}MB")
print(f"活跃账号: {status['active_accounts']}")
print(f"不可用账号: {status['unavailable_accounts']}")
