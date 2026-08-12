# -*- coding: utf-8 -*-
"""
ProcessManager 门面（聚合 scanner/config/backup/restore）

修复：用 S3Pool 代替 Turso 做上传/下载。
"""
import os
import time
import threading

import config
import log
from core import utils, crypto
from worker.process import scanner
from worker.process import tunnels
from worker.process import config as pconfig
from worker.process import backup as pbackup
from worker.process import restore as prestore

logger = log.setup_logger("process")


class ProcessManager:
    def __init__(self, inst_cfg=None, s3pool=None):
        self.inst_cfg = inst_cfg
        self.s3pool = s3pool
        self.known = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        os.makedirs(config.PROC_DIR, exist_ok=True)
        os.makedirs(config.LOGS_DIR, exist_ok=True)

    def snapshot(self, reason="periodic"):
        saved, meta = pbackup.snapshot(reason=reason)
        with self._lock:
            for name, m in meta.items():
                old_entry = self.known.get(name, {})
                self.known[name] = {
                    "name": name, "pid": m.get("pid"), "status": "running",
                    "config": pconfig.load_proc_config(name),
                    "started_at": m.get("saved_at"),
                    "tunnel_pids": old_entry.get("tunnel_pids", {}),
                }
        try:
            self._upload_snapshot()
        except Exception as e:
            logger.error(f"[process] 快照上传失败: {e}")
        return saved

    def _upload_snapshot(self):
        """上传进程快照。S3 分片优先，Releases 降级。"""
        if not self.inst_cfg:
            return
        from core import releases
        import os
        inst_id = self.inst_cfg.instance_id
        key = f"inst-proc/{inst_id}/proc.tar.gz"
        tmp = pbackup.pack_processes_to_disk()
        if not tmp:
            return
        file_size = os.path.getsize(tmp)
        # S3（分片上传）
        if self.s3pool and self.s3pool.is_ready():
            try:
                self.s3pool.put_file(key, tmp)
                logger.info(f"[process] 快照已存入 S3 ({file_size} 字节)")
            except Exception as e:
                logger.warning(f"[process] S3 快照失败: {e}")
        # Releases（<50MB双写）
        if file_size < 50 * 1024 * 1024:
            with open(tmp, "rb") as f:
                data = f.read()
            asset = f"inst-{inst_id}.processes.tar.gz.enc"
            releases.upload_chunked(asset, data)
            logger.info(f"[process] 快照已存入 Releases ({file_size} 字节)")
        try:
            os.remove(tmp)
        except Exception:
            pass

    def _download_snapshot(self):
        """下载进程快照。S3 分片优先，Releases 降级。"""
        if not self.inst_cfg:
            return
        from core import releases
        import os
        inst_id = self.inst_cfg.instance_id
        key = f"inst-proc/{inst_id}/proc.tar.gz"
        tmp = os.path.join("/tmp/ghbox_backup", "proc_restore.tar.gz")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        # S3（分片下载到磁盘）
        if self.s3pool and self.s3pool.is_ready():
            try:
                if self.s3pool.get_to_file(key, tmp):
                    pbackup.unpack_processes_from_file(tmp)
                    logger.info(f"[process] 快照从 S3 恢复")
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                    return
            except Exception as e:
                logger.warning(f"[process] S3 快照下载失败: {e}")
        # Releases 降级
        asset = f"inst-{inst_id}.processes.tar.gz.enc"
        data = releases.download_chunked(asset)
        if data:
            with open(tmp, "wb") as f:
                f.write(data)
            pbackup.unpack_processes_from_file(tmp)
            try:
                os.remove(tmp)
            except Exception:
                pass
            logger.info(f"[process] 快照从 Releases 恢复")

    def final_snapshot(self):
        logger.info("[process] 最终快照")
        try:
            self.snapshot(reason="final")
        except Exception as e:
            logger.error(f"[process] 最终快照失败: {e}")

    def restore_all(self):
        try:
            self._download_snapshot()
        except Exception as e:
            logger.warning(f"[process] 快照下载失败: {e}")
        restored, failed = prestore.restore_all()
        with self._lock:
            for name in pconfig.load_manifest():
                cfg = pconfig.load_proc_config(name)
                if cfg is None:
                    continue
                self.known[name] = {
                    "name": name, "status": "running", "config": cfg,
                    "started_at": time.time(),
                }
                pid = self._find_pid_by_cmd(cfg)
                if pid:
                    self.known[name]["pid"] = pid
                tunnels.start_tunnels(cfg, self.known[name], name)
        return restored, failed

    def _find_pid_by_cmd(self, cfg):
        cmd = cfg.get("command") or ""
        if not cmd:
            return None
        for proc in scanner.scan_user_processes():
            if proc.cmdline_str() == cmd:
                return proc.pid
        return None

    def start(self, name):
        ok, pid = prestore.start_process(name)
        if ok:
            cfg = pconfig.load_proc_config(name) or {}
            with self._lock:
                self.known[name] = {
                    "name": name, "pid": pid, "status": "running",
                    "config": cfg, "started_at": time.time(),
                }
            tunnels.start_tunnels(cfg, self.known[name], name)
        return ok

    def stop(self, name):
        with self._lock:
            entry = self.known.get(name) or {}
            pid = entry.get("pid")
        tunnels.stop_tunnels(entry)
        ok, msg = prestore.stop_process(name, pid=pid)
        if ok:
            with self._lock:
                self.known.pop(name, None)
        return ok, msg

    def restart(self, name):
        self.stop(name)
        time.sleep(1)
        return self.start(name)

    def list_processes(self):
        procs = pconfig.load_manifest()
        if not procs:
            # manifest为空，从processes目录扫描ghvps.json
            import os
            proc_dir = pconfig.proc_dir()
            if os.path.isdir(proc_dir):
                for name in os.listdir(proc_dir):
                    if name == "manifest.json":
                        continue
                    ghvps = os.path.join(proc_dir, name, "ghvps.json")
                    if os.path.exists(ghvps):
                        procs[name] = {}
        result = []
        for name, meta in procs.items():
            cfg = pconfig.load_proc_config(name) or {}
            pid = None
            with self._lock:
                entry = self.known.get(name, {})
                pid = entry.get("pid")
            running = bool(pid and utils.is_alive(pid))
            # known没有PID时实时扫描/proc
            if not running:
                cmd = cfg.get("command") or ""
                if cmd:
                    _scanned = scanner.scan_user_processes()
                    logger.info(f"[list] {name}: known无PID, 实时扫描{len(_scanned)}个进程, cmd={cmd[:40]}")
                    for proc in _scanned:
                        if proc.cmdline_str() == cmd:
                            pid = proc.pid
                            running = True
                            with self._lock:
                                if name not in self.known:
                                    self.known[name] = {}
                                self.known[name]["pid"] = pid
                                self.known[name]["config"] = cfg
                            break
            item = {
                "name": name,
                "cmdline": meta.get("cmdline", cfg.get("command", "")),
                "cwd": meta.get("cwd", cfg.get("cwd", "")),
                "size_mb": meta.get("size_mb", 0),
                "files_backed": meta.get("files_backed", True),
                "pid": pid, "running": running,
                "auto_restart": cfg.get("auto_restart", True),
                "saved_at": meta.get("saved_at", cfg.get("saved_at")),
                "tunnels": tunnels.get_tunnel_status(cfg, entry),
            }
            result.append(item)
        return result

    def get_process_log(self, name, limit=200):
        return log.read_process_log(name, limit=limit)

    def monitor_loop(self):
        while not self._stop.is_set():
            try:
                self.snapshot(reason="periodic")
                self._recover_crashed()
                self._check_tunnels()
            except Exception as e:
                logger.error(f"[process] 监控异常: {e}")
            self._stop.wait(config.PROC_SCAN_INTERVAL)

    def _recover_crashed(self):
        with self._lock:
            names = list(self.known.keys())
        for name in names:
            with self._lock:
                entry = self.known.get(name)
                if not entry:
                    continue
                pid = entry.get("pid")
                auto = (entry.get("config") or {}).get("auto_restart", True)
            if not auto:
                continue
            if pid and not utils.is_alive(pid):
                logger.warning(f"[restore] {name} 崩溃(pid={pid})，重启")
                delay = (entry.get("config") or {}).get("restart_delay", 3)
                time.sleep(delay)
                self.start(name)

    def start_monitor(self):
        threading.Thread(target=self.monitor_loop, daemon=True).start()
        logger.info("[process] 监控已启动")


    def _check_tunnels(self):
        """检查隧道崩溃并重启"""
        with self._lock:
            names = list(self.known.keys())
        for name in names:
            with self._lock:
                entry = self.known.get(name)
            if not entry:
                continue
            tunnels.check_and_restart(entry, name)

    def shutdown(self):
        self._stop.set()
