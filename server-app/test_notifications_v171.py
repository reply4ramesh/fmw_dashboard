import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from notification_alerts import (
    _email_html,
    _send_mail,
    build_dashboard_alert_events,
    send_collection_failure_notification,
    send_collection_success_notification,
    send_environment_alerts,
)
from notification_store import (
    DEFAULT_NOTIFICATION_SETTINGS,
    get_notification_settings,
    save_notification_recipient,
    save_notification_settings,
)


class NotificationPolicyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "monitoring.sqlite")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_existing_settings_table_is_migrated_without_losing_password(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE notification_settings (
                    id INTEGER PRIMARY KEY,
                    smtp_host TEXT NOT NULL DEFAULT '',
                    smtp_port INTEGER NOT NULL DEFAULT 587,
                    smtp_username TEXT NOT NULL DEFAULT '',
                    smtp_password TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    sender_email TEXT NOT NULL DEFAULT '',
                    security_mode TEXT NOT NULL DEFAULT 'starttls',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
            conn.execute(
                """
                INSERT INTO notification_settings VALUES
                (1, 'mail.example.com', 587, 'fmw', 'secret', 'FMW',
                 'fmw@example.com', 'starttls', 1, 'old', 'old')
                """
            )
            conn.commit()
        settings = get_notification_settings(self.db_path, include_secret=True)
        self.assertEqual("secret", settings["smtpPassword"])
        self.assertEqual(DEFAULT_NOTIFICATION_SETTINGS["eventTypes"], settings["eventTypes"])
        self.assertEqual(360, settings["repeatMinutes"])
        self.assertEqual("all", settings["environmentScope"])
        self.assertEqual([], settings["environmentIds"])

    def test_policy_round_trip_normalizes_thresholds_and_preserves_smtp(self):
        save_notification_settings(
            self.db_path,
            {
                "smtpHost": "mail.example.com",
                "smtpUsername": "fmw@example.com",
                "smtpPassword": "secret",
                "senderEmail": "fmw@example.com",
                "enabled": True,
            },
        )
        saved = save_notification_settings(
            self.db_path,
            {
                "eventTypes": {"collectorSuccess": True, "cpu": False},
                "thresholds": {"memoryWarning": 94, "memoryCritical": 90},
                "repeatMinutes": 15,
            },
        )
        secret = get_notification_settings(self.db_path, include_secret=True)
        self.assertTrue(saved["eventTypes"]["collectorSuccess"])
        self.assertFalse(saved["eventTypes"]["cpu"])
        self.assertEqual(94, saved["thresholds"]["memoryCritical"])
        self.assertEqual(15, saved["repeatMinutes"])
        self.assertEqual("mail.example.com", saved["smtpHost"])
        self.assertEqual("secret", secret["smtpPassword"])

    def test_blank_username_clears_retained_smtp_authentication(self):
        save_notification_settings(
            self.db_path,
            {
                "smtpHost": "smtp.gmail.com",
                "smtpUsername": "fmw@example.com",
                "smtpPassword": "secret",
                "senderEmail": "fmw@example.com",
            },
        )
        saved = save_notification_settings(
            self.db_path,
            {
                "smtpHost": "internal-mail-router.oracle.com",
                "smtpPort": 25,
                "smtpUsername": "",
                "smtpPassword": "",
                "securityMode": "starttls",
            },
        )
        secret = get_notification_settings(self.db_path, include_secret=True)
        self.assertEqual("", saved["smtpUsername"])
        self.assertFalse(saved["hasPassword"])
        self.assertEqual("", secret["smtpPassword"])

    def test_environment_scope_round_trip_defaults_to_all_and_normalizes_ids(self):
        defaults = get_notification_settings(self.db_path)
        self.assertEqual("all", defaults["environmentScope"])
        self.assertEqual([], defaults["environmentIds"])
        saved = save_notification_settings(
            self.db_path,
            {
                "environmentScope": "selected",
                "environmentIds": ["oig-prod", "oud-prod", "oig-prod", ""],
            },
        )
        self.assertEqual("selected", saved["environmentScope"])
        self.assertEqual(["oig-prod", "oud-prod"], saved["environmentIds"])


class NotificationAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "monitoring.sqlite")
        self.environment = {"id": "oig-prod", "name": "OIG Production"}
        self.settings = dict(DEFAULT_NOTIFICATION_SETTINGS)
        self.settings["eventTypes"] = dict(DEFAULT_NOTIFICATION_SETTINGS["eventTypes"])
        self.settings["thresholds"] = dict(DEFAULT_NOTIFICATION_SETTINGS["thresholds"])

    def tearDown(self):
        self.temp_dir.cleanup()

    def _configure_delivery(self, success=False):
        event_types = dict(DEFAULT_NOTIFICATION_SETTINGS["eventTypes"])
        event_types["collectorSuccess"] = success
        save_notification_settings(
            self.db_path,
            {
                "smtpHost": "mail.example.com",
                "smtpPort": 25,
                "senderEmail": "fmw@example.com",
                "securityMode": "none",
                "enabled": True,
                "eventTypes": event_types,
                "repeatMinutes": 60,
            },
        )
        save_notification_recipient(
            self.db_path,
            {"name": "Operations", "email": "ops@example.com", "severity": "any", "enabled": True},
        )
        save_notification_recipient(
            self.db_path,
            {"name": "Critical", "email": "critical@example.com", "severity": "critical", "enabled": True},
        )

    def test_thresholds_classify_cpu_memory_disk_and_weblogic_threads(self):
        dashboard = {
            "server": {
                "reachable": True,
                "uptime": {"cpuBreakdown": {"idlePercent": 4}},
                "memory": {"usedPercent": 88},
                "rootDisk": {"size": "100G", "usedPercent": 81},
                "productFilesystems": [
                    {"mount": "/opt", "usedPercent": 93, "labels": ["ORACLE_HOME"]}
                ],
            },
            "productMetrics": {
                "weblogic": {"stuckThreadCount": 5, "hoggingThreadCount": 2}
            },
        }
        events = build_dashboard_alert_events(dashboard, settings=self.settings)
        by_type = {event["eventType"]: event for event in events}
        self.assertEqual("critical", by_type["cpu"]["severity"])
        self.assertEqual("warning", by_type["memory"]["severity"])
        self.assertEqual("critical", max(
            (event for event in events if event["eventType"] == "disk"),
            key=lambda event: 2 if event["severity"] == "critical" else 1,
        )["severity"])
        self.assertEqual("critical", by_type["stuckThreads"]["severity"])
        self.assertEqual("warning", by_type["hoggingThreads"]["severity"])

    def test_html_alert_highlights_percentages_and_escapes_collected_values(self):
        rendered = _email_html(
            "2 alerts detected", "critical", "OIG <Production>", "critical", "2026-11-04T20:40:11Z",
            "Review the highlighted conditions.",
            events=[{
                "severity": "critical",
                "title": "Primary server disk utilization",
                "detail": "Root filesystem is 96.0% used; warning 80%, critical 90%.",
            }],
        )
        self.assertIn("OIG &lt;Production&gt;", rendered)
        self.assertIn(">96.0%</strong>", rendered)
        self.assertIn(">80%</strong>", rendered)
        self.assertIn(">90%</strong>", rendered)
        self.assertIn("background:#fef3f2", rendered)

    @mock.patch("notification_alerts.smtplib.SMTP")
    def test_send_mail_includes_plain_text_and_html_alternatives(self, smtp):
        settings = {
            "smtpHost": "mail.example.com", "smtpPort": 25, "smtpUsername": "",
            "smtpPassword": "", "senderName": "FMW Operations Console",
            "senderEmail": "fmw@example.com", "securityMode": "none",
        }
        _send_mail(
            settings, [{"email": "ops@example.com"}], "Alert", "Plain fallback",
            "<html><body><strong>HTML alert</strong></body></html>",
        )
        message = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        self.assertTrue(message.is_multipart())
        self.assertEqual("Plain fallback\n", message.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("HTML alert", message.get_body(preferencelist=("html",)).get_content())

    def test_disabled_event_is_not_evaluated(self):
        self.settings["eventTypes"]["memory"] = False
        events = build_dashboard_alert_events(
            {"server": {"reachable": True, "memory": {"usedPercent": 99}}},
            settings=self.settings,
        )
        self.assertFalse(any(event["eventType"] == "memory" for event in events))

    @mock.patch("notification_alerts._send_mail")
    def test_alert_delivery_routes_by_severity_and_suppresses_duplicate(self, send_mail):
        self._configure_delivery()
        dashboard = {
            "status": "critical",
            "server": {"reachable": True, "memory": {"usedPercent": 99}},
        }
        first = send_environment_alerts(self.db_path, self.environment, dashboard)
        second = send_environment_alerts(self.db_path, self.environment, dashboard)
        self.assertTrue(first["sent"])
        self.assertEqual(2, first["recipientCount"])
        self.assertEqual("deduped", second["reason"])
        self.assertEqual(1, send_mail.call_count)

    @mock.patch("notification_alerts._send_mail")
    def test_alert_is_delivered_after_first_matching_recipient_is_added(self, send_mail):
        event_types = dict(DEFAULT_NOTIFICATION_SETTINGS["eventTypes"])
        save_notification_settings(
            self.db_path,
            {
                "smtpHost": "mail.example.com",
                "smtpPort": 25,
                "senderEmail": "fmw@example.com",
                "securityMode": "none",
                "enabled": True,
                "eventTypes": event_types,
            },
        )
        dashboard = {
            "status": "critical",
            "server": {"reachable": True, "memory": {"usedPercent": 99}},
        }
        first = send_environment_alerts(self.db_path, self.environment, dashboard)
        self.assertEqual("no_matching_recipients", first["reason"])
        save_notification_recipient(
            self.db_path,
            {"name": "Operations", "email": "ops@example.com", "severity": "any", "enabled": True},
        )
        second = send_environment_alerts(self.db_path, self.environment, dashboard)
        self.assertTrue(second["sent"])
        self.assertEqual(1, send_mail.call_count)

    @mock.patch("notification_alerts._send_mail")
    def test_success_and_failure_notifications_are_selectable(self, send_mail):
        self._configure_delivery(success=True)
        dashboard = {"status": "healthy", "job": {"trigger": "manual", "durationMs": 1234}}
        success = send_collection_success_notification(self.db_path, self.environment, dashboard)
        failure = send_collection_failure_notification(self.db_path, self.environment, "SSH timed out")
        duplicate = send_collection_failure_notification(self.db_path, self.environment, "SSH timed out")
        self.assertTrue(success["sent"])
        self.assertEqual(1, success["recipientCount"])
        self.assertTrue(failure["sent"])
        self.assertEqual(2, failure["recipientCount"])
        self.assertEqual("deduped", duplicate["reason"])
        self.assertEqual(2, send_mail.call_count)

    @mock.patch("notification_alerts._send_mail")
    def test_selected_environment_scope_blocks_every_notification_type(self, send_mail):
        self._configure_delivery(success=True)
        save_notification_settings(
            self.db_path,
            {"environmentScope": "selected", "environmentIds": ["another-environment"]},
        )
        dashboard = {"status": "critical", "server": {"reachable": True, "memory": {"usedPercent": 99}}}
        alert = send_environment_alerts(self.db_path, self.environment, dashboard)
        success = send_collection_success_notification(self.db_path, self.environment, dashboard)
        failure = send_collection_failure_notification(self.db_path, self.environment, "SSH timed out")
        self.assertEqual("environment_disabled", alert["reason"])
        self.assertEqual("environment_disabled", success["reason"])
        self.assertEqual("environment_disabled", failure["reason"])
        self.assertEqual(0, send_mail.call_count)

    @mock.patch("notification_alerts._send_mail")
    def test_selected_environment_scope_allows_selected_profile(self, send_mail):
        self._configure_delivery(success=True)
        save_notification_settings(
            self.db_path,
            {"environmentScope": "selected", "environmentIds": [self.environment["id"]]},
        )
        dashboard = {"status": "healthy", "server": {"reachable": True}, "job": {"trigger": "manual"}}
        result = send_collection_success_notification(self.db_path, self.environment, dashboard)
        self.assertTrue(result["sent"])
        self.assertEqual(1, send_mail.call_count)


if __name__ == "__main__":
    unittest.main()
