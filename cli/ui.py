# -*- coding: utf-8 -*-
"""
CLI 主菜单 + 子菜单 UI（rich 美化版）
"""
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from cli import config, ops

console = Console()


def main_menu():
    console.print(Panel(
        f"Manager: {config.MANAGER}",
        title="[bold cyan]ghbox GitHub Actions 云端实例管理[/]",
        border_style="cyan"))
    console.print()
    console.print(Rule("实例", style="dim"))
    console.print("  [1] 查看实例列表")
    console.print("  [2] 新建实例")
    console.print("  [3] 连接实例终端")
    console.print("  [4] [yellow]重启实例[/]")
    console.print("  [5] 关闭实例")
    console.print()
    console.print(Rule("管理", style="dim"))
    console.print("  [6] 账号管理")
    console.print("  [7] 任务队列")
    console.print("  [8] 服务器日志")
    console.print("  [9] S3 存储管理")
    console.print()
    console.print(Rule("工具", style="dim"))
    console.print(r"  \[a] 实例管理（进程/备份/命令/资源）")
    console.print(r"  \[b] 攻击功能")
    console.print(r"  \[o] 总览")
    console.print("  [0] 退出")
    return ops._input("\n  请选择: ")


def account_menu():
    while True:
        console.print()
        console.print(Rule("账号管理", style="cyan"))
        console.print("  [1] 查看账号列表")
        console.print("  [2] 添加账号")
        console.print("  [3] 被封账号")
        console.print("  [0] 返回")
        choice = ops._input("\n  请选择: ")
        if choice == "1":
            ops.list_accounts()
        elif choice == "2":
            ops.add_account()
        elif choice == "3":
            ops.banned_accounts()
        elif choice == "0" or choice is None:
            break


def s3_menu():
    while True:
        console.print()
        console.print(Rule("S3 存储管理", style="cyan"))
        console.print("  [1] 全局状态")
        console.print("  [2] 健康摘要")
        console.print("  [3] 账号详情（非active）")
        console.print("  [4] Worker 详情")
        console.print("  [0] 返回")
        choice = ops._input("\n  请选择: ")
        if choice == "1":
            ops.s3_status()
        elif choice == "2":
            ops.s3_health()
        elif choice == "3":
            ops.s3_accounts()
        elif choice == "4":
            ops.s3_workers()
        elif choice == "0" or choice is None:
            break


def instance_menu():
    while True:
        console.print()
        console.print(Rule("实例管理", style="cyan"))
        console.print("  [1] 查看进程列表")
        console.print("  [2] 触发进程快照")
        console.print("  [3] 启动进程")
        console.print("  [4] 停止进程")
        console.print("  [5] 重启进程")
        console.print("  [6] 查看进程日志")
        console.print("  [7] 立即备份")
        console.print("  [8] 执行命令")
        console.print("  [9] 资源监控")
        console.print(r"  \[a] Worker日志（代理）")
        console.print(r"  \[b] Worker进程（代理）")
        console.print(r"  \[c] Worker资源（代理）")
        console.print(r"  \[d] 日志实时跟随")
        console.print(r"  \[e] 批量执行命令")
        console.print(r"  \[f] MCP 服务开关")
        console.print(r"  \[g] 托管项目")
        console.print("  [0] 返回")
        choice = ops._input("\n  请选择: ")
        if choice == "1":
            ops.list_processes()
        elif choice == "2":
            ops.snapshot_processes()
        elif choice == "3":
            ops.start_process()
        elif choice == "4":
            ops.stop_process()
        elif choice == "5":
            ops.restart_process()
        elif choice == "6":
            ops.process_log()
        elif choice == "7":
            ops.backup_now()
        elif choice == "8":
            ops.exec_cmd()
        elif choice == "9":
            ops.resource_monitor()
        elif choice in ("a", "A"):
            ops.proxy_logs()
        elif choice in ("b", "B"):
            ops.proxy_processes()
        elif choice in ("c", "C"):
            ops.proxy_resource()
        elif choice in ("d", "D"):
            ops.logs_follow()
        elif choice in ("e", "E"):
            ops.exec_batch()
        elif choice in ("f", "F"):
            ops.toggle_mcp()
        elif choice in ("g", "G"):
            ops.adopt_project()
        elif choice == "0" or choice is None:
            break


def attack_menu():
    while True:
        console.print()
        console.print(Rule("攻击功能", style="cyan"))
        console.print("  [1] 发起攻击（支持多实例）")
        console.print("  [2] 停止攻击（支持多实例）")
        console.print("  [3] 攻击状态（全部实例）")
        console.print("  [0] 返回")
        choice = ops._input("\n  请选择: ")
        if choice == "1":
            ops.attack_start()
        elif choice == "2":
            ops.attack_stop()
        elif choice == "3":
            ops.attack_status()
        elif choice == "0" or choice is None:
            break


def run_menu():
    """主菜单循环"""
    from cli import terminal
    while True:
        try:
            choice = main_menu()
            if choice == "1":
                ops.list_instances()
            elif choice == "2":
                ops.create_instance()
            elif choice == "3":
                inst = ops.pick_instance()
                if inst:
                    console.print(f"  [cyan]连接 {inst['id']} ... (Ctrl+4 退出)[/]")
                    terminal.connect_terminal(inst.get("hostname", ""))
            elif choice == "4":
                ops.restart_instance()
            elif choice == "5":
                ops.close_instance()
            elif choice == "6":
                account_menu()
            elif choice == "7":
                ops.list_tasks()
            elif choice == "8":
                ops.view_logs()
            elif choice == "9":
                s3_menu()
            elif choice in ("a", "A"):
                instance_menu()
            elif choice in ("b", "B"):
                attack_menu()
            elif choice in ("o", "O"):
                ops.overview()
            elif choice == "0" or choice is None:
                console.print("  [dim]再见！[/]")
                break
            else:
                ops._err("无效选择")
        except KeyboardInterrupt:
            console.print("\n  [dim]返回菜单[/]")
        except Exception as e:
            ops._err(str(e))
