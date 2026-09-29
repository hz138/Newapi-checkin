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
    payload = result.get("json")
    if isinstance(payload, dict):
        message = payload.get("message")
        if message:
            return str(message)
    status = result.get("status")
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

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            if method.upper() == "GET":
                response = self.session.get(url, timeout=30)
            else:
                response = self.session.post(url, json=body, timeout=30)
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError):
                payload = None
            return {
                "ok": 200 <= response.status_code < 300,
                "status": response.status_code,
                "json": payload,
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
        self.api = GwentApi(self.session, self.base_url, interval, sleep_func=sleep_func)
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
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run one account through the anti-waste automation cycle."""
    automation = VsllmAutomation(session, base_url, complete_tasks=complete_tasks, **kwargs)
    return automation.run()
