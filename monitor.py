from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


FORECAST_URL = "https://codex-reset.com/api/forecast"
SITE_URL = "https://codex-reset.com/"
BEIJING = timezone(timedelta(hours=8))
STATE_PATH = "monitor-state.json"
STATE_BRANCH = "monitor-state"
HEARTBEAT_INTERVAL = timedelta(days=30)


class MonitorError(RuntimeError):
    """Base error for expected monitor failures."""


class ForecastError(MonitorError):
    """The third-party forecast is unavailable or invalid."""


class NotificationError(MonitorError):
    """Enterprise WeChat rejected or could not receive a notification."""


class StateStoreError(MonitorError):
    """The monitor state could not be read or written."""


@dataclass(frozen=True)
class Forecast:
    probability_24h: int
    updated_at: str
    last_reset_at: str
    confidence: str | None = None
    confidence_note: str | None = None
    announcement_at: str | None = None
    announcement_summary: str | None = None
    announcement_url: str | None = None
    window_start_at: str | None = None
    window_end_at: str | None = None
    window_target_at: str | None = None
    window_label: str | None = None
    window_timezone: str | None = None


@dataclass
class MonitorState:
    schema_version: int = 1
    initialized: bool = False
    above_threshold: bool = False
    last_observed_reset_at: str | None = None
    consecutive_failures: int = 0
    failure_alert_sent: bool = False
    last_heartbeat_at: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MonitorState":
        last_reset = raw.get("last_observed_reset_at")
        heartbeat = raw.get("last_heartbeat_at")
        return cls(
            schema_version=max(1, int(raw.get("schema_version", 1))),
            initialized=bool(raw.get("initialized", False)),
            above_threshold=bool(raw.get("above_threshold", False)),
            last_observed_reset_at=last_reset if isinstance(last_reset, str) else None,
            consecutive_failures=max(0, int(raw.get("consecutive_failures", 0))),
            failure_alert_sent=bool(raw.get("failure_alert_sent", False)),
            last_heartbeat_at=heartbeat if isinstance(heartbeat, str) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed


def format_beijing(value: str | datetime) -> str:
    parsed = parse_iso(value) if isinstance(value, str) else value
    return parsed.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def valid_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parse_iso(value)
    except ValueError:
        return None
    return value


def heartbeat_due(state: MonitorState, now: datetime) -> bool:
    if not state.last_heartbeat_at:
        return True
    try:
        return now - parse_iso(state.last_heartbeat_at) >= HEARTBEAT_INTERVAL
    except (TypeError, ValueError):
        return True


def reset_is_newer(previous: str | None, current: str) -> bool:
    if not previous:
        return False
    try:
        return parse_iso(current) > parse_iso(previous)
    except (TypeError, ValueError):
        return False


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
    allow_404: bool = False,
    label: str | None = None,
) -> dict[str, Any] | None:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "codex-reset-wecom-monitor/1.0",
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    safe_target = label or url

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        if allow_404 and exc.code == 404:
            return None
        raise MonitorError(f"{safe_target} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc.__class__.__name__)
        raise MonitorError(f"Request to {safe_target} failed: {reason}") from exc

    if not content:
        return {}
    try:
        parsed = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MonitorError(f"{safe_target} returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise MonitorError(f"{safe_target} did not return a JSON object")
    return parsed


def fetch_forecast(*, attempts: int = 3, timeout: int = 15) -> Forecast:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            payload = request_json(FORECAST_URL, timeout=timeout, label="forecast API")
            if payload is None:
                raise ForecastError("forecast API returned no data")
            probabilities = payload.get("probabilities")
            if not isinstance(probabilities, dict):
                raise ForecastError("missing probabilities object")
            probability = probabilities.get("rounded_24h")
            if isinstance(probability, bool) or not isinstance(probability, (int, float)):
                raise ForecastError("missing numeric probabilities.rounded_24h")
            probability_int = int(probability)
            if not 0 <= probability_int <= 100:
                raise ForecastError("probabilities.rounded_24h is outside 0..100")

            updated_at = payload.get("updated_at")
            last_reset_at = payload.get("last_reset_at")
            if not isinstance(updated_at, str) or not isinstance(last_reset_at, str):
                raise ForecastError("missing updated_at or last_reset_at")
            parse_iso(updated_at)
            parse_iso(last_reset_at)

            confidence = payload.get("confidence")
            confidence_note = payload.get("confidence_note")
            latest_alert = payload.get("latest_alert")
            if not isinstance(latest_alert, dict):
                latest_alert = {}
            window = latest_alert.get("window")
            if not isinstance(window, dict):
                window = payload.get("teased_window")
            if not isinstance(window, dict):
                window = {}
            return Forecast(
                probability_24h=probability_int,
                updated_at=updated_at,
                last_reset_at=last_reset_at,
                confidence=confidence if isinstance(confidence, str) else None,
                confidence_note=confidence_note if isinstance(confidence_note, str) else None,
                announcement_at=valid_timestamp(latest_alert.get("source_at")),
                announcement_summary=(
                    latest_alert.get("summary") if isinstance(latest_alert.get("summary"), str) else None
                ),
                announcement_url=(
                    latest_alert.get("url") if isinstance(latest_alert.get("url"), str) else None
                ),
                window_start_at=valid_timestamp(window.get("start_at")),
                window_end_at=valid_timestamp(window.get("end_at")),
                window_target_at=valid_timestamp(window.get("target_at")),
                window_label=window.get("label") if isinstance(window.get("label"), str) else None,
                window_timezone=(
                    window.get("time_zone") if isinstance(window.get("time_zone"), str) else None
                ),
            )
        except (MonitorError, ValueError, TypeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt)
    raise ForecastError(f"forecast request failed after {attempts} attempts: {last_error}")


class WeComNotifier:
    def __init__(self, webhook: str) -> None:
        self.webhook = webhook.strip()
        parsed = urllib.parse.urlparse(self.webhook)
        query = urllib.parse.parse_qs(parsed.query)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "qyapi.weixin.qq.com"
            or parsed.path != "/cgi-bin/webhook/send"
            or not query.get("key")
        ):
            raise NotificationError("WECOM_WEBHOOK is not a valid Enterprise WeChat group-bot webhook")

    @classmethod
    def from_env(cls) -> "WeComNotifier":
        return cls(os.getenv("WECOM_WEBHOOK", ""))

    def _send_text(self, content: str) -> None:
        if len(content.encode("utf-8")) > 2048:
            raise NotificationError("Enterprise WeChat text message exceeds 2048 bytes")
        try:
            response = request_json(
                self.webhook,
                method="POST",
                payload={"msgtype": "text", "text": {"content": content}},
                timeout=20,
                label="Enterprise WeChat webhook",
            )
        except MonitorError as exc:
            raise NotificationError(str(exc)) from exc
        if response is None:
            raise NotificationError("Enterprise WeChat webhook returned no response")
        errcode = response.get("errcode")
        if errcode != 0:
            errmsg = str(response.get("errmsg", "unknown error"))[:200]
            raise NotificationError(f"Enterprise WeChat rejected the message: {errcode} {errmsg}")

    @staticmethod
    def _confidence_label(value: str | None) -> str:
        return {"low": "低", "medium": "中", "high": "高"}.get(value or "", value or "未提供")

    @staticmethod
    def _announcement_at(forecast: Forecast) -> str:
        return forecast.announcement_at or forecast.last_reset_at

    @staticmethod
    def _window_lines(forecast: Forecast) -> list[str]:
        if forecast.window_start_at and forecast.window_end_at:
            start = format_beijing(forecast.window_start_at)
            end = format_beijing(forecast.window_end_at)
            lines = [f"预计重置窗口：{start} 至 {end}（北京时间）"]
        elif forecast.window_target_at:
            lines = [f"预计重置时间：{format_beijing(forecast.window_target_at)}（北京时间）"]
        else:
            return []

        if forecast.window_label:
            lines.append(f"原公告时间表述：{forecast.window_label}")
        return lines

    def send_probability_alert(self, forecast: Forecast, threshold: int, checked_at: datetime) -> None:
        content = "\n".join(
            [
                "【Codex 重置预报】",
                "状态：较高（第三方预测）",
                f"未来 24 小时概率：{forecast.probability_24h}%",
                f"提醒阈值：严格大于 {threshold}%",
                f"模型置信度：{self._confidence_label(forecast.confidence)}",
                f"检查时间：{format_beijing(checked_at)}（北京时间）",
                f"数据更新时间：{format_beijing(forecast.updated_at)}（北京时间）",
                f"最近一次 X 公告/确认时间：{format_beijing(self._announcement_at(forecast))}（北京时间）",
                *self._window_lines(forecast),
                "",
                "说明：这是 codex-reset.com 的实验性预测，并非 OpenAI 官方预告，也不是你的个人额度倒计时。",
                f"第三方数据源：{SITE_URL}",
            ]
        )
        self._send_text(content)

    def send_reset_alert(self, forecast: Forecast, checked_at: datetime) -> None:
        content = "\n".join(
            [
                "【Codex 重置动态】",
                "状态：发现新记录",
                "告警内容：第三方数据源新增一条全局重置记录",
                f"X 公告/确认时间：{format_beijing(self._announcement_at(forecast))}（北京时间）",
                *self._window_lines(forecast),
                f"检测时间：{format_beijing(checked_at)}（北京时间）",
                "",
                "说明：X 公告/确认时间不一定等于额度实际生效时间；请以官方公告和你账号的实际状态为准。",
                f"第三方数据源：{SITE_URL}",
            ]
        )
        self._send_text(content)

    def send_failure_alert(self, error: Exception, failures: int, checked_at: datetime) -> None:
        content = "\n".join(
            [
                "【Codex 重置监控故障】",
                "状态：连续检查失败",
                f"连续失败次数：{failures}",
                f"检查时间：{format_beijing(checked_at)}（北京时间）",
                f"最近错误：{str(error)[:300]}",
                "",
                "监控会继续定时重试，恢复后会另行通知。",
            ]
        )
        self._send_text(content)

    def send_recovery_alert(self, previous_failures: int, checked_at: datetime) -> None:
        content = "\n".join(
            [
                "【Codex 重置监控恢复】",
                "状态：接口已恢复",
                f"此前连续失败：{previous_failures} 次",
                f"恢复时间：{format_beijing(checked_at)}（北京时间）",
            ]
        )
        self._send_text(content)

    def send_test_notification(self, checked_at: datetime) -> None:
        content = "\n".join(
            [
                "【Codex 重置监控】",
                "状态：微信兼容纯文本推送配置成功",
                f"测试时间：{format_beijing(checked_at)}（北京时间）",
                "备注：本次测试不会修改正式监控状态",
                "",
                "正式消息会明确区分第三方预测、X 公告/确认时间、预计重置窗口和监控故障。",
            ]
        )
        self._send_text(content)


class GitHubStateStore:
    def __init__(self, repository: str, token: str, branch: str = STATE_BRANCH) -> None:
        self.repository = repository.strip()
        self.token = token.strip()
        self.branch = branch
        self.file_sha: str | None = None
        if not self.repository or "/" not in self.repository or not self.token:
            raise StateStoreError("GITHUB_REPOSITORY and GITHUB_TOKEN are required")
        self.api_base = f"https://api.github.com/repos/{self.repository}"
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Accept": "application/vnd.github+json",
        }

    @classmethod
    def from_env(cls) -> "GitHubStateStore":
        return cls(
            os.getenv("GITHUB_REPOSITORY", ""),
            os.getenv("GITHUB_TOKEN", ""),
            os.getenv("STATE_BRANCH", STATE_BRANCH),
        )

    def _api(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        try:
            return request_json(
                f"{self.api_base}{path}",
                method=method,
                payload=payload,
                headers=self.headers,
                allow_404=allow_404,
                label="GitHub state API",
            )
        except MonitorError as exc:
            raise StateStoreError(str(exc)) from exc

    def load(self) -> MonitorState:
        branch = urllib.parse.quote(self.branch, safe="")
        path = urllib.parse.quote(STATE_PATH, safe="/")
        data = self._api(f"/contents/{path}?ref={branch}", allow_404=True)
        if data is None:
            self.file_sha = None
            return MonitorState()
        try:
            encoded = str(data["content"]).replace("\n", "")
            raw = json.loads(base64.b64decode(encoded).decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state is not an object")
            self.file_sha = str(data["sha"])
            return MonitorState.from_dict(raw)
        except (KeyError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateStoreError("invalid monitor state file") from exc

    def _ensure_branch(self) -> None:
        branch_ref = urllib.parse.quote(f"heads/{self.branch}", safe="/")
        if self._api(f"/git/ref/{branch_ref}", allow_404=True) is not None:
            return
        repository = self._api("")
        if repository is None or not isinstance(repository.get("default_branch"), str):
            raise StateStoreError("could not determine the default branch")
        default_ref = urllib.parse.quote(f"heads/{repository['default_branch']}", safe="/")
        ref_data = self._api(f"/git/ref/{default_ref}")
        try:
            base_sha = str(ref_data["object"]["sha"])  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise StateStoreError("could not determine the default branch SHA") from exc
        try:
            self._api(
                "/git/refs",
                method="POST",
                payload={"ref": f"refs/heads/{self.branch}", "sha": base_sha},
            )
        except StateStoreError as exc:
            if self._api(f"/git/ref/{branch_ref}", allow_404=True) is None:
                raise exc

    def save(self, state: MonitorState, message: str) -> None:
        self._ensure_branch()
        encoded = base64.b64encode(
            (json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        ).decode("ascii")
        payload: dict[str, Any] = {
            "message": message,
            "content": encoded,
            "branch": self.branch,
        }
        if self.file_sha:
            payload["sha"] = self.file_sha
        path = urllib.parse.quote(STATE_PATH, safe="/")
        result = self._api(f"/contents/{path}", method="PUT", payload=payload)
        try:
            self.file_sha = str(result["content"]["sha"])  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise StateStoreError("GitHub did not return the saved state SHA") from exc


class Monitor:
    def __init__(
        self,
        store: Any,
        notifier: Any,
        *,
        threshold: int = 80,
        forecast_fetcher: Callable[[], Forecast] = fetch_forecast,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self.threshold = threshold
        self.forecast_fetcher = forecast_fetcher
        self.clock = clock

    def _save_if_changed(self, state: MonitorState, original: dict[str, Any], now: datetime) -> None:
        only_heartbeat = state.to_dict() == original
        if heartbeat_due(state, now):
            state.last_heartbeat_at = iso_utc(now)
        if state.to_dict() != original:
            message = "[monitor] heartbeat" if only_heartbeat else "[monitor] update state"
            self.store.save(state, message)

    def run_once(self) -> int:
        state = self.store.load()
        original = state.to_dict()
        now = self.clock()

        try:
            forecast = self.forecast_fetcher()
        except Exception as exc:
            state.consecutive_failures += 1
            notification_error: Exception | None = None
            if state.consecutive_failures >= 3 and not state.failure_alert_sent:
                try:
                    self.notifier.send_failure_alert(exc, state.consecutive_failures, now)
                    state.failure_alert_sent = True
                except Exception as notify_exc:
                    notification_error = notify_exc
            self._save_if_changed(state, original, now)
            print(f"Forecast check failed ({state.consecutive_failures} consecutive): {exc}", file=sys.stderr)
            if notification_error:
                print(f"Failure notification could not be sent: {notification_error}", file=sys.stderr)
            return 1

        previous_failures = state.consecutive_failures
        if state.failure_alert_sent:
            try:
                self.notifier.send_recovery_alert(previous_failures, now)
            except Exception as exc:
                self._save_if_changed(state, original, now)
                print(f"Recovery notification could not be sent: {exc}", file=sys.stderr)
                return 1
            state.failure_alert_sent = False
        state.consecutive_failures = 0

        new_reset = reset_is_newer(state.last_observed_reset_at, forecast.last_reset_at)
        if new_reset:
            try:
                self.notifier.send_reset_alert(forecast, now)
            except Exception as exc:
                self._save_if_changed(state, original, now)
                print(f"Reset notification could not be sent: {exc}", file=sys.stderr)
                return 1
        if not state.last_observed_reset_at or new_reset:
            state.last_observed_reset_at = forecast.last_reset_at

        is_above = forecast.probability_24h > self.threshold
        should_alert = is_above and (not state.initialized or not state.above_threshold)
        if should_alert:
            try:
                self.notifier.send_probability_alert(forecast, self.threshold, now)
            except Exception as exc:
                self._save_if_changed(state, original, now)
                print(f"Probability notification could not be sent: {exc}", file=sys.stderr)
                return 1

        state.initialized = True
        state.above_threshold = is_above
        self._save_if_changed(state, original, now)
        print(
            f"Checked {format_beijing(now)} Beijing: rounded_24h={forecast.probability_24h}% "
            f"threshold=>{self.threshold}% alerted={should_alert} new_reset={new_reset}"
        )
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor a third-party Codex reset forecast")
    parser.add_argument(
        "--send-test",
        action="store_true",
        help="Send a test Enterprise WeChat notification without changing monitor state",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    notifier = WeComNotifier.from_env()
    if args.send_test:
        notifier.send_test_notification(utc_now())
        print("Test notification sent; monitor state was not changed.")
        return 0

    threshold = int(os.getenv("ALERT_THRESHOLD", "80"))
    if not 0 <= threshold < 100:
        raise MonitorError("ALERT_THRESHOLD must be between 0 and 99")
    store = GitHubStateStore.from_env()
    return Monitor(store, notifier, threshold=threshold).run_once()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MonitorError, OSError, ValueError) as exc:
        print(f"Monitor failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
