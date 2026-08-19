# -*- coding: utf-8 -*-
"""
Worker 后台循环

修复：
1. _worker_pre_wake 从 S3 读实例配置（旧项目只从 Releases 读）
2. _report_running 用 time.time()（旧项目格式不一致）
3. _backup_loop 统一（去掉 persistence.py 的重复 backup_loop）
4. _report_running 增加关闭检测：上报收到 404（墓碑拒绝自愈）连续3次
   主动退出释放配额。404 是明确的关闭信号（只有墓碑拒绝才返回），
   网络错误/超时/其他状态码不会计数，配置误删也不会触发（实例仍在上报200）。
"""
import os
import time
import json
import threading
import subprocess
import urllib.request
import urllib.error

import config
import log
from core import ghapi, releases
from core import status as core_status
from worker import state, persistence

logger = log.setup_logger("loops")

# 关闭检测阈值：上报连续被拒（404）次数达到该值则退出
REJECT_THRESHOLD = 3


def start_loops():
    """启动所有后台循环"""
    if state.leader and state.leader.is_leader:
        threading.Thread(target=_backup_loop, daemon=True).start()
    threading.Thread(target=_report_running, daemon=True).start()
    threading.Thread(target=_worker_pre_wake, daemon=True).start()
    threading.Thread(target=_auto_update_loop, daemon=True).start()
    threading.Thread(target=_disk_monitor_loop, daemon=True).start()
    threading.Thread(target=_releases_cleanup_loop, daemon=True).start()
    logger.info("后台循环已启动")


def _backup_loop():
    """周期备份（数据库 + 文件 + 进程快照 + 记录stats）"""
    while True:
        time.sleep(config.BACKUP_INTERVAL)
        if state.shutting_down:
            return
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
            logger.error(f"失败: {e}")


def _report_running():
    """周期向 manager 上报（每60秒，携带S3摘要+进程数+磁盘）

    关闭检测：上报收到 404 表示实例已被关闭（墓碑拒绝自愈），
    连续 REJECT_THRESHOLD 次（约3分钟）主动退出释放 GitHub 配额。
    只对 404 计数；网络错误/超时/其他状态码不计数（防误杀）。
    """
    mgr = config.MANAGER_HOST or "ghvps2.kekeke.cc.cd"
    rejected_count = 0
    while True:
        if state.shutting_down:
            logger.info("关闭中，停止上报")
            return
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
            # 上报 S3Pool 累积统计（含恢复时的 B类操作，pending 只记备份增量）
            if state.s3pool and state.s3pool.is_ready():
                _s3status = state.s3pool.get_status()
                s3_summary["a_ops_total"] = _s3status.get("total_a_ops", 0)
                s3_summary["b_ops_total"] = _s3status.get("total_b_ops", 0)
            s3_summary["storage_mb"] = _stats.get("storage_mb", 0)
            s3_summary["backup_history"] = _stats.get("backup_history", [])
            # 收集进程数
            proc_count = 0
            if state.proc_mgr:
                try:
                    procs = state.proc_mgr.list_processes()
                    proc_count = len(procs)
                except Exception as e:
                    logger.debug(f"S3摘要失败: {e}")
            # 收集磁盘
            disk_pct = 0
            try:
                stats = log.get_resource_stats()
                disk_pct = round(stats.get("disk_use_pct", 0), 1)
            except Exception as e:
                logger.debug(f"进程数统计失败: {e}")
            url = f"https://{mgr}/api/instances/{config.INSTANCE_ID}/report"
            payload = json.dumps({
                "token": config.EXEC_TOKEN,
                "url": f"https://{state.inst_cfg.tunnel_host}" if state.inst_cfg else "",
                "s3": s3_summary,
                "procs": proc_count,
                "disk_pct": disk_pct,
                "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            }).encode()
            req = urllib.request.Request(url, data=payload, headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (ghbox-worker)"})
            try:
                urllib.request.urlopen(req, timeout=20)
                # 上报成功，清零pending + 重置拒绝计数
                from worker import persistence
                persistence.clear_pending()
                if rejected_count > 0:
                    logger.info(f"上报恢复成功（此前被拒{rejected_count}次）")
                rejected_count = 0
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    # 404 = 实例已关闭（墓碑拒绝自愈），连续多次确认后退出
                    rejected_count += 1
                    logger.warning(f"上报被拒(404) 第{rejected_count}/{REJECT_THRESHOLD}次，实例可能已关闭")
                    if rejected_count >= REJECT_THRESHOLD:
                        logger.warning(f"连续{REJECT_THRESHOLD}次被拒，确认实例已关闭，主动退出释放配额")
                        os._exit(0)
                else:
                    # 其他HTTP错误不计数（可能是manager临时故障）
                    logger.warning(f"上报失败: HTTP {e.code}")
                    rejected_count = 0
            except Exception as e:
                # 网络错误/超时不计数（防误杀）
                logger.warning(f"上报失败: {e}")
                rejected_count = 0
        except Exception as e:
            logger.warning(f"上报失败: {e}")
            rejected_count = 0
        time.sleep(60)


def _worker_pre_wake():
    """续命：到期前备份 + 预触发下一个 worker"""
    done = False
    while True:
        if state.shutting_down:
            return
        elapsed = core_status.elapsed()
        if elapsed >= config.PRE_WAKE_SECONDS and not done:
            done = True
            # 修复：先从 S3 读，再从 Releases 读
            cfg = _load_inst_cfg()
            if cfg is None:
                logger.info(f"实例 {config.INSTANCE_ID} 已关闭，不续命")
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
                    logger.info("强制备份完成")
                except Exception as e:
                    persistence.record_backup("failed", 0, f"prewake: {e}", 0, 0)
                    logger.error(f"备份失败: {e}")
                # 触发下一个 worker（重试3次，避免续命失败导致实例到期终止）
                url = (f"{ghapi.API_BASE}/repos/{config.REPO}/actions/workflows/"
                       f"{config.WORKER_WORKFLOW}/dispatches")
                _triggered = False
                for _attempt in range(3):
                    try:
                        _st, _ = ghapi.gh_request("POST", url,
                                                  data={"ref": "main",
                                                        "inputs": {"INSTANCE_ID": config.INSTANCE_ID}})
                        if _st in (200, 204):
                            _triggered = True
                            break
                    except Exception as e:
                        logger.warning(f"续命触发第{_attempt+1}次失败: {e}")
                    time.sleep(3)
                if _triggered:
                    logger.info(f"已预触发 ({elapsed}s)")
                else:
                    logger.error("续命触发失败(3次重试)，实例将到期终止")
            except Exception as e:
                logger.error(f"触发失败: {e}")
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
    """自动更新：检测主仓库新版本 → 更新前全量备份 → merge-upstream 同步 fork
    → 验证 fork 指向 → 触发新 run → 本实例退出

    任何一步失败都放弃本次更新并继续运行（下轮重试），不盲冒风险。
    """
    sha = config.CURRENT_SHA
    if not sha:
        return
    updated = False
    while not state.shutting_down and not updated:
        time.sleep(300)
        try:
            # 1) 检测主仓库新版本
            status, d = ghapi.gh_request(
                "GET", f"{ghapi.API_BASE}/repos/{config.MAIN_REPO}/commits/main")
            if status != 200 or not d:
                continue
            latest = d.get("sha", "")
            if not latest or latest == sha:
                continue
            logger.info(f"检测到新版本 {latest[:10]}（当前 {sha[:10]}）")

            # 2) 更新前全量备份 + flush Releases 异步队列
            try:
                from worker import persistence, upload_queue
                if state.proc_mgr:
                    state.proc_mgr.final_snapshot()
                else:
                    persistence.backup_database(state.inst_cfg)
                    persistence.backup_files(state.inst_cfg)
                upload_queue.stop_queue(flush=True)
                logger.info("更新前备份完成，Releases 队列已 flush")
            except Exception as e:
                logger.warning(f"更新前备份异常（继续尝试更新）: {e}")

            # 3) merge-upstream 同步 fork（PATCH refs 必坏：fork 无主仓 commit 对象）
            m_status, m_d = ghapi.gh_request(
                "POST", f"{ghapi.API_BASE}/repos/{config.REPO}/merge-upstream",
                data={"branch": "main"}, token=config.GH_TOKEN)
            if m_status != 200:
                logger.error(f"fork 同步失败({m_status})，放弃本次更新")
                continue
            time.sleep(3)

            # 4) 验证 fork main 已指向新版本（防止用旧代码白重启）
            v_status, v_d = ghapi.gh_request(
                "GET", f"{ghapi.API_BASE}/repos/{config.REPO}/git/refs/heads/main",
                token=config.GH_TOKEN)
            fork_sha = ""
            if v_status == 200 and isinstance(v_d, dict):
                fork_sha = (v_d.get("object") or {}).get("sha", "") or ""
            if not fork_sha or fork_sha != latest:
                logger.error(f"fork 验证失败(fork={fork_sha[:10]} latest={latest[:10]})，放弃本次更新")
                continue

            # 5) 触发新 run（concurrency cancel-in-progress 会接管）
            d_url = (f"{ghapi.API_BASE}/repos/{config.REPO}/actions/workflows/"
                     f"{config.WORKER_WORKFLOW}/dispatches")
            d_status, _ = ghapi.gh_request("POST", d_url,
                data={"ref": "main", "inputs": {"INSTANCE_ID": config.INSTANCE_ID}},
                token=config.GH_TOKEN)
            if d_status not in (200, 204):
                logger.error(f"触发新 run 失败({d_status})，继续运行")
                continue
            logger.info(f"新 run 已触发，{60}s 后本实例退出")
            time.sleep(60)
            updated = True
        except Exception as e:
            logger.error(f"自动更新异常: {e}")
            time.sleep(60)

    if updated:
        # 退出前再 flush 一次（保证待传不丢）
        try:
            from worker import upload_queue
            upload_queue.stop_queue(flush=True)
        except Exception:
            pass
        os._exit(0)


def _disk_monitor_loop():
    """磁盘监控：超阈值清理临时文件"""
    while True:
        if state.shutting_down:
            return
        time.sleep(config.DISK_CHECK_INTERVAL)
        try:
            stats = log.get_resource_stats()
            pct = stats.get("disk_use_pct", 0)
            if pct >= config.DISK_CLEAN_TRIGGER_PERCENT:
                logger.warning(f"{pct}% 超阈值，清理")
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
                                logger.debug(f"磁盘清理失败: {e}")
                stats2 = log.get_resource_stats()
                logger.info(f"清理后 {stats2.get('disk_use_pct', 0)}%")
            elif pct >= config.DISK_WARN_PERCENT:
                logger.warning(f"{pct}%，注意空间")
        except Exception as e:
            logger.error(f"监控异常: {e}")


def _releases_cleanup_loop():
    """Releases 旧版本低频清理（每小时一次，保留最近 N 个版本）

    版本化命名不删旧，避免每次备份的 delete 配额；旧版本定期清理防仓库膨胀。
    每个删除消耗 2 配额，低频可接受。
    """
    while True:
        time.sleep(3600)
        if state.shutting_down:
            return
        try:
            if not state.inst_cfg:
                continue
            inst_id = state.inst_cfg.instance_id
            bases = [
                f"inst-{inst_id}.db",
                f"inst-{inst_id}.files.tar.gz",
            ]
            for base in bases:
                try:
                    removed = releases.cleanup_old_versions(
                        base, keep=releases.VERSION_KEEP, older_than=3600,
                        token=config.GH_TOKEN, repo=config.MAIN_REPO or config.REPO)
                    if removed:
                        logger.info(f"Releases 清理 {base}: 删除 {len(removed)} 个旧版本")
                except Exception as e:
                    logger.warning(f"Releases 清理 {base} 失败: {e}")
        except Exception as e:
            logger.error(f"Releases 清理循环异常: {e}")