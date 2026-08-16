# -*- coding: utf-8 -*-
"""
ghbox CLI 客户端入口

用法：
  ghss                          # 交互模式（主菜单）
  ghss <EXEC_TOKEN>             # 指定 token
  ghss <EXEC_TOKEN> <INSTANCE_URL>  # 直接连接终端
  ghss --json <op> [args]       # JSON 脚本模式

JSON 模式操作：
  instances / create / close <id> / restart <id> /
  accounts / add-account <name> <token> / banned /
  tasks / logs [limit] / overview /
  s3-status / s3-health / s3-accounts / s3-workers /
  processes <inst_id> / snapshot <inst_id> / backup <inst_id> /
  exec <inst_id> <cmd> / exec-batch <inst_id> <cmd1> <cmd2> ... /
  health <inst_id> / wait <inst_id> / logs-follow <inst_id> /
  backup-history <inst_id> / restore-history <inst_id> /
  timeline <inst_id> / worker-stats <inst_id> /
  attack/start <inst_id> <target> [type] [port] [duration] /
  attack/stop <inst_id> / attack/status <inst_id> /
  logs-proxy <inst_id> / processes-proxy <inst_id> / resource-proxy <inst_id>
"""
import sys
import os
import json


def json_dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def json_mode(args):
    """JSON 脚本模式：单次操作输出 JSON"""
    from cli import api, config
    op = args[0] if args else "instances"

    # === 实例 ===
    if op == "instances":
        print(json_dumps(api.get("/api/instances")))
    elif op == "create":
        payload = {}
        for arg in args[1:]:
            if arg == "--no-mcp":
                payload["mcp_enabled"] = False
            elif not arg.startswith("--"):
                payload["account"] = arg
        print(json_dumps(api.post("/api/instances", payload)))
    elif op == "close" and len(args) > 1:
        print(json_dumps(api.delete(f"/api/instances/{args[1]}")))
    elif op == "restart" and len(args) > 1:
        print(json_dumps(api.post(f"/api/instances/{args[1]}/restart")))
    elif op == "mcp/toggle" and len(args) > 2:
        inst = api.get("/api/instances")
        for i in inst.get("instances", []):
            if i["id"] == args[1]:
                enabled = args[2].lower() in ("on", "true", "1", "yes")
                print(json_dumps(api.post_inst(i["hostname"], "/api/mcp/toggle",
                               {"token": config.TOKEN, "enabled": enabled})))
                return
        print(json_dumps({"ok": False, "error": "实例不存在"}))
    elif op == "detail" and len(args) > 1:
        inst = api.get("/api/instances")
        for i in inst.get("instances", []):
            if i["id"] == args[1]:
                result = {"instance": i}
                result["health"] = api.get_inst(i["hostname"], "/api/health")
                result["processes"] = api.get_inst(i["hostname"], "/api/processes")
                result["resource"] = api.get_inst(i["hostname"], "/api/resource")
                print(json_dumps(result))
                return
        print(json_dumps({"ok": False, "error": "实例不存在"}))
    elif op == "health" and len(args) > 1:
        inst = api.get("/api/instances")
        for i in inst.get("instances", []):
            if i["id"] == args[1]:
                print(json_dumps(api.get_inst(i["hostname"], "/api/health")))
                return
        print(json_dumps({"ok": False, "error": "实例不存在"}))
    elif op == "wait" and len(args) > 1:
        import time as _t
        inst = api.get("/api/instances")
        host = None
        for i in inst.get("instances", []):
            if i["id"] == args[1]:
                host = i.get("hostname")
                break
        if not host:
            print(json_dumps({"ok": False, "error": "实例不存在"}))
        else:
            for attempt in range(60):
                r = api.get_url(f"https://{host}/api/health", timeout=10)
                if r.get("ok"):
                    print(json_dumps({"ok": True, "host": host, "attempts": attempt + 1}))
                    break
                _t.sleep(5)
            else:
                print(json_dumps({"ok": False, "error": "超时未就绪"}))

    # === 账号 ===
    elif op == "accounts":
        print(json_dumps(api.get("/api/accounts")))
    elif op == "add-account" and len(args) > 2:
        print(json_dumps(api.post("/api/accounts", {"name": args[1], "token": args[2]})))
    elif op == "banned":
        print(json_dumps(api.get("/api/accounts/banned")))

    # === 任务/日志/总览 ===
    elif op == "tasks":
        print(json_dumps(api.get("/api/tasks")))
    elif op == "logs":
        limit = int(args[1]) if len(args) > 1 else 300
        print(json_dumps(api.get(f"/api/logs?limit={limit}")))
    elif op == "overview":
        print(json_dumps(api.get("/api/overview")))

    # === S3 ===
    elif op == "s3-status":
        print(json_dumps(api.get("/api/s3/status")))
    elif op == "s3-health":
        print(json_dumps(api.get("/api/s3/health")))
    elif op == "s3-accounts":
        print(json_dumps(api.get("/api/s3/accounts")))
    elif op == "s3-workers":
        print(json_dumps(api.get("/api/s3/workers")))
    elif op == "backup-history" and len(args) > 1:
        print(json_dumps(api.get(f"/api/instances/{args[1]}/backup-history")))
    elif op == "restore-history" and len(args) > 1:
        print(json_dumps(api.get(f"/api/instances/{args[1]}/restore-history")))
    elif op == "timeline" and len(args) > 1:
        print(json_dumps(api.get(f"/api/instances/{args[1]}/timeline")))
    elif op == "worker-stats" and len(args) > 1:
        print(json_dumps(api.get(f"/api/instances/{args[1]}/stats")))

    # === 进程/备份/执行 ===
    elif op == "processes" and len(args) > 1:
        inst = api.get("/api/instances")
        for i in inst.get("instances", []):
            if i["id"] == args[1]:
                print(json_dumps(api.get_inst(i["hostname"], "/api/processes")))
                return
        print(json_dumps({"ok": False, "error": "实例不存在"}))
    elif op == "snapshot" and len(args) > 1:
        inst = api.get("/api/instances")
        for i in inst.get("instances", []):
            if i["id"] == args[1]:
                print(json_dumps(api.post_inst(i["hostname"], "/api/processes/snapshot",
                               {"token": config.TOKEN})))
                return
        print(json_dumps({"ok": False, "error": "实例不存在"}))
    elif op == "backup" and len(args) > 1:
        inst = api.get("/api/instances")
        for i in inst.get("instances", []):
            if i["id"] == args[1]:
                print(json_dumps(api.post_inst(i["hostname"], "/api/backup/now",
                               {"token": config.TOKEN})))
                return
        print(json_dumps({"ok": False, "error": "实例不存在"}))
    elif op == "exec" and len(args) > 2:
        print(json_dumps(api.post(f"/api/instances/{args[1]}/exec",
                                  {"cmd": args[2], "timeout": 30})))
    elif op == "exec-batch" and len(args) > 2:
        results = []
        for cmd in args[2:]:
            r = api.post(f"/api/instances/{args[1]}/exec", {"cmd": cmd, "timeout": 30})
            results.append({"cmd": cmd, "result": r.get("result") or r})
        print(json_dumps({"ok": True, "results": results}))
    elif op == "logs-follow" and len(args) > 1:
        import time as _t
        seen = 0
        while True:
            try:
                d = api.get(f"/api/instances/{args[1]}/logs?limit=500")
                if d.get("ok"):
                    logs = d.get("logs", [])
                    new_logs = logs[seen:] if seen < len(logs) else []
                    for entry in new_logs:
                        if isinstance(entry, dict):
                            print(f"[{entry.get('level','')}] {entry.get('msg','')}")
                    seen = len(logs)
            except Exception:
                pass
            _t.sleep(2)

    # === 代理 ===
    elif op == "logs-proxy" and len(args) > 1:
        print(json_dumps(api.get(f"/api/instances/{args[1]}/logs?limit=50")))
    elif op == "processes-proxy" and len(args) > 1:
        print(json_dumps(api.get(f"/api/instances/{args[1]}/processes")))
    elif op == "resource-proxy" and len(args) > 1:
        print(json_dumps(api.get(f"/api/instances/{args[1]}/resource")))

    # === 攻击 ===
    elif op == "attack/start" and len(args) > 2:
        inst = api.get("/api/instances")
        for i in inst.get("instances", []):
            if i["id"] == args[1]:
                payload = {"target": args[2], "token": config.TOKEN}
                if len(args) > 3: payload["type"] = args[3]
                if len(args) > 4: payload["port"] = int(args[4])
                if len(args) > 5: payload["duration"] = int(args[5])
                print(json_dumps(api.post_inst(i["hostname"], "/api/attack/start", payload)))
                return
        print(json_dumps({"ok": False, "error": "实例不存在"}))
    elif op == "attack/stop" and len(args) > 1:
        inst = api.get("/api/instances")
        for i in inst.get("instances", []):
            if i["id"] == args[1]:
                print(json_dumps(api.post_inst(i["hostname"], "/api/attack/stop",
                               {"token": config.TOKEN})))
                return
        print(json_dumps({"ok": False, "error": "实例不存在"}))
    elif op == "attack/status" and len(args) > 1:
        inst = api.get("/api/instances")
        for i in inst.get("instances", []):
            if i["id"] == args[1]:
                print(json_dumps(api.get_inst(i["hostname"], "/api/attack/status")))
                return
        print(json_dumps({"ok": False, "error": "实例不存在"}))

    else:
        print(json_dumps({"ok": False, "error": f"未知操作: {op}"}))


def main():
    # JSON 模式
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        from cli import config
        if not config.TOKEN:
            config.TOKEN = os.environ.get("EXEC_TOKEN", "")
        json_mode(sys.argv[2:])
        return

    from cli import config

    # 解析参数
    if len(sys.argv) > 1 and not sys.argv[1].startswith(("http", "--")):
        config.set_token(sys.argv[1])
    if not config.TOKEN:
        config.TOKEN = os.environ.get("EXEC_TOKEN", "")
    if not config.TOKEN:
        print("用法: ghss <EXEC_TOKEN> [INSTANCE_URL]")
        print("  或: ghss --json instances")
        print("  或: 设置 EXEC_TOKEN 环境变量后直接 ghss")
        sys.exit(1)

    # 直接连接终端
    if len(sys.argv) > 2 and sys.argv[2].startswith("http"):
        from cli import terminal
        url = sys.argv[2]
        if "://" in url and not url.startswith("https://"):
            url = url.replace("http://", "https://")
        terminal.connect_terminal(url)
        return

    # 交互模式
    from cli.ui import run_menu
    run_menu()


if __name__ == "__main__":
    main()
