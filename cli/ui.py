# -*- coding: utf-8 -*-
"""
CLI 主菜单 + 子菜单 UI
"""
from cli import config
from cli.ops import _input


def main_menu():
    print("\n" + "=" * 54)
    print("  ghbox GitHub Actions 云端实例管理")
    print(f"  Manager: {config.MANAGER}")
    print("=" * 54)
    print("  [1] 查看所有实例")
    print("  [2] 新建实例")
    print("  [3] 连接实例终端")
    print("  [4] 关闭实例")
    print("  [5] 查看账号")
    print("  [6] 添加账号")
    print("  [7] 任务队列")
    print("  [8] 查看服务器日志")
    print("  [9] S3 存储（状态/健康/账号详情）")
    print("  [a] 实例管理（进程/备份/执行命令）")
    print("  [b] 攻击功能")
    print("  [o] 总览")
    print("  [0] 退出")
    return _input("\n  请选择: ")


def instance_menu():
    """实例管理子菜单"""
    from cli import ops
    while True:
        print("\n" + "-" * 40)
        print("  实例管理（进程/备份/执行命令）")
        print("-" * 40)
        print("  [1] 查看进程列表")
        print("  [2] 触发进程快照")
        print("  [3] 启动进程")
        print("  [4] 停止进程")
        print("  [5] 重启进程")
        print("  [6] 查看进程日志")
        print("  [7] 立即备份")
        print("  [8] 执行命令")
        print("  [9] 资源监控")
        print("  [a] Worker日志（代理）")
        print("  [b] Worker进程（代理）")
        print("  [c] Worker资源（代理）")
        print("  [0] 返回主菜单")
        choice = _input("\n  请选择: ")
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
        elif choice == "0" or choice is None:
            break
        else:
            print("  无效选择")


def attack_menu():
    """攻击功能子菜单"""
    from cli import ops
    while True:
        print("\n" + "-" * 40)
        print("  攻击功能")
        print("-" * 40)
        print("  [1] 发起攻击")
        print("  [2] 停止攻击")
        print("  [3] 攻击状态")
        print("  [0] 返回主菜单")
        choice = _input("\n  请选择: ")
        if choice == "1":
            ops.attack_start()
        elif choice == "2":
            ops.attack_stop()
        elif choice == "3":
            ops.attack_status()
        elif choice == "0" or choice is None:
            break
        else:
            print("  无效选择")


def run_menu():
    """主菜单循环"""
    from cli import ops
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
                    print(f"  连接 {inst['id']} ... (Ctrl+4 退出)")
                    terminal.connect_terminal(inst.get("hostname", ""))
            elif choice == "4":
                ops.close_instance()
            elif choice == "5":
                ops.list_accounts()
            elif choice == "6":
                ops.add_account()
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
                print("  再见！")
                break
            else:
                print("  无效选择")
        except KeyboardInterrupt:
            print("\n  返回菜单")
        except Exception as e:
            print(f"  错误: {e}")


def s3_menu():
    """S3 存储子菜单"""
    from cli import ops
    while True:
        print("\n" + "-" * 40)
        print("  S3 存储管理")
        print("-" * 40)
        print("  [1] 全局状态")
        print("  [2] 健康摘要")
        print("  [3] 账号详情（非active）")
        print("  [0] 返回主菜单")
        choice = _input("\n  请选择: ")
        if choice == "1":
            ops.s3_status()
        elif choice == "2":
            ops.s3_health()
        elif choice == "3":
            ops.s3_accounts()
        elif choice == "0" or choice is None:
            break
        else:
            print("  无效选择")
