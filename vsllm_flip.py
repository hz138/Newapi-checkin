#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VSLLM runner for the every-two-hour GitHub Actions workflow.

By default it prioritises draws and can supplement currently executable daily
tasks using the same anti-waste ordering as the daily check-in workflow.
"""

import os
import sys

import checkin
from vsllm_tasks import is_vsllm_url, run_for_account


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        value = default
    return max(minimum, value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in ("0", "false", "off", "no")


def _load_accounts_string() -> str:
    config_url = os.environ.get("CONFIG_URL", "")
    config_auth = os.environ.get("CONFIG_AUTH", "")

    accounts_str = ""
    if config_url:
        accounts_str = checkin.load_config_from_cloud(config_url, config_auth) or ""
    if not accounts_str:
        accounts_str = os.environ.get("NEWAPI_ACCOUNTS", "")
    if not accounts_str:
        env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.isfile(env_file):
            with open(env_file, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line.startswith("NEWAPI_ACCOUNTS="):
                        accounts_str = line[len("NEWAPI_ACCOUNTS="):]
                        break
    return accounts_str


def main() -> int:
    checkin.load_env_file()
    execution_time = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 50)
    print("VSLLM 自动翻卡")
    print(f"执行时间: {execution_time}")
    print("=" * 50)

    accounts_str = _load_accounts_string()
    if not accounts_str:
        print("[错误] 未配置账号信息")
        print("请设置 CONFIG_URL（云端配置）或 NEWAPI_ACCOUNTS（本地配置）环境变量")
        return 1

    accounts = checkin.parse_accounts(accounts_str)
    if not accounts:
        print("[错误] 账号配置解析失败")
        return 1

    complete_tasks = _env_bool("VSLLM_FLIP_COMPLETE_TASKS", True)
    default_draw_limit = 12 if complete_tasks else 4
    draw_limit = _env_int(
        "VSLLM_FLIP_DRAW_LIMIT",
        _env_int("VSLLM_DRAW_LIMIT", default_draw_limit),
    )
    request_interval = max(0.0, float(os.environ.get("VSLLM_REQUEST_INTERVAL_SECONDS", "1") or 1))
    share_unlock = os.environ.get("VSLLM_SHARE_UNLOCK", "1").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )

    checked = 0
    failed = 0
    for index, account in enumerate(accounts, 1):
        url = account.get("url", "")
        if not is_vsllm_url(url):
            continue

        checked += 1
        name = account.get("name") or f"账号{index}"
        print(f"[{index}/{len(accounts)}] {name}")
        print(f"  站点: {checkin.NewAPICheckin._mask_url(url)}")

        client = checkin.NewAPICheckin(
            url,
            account.get("session", ""),
            account.get("user_id"),
            account.get("cf_clearance"),
            account.get("login_username"),
            account.get("login_password"),
            account.get("access_token"),
        )
        user_info = client.get_user_info()
        if user_info is None:
            print("  [警告] 获取用户信息失败，仍尝试使用现有认证调用翻卡接口")

        try:
            result = run_for_account(
                client.session,
                url,
                complete_tasks=complete_tasks,
                draw_limit=draw_limit,
                request_interval_seconds=request_interval,
                share_unlock=share_unlock,
            )
        except Exception as exc:
            failed += 1
            print(f"  结果: ❌ 翻卡异常: {exc}")
            print()
            continue

        for item in result.get("items", []):
            print(f"  {item}")
        for warning in result.get("warnings", []):
            print(f"  [警告] {warning}")
        for error in result.get("errors", []):
            print(f"  [错误] {error}")

        if result.get("status_ok") or result.get("successful_draws", 0) > 0:
            print(
                f"  结果: ✅ 请求翻卡 {result.get('draw_attempts', 0)} 次，"
                f"剩余可用 {result.get('available_draws', 0)} 次"
            )
        else:
            failed += 1
            print("  结果: ❌ 状态读取和翻卡请求均失败")
        print()

    print("=" * 50)
    print(f"翻卡完成: VSLLM 账号 {checked}，失败 {failed}")
    print("=" * 50)

    if checked == 0:
        print("[跳过] 账号配置中没有 VSLLM 站点")
        return 0
    return 1 if failed == checked else 0


if __name__ == "__main__":
    sys.exit(main())
