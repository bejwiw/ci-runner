# -*- coding: utf-8 -*-
"""
多账号管理（manager 侧）

- 账号配置存储（通过 store.py）
- fork 同步（merge-upstream）
- Secrets 配置（PyNaCl sealed box）
- 负载均衡（选并发余量最大的账号）
"""
import os
import time
import base64

import config
import log
from core import ghapi
from manager import store

logger = log.setup_logger("accounts")


def load_accounts():
    return store.load_accounts()


def save_accounts(accounts):
    store.save_accounts(accounts)


def list_accounts():
    """返回账号列表（脱敏）"""
    accounts = load_accounts()
    result = []
    for a in accounts:
        tok = a.get("token", "")
        masked = (tok[:6] + "***" + tok[-4:]) if len(tok) > 12 else "***"
        result.append({
            "name": a.get("name"),
            "token_masked": masked,
            "repo": a.get("repo"),
            "max_concurrency": a.get("max_concurrency", 20),
        })
    return result


def add_account(name, gh_token, repo=None, max_conc=None):
    accounts = load_accounts()
    for a in accounts:
        if a.get("name") == name:
            a["token"] = gh_token
            if repo:
                a["repo"] = repo
            if max_conc:
                a["max_concurrency"] = max_conc
            save_accounts(accounts)
            return {"ok": True, "msg": f"账号 {name} 已更新"}
    accounts.append({
        "name": name,
        "token": gh_token,
        "repo": repo or config.REPO,
        "max_concurrency": max_conc or 20,
    })
    save_accounts(accounts)
    return {"ok": True, "msg": f"账号 {name} 已添加"}


def remove_account(name):
    accounts = load_accounts()
    new = [a for a in accounts if a.get("name") != name]
    if len(new) == len(accounts):
        return {"ok": False, "msg": f"账号 {name} 不存在"}
    save_accounts(new)
    return {"ok": True, "msg": f"账号 {name} 已删除"}


def sync_fork(account):
    """把 fork 同步到上游最新"""
    try:
        repo = account.get("repo") or config.REPO
        token = account.get("token")
        if repo == config.MAIN_REPO:
            return True
        url = f"{ghapi.API_BASE}/repos/{repo}/merge-upstream"
        status, d = ghapi.gh_request("POST", url, token=token, data={"branch": "main"})
        ok = status in (200, 201)
        if not ok:
            logger.info(f"[sync] {repo} 同步状态: {status}")
        return ok
    except Exception as e:
        logger.error(f"[sync] 同步失败: {e}")
        return False


def _set_repo_secret(account_token, repo, secret_name, secret_value):
    """配置仓库 secret（幂等）"""
    try:
        chk = ghapi.gh_request("GET",
            f"{ghapi.API_BASE}/repos/{repo}/actions/secrets/{secret_name}",
            token=account_token)
        if chk[0] == 200 and isinstance(chk[1], dict) and chk[1].get("name"):
            return True
        status, d = ghapi.gh_request("GET",
            f"{ghapi.API_BASE}/repos/{repo}/actions/secrets/public-key",
            token=account_token)
        if status != 200:
            return False
        from nacl.public import PublicKey, SealedBox
        pub = PublicKey(base64.b64decode(d["key"]))
        sealed = SealedBox(pub)
        encrypted = sealed.encrypt(str(secret_value).encode())
        encrypted_b64 = base64.b64encode(encrypted).decode()
        status, _ = ghapi.gh_request("PUT",
            f"{ghapi.API_BASE}/repos/{repo}/actions/secrets/{secret_name}",
            token=account_token,
            data={"encrypted_value": encrypted_b64, "key_id": d["key_id"]})
        return status in (200, 201, 204)
    except Exception as e:
        logger.error(f"[secrets] 配置 {secret_name} 失败: {e}")
        return False


def auto_provision_account(name, account_token, repo=None, max_conc=None):
    """全自动创建账号（幂等）"""
    # ① 验证 token
    status, user = ghapi.gh_request("GET", f"{ghapi.API_BASE}/user", token=account_token)
    if status != 200:
        return {"ok": False, "error": f"token 无效（{status}）"}
    login = user.get("login", "")
    logger.info(f"[account] 配置账号 {name} ({login})")
    # ② 确保仓库
    if not repo:
        repo = f"{login}/{config.MAIN_REPO.split('/')[-1]}"
    status, _ = ghapi.gh_request("GET", f"{ghapi.API_BASE}/repos/{repo}", token=account_token)
    if status != 200:
        logger.info("[account] fork 主仓库...")
        ghapi.gh_request("POST", f"{ghapi.API_BASE}/repos/{config.MAIN_REPO}/forks",
                         token=account_token, data={"default_branch_only": True})
        for _ in range(60):
            time.sleep(5)
            status, _ = ghapi.gh_request("GET",
                f"{ghapi.API_BASE}/repos/{repo}", token=account_token)
            if status == 200:
                break
        else:
            return {"ok": False, "error": "fork 超时"}
    # ③ 同步
    sync_fork({"repo": repo, "token": account_token})
    time.sleep(3)
    # ④ 配 secrets
    needed = {
        "GH_TOKEN": account_token,
        "EXEC_TOKEN": config.EXEC_TOKEN,
        "DEMO_KEY": config.DEMO_KEY,
        "S3_BOOTSTRAP": os.environ.get("S3_BOOTSTRAP", ""),
        "S3_ENDPOINT": config.S3_ENDPOINT,
        "S3_REGION": config.S3_REGION,
    }
    for sname, sval in needed.items():
        if sval:
            _set_repo_secret(account_token, repo, sname, sval)
    logger.info(f"[account] {name} secrets 配置完成")
    # ⑤ 报备
    return add_account(name, account_token, repo=repo, max_conc=max_conc)


def _account_usage(account, workflow=None):
    try:
        repo = account.get("repo") or config.REPO
        token = account.get("token")
        url = f"{ghapi.API_BASE}/repos/{repo}/actions/runs?status=in_progress&per_page=100"
        status, data = ghapi.gh_request("GET", url, token=token)
        if status != 200:
            return 0
        runs = data.get("workflow_runs", [])
        if workflow:
            return sum(1 for r in runs if workflow in r.get("path", ""))
        return len(runs)
    except Exception:
        return 0


def select_best_account(workflow=None):
    """负载均衡：选并发余量最大的账号"""
    accounts = load_accounts()
    if not accounts:
        return None
    best = None
    for acc in accounts:
        running = _account_usage(acc, workflow=workflow)
        max_c = acc.get("max_concurrency", 20)
        if running >= max_c:
            continue
        if best is None or (max_c - running) > (best["max_concurrency"] - best["running"]):
            best = {"account": acc, "running": running, "max_concurrency": max_c}
    if best is None:
        return None
    return best["account"], best["running"]
