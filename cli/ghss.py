# -*- coding: utf-8 -*-
"""
ghbox CLI 客户端入口

用法：
  ghss                          # 交互模式（主菜单）
  ghss <EXEC_TOKEN>             # 指定 token
  ghss <EXEC_TOKEN> <INSTANCE_URL>  # 直接连接终端
  ghss --json <op> [args]       # JSON 脚本模式

JSON 模式操作：
  instances / create / close <id> / accounts /
  add-account <name> <token> / tasks / logs [limit] /
  s3-status / s3-workers / overview / processes <inst_id> /
  snapshot <inst_id> / backup <inst_id> /
  exec <inst_id> <cmd> / health <inst_id>
  attack/start <inst_id> <target> [type] [port] [duration]
  attack/stop <inst_id> / attack/status <inst_id>
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

    if op == "instances":
        print(json_dumps(api.get("/api/instances")))
    elif op == "create":
        print(json_dumps(api.post("/api/instances")))
    elif op == "close" and len(args) > 1:
        print(json_dumps(api.delete(f"/api/instances/{args[1]}")))
    elif op == "accounts":
        print(json_dumps(api.get("/api/accounts")))
    elif op == "add-account" and len(args) > 2:
        print(json_dumps(api.post("/api/accounts", {"name": args[1], "token": args[2]})))
    elif op == "tasks":
        print(json_dumps(api.get("/api/tasks")))
    elif op == "logs":
        limit = int(args[1]) if len(args) > 1 else 300
        print(json_dumps(api.get(f"/api/logs?limit={limit}")))
    elif op == "s3-status":
        print(json_dumps(api.get("/api/s3/status")))
    elif op == "overview":
        print(json_dumps(api.get("/api/overview")))
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
    elif op == "health" and len(args) > 1:
        inst = api.get("/api/instances")
        for i in inst.get("instances", []):
            if i["id"] == args[1]:
                print(json_dumps(api.get_inst(i["hostname"], "/api/health")))
                return
        print(json_dumps({"ok": False, "error": "实例不存在"}))
    elif op == "attack/start" and len(args) > 2:
        inst = api.get("/api/instances")
        for i in inst.get("instances", []):
            if i["id"] == args[1]:
                payload = {"target": args[2], "token": config.TOKEN}
                if len(args) > 3:
                    payload["type"] = args[3]
                if len(args) > 4:
                    payload["port"] = int(args[4])
                if len(args) > 5:
                    payload["duration"] = int(args[5])
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

    elif op == "s3-health":
        print(json_dumps(api.get("/api/s3/health")))
    elif op == "s3-accounts":
        print(json_dumps(api.get("/api/s3/accounts")))
    elif op == "s3-workers":
        print(json_dumps(api.get("/api/s3/workers")))
    elif op == "logs-proxy" and len(args) > 1:
        print(json_dumps(api.get(f"/api/instances/{args[1]}/logs?limit=50")))
    elif op == "processes-proxy" and len(args) > 1:
        print(json_dumps(api.get(f"/api/instances/{args[1]}/processes")))
    elif op == "resource-proxy" and len(args) > 1:
        print(json_dumps(api.get(f"/api/instances/{args[1]}/resource")))

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
