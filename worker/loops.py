# -*- coding: utf-8 -*-
"""
Worker 后台循环

修复：
1. _worker_pre_wake 从 S3 读实例配置（旧项目只从 Releases 读）
2. _report_running 用 time.time()（旧项目格式不一致）
3. _backup_loop 统一（去掉 persistence.py 的重复 backup_loop）
"""
import os
import time
import json
import threading
import subprocess
import urllib.request

import config
import log
from core import ghapi, releases
from core import status as core_status
from worker import state, persistence

logger = log.setup_logger("loops")


def start_loops():
    """启动所有后台循环"""
    if state.leader and state.leader.is_leader:
        threading.Thread(target=_backup_loop, daemon=True).start()
    threading.Thread(target=_report_running, daemon=True).start()
    threading.Thread(target=_worker_pre_wake, daemon=True).start()
    threading.Thread(target=_auto_update_loop, daemon=True).start()
    threading.Thread(target=_disk_monitor_loop, daemon=True).start()
    logger.info("[loops] 后台循环已启动")


def _backup_loop():
    """周期备份（数据库 + 文件 + 进程快照 + 记录stats）"""
    while True:
        time.sleep(config.BACKUP_INTERVAL)
        if state.leader and not state.leader.is_leader:
            continue
        try:
            _ba, _bb = 0, 0
            if state.s3pool and state.s3pool.is_ready():
                _s = state.s3pool.get_status()
                _ba, _bb = _s.get("total_a_ops", 0), _s.get("total_b_ops", 0)
            db_size, _ = persistence.backup_database(state.inst_cfg)
            res = persistence.backup_files(state.inst_cfg)
            if state.proc_mgr:
                state.proc_mgr.snapshot(reason="periodic")
            _aa, _ab = _ba, _bb
            if state.s3pool and state.s3pool.is_ready():
                _s = state.s3pool.get_status()
                _aa, _ab = _s.get("total_a_ops", 0), _s.get("total_b_ops", 0)
            _da, _db = max(0, _aa - _ba), max(0, _ab - _bb)
            _size = int(db_size or 0) + int((res[0] if res else 0))
            _files_mb = round((res[0] if res else 0) / 1048576, 1)
            persistence.record_backup("success", _size,
                f"auto: db={db_size}B files={_files_mb}MB", _da, _db)
        except Exception as e:
            persistence.record_backup("failed", 0, str(e), 0, 0)
            logger.error(f"[backup] 失败: {e}")


def _report_running():
    """周期向 manager 上报（每60秒，携带S3摘要+进程数+磁盘）"""
    mgr = config.MANAGER_HOST or "ghvps2.kekeke.cc.cd"
    while True:
        try:
            # 收集S3摘要 + pending
            s3_summary = {"active": 0, "degraded": 0, "unavailable": 0}
            if state.s3pool and state.s3pool.is_ready():
                s3_summary = state.s3pool.get_health()
                s3_summary.pop("ready", None)
                s3_summary.pop("total", None)
            # 从stats.json获取pending、存储用量和备份历史
            from worker import persistence
            _stats = persistence.load_stats()
            _pa, _pb = _stats.get("pending_a", 0), _stats.get("pending_b", 0)
            s3_summary["a_ops"] = _pa
            s3_summary["b_ops"] = _pb
            s3_summary["storage_mb"] = _stats.get("storage_mb", 0)
            s3_summary["backup_history"] = _stats.get("backup_history", [])
            # 收集进程数
            proc_count = 0
            if state.proc_mgr:
                try:
                    procs = state.proc_mgr.list_processes()
                    proc_count = len(procs)
                except Exception as e:
                    logger.debug(f"[loops] S3摘要失败: {e}")
            # 收集磁盘
            disk_pct = 0
            try:
                stats = log.get_resource_stats()
                disk_pct = round(stats.get("disk_use_pct", 0), 1)
            except Exception as e:
                logger.debug(f"[loops] 进程数统计失败: {e}")
            url = f"https://{mgr}/api/instances/{config.INSTANCE_ID}/report"
            payload = json.dumps({
                "token": config.EXEC_TOKEN,
                "url": f"https://{state.inst_cfg.tunnel_host}" if state.inst_cfg else "",
                "s3": s3_summary,
                "procs": proc_count,
                "disk_pct": disk_pct,
            }).encode()
            req = urllib.request.Request(url, data=payload, headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (ghbox-worker)"})
            urllib.request.urlopen(req, timeout=20)
            # 上报成功，清零pending
            from worker import persistence
            persistence.clear_pending()
        except Exception as e:
            logger.warning(f"[report] 上报失败: {e}")
        time.sleep(60)


def _worker_pre_wake():
    """续命：到期前备份 + 预触发下一个 worker"""
    done = False
    while True:
        elapsed = core_status.elapsed()
        if elapsed >= config.PRE_WAKE_SECONDS and not done:
            done = True
            # 修复：先从 S3 读，再从 Releases 读
            cfg = _load_inst_cfg()
            if cfg is None:
                logger.info(f"[prewake] 实例 {config.INSTANCE_ID} 已关闭，不续命")
                return
            try:
                # 强制备份
                try:
                    if state.proc_mgr:
                        state.proc_mgr.final_snapshot()
                    db_size, _ = persistence.backup_database(state.inst_cfg)
                    res = persistence.backup_files(state.inst_cfg)
                    _size = int(db_size or 0) + int((res[0] if res else 0))
                    _files_mb = round((res[0] if res else 0) / 1048576, 1)
                    persistence.record_backup("prewake", _size,
                        f"prewake: db={db_size}B files={_files_mb}MB", 0, 0)
                    logger.info("[prewake] 强制备份完成")
                except Exception as e:
                    persistence.record_backup("failed", 0, f"prewake: {e}", 0, 0)
                    logger.error(f"[prewake] 备份失败: {e}")
                # 触发下一个 worker
                url = (f"{ghapi.API_BASE}/repos/{config.REPO}/actions/workflows/"
                       f"{config.WORKER_WORKFLOW}/dispatches")
                ghapi.gh_request("POST", url,
                                 data={"ref": "main",
                                       "inputs": {"INSTANCE_ID": config.INSTANCE_ID}})
                logger.info(f"[prewake] 已预触发 ({elapsed}s)")
            except Exception as e:
                logger.error(f"[prewake] 触发失败: {e}")
            break
        time.sleep(60)


def _load_inst_cfg():
    """读取实例配置。S3 优先，Releases 降级。"""
    if state.s3pool and state.s3pool.is_ready():
        data = state.s3pool.get_meta_json(
            f"meta/inst-config/{config.INSTANCE_ID}.json", default=None)
        if data:
            return data
    return releases.load_json_enc(f"inst-{config.INSTANCE_ID}.json.enc", default=None)


def _auto_update_loop():
    """自动更新：检测主仓库新版本 → 同步 fork → 重启"""
    sha = config.CURRENT_SHA
    if not sha:
        return
    while True:
        time.sleep(300)
        try:
            url = f"{ghapi.API_BASE}/repos/{config.MAIN_REPO}/commits/main"
            _, d = ghapi.gh_request("GET", url)
            latest = d.get("sha", "")
            if latest and latest != sha:
                logger.info(f"[update] 新版本 {latest[:10]}，同步 fork + 重启")
                try:
                    url2 = f"{ghapi.API_BASE}/repos/{config.REPO}/git/refs/heads/main"
                    ghapi.gh_request("PATCH", url2, token=config.GH_TOKEN,
                                     data={"sha": latest, "force": True})
                    time.sleep(3)
                except Exception as e:
                    logger.error(f"[update] fork 同步失败: {e}")
                url3 = (f"{ghapi.API_BASE}/repos/{config.REPO}/actions/workflows/"
                        f"{config.WORKER_WORKFLOW}/dispatches")
                status, _ = ghapi.gh_request("POST", url3,
                    data={"ref": "main", "inputs": {"INSTANCE_ID": config.INSTANCE_ID}})
                if status in (200, 204):
                    time.sleep(60)
                    os._exit(0)
                else:
                    logger.error(f"[update] 触发失败({status})，继续运行")
        except Exception as e:
            logger.error(f"[update] 检查失败: {e}")


def _disk_monitor_loop():
    """磁盘监控：超阈值清理临时文件"""
    while True:
        time.sleep(config.DISK_CHECK_INTERVAL)
        try:
            stats = log.get_resource_stats()
            pct = stats.get("disk_use_pct", 0)
            if pct >= config.DISK_CLEAN_TRIGGER_PERCENT:
                logger.warning(f"[disk] {pct}% 超阈值，清理")
                for d in ("/tmp", os.path.join(config.FILES_DIR, ".tmp")):
                    if os.path.isdir(d):
                        for item in os.listdir(d):
                            try:
                                p = os.path.join(d, item)
                                if os.path.isdir(p):
                                    subprocess.run(["sudo", "rm", "-rf", p], timeout=30)
                                else:
                                    os.remove(p)
                            except Exception as e:
                                logger.debug(f"[loops] 磁盘清理失败: {e}")
                stats2 = log.get_resource_stats()
                logger.info(f"[disk] 清理后 {stats2.get('disk_use_pct', 0)}%")
            elif pct >= config.DISK_WARN_PERCENT:
                logger.warning(f"[disk] {pct}%，注意空间")
        except Exception as e:
            logger.error(f"[disk] 监控异常: {e}")
