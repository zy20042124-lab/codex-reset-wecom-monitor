from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import URLError

from monitor import (
    Forecast,
    Monitor,
    MonitorState,
    NotificationError,
    WeComNotifier,
    fetch_forecast,
    format_beijing,
)


NOW = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)
FORECAST_LOW = Forecast(80, "2026-08-31T07:00:00Z", "2026-08-30T04:09:02Z", "low")
FORECAST_HIGH = Forecast(81, "2026-08-31T07:00:00Z", "2026-08-30T04:09:02Z", "medium")
FORECAST_AFTER_RESET = Forecast(25, "2026-08-31T08:00:00Z", "2026-08-31T07:55:00Z", "low")


class FakeStore:
    def __init__(self, state: MonitorState | None = None) -> None:
        self.state = state or MonitorState()
        self.saves: list[tuple[MonitorState, str]] = []

    def load(self) -> MonitorState:
        return MonitorState.from_dict(self.state.to_dict())

    def save(self, state: MonitorState, message: str) -> None:
        self.state = MonitorState.from_dict(state.to_dict())
        self.saves.append((self.state, message))


class FakeNotifier:
    def __init__(self) -> None:
        self.probability_alerts: list[int] = []
        self.reset_alerts: list[str] = []
        self.failure_alerts: list[int] = []
        self.recovery_alerts: list[int] = []

    def send_probability_alert(self, forecast: Forecast, threshold: int, checked_at: datetime) -> None:
        self.probability_alerts.append(forecast.probability_24h)

    def send_reset_alert(self, forecast: Forecast, checked_at: datetime) -> None:
        self.reset_alerts.append(forecast.last_reset_at)

    def send_failure_alert(self, error: Exception, failures: int, checked_at: datetime) -> None:
        self.failure_alerts.append(failures)

    def send_recovery_alert(self, previous_failures: int, checked_at: datetime) -> None:
        self.recovery_alerts.append(previous_failures)


class MonitorTests(unittest.TestCase):
    def make_monitor(self, store: FakeStore, notifier: FakeNotifier, result: Forecast | Exception) -> Monitor:
        def fetcher() -> Forecast:
            if isinstance(result, Exception):
                raise result
            return result

        return Monitor(store, notifier, forecast_fetcher=fetcher, clock=lambda: NOW)

    def test_exactly_at_threshold_does_not_alert(self) -> None:
        store, notifier = FakeStore(), FakeNotifier()
        self.assertEqual(self.make_monitor(store, notifier, FORECAST_LOW).run_once(), 0)
        self.assertEqual(notifier.probability_alerts, [])
        self.assertFalse(store.state.above_threshold)

    def test_first_high_value_alerts_once(self) -> None:
        store, notifier = FakeStore(), FakeNotifier()
        self.make_monitor(store, notifier, FORECAST_HIGH).run_once()
        self.make_monitor(store, notifier, FORECAST_HIGH).run_once()
        self.assertEqual(notifier.probability_alerts, [81])

    def test_drop_then_cross_alerts_again(self) -> None:
        store = FakeStore(MonitorState(initialized=True, above_threshold=True))
        notifier = FakeNotifier()
        self.make_monitor(store, notifier, FORECAST_LOW).run_once()
        self.make_monitor(store, notifier, FORECAST_HIGH).run_once()
        self.assertEqual(notifier.probability_alerts, [81])

    def test_newer_reset_time_sends_one_alert(self) -> None:
        state = MonitorState(initialized=True, last_observed_reset_at=FORECAST_LOW.last_reset_at)
        store, notifier = FakeStore(state), FakeNotifier()
        self.make_monitor(store, notifier, FORECAST_AFTER_RESET).run_once()
        self.make_monitor(store, notifier, FORECAST_AFTER_RESET).run_once()
        self.assertEqual(notifier.reset_alerts, [FORECAST_AFTER_RESET.last_reset_at])

    def test_first_reset_time_only_establishes_baseline(self) -> None:
        store, notifier = FakeStore(), FakeNotifier()
        self.make_monitor(store, notifier, FORECAST_AFTER_RESET).run_once()
        self.assertEqual(notifier.reset_alerts, [])
        self.assertEqual(store.state.last_observed_reset_at, FORECAST_AFTER_RESET.last_reset_at)

    def test_failure_alert_on_third_failure_and_recovery(self) -> None:
        store, notifier = FakeStore(), FakeNotifier()
        for _ in range(3):
            self.make_monitor(store, notifier, RuntimeError("offline")).run_once()
        self.assertEqual(notifier.failure_alerts, [3])
        self.make_monitor(store, notifier, FORECAST_LOW).run_once()
        self.assertEqual(notifier.recovery_alerts, [3])
        self.assertEqual(store.state.consecutive_failures, 0)

    def test_reset_notification_failure_keeps_baseline_for_retry(self) -> None:
        state = MonitorState(initialized=True, last_observed_reset_at=FORECAST_LOW.last_reset_at)
        store = FakeStore(state)

        class FailingNotifier(FakeNotifier):
            def send_reset_alert(self, forecast: Forecast, checked_at: datetime) -> None:
                raise RuntimeError("offline")

        self.assertEqual(self.make_monitor(store, FailingNotifier(), FORECAST_AFTER_RESET).run_once(), 1)
        self.assertEqual(store.state.last_observed_reset_at, FORECAST_LOW.last_reset_at)


class WeComNotifierTests(unittest.TestCase):
    WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-webhook-key"

    def test_rejects_non_wecom_webhook(self) -> None:
        with self.assertRaises(NotificationError):
            WeComNotifier("https://example.com/hook?key=secret")

    def test_network_error_does_not_expose_webhook_secret(self) -> None:
        notifier = WeComNotifier(self.WEBHOOK)
        with patch("urllib.request.urlopen", side_effect=URLError("offline")):
            with self.assertRaises(NotificationError) as caught:
                notifier.send_test_notification(NOW)
        self.assertNotIn("test-webhook-key", str(caught.exception))

    def test_test_notification_uses_plain_text_payload(self) -> None:
        notifier = WeComNotifier(self.WEBHOOK)
        with patch("monitor.request_json", return_value={"errcode": 0}) as request:
            notifier.send_test_notification(NOW)

        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["msgtype"], "text")
        self.assertIn("微信兼容纯文本", payload["text"]["content"])
        self.assertNotIn("markdown", payload)

    def test_beijing_format_respects_existing_timezone(self) -> None:
        self.assertEqual(format_beijing("2026-08-23T21:00:00Z"), "2026-08-24 05:00:00")
        self.assertEqual(format_beijing("2026-08-24T05:00:00+08:00"), "2026-08-24 05:00:00")

    def test_forecast_reads_normalized_reset_window(self) -> None:
        payload = {
            "updated_at": "2026-08-23T06:30:00Z",
            "last_reset_at": "2026-08-20T00:00:00Z",
            "probabilities": {"rounded_24h": 90},
            "latest_alert": {
                "source_at": "2026-08-23T06:29:05Z",
                "summary": "Reset around 2 PM PT",
                "url": "https://x.com/example/status/1",
                "window": {
                    "label": "around 2 PM PT on Aug 23",
                    "start_at": "2026-08-23T20:00:00Z",
                    "end_at": "2026-08-23T22:00:00Z",
                    "target_at": "2026-08-23T21:00:00Z",
                    "time_zone": "America/Los_Angeles",
                },
            },
        }
        with patch("monitor.request_json", return_value=payload):
            forecast = fetch_forecast(attempts=1)

        self.assertEqual(forecast.announcement_at, "2026-08-23T06:29:05Z")
        self.assertEqual(forecast.window_target_at, "2026-08-23T21:00:00Z")
        self.assertEqual(
            WeComNotifier._window_lines(forecast)[0],
            "预计重置窗口：2026-08-24 04:00:00 至 2026-08-24 06:00:00（北京时间）",
        )


if __name__ == "__main__":
    unittest.main()
