#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VSLLM gwent task/draw automation.

This module intentionally uses the authenticated ``requests.Session`` created by
``checkin.py``.  It never logs cookies, tokens, or session values.
"""

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests


_DEFAULT_ANSWER_CACHE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".cache",
    "vsllm-daily-task-answers.json",
)
_STOP_DRAW_RE = re.compile(
    r"次数.*(不足|用完|没有)|没有.*次数|没有.*机会|机会.*不足|已用完|"
    r"后可再抽|冷却|cooldown|too\s*many|too\s*frequent|wait|later",
    re.IGNORECASE,
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in ("0", "false", "off", "no")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw is not None and raw.strip() else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw is not None and raw.strip() else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _format_quota(value: Any) -> str:
    quota = _number(value, 0)
    if quota >= 1000000:
        return f"{quota / 1000000:.2f}M"
    if quota >= 1000:
        return f"{quota / 1000:.2f}K"
    if quota == int(quota):
        return str(int(quota))
    return f"{quota:.2f}"


def is_vsllm_url(url: str) -> bool:
    """Return whether an account URL is a VSLLM host."""
    try:
        host = urlparse(url if "://" in url else f"https://{url}").hostname or url
    except Exception:
        host = url or ""
    return "vsllm" in host.lower()


def available_draw_count(status: Optional[Dict[str, Any]]) -> int:
    """Available draws = current charges + extra draw tickets."""
    if not isinstance(status, dict):
        return 0
    return max(
        0,
        _integer(status.get("charges_current"), 0)
        + _integer(status.get("extra_draws_left"), 0),
    )


def get_ready_model_task_tier(status: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the current already-completed model task tier, if claimable."""
    task = (status or {}).get("tasks", {}).get("task1")
    if not isinstance(task, dict) or task.get("all_done"):
        return None
    tiers = task.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        return None
    step = _integer(task.get("current_step"), 0)
    tier = tiers[step] if 0 <= step < len(tiers) else None
    if not isinstance(tier, dict):
        return None
    if _integer(task.get("current_count"), 0) < _integer(tier.get("target_count"), 0):
        return None
    return {"task": task, "tier": tier, "step": step}


def is_ad_task_ready(
    status: Optional[Dict[str, Any]], now_seconds: Optional[int] = None
) -> bool:
    task = (status or {}).get("tasks", {}).get("task2")
    if not isinstance(task, dict) or task.get("suspended"):
        return False
    remaining = max(
        0,
        _integer(task.get("daily_cap"), 0) - _integer(task.get("done_count"), 0),
    )
    now_value = int(time.time()) if now_seconds is None else int(now_seconds)
    return remaining > 0 and _integer(task.get("next_available_at"), 0) <= now_value


def is_quiz_task_ready(status: Optional[Dict[str, Any]]) -> bool:
    task = (status or {}).get("tasks", {}).get("task3")
    return bool(
        isinstance(task, dict)
        and not task.get("suspended")
        and task.get("status") != "won"
    )


def normalize_quiz_question(question: Any) -> Dict[str, Any]:
    if not isinstance(question, dict):
        return {"text": "", "options": []}
    text = question.get("text", question.get("title", question.get("question", "")))
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    raw_options = question.get("options", question.get("choices", question.get("answers", [])))
    options: List[str] = []
    if isinstance(raw_options, list):
        for option in raw_options:
            if isinstance(option, dict):
                option = option.get("text", option.get("title", option.get("label", "")))
            option_text = re.sub(r"\s+", " ", str(option or "")).strip()
            if option_text:
                options.append(option_text)
    return {"text": text, "options": options}


def quiz_question_key(question: Dict[str, Any]) -> str:
    return json.dumps(
        [question.get("text", ""), question.get("options", [])],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def choose_quiz_answer(
    question: Dict[str, Any], answer_cache: Dict[str, Any]
) -> Tuple[str, int, Dict[str, Any]]:
    options = question.get("options") or []
    if not options:
        return "", -1, {}
    key = quiz_question_key(question)
    cached = answer_cache.get(key)
    entry = cached if isinstance(cached, dict) else {"correct_index": None, "rejected_indexes": []}
    correct_index = entry.get("correct_index")
    if correct_index is not None:
        try:
            correct_index = int(correct_index)
        except (TypeError, ValueError):
            correct_index = -1
        if 0 <= correct_index < len(options):
            return key, correct_index, entry
    rejected = set()
    for value in entry.get("rejected_indexes") or []:
        try:
            rejected.add(int(value))
        except (TypeError, ValueError):
            continue
    answer_index = next((i for i in range(len(options)) if i not in rejected), -1)
    if answer_index < 0:
        answer_index = 0
        entry["rejected_indexes"] = []
    return key, answer_index, entry


def describe_task_reward(task: Optional[Dict[str, Any]]) -> str:
    task = task if isinstance(task, dict) else {}
    amount = _number(task.get("reward_amount"), 0)
    reward_type = task.get("reward_type")
    if reward_type == "charge":
        return "充能奖励（翻卡次数回满）"
    if reward_type == "extra_draw":
        return f"抽奖券 +{int(amount) if amount == int(amount) else amount}"
    if reward_type == "quota":
        return f"额度 +{_format_quota(amount)}"
    if amount:
        return f"奖励 +{_format_quota(amount)}"
    return "任务奖励已到账"


def _result_data(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not result or not result.get("ok"):
        return None
    payload = result.get("json")
    if not isinstance(payload, dict) or not payload.get("success"):
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _result_message(result: Optional[Dict[str, Any]]) -> str:
    if not result:
        return "无响应"
    if result.get("error"):
        return str(result.get("error"))[:180]
    payload = result.get("json")
    if isinstance(payload, dict):
        message = payload.get("message")
        if message:
            return str(message)
    status = result.get("status")
    body = re.sub(r"<[^>]+>", " ", str(result.get("text") or ""))
    body = re.sub(r"\s+", " ", body).strip()
    if body and status in (401, 403, 429):
        return f"HTTP {status}: {body[:180]}"
    return f"HTTP {status}" if status else "请求失败"


class GwentApi:
    """Small authenticated API client for /api/gwent endpoints."""

    def __init__(
        self,
        session: requests.Session,
        base_url: str,
        request_interval_seconds: float = 1.0,
        sleep_func=time.sleep,
    ):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.request_interval_seconds = max(0.0, request_interval_seconds)
        self.sleep_func = sleep_func
        self.request_headers = {
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/console/personal",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def _needs_page_fallback(self, result: Dict[str, Any]) -> bool:
        if self._looks_like_cloudflare(result):
            return True
        return (
            isinstance(result, dict)
            and not result.get("ok")
            and _integer(result.get("status"), 0) in (0, 403, 429)
        )

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            if method.upper() == "GET":
                response = self.session.get(url, timeout=30, headers=self.request_headers)
            else:
                response = self.session.post(
                    url,
                    json=body,
                    timeout=30,
                    headers=self.request_headers,
                )
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError):
                payload = None
            return {
                "ok": 200 <= response.status_code < 300,
                "status": response.status_code,
                "json": payload,
                "text": response.text[:500],
                "path": path,
            }
        except requests.exceptions.RequestException as exc:
            return {"ok": False, "status": 0, "json": None, "path": path, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "status": 0, "json": None, "path": path, "error": str(exc)}
        finally:
            if self.request_interval_seconds > 0:
                self.sleep_func(self.request_interval_seconds)

    def status(self) -> Dict[str, Any]:
        return self._request("GET", "/api/gwent/status")

    def draw(self) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/draw")

    def share_unlock(self) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/share_unlock")

    def ad_start(self) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/ad/start")

    def ad_claim(self) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/ad/claim")

    def quiz_start(self) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/task3/start")

    def quiz_answer(self, answer_index: int) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/task3/answer", {"answer_index": answer_index})

    def claim_task(self, task_id: str) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/task/claim", {"task_id": task_id})


class BrowserGwentApi:
    """Playwright-backed Gwent API transport.

    Cloudflare can reject a plain requests client even after a successful
    challenge elsewhere.  This transport keeps an authenticated browser
    context alive and performs Gwent calls through that context, falling back
    to an in-page fetch when APIRequestContext is still challenged.
    """

    def __init__(
        self,
        session: requests.Session,
        base_url: str,
        request_interval_seconds: float = 1.0,
        sleep_func=time.sleep,
        navigation_timeout_ms: Optional[int] = None,
        cf_wait_seconds: Optional[float] = None,
    ):
        parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
        origin = f"{parsed.scheme or 'https'}://{parsed.netloc}".rstrip("/")
        self.session = session
        self.base_url = origin
        self.target_url = f"{origin}/console/personal"
        self.request_interval_seconds = max(0.0, float(request_interval_seconds))
        self.sleep_func = sleep_func
        self.navigation_timeout_ms = max(
            10000,
            int(
                navigation_timeout_ms
                if navigation_timeout_ms is not None
                else _env_int("VSLLM_BROWSER_NAVIGATION_TIMEOUT_MS", 60000)
            ),
        )
        self.cf_wait_seconds = max(
            10.0,
            float(
                cf_wait_seconds
                if cf_wait_seconds is not None
                else _env_float("VSLLM_CF_WAIT_SECONDS", 90.0)
            ),
        )
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._started = False
        self._user_id = str(session.headers.get("new-api-user") or "").strip()
        self._authorization = str(session.headers.get("Authorization") or "").strip()
        self._user_agent = str(session.headers.get("User-Agent") or "").strip() or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        self._cookie_header = ""

    @staticmethod
    def _is_cf_challenge(title: str, body: str = "") -> bool:
        title_lower = str(title or "").lower()
        if (
            "just a moment" in title_lower
            or "checking your browser" in title_lower
            or "attention required" in title_lower
            or ("cloudflare" in title_lower and "challenge" in title_lower)
        ):
            return True
        body_lower = str(body or "")[:200000].lower()
        return (
            "just a moment" in body_lower
            or "checking your browser" in body_lower
            or "cf-chl-" in body_lower
            or "cf-chl_" in body_lower
        )

    def _add_session_cookies(self) -> None:
        cookies = []
        for cookie in self.session.cookies:
            if not cookie.name or cookie.value is None:
                continue
            item = {
                "name": str(cookie.name),
                "value": str(cookie.value),
                "path": str(cookie.path or "/"),
            }
            if cookie.domain:
                item["domain"] = str(cookie.domain)
                if cookie.secure:
                    item["secure"] = True
                if cookie.expires:
                    item["expires"] = int(cookie.expires)
            else:
                item["url"] = self.base_url
            cookies.append(item)
        if cookies:
            self._context.add_cookies(cookies)

    def _observe_user_id(self, request) -> None:
        if self._user_id:
            return
        try:
            candidate = request.headers.get("new-api-user") or request.headers.get("New-Api-User")
        except Exception:
            return
        candidate = str(candidate or "").strip()
        if candidate:
            self._user_id = candidate

    def _find_user_id_from_storage(self) -> str:
        try:
            value = self._page.evaluate(
                """() => {
                    const preferred = [
                        'new-api-user', 'new_api_user', 'newApiUser',
                        'userId', 'user_id', 'uid', 'id'
                    ];
                    for (const store of [localStorage, sessionStorage]) {
                        for (const key of preferred) {
                            const raw = store.getItem(key);
                            if (raw && /^[A-Za-z0-9_-]{1,80}$/.test(String(raw).trim())) {
                                return String(raw).trim();
                            }
                        }
                        for (let index = 0; index < store.length; index += 1) {
                            const key = store.key(index);
                            const raw = store.getItem(key) || '';
                            const match = raw.match(
                                /new-api-user["']?\\s*[:=]\\s*["']?([A-Za-z0-9_-]{1,80})/i
                            );
                            if (match) {
                                return match[1];
                            }
                        }
                    }
                    return '';
                }"""
            )
        except Exception:
            return ""
        return str(value or "").strip()

    def _navigate(self, url: str) -> None:
        last_error = None
        for attempt in range(3):
            try:
                self._page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.navigation_timeout_ms,
                )
                return
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    self.sleep_func(3.0 * (attempt + 1))
        raise RuntimeError(f"打开 VSLLM 页面失败：{type(last_error).__name__}") from last_error

    def _wait_for_cloudflare(self) -> None:
        deadline = time.monotonic() + self.cf_wait_seconds
        while time.monotonic() < deadline:
            try:
                title = self._page.title()
            except Exception:
                title = ""
            try:
                body = self._page.content()
            except Exception:
                body = ""
            if not self._is_cf_challenge(title, body):
                return
            try:
                self._page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            self.sleep_func(2.0)
        raise RuntimeError("Cloudflare 验证未通过")

    def _refresh_cookie_header(self) -> None:
        try:
            cookies = self._context.cookies()
        except Exception:
            cookies = []
        self._cookie_header = "; ".join(
            f"{cookie.get('name')}={cookie.get('value')}"
            for cookie in cookies
            if cookie.get("name") and cookie.get("value") is not None
        )

    def _build_headers(self, body: Optional[Dict[str, Any]]) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Origin": self.base_url,
            "Referer": self.target_url,
            "User-Agent": self._user_agent,
            "new-api-user": self._user_id,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if self._authorization:
            headers["Authorization"] = self._authorization
        if self._cookie_header:
            headers["Cookie"] = self._cookie_header
        if body is not None:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _parse_response(status: int, ok: bool, text: str) -> Dict[str, Any]:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        return {
            "ok": bool(ok),
            "status": int(status),
            "json": payload,
            "text": str(text or "")[:500],
        }

    @staticmethod
    def _looks_like_cloudflare(result: Dict[str, Any]) -> bool:
        if not isinstance(result, dict) or _integer(result.get("status"), 0) not in (403, 429):
            return False
        text = str(result.get("text") or "").lower()
        return (
            "just a moment" in text
            or "checking your browser" in text
            or "cf-chl-" in text
            or "cf-chl_" in text
        )

    def _request_via_context(
        self, method: str, path: str, body: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        headers = self._build_headers(body)
        url = f"{self.base_url}{path}"
        if method.upper() == "GET":
            response = self._context.request.get(url, headers=headers, timeout=30000)
            text = response.text()
            return self._parse_response(response.status, response.ok, text)
        data = json.dumps(body, ensure_ascii=False) if body is not None else None
        response = self._context.request.post(
            url,
            headers=headers,
            data=data,
            timeout=30000,
        )
        return self._parse_response(response.status, response.ok, response.text())

    def _request_via_page_fetch(self, method: str, path: str, body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        fetch_headers = {
            "Accept": "application/json, text/plain, */*",
            "new-api-user": self._user_id,
        }
        if self._authorization:
            fetch_headers["Authorization"] = self._authorization
        if body is not None:
            fetch_headers["Content-Type"] = "application/json"
        result = self._page.evaluate(
            """async ({path, method, body, headers}) => {
                const options = {method, headers, credentials: 'include'};
                if (body !== null && body !== undefined) {
                    options.body = JSON.stringify(body);
                }
                const response = await fetch(path, options);
                return {
                    status: response.status,
                    ok: response.ok,
                    text: await response.text()
                };
            }""",
            {"path": path, "method": method, "body": body, "headers": fetch_headers},
        )
        if not isinstance(result, dict):
            return {"ok": False, "status": 0, "json": None, "text": "", "path": path}
        return self._parse_response(
            _integer(result.get("status"), 0),
            bool(result.get("ok")),
            str(result.get("text") or ""),
        )

    def _recover_cloudflare(self) -> bool:
        try:
            self._navigate(self.target_url)
            self._wait_for_cloudflare()
            self._refresh_cookie_header()
            return True
        except Exception:
            return False

    def _ensure_started(self) -> None:
        if self._started:
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=_env_bool("VSLLM_HEADLESS", True),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self._context = self._browser.new_context(
            user_agent=self._user_agent,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self._context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
            """
        )
        self._add_session_cookies()
        self._page = self._context.new_page()
        self._page.on("request", self._observe_user_id)
        self._navigate(self.target_url)
        self._wait_for_cloudflare()
        if not self._user_id:
            self._user_id = self._find_user_id_from_storage()
        if not self._user_id:
            raise RuntimeError("未识别到 new-api-user，无法调用 Gwent API")
        self._refresh_cookie_header()
        self._started = True

    def start(self) -> None:
        self._ensure_started()

    def close(self) -> None:
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
            getattr(self._playwright, "stop", None),
        ):
            if closer is None:
                continue
            try:
                closer()
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None
        self._started = False

    def _needs_page_fallback(self, result: Dict[str, Any]) -> bool:
        if self._looks_like_cloudflare(result):
            return True
        return (
            isinstance(result, dict)
            and not result.get("ok")
            and _integer(result.get("status"), 0) in (0, 403, 429)
        )

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            self._ensure_started()
            self._refresh_cookie_header()
            result = self._request_via_context(method, path, body)
            if not self._needs_page_fallback(result):
                result["path"] = path
                return result

            if self._recover_cloudflare():
                self._refresh_cookie_header()
                result = self._request_via_context(method, path, body)
                if not self._needs_page_fallback(result):
                    result["path"] = path
                    return result

            result = self._request_via_page_fetch(method, path, body)
            result["path"] = path
            return result
        except Exception as exc:
            return {
                "ok": False,
                "status": 0,
                "json": None,
                "text": "",
                "error": f"{type(exc).__name__}: {exc}",
                "path": path,
            }
        finally:
            if self.request_interval_seconds > 0:
                self.sleep_func(self.request_interval_seconds)

    def status(self) -> Dict[str, Any]:
        return self._request("GET", "/api/gwent/status")

    def draw(self) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/draw")

    def share_unlock(self) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/share_unlock")

    def ad_start(self) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/ad/start")

    def ad_claim(self) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/ad/claim")

    def quiz_start(self) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/task3/start")

    def quiz_answer(self, answer_index: int) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/task3/answer", {"answer_index": answer_index})

    def claim_task(self, task_id: str) -> Dict[str, Any]:
        return self._request("POST", "/api/gwent/task/claim", {"task_id": task_id})

class VsllmAutomation:
    """Anti-waste draw/task state machine.

    Rewards from the ad and quiz tasks reset draw charges.  Therefore the
    machine always consumes all currently available draws first, then claims
    at most one ready reward, refreshes status, and consumes the reset draws.
    """

    def __init__(
        self,
        session: requests.Session,
        base_url: str,
        complete_tasks: Optional[bool] = None,
        draw_limit: Optional[int] = None,
        ad_limit: Optional[int] = None,
        quiz_attempts: Optional[int] = None,
        ad_max_wait_seconds: Optional[float] = None,
        request_interval_seconds: Optional[float] = None,
        share_unlock: Optional[bool] = None,
        claim_model_task: Optional[bool] = None,
        answer_cache_file: Optional[str] = None,
        sleep_func=time.sleep,
        now_func=time.time,
        api: Optional[Any] = None,
    ):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.complete_tasks = _env_bool("VSLLM_DAILY_TASKS", True) if complete_tasks is None else complete_tasks
        self.draw_limit = max(1, _env_int("VSLLM_DRAW_LIMIT", 12) if draw_limit is None else int(draw_limit))
        self.ad_limit = max(1, _env_int("VSLLM_TASK_AD_LIMIT", 3) if ad_limit is None else int(ad_limit))
        self.quiz_attempts = max(
            1, _env_int("VSLLM_TASK_QUIZ_ATTEMPTS", 8) if quiz_attempts is None else int(quiz_attempts)
        )
        self.ad_max_wait_seconds = (
            _env_float("VSLLM_AD_MAX_WAIT_SECONDS", 300.0)
            if ad_max_wait_seconds is None
            else max(0.0, float(ad_max_wait_seconds))
        )
        interval = (
            _env_float("VSLLM_REQUEST_INTERVAL_SECONDS", 1.0)
            if request_interval_seconds is None
            else max(0.0, float(request_interval_seconds))
        )
        self.share_unlock_enabled = (
            _env_bool("VSLLM_SHARE_UNLOCK", True) if share_unlock is None else share_unlock
        )
        self.claim_model_task = (
            _env_bool("VSLLM_CLAIM_MODEL_TASK", False)
            if claim_model_task is None
            else claim_model_task
        )
        self.answer_cache_file = (
            answer_cache_file
            or os.environ.get("VSLLM_ANSWER_CACHE_FILE", "").strip()
            or _DEFAULT_ANSWER_CACHE
        )
        self.sleep_func = sleep_func
        self.now_func = now_func
        self.api = api if api is not None else GwentApi(self.session, self.base_url, interval, sleep_func=sleep_func)
        self.items: List[str] = []
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.draw_attempts = 0
        self.draw_successes = 0
        self.model_claims = 0
        self.ad_completions = 0
        self.quiz_attempted = False
        self.model_blocked = False
        self.ad_blocked = False
        self.stop_reason = ""

    def _read_status(self) -> Optional[Dict[str, Any]]:
        result = self.api.status()
        data = _result_data(result)
        if data is None:
            message = f"读取翻卡状态失败：{_result_message(result)}"
            if message not in self.errors:
                self.errors.append(message)
            return None
        return data

    def _available(self, status: Optional[Dict[str, Any]], fallback: Optional[int]) -> int:
        if status is not None:
            return available_draw_count(status)
        return max(0, int(fallback or 0))

    def _load_answer_cache(self) -> Dict[str, Any]:
        try:
            with open(self.answer_cache_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _save_answer_cache(self, data: Dict[str, Any]) -> None:
        try:
            directory = os.path.dirname(os.path.abspath(self.answer_cache_file))
            os.makedirs(directory, exist_ok=True)
            temp_path = f"{self.answer_cache_file}.tmp"
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(temp_path, self.answer_cache_file)
        except OSError as exc:
            self.warnings.append(f"保存答题缓存失败：{exc}")

    def _draw_once(self, draw_number: int) -> bool:
        result = self.api.draw()
        self.draw_attempts += 1
        data = _result_data(result)
        if data is None:
            message = _result_message(result)
            if _STOP_DRAW_RE.search(message) or result.get("status") == 401:
                self.stop_reason = message
            else:
                self.errors.append(f"翻卡失败：{message}")
            return False

        self.draw_successes += 1
        prize = data.get("prize") if isinstance(data.get("prize"), dict) else {}
        name = str(prize.get("name") or "未知奖励")
        quota = prize.get("quota", 0)
        quota_text = f" +{_format_quota(quota)}" if _number(quota, 0) > 0 else ""
        self.items.append(f"🎉 第{draw_number}次 {name}{quota_text}")
        return True

    def _refresh_or_fallback(
        self, status: Optional[Dict[str, Any]], fallback: Optional[int], available_before: int
    ) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
        refreshed = self._read_status()
        if refreshed is not None:
            return refreshed, None
        return None, max(0, available_before - 1)

    def _run_ad_task(self, status: Dict[str, Any]) -> Dict[str, Any]:
        task = (status.get("tasks") or {}).get("task2") or {}
        start_result = self.api.ad_start()
        started = _result_data(start_result)
        if started is None:
            self.ad_blocked = True
            self.errors.append(f"广告任务开始失败：{_result_message(start_result)}")
            return {"performed": False, "state_fresh": True, "status": status}

        duration = max(0.0, _number(started.get("duration_sec"), 15.0))
        if duration > self.ad_max_wait_seconds:
            self.ad_blocked = True
            self.errors.append(
                f"广告时长 {duration:g} 秒超过安全上限 {self.ad_max_wait_seconds:g} 秒，未领取奖励"
            )
            return {"performed": False, "state_fresh": True, "status": status}

        self.sleep_func(duration + 1.2)
        claim_result = self.api.ad_claim()
        claimed = _result_data(claim_result)
        if claimed is None:
            self.ad_blocked = True
            self.errors.append(f"广告奖励领取失败：{_result_message(claim_result)}")
            return {"performed": False, "state_fresh": True, "status": status}

        refreshed = self._read_status()
        self.items.append(f"✅ 看广告：{describe_task_reward(task)}")
        return {
            "performed": True,
            "state_fresh": refreshed is not None,
            "status": refreshed if refreshed is not None else status,
        }

    def _run_quiz_task(self, status: Dict[str, Any]) -> Dict[str, Any]:
        self.quiz_attempted = True
        task = (status.get("tasks") or {}).get("task3") or {}
        answer_cache = self._load_answer_cache()
        blocked = False
        attempts_made = 0

        for attempt in range(1, self.quiz_attempts + 1):
            attempts_made = attempt
            start_result = self.api.quiz_start()
            started = _result_data(start_result)
            question = started.get("question") if isinstance(started, dict) else None
            if not question:
                blocked = True
                self.errors.append(f"每日答题开始失败：{_result_message(start_result)}")
                break

            normalized = normalize_quiz_question(question)
            if not normalized["text"] or not normalized["options"]:
                blocked = True
                self.errors.append("每日答题题目格式无法识别")
                break

            key, answer_index, entry = choose_quiz_answer(normalized, answer_cache)
            if answer_index < 0:
                blocked = True
                self.errors.append("每日答题没有可选答案")
                break

            answer_result = self.api.quiz_answer(answer_index)
            answered = _result_data(answer_result)
            if answered is None:
                blocked = True
                self.errors.append(f"每日答题第 {attempt} 次提交失败：{_result_message(answer_result)}")
                break

            if answered.get("correct"):
                entry["correct_index"] = answer_index
                entry["rejected_indexes"] = []
                answer_cache[key] = entry
                self._save_answer_cache(answer_cache)
                refreshed = self._read_status()
                self.items.append(f"✅ 每日答题：{describe_task_reward(task)}")
                return {
                    "performed": True,
                    "state_fresh": refreshed is not None,
                    "status": refreshed if refreshed is not None else status,
                }

            rejected = set(entry.get("rejected_indexes") or [])
            rejected.add(answer_index)
            entry["correct_index"] = None
            entry["rejected_indexes"] = sorted(rejected)
            answer_cache[key] = entry
            self._save_answer_cache(answer_cache)
            if attempt < self.quiz_attempts:
                self.sleep_func(1.0)

        refreshed = self._read_status()
        if not blocked:
            self.errors.append(f"每日答题达到 {self.quiz_attempts} 次尝试上限，仍未答对")
        return {
            "performed": attempts_made > 0,
            "state_fresh": refreshed is not None,
            "status": refreshed if refreshed is not None else status,
        }

    def _run_model_task(self, status: Dict[str, Any]) -> Dict[str, Any]:
        ready = get_ready_model_task_tier(status)
        if not ready:
            return {"performed": False, "state_fresh": True, "status": status}
        result = self.api.claim_task("model_usage")
        if _result_data(result) is None:
            self.model_blocked = True
            self.errors.append(f"模型使用任务领奖失败：{_result_message(result)}")
            return {"performed": False, "state_fresh": True, "status": status}
        refreshed = self._read_status()
        self.items.append(f"✅ 模型使用任务：{describe_task_reward(ready['tier'])}")
        return {
            "performed": True,
            "state_fresh": refreshed is not None,
            "status": refreshed if refreshed is not None else status,
        }

    def run(self) -> Dict[str, Any]:
        if self.share_unlock_enabled:
            result = self.api.share_unlock()
            if _result_data(result) is None:
                self.warnings.append(f"分享解锁失败：{_result_message(result)}")

        status = self._read_status()
        status_ok = status is not None
        fallback_available: Optional[int] = None if status is not None else self.draw_limit
        max_steps = self.draw_limit + self.quiz_attempts + self.ad_limit + 10

        for _ in range(max_steps):
            available = self._available(status, fallback_available)
            if self.draw_attempts >= self.draw_limit:
                self.stop_reason = f"达到本轮翻卡上限 {self.draw_limit} 次"
                break

            if available > 0:
                if not self._draw_once(self.draw_attempts + 1):
                    break
                status, fallback_available = self._refresh_or_fallback(status, fallback_available, available)
                continue

            if not self.complete_tasks or status is None:
                self.stop_reason = "当前没有可用翻卡次数"
                break

            model_ready = (
                self.claim_model_task
                and not self.model_blocked
                and self.model_claims < 8
                and get_ready_model_task_tier(status) is not None
            )
            ad_ready = (
                not self.ad_blocked
                and self.ad_completions < self.ad_limit
                and is_ad_task_ready(status, now_seconds=int(self.now_func()))
            )
            quiz_ready = not self.quiz_attempted and is_quiz_task_ready(status)

            if model_ready:
                result = self._run_model_task(status)
                if result["performed"]:
                    self.model_claims += 1
            elif ad_ready:
                result = self._run_ad_task(status)
                if result["performed"]:
                    self.ad_completions += 1
            elif quiz_ready:
                result = self._run_quiz_task(status)
            else:
                self.stop_reason = "没有当前可执行的翻卡或每日任务"
                break

            status = result.get("status") if isinstance(result.get("status"), dict) else None
            fallback_available = None
            if result.get("performed") and not result.get("state_fresh"):
                self.errors.append("任务奖励后未能刷新状态，已停止以避免重复领奖")
                self.stop_reason = "任务奖励后状态刷新失败"
                break

        final_status = self._read_status()
        if final_status is not None:
            status = final_status
        final_available = available_draw_count(status)
        if not self.stop_reason:
            self.stop_reason = "本轮处理完成"

        return {
            "items": list(self.items),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "draw_attempts": self.draw_attempts,
            "successful_draws": self.draw_successes,
            "model_claims": self.model_claims,
            "ad_completions": self.ad_completions,
            "quiz_attempted": self.quiz_attempted,
            "available_draws": final_available,
            "status_ok": status_ok or final_status is not None,
            "stop_reason": self.stop_reason,
        }


def run_for_account(
    session: requests.Session,
    base_url: str,
    complete_tasks: Optional[bool] = None,
    browser_transport: Optional[bool] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run one account through the anti-waste automation cycle."""
    use_browser = (
        _env_bool("VSLLM_BROWSER_TRANSPORT", True)
        if browser_transport is None
        else bool(browser_transport)
    )
    browser_api: Optional[BrowserGwentApi] = None
    if use_browser:
        request_interval = kwargs.get("request_interval_seconds")
        try:
            browser_api = BrowserGwentApi(
                session,
                base_url,
                request_interval_seconds=(
                    float(request_interval) if request_interval is not None else 1.0
                ),
                sleep_func=kwargs.get("sleep_func", time.sleep),
            )
            browser_api.start()
            kwargs["api"] = browser_api
        except Exception as exc:
            if browser_api is not None:
                browser_api.close()
            browser_api = None
            if _env_bool("VSLLM_BROWSER_REQUIRED", False):
                raise RuntimeError(
                    f"Playwright API 初始化失败：{type(exc).__name__}: {exc}"
                ) from exc
            print(f"[警告] Playwright API 初始化失败，回退普通请求：{type(exc).__name__}: {exc}")

    try:
        automation = VsllmAutomation(session, base_url, complete_tasks=complete_tasks, **kwargs)
        result = automation.run()
        result["transport"] = "browser" if browser_api is not None else "requests"
        return result
    finally:
        if browser_api is not None:
            browser_api.close()
