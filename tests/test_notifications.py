import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from automation.message_cards import TaskMessageCard
from automation.notification_store import NotificationProfile, NotificationStore

def card(**overrides):
    values = {
        "task_id": "alpha-13452",
        "task_version": 1,
        "proposal": "무더위쉼터 현황 현행화",
        "assessment": "ready",
        "evidence": ("rule:heat-shelter-refresh 조건과 일치",),
        "risk": "local_artifact_only",
        "impact": "산출물 1건 생성",
    }
    values.update(overrides)
    return TaskMessageCard(**values)


class RenderNotificationTest(unittest.TestCase):
    def module(self):
        from automation import notifications

        return notifications

    def test_renders_only_c7_fields_and_actions(self):
        notifications = self.module()
        text = notifications.render_notification(
            card(), event="classification_review", local_reference="state/tasks/alpha-13452.json"
        )
        self.assertIn("event: classification\\_review", text)
        self.assertIn("local\\_reference: state/tasks/alpha\\-13452\\.json", text)
        self.assertIn("task\\_name: 무더위쉼터 현황 현행화", text)
        self.assertIn("reason: ready", text)
        self.assertIn("next\\_action: 산출물 1건 생성", text)
        self.assertNotIn("artifacts", text)


    def test_long_messages_are_split_on_boundaries_within_the_limit(self):
        notifications = self.module()
        chunks = notifications.split_notification("가" * 9000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            with self.subTest():
                self.assertLessEqual(len(chunk), notifications.MAX_MESSAGE_LENGTH)
        self.assertEqual("".join(chunks).replace("\n", ""), "가" * 9000)

    def test_short_message_is_not_split(self):
        notifications = self.module()
        self.assertEqual(notifications.split_notification("짧다"), ["짧다"])

    def test_actions_use_the_task_contract_not_hermes_approve(self):
        notifications = self.module()
        text = notifications.render_notification(
            card(), event="classification_review", local_reference="state/tasks/alpha-13452.json"
        )
        self.assertIn("/task approve", text)
        self.assertNotIn("\n/approve", text)
        self.assertNotIn("/deny", text)


class DeliveryTest(unittest.TestCase):
    def module(self):
        from automation import notifications

        return notifications

    def test_gateway_request_shape_is_explicit_and_local(self):
        notifications = self.module()
        request = notifications.build_gateway_request(
            "http://127.0.0.1:8000", "chat-1", "hello"
        )
        self.assertEqual(request["url"], "http://127.0.0.1:8000/notify")
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["json"], {"chat_id": "chat-1", "text": "hello"})

    def test_gateway_url_must_be_loopback(self):
        notifications = self.module()
        for url in (
            "https://example.invalid/notify",
            "http://10.0.0.5:8000",
            "ftp://127.0.0.1",
            "http://127.0.0.1:8000?a=b",
        ):
            with self.subTest(url=url):
                with self.assertRaises(notifications.NotificationError):
                    notifications.build_gateway_request(url, "chat-1", "hello")

    def test_delivery_falls_back_deterministically_when_gateway_is_absent(self):
        notifications = self.module()

        def unavailable(_request):
            raise notifications.NotificationError("gateway unavailable")

        outcome = notifications.deliver(
            "http://127.0.0.1:8000", "chat-1", "hello", transport=unavailable
        )
        self.assertEqual(outcome["delivered"], False)
        self.assertEqual(outcome["fallback"], "extension")
        self.assertEqual(outcome["chunks"], 1)

    def test_successful_delivery_reports_each_chunk(self):
        notifications = self.module()
        sent = []

        def transport(request):
            sent.append(request)

        outcome = notifications.deliver(
            "http://127.0.0.1:8000", "chat-1", "가" * 9000, transport=transport
        )
        self.assertTrue(outcome["delivered"])
        self.assertEqual(outcome["chunks"], len(sent))
        self.assertGreater(len(sent), 1)



class _StoreDouble:
    def __init__(self):
        self.records = []

    def read_deliveries(self):
        return list(self.records)

    def append_delivery(self, delivery):
        if any(r["delivery_key"] == delivery["delivery_key"] for r in self.records):
            return False
        self.records.append(delivery)
        return True


class _ProfileDouble:
    def __init__(self, channels):
        self.channels = tuple(channels)

class NotificationProfileValidationTest(unittest.TestCase):
    def test_direct_construction_rejects_duplicates_and_telegram_without_activation(self):
        with self.assertRaises(ValueError):
            NotificationProfile(profile_id="x", channels=("local", "local"))
        with self.assertRaises(ValueError):
            NotificationProfile(profile_id="x", channels=("telegram",))

    def test_from_dict_rejects_unknown_retry_confirmation_profile_fields_and_unbounded_retry(self):
        with self.assertRaises(ValueError):
            NotificationProfile.from_dict({"profile_id": "x", "retry_policy": {"unknown": 1}})
        with self.assertRaises(ValueError):
            NotificationProfile.from_dict({"profile_id": "x", "retry_policy": {"max_attempts": 11}})
        with self.assertRaises(ValueError):
            NotificationProfile.from_dict({"profile_id": "x", "confirmation_policy": "automatic"})
        with self.assertRaises(ValueError):
            NotificationProfile.from_dict({"profile_id": "x", "unexpected": True})
    def test_delivery_history_query_keeps_90_day_boundary_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStore(directory)
            store.root.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            records = []
            for age, key in ((89, "recent"), (91, "old")):
                records.append({
                    "delivery_key": key,
                    "task_id": "task-1",
                    "event": "failure",
                    "channel": "local",
                    "status": "delivered",
                    "attempt": 1,
                    "recorded_at": (now - timedelta(days=age)).isoformat(),
                })
            store.delivery_path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            visible = store.read_deliveries(since_days=90)
            all_history = store.read_deliveries(since_days=365)
        self.assertEqual([record["delivery_key"] for record in visible], ["recent"])
        self.assertEqual({record["delivery_key"] for record in all_history}, {"recent", "old"})



class DeliverWithProfileTest(unittest.TestCase):
    def module(self):
        from automation import notifications

        return notifications

    def test_local_channel_without_a_sink_is_not_falsely_delivered(self):
        notifications = self.module()
        outcome = notifications.deliver_with_profile(
            _ProfileDouble(["local"]), task_id="t", event="failure", card=card(), store=None
        )
        self.assertEqual(outcome["channels"]["local"], "failed")
        self.assertEqual(outcome["status"], "failed")

    def test_local_channel_is_queued_until_a_sink_accepts_it(self):
        notifications = self.module()
        store = _StoreDouble()
        outcome = notifications.deliver_with_profile(
            _ProfileDouble(["local"]), task_id="t", event="failure", card=card(), store=store
        )
        self.assertEqual(outcome["channels"]["local"], "queued")
        self.assertEqual(len(store.records), 1)

    def test_repeat_delivery_does_not_fire_transport_twice(self):
        notifications = self.module()
        store = _StoreDouble()
        calls = []

        def transport(request):
            calls.append(request)

        for _ in range(2):
            notifications.deliver_with_profile(
                _ProfileDouble(["telegram"]), task_id="t", event="failure",
                card=card(), store=store, transport=transport,
            )
        # One durable record and one transport invocation despite two calls.
        self.assertEqual(len([r for r in store.records if r["status"] == "delivered"]), 1)
        self.assertEqual(len(calls), 1)

    def test_concurrent_same_key_fires_transport_once(self):
        notifications = self.module()
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStore(directory)
            profile = NotificationProfile(profile_id="default")
            calls = []
            def transport(request):
                calls.append(request)
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(
                    lambda _: notifications.deliver_with_profile(
                        profile, task_id="task-1", event="failure", card=card(),
                        store=store, transport=transport,
                    ),
                    range(8),
                ))
            records = store.read_deliveries(since_days=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "delivered")

    def test_all_channel_failures_are_reported_without_short_circuiting(self):
        notifications = self.module()
        profile = NotificationProfile(
            profile_id="multi", channels=("local", "dashboard"), retry_policy={"max_attempts": 1}
        )
        calls = []
        def down(request):
            calls.append(request["channel"])
            raise RuntimeError("isolated failure")
        with tempfile.TemporaryDirectory() as directory:
            outcome = notifications.deliver_with_profile(
                profile, task_id="task-1", event="failure", card=card(),
                store=NotificationStore(directory), transport=down,
            )
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(set(outcome["channels"]), {"local", "dashboard"})
        self.assertEqual(set(calls), {"local", "dashboard"})

    def test_pipeline_event_record_contains_source_and_local_reference(self):
        notifications = self.module()
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStore(directory)
            outcome = notifications.deliver_with_profile(
                NotificationProfile(profile_id="default"),
                task_id="task-1",
                event="classification_review",
                event_id="pipeline-event-1",
                state_version="2",
                card=card(),
                store=store,
            )
            record = store.read_deliveries()[0]
        self.assertEqual(outcome["status"], "queued")
        self.assertEqual(record["event_payload"], {
            "event": "classification_review",
            "event_id": "pipeline-event-1",
            "state_version": "2",
        })
        self.assertEqual(record["local_reference"], "state/tasks/task-1.json")
        self.assertEqual(record["attempt"], 1)

    def test_batched_event_is_queued_then_flushed_locally(self):
        notifications = self.module()
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStore(directory)
            profile = NotificationProfile(
                profile_id="default",
                channels=("local",),
                event_policy={"preparation_completed": "batched"},
            )
            queued = notifications.deliver_with_profile(
                profile,
                task_id="task-1",
                event="preparation_completed",
                event_id="pipeline-event-1",
                state_version="7",
                card=card(),
                store=store,
            )
            sent = []
            flushed = notifications.flush_batched(store, transport=sent.append)
            records = store.read_deliveries()
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(flushed, {"flushed": 1, "failed": 0, "skipped": 0})
        self.assertEqual([record["status"] for record in records], ["queued", "delivered"])
        self.assertEqual(records[1]["event_payload"], {
            "event": "preparation_completed",
            "event_id": "pipeline-event-1",
            "state_version": "7",
        })
        self.assertEqual(len(sent), 1)

    def test_retryable_failure_keeps_history_and_succeeds_on_next_attempt(self):
        notifications = self.module()
        calls = []

        def flaky(request):
            calls.append(request)
            if len(calls) == 1:
                raise RuntimeError("temporary gateway failure")

        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStore(directory)
            profile = NotificationProfile(profile_id="default", channels=("telegram",), telegram_enabled=True)
            first = notifications.deliver_with_profile(
                profile, task_id="task-1", event="failure", card=card(),
                store=store, transport=flaky,
            )
            second = notifications.deliver_with_profile(
                profile, task_id="task-1", event="failure", card=card(),
                store=store, transport=flaky,
            )
            records = store.read_deliveries()
        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "delivered")
        self.assertEqual([record["attempt"] for record in records], [1, 2])
        self.assertTrue(records[0]["retryable"])

    def test_retry_policy_stops_after_configured_attempts(self):
        notifications = self.module()
        calls = []
        def down(request):
            calls.append(request)
            raise RuntimeError("down")
        profile = NotificationProfile(
            profile_id="limited",
            channels=("telegram",),
            telegram_enabled=True,
            retry_policy={"max_attempts": 1},
        )
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStore(directory)
            for _ in range(2):
                notifications.deliver_with_profile(
                    profile,
                    task_id="limited-task",
                    event="failure",
                    card=card(),
                    store=store,
                    transport=down,
                )
            records = store.read_deliveries()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["retryable"])

    def test_sensitive_profile_only_delivers_event_and_local_reference(self):
        notifications = self.module()
        sent = []
        with tempfile.TemporaryDirectory() as directory:
            outcome = notifications.deliver_with_profile(
                NotificationProfile(profile_id="sensitive", channels=("telegram",), sensitive=True, telegram_enabled=True),
                task_id="task-1",
                event="failure",
                card=card(proposal="must not be sent"),
                store=NotificationStore(directory),
                transport=sent.append,
            )
        text = sent[0]["text"]
        self.assertEqual(outcome["status"], "delivered")
        self.assertIn("event: failure", text)
        self.assertIn("local\\_reference: state/tasks/task\\-1\\.json", text)
        self.assertNotIn("must not be sent", text)
if __name__ == "__main__":
    unittest.main()
