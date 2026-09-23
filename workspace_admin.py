#!/usr/bin/env python3
"""Google Workspace 批量账号管理工具（基于 Admin SDK Directory API）。

子命令：
  create-ou       创建组织部门（OU），用于挂统一安全策略
  create-users    从 CSV 批量创建用户，自动生成强密码并要求首次登录改密
  reset-password  批量重置密码（并强制下次登录修改）
  move-ou         把用户批量移动到指定 OU
  signout         批量强制登出所有会话
  suspend / unsuspend  批量停用 / 恢复
  audit           导出安全审计报表（2SV 注册/强制状态、最后登录时间等）

认证方式：服务账号 + 全网域委派（domain-wide delegation），以一个超级管理员身份执行。
"""

import argparse
import csv
import logging
import random
import secrets
import string
import sys
import time
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/admin.directory.user.security",
    "https://www.googleapis.com/auth/admin.directory.orgunit",
]

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRYABLE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "backendError"}

log = logging.getLogger("workspace_admin")


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

def build_service(key_file: str, admin_email: str):
    creds = service_account.Credentials.from_service_account_file(key_file, scopes=SCOPES)
    creds = creds.with_subject(admin_email)
    return build("admin", "directory_v1", credentials=creds, cache_discovery=False)


def _is_retryable(err: HttpError) -> bool:
    if err.resp.status in RETRYABLE_STATUS:
        return True
    if err.resp.status == 403:
        try:
            reasons = {e.get("reason") for e in err.error_details or []}
        except Exception:
            reasons = set()
        text = str(err)
        return bool(reasons & RETRYABLE_REASONS) or any(r in text for r in RETRYABLE_REASONS)
    return False


def execute(request, max_attempts: int = 6):
    """带指数退避的请求执行。"""
    for attempt in range(1, max_attempts + 1):
        try:
            return request.execute()
        except HttpError as err:
            if attempt == max_attempts or not _is_retryable(err):
                raise
            delay = min(2 ** attempt, 64) + random.random()
            log.warning("请求受限/失败 (HTTP %s)，%.1fs 后重试 (%d/%d)",
                        err.resp.status, delay, attempt, max_attempts)
            time.sleep(delay)


def generate_password(length: int = 16) -> str:
    """生成包含大小写、数字、符号的强密码。"""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(not c.isalnum() for c in pw)):
            return pw


def read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]
    if not rows:
        sys.exit(f"CSV 为空: {path}")
    return rows


def read_emails(path: str) -> list[str]:
    """读取邮箱列表：支持带 primaryEmail/email 列的 CSV，或每行一个邮箱的纯文本。"""
    text = Path(path).read_text(encoding="utf-8-sig")
    first = text.splitlines()[0] if text else ""
    if "," in first or first.strip() in ("primaryEmail", "email"):
        rows = read_csv(path)
        key = "primaryEmail" if "primaryEmail" in rows[0] else "email"
        return [r[key] for r in rows if r.get(key)]
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def write_secret_csv(path: str, rows: list[dict], fields: list[str]):
    """写出含初始密码的 CSV，并尽量把权限收紧到仅当前用户可读。"""
    p = Path(path)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    log.info("已写出 %d 条记录到 %s（含初始密码，请安全分发后删除）", len(rows), path)


def for_each_email(emails, action, dry_run: bool, label: str, pause: float):
    ok = fail = 0
    for i, email in enumerate(emails, 1):
        if dry_run:
            log.info("[dry-run] %s: %s", label, email)
            continue
        try:
            action(email)
            ok += 1
            log.info("[%d/%d] %s 成功: %s", i, len(emails), label, email)
        except HttpError as err:
            fail += 1
            log.error("[%d/%d] %s 失败: %s -> %s", i, len(emails), label, email, err)
        time.sleep(pause)
    if not dry_run:
        log.info("%s 完成：成功 %d，失败 %d", label, ok, fail)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_create_ou(svc, args):
    parent, _, name = args.path.rstrip("/").rpartition("/")
    body = {"name": name, "parentOrgUnitPath": parent or "/", "description": args.description or ""}
    if args.dry_run:
        log.info("[dry-run] 创建 OU: %s", body)
        return
    try:
        execute(svc.orgunits().insert(customerId="my_customer", body=body))
        log.info("已创建 OU: %s", args.path)
    except HttpError as err:
        if err.resp.status == 409 or "already exists" in str(err).lower():
            log.info("OU 已存在: %s", args.path)
        else:
            raise


def cmd_create_users(svc, args):
    rows = read_csv(args.csv)
    required = {"primaryEmail", "givenName", "familyName"}
    missing = required - rows[0].keys()
    if missing:
        sys.exit(f"CSV 缺少必需列: {', '.join(sorted(missing))}")

    results = []
    for i, row in enumerate(rows, 1):
        email = row["primaryEmail"]
        password = row.get("password") or generate_password(args.password_length)
        body = {
            "primaryEmail": email,
            "name": {"givenName": row["givenName"], "familyName": row["familyName"]},
            "password": password,
            "changePasswordAtNextLogin": True,
            "orgUnitPath": row.get("orgUnitPath") or args.org_unit,
        }
        if row.get("recoveryEmail"):
            body["recoveryEmail"] = row["recoveryEmail"]
        if row.get("recoveryPhone"):
            body["recoveryPhone"] = row["recoveryPhone"]  # 必须是 E.164 格式，如 +8613800000000

        if args.dry_run:
            log.info("[dry-run] 创建用户: %s -> OU %s", email, body["orgUnitPath"])
            continue
        try:
            execute(svc.users().insert(body=body))
            status = "created"
            log.info("[%d/%d] 已创建: %s", i, len(rows), email)
        except HttpError as err:
            if err.resp.status == 409:
                status, password = "exists", ""
                log.warning("[%d/%d] 已存在，跳过: %s", i, len(rows), email)
            else:
                status, password = f"error: {err.resp.status}", ""
                log.error("[%d/%d] 创建失败: %s -> %s", i, len(rows), email, err)
        results.append({"primaryEmail": email, "initialPassword": password,
                        "orgUnitPath": body["orgUnitPath"], "status": status})
        time.sleep(args.pause)

    if results:
        write_secret_csv(args.out, results, ["primaryEmail", "initialPassword", "orgUnitPath", "status"])


def cmd_reset_password(svc, args):
    emails = read_emails(args.input)
    results = []

    def action(email):
        pw = generate_password(args.password_length)
        execute(svc.users().update(userKey=email, body={
            "password": pw, "changePasswordAtNextLogin": True}))
        results.append({"primaryEmail": email, "newPassword": pw})
        if args.signout:
            execute(svc.users().signOut(userKey=email))

    for_each_email(emails, action, args.dry_run, "重置密码", args.pause)
    if results:
        write_secret_csv(args.out, results, ["primaryEmail", "newPassword"])


def cmd_move_ou(svc, args):
    emails = read_emails(args.input)
    for_each_email(
        emails,
        lambda e: execute(svc.users().update(userKey=e, body={"orgUnitPath": args.org_unit})),
        args.dry_run, f"移动到 {args.org_unit}", args.pause)


def cmd_signout(svc, args):
    emails = read_emails(args.input)
    for_each_email(emails, lambda e: execute(svc.users().signOut(userKey=e)),
                   args.dry_run, "强制登出", args.pause)


def cmd_suspend(svc, args, suspended: bool):
    emails = read_emails(args.input)
    for_each_email(emails,
                   lambda e: execute(svc.users().update(userKey=e, body={"suspended": suspended})),
                   args.dry_run, "停用" if suspended else "恢复", args.pause)


def cmd_audit(svc, args):
    fields = ["primaryEmail", "fullName", "orgUnitPath", "isAdmin", "suspended",
              "isEnrolledIn2Sv", "isEnforcedIn2Sv", "hasRecoveryEmail", "hasRecoveryPhone",
              "lastLoginTime", "creationTime"]
    rows, token = [], None
    query = f"orgUnitPath='{args.org_unit}'" if args.org_unit else None
    while True:
        resp = execute(svc.users().list(customer="my_customer", maxResults=500, orderBy="email",
                                        projection="full", query=query, pageToken=token))
        for u in resp.get("users", []):
            rows.append({
                "primaryEmail": u["primaryEmail"],
                "fullName": u.get("name", {}).get("fullName", ""),
                "orgUnitPath": u.get("orgUnitPath", ""),
                "isAdmin": u.get("isAdmin", False),
                "suspended": u.get("suspended", False),
                "isEnrolledIn2Sv": u.get("isEnrolledIn2Sv", False),
                "isEnforcedIn2Sv": u.get("isEnforcedIn2Sv", False),
                "hasRecoveryEmail": bool(u.get("recoveryEmail")),
                "hasRecoveryPhone": bool(u.get("recoveryPhone")),
                "lastLoginTime": u.get("lastLoginTime", ""),
                "creationTime": u.get("creationTime", ""),
            })
        token = resp.get("nextPageToken")
        if not token:
            break

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    enrolled = sum(r["isEnrolledIn2Sv"] for r in rows)
    enforced = sum(r["isEnforcedIn2Sv"] for r in rows)
    suspended = sum(r["suspended"] for r in rows)
    never = sum(1 for r in rows if r["lastLoginTime"].startswith("1970"))
    log.info("审计完成：共 %d 个账号 | 已注册 2SV %d | 已强制 2SV %d | 已停用 %d | 从未登录 %d",
             total, enrolled, enforced, suspended, never)
    not_enrolled = [r["primaryEmail"] for r in rows if not r["isEnrolledIn2Sv"] and not r["suspended"]]
    if not_enrolled:
        log.info("未注册 2SV 的活跃账号 %d 个（见 %s），示例: %s",
                 len(not_enrolled), args.out, ", ".join(not_enrolled[:5]))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description="Google Workspace 批量账号管理")
    p.add_argument("--key", default="service-account.json", help="服务账号 JSON 密钥路径")
    p.add_argument("--admin", required=True, help="要模拟的超级管理员邮箱，如 admin@example.com")
    p.add_argument("--dry-run", action="store_true", help="只打印将执行的操作，不调用写接口")
    p.add_argument("--pause", type=float, default=0.3, help="每个请求间隔秒数，避免触发配额限制")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("create-ou", help="创建组织部门")
    s.add_argument("path", help="OU 完整路径，如 /Staff/Support")
    s.add_argument("--description")

    s = sub.add_parser("create-users", help="从 CSV 批量创建用户")
    s.add_argument("csv", help="列: primaryEmail,givenName,familyName[,orgUnitPath,password,recoveryEmail,recoveryPhone]")
    s.add_argument("--org-unit", default="/", help="CSV 未指定 orgUnitPath 时的默认 OU")
    s.add_argument("--out", default="created_users.csv", help="输出初始密码的 CSV")
    s.add_argument("--password-length", type=int, default=16)

    s = sub.add_parser("reset-password", help="批量重置密码")
    s.add_argument("input", help="邮箱列表（txt 每行一个，或含 primaryEmail 列的 CSV）")
    s.add_argument("--out", default="reset_passwords.csv")
    s.add_argument("--password-length", type=int, default=16)
    s.add_argument("--signout", action="store_true", help="重置后同时强制登出所有会话")

    s = sub.add_parser("move-ou", help="批量移动到 OU")
    s.add_argument("input")
    s.add_argument("org_unit", help="目标 OU，如 /Staff/Enforce2SV")

    for name, desc in (("signout", "批量强制登出"), ("suspend", "批量停用"), ("unsuspend", "批量恢复")):
        sub.add_parser(name, help=desc).add_argument("input", help="邮箱列表")

    s = sub.add_parser("audit", help="导出安全审计报表")
    s.add_argument("--org-unit", help="只审计该 OU（含子 OU）")
    s.add_argument("--out", default="audit.csv")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    svc = build_service(args.key, args.admin)
    handlers = {
        "create-ou": cmd_create_ou,
        "create-users": cmd_create_users,
        "reset-password": cmd_reset_password,
        "move-ou": cmd_move_ou,
        "signout": cmd_signout,
        "suspend": lambda s_, a: cmd_suspend(s_, a, True),
        "unsuspend": lambda s_, a: cmd_suspend(s_, a, False),
        "audit": cmd_audit,
    }
    handlers[args.cmd](svc, args)


if __name__ == "__main__":
    main()
