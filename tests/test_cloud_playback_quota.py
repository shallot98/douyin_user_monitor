"""Synthetic quota responses; no network calls or real waits."""

import unittest
from unittest.mock import Mock, call

from scripts.cloud_playback.d2_client import D2Error, D2HTTPError
from scripts.cloud_playback.quota import QUOTA_ENDPOINT, upload_capacity, wait_for_upload_capacity


def quota_response(quota="10737418240", used="17271364183"):
    return {"traffic_statistics_quota": {"daily_bytes_upload_quota": quota, "bytes_uploaded_today": used}}


class UploadCapacityTests(unittest.TestCase):
    def test_actual_exhausted_shape_returns_only_numeric_capacity(self):
        response = {**quota_response(), "extra_private_field": "private-bearer"}
        client = Mock()
        client.post.return_value = response
        result = upload_capacity(client)
        self.assertEqual(result, {"quota": 10737418240, "used": 17271364183, "remaining": 0})
        client.post.assert_called_once_with(QUOTA_ENDPOINT, {})
        self.assertEqual(response["extra_private_field"], "private-bearer")

    def test_available_capacity_uses_exact_integer_arithmetic(self):
        client = Mock()
        client.post.return_value = quota_response("9007199254740993", "9007199254740992")
        self.assertEqual(upload_capacity(client),
                         {"quota": 9007199254740993, "used": 9007199254740992, "remaining": 1})

    def test_zero_bytes_used_is_valid(self):
        client = Mock()
        client.post.return_value = quota_response(used="0")
        self.assertEqual(upload_capacity(client)["remaining"], 10737418240)

    def test_proto3_omitted_uploaded_bytes_means_zero_without_mutating_response(self):
        response = {"traffic_statistics_quota": {"daily_bytes_upload_quota": "10737418240"}}
        client = Mock()
        client.post.return_value = response
        self.assertEqual(upload_capacity(client),
                         {"quota": 10737418240, "used": 0, "remaining": 10737418240})
        self.assertEqual(response, {"traffic_statistics_quota": {"daily_bytes_upload_quota": "10737418240"}})
        client.post.assert_called_once_with(QUOTA_ENDPOINT, {})

    def test_present_invalid_uploaded_bytes_are_not_defaulted_to_zero(self):
        for used in ("-1", "NaN", "1e3", "1.5", "１", "", 0, True, None, [], {}):
            with self.subTest(used=used):
                client = Mock()
                client.post.return_value = quota_response(used=used)
                with self.assertRaisesRegex(D2Error, "bytes_uploaded_today"):
                    upload_capacity(client)
                client.post.assert_called_once_with(QUOTA_ENDPOINT, {})

    def test_omitted_used_bytes_still_requires_positive_total_quota(self):
        for traffic in ({}, {"daily_bytes_upload_quota": "0"}, {"daily_bytes_upload_quota": "-1"}):
            with self.subTest(traffic=traffic):
                client = Mock()
                client.post.return_value = {"traffic_statistics_quota": traffic}
                with self.assertRaisesRegex(D2Error, "quota"):
                    upload_capacity(client)
                client.post.assert_called_once_with(QUOTA_ENDPOINT, {})

    def test_unexpected_schema_is_explicit(self):
        responses = [None, [], {}, {"traffic_statistics_quota": []}, {"traffic_statistics_quota": {}}]
        responses.extend(quota_response(quota=value) for value in ("0", "-1", "1.5", "１", "", 10, True, None))
        for response in responses:
            with self.subTest(response=response):
                client = Mock()
                client.post.return_value = response
                with self.assertRaises(D2Error):
                    upload_capacity(client)
                client.post.assert_called_once_with(QUOTA_ENDPOINT, {})

    def test_http_failure_keeps_original_status_and_does_not_refresh(self):
        failure = D2HTTPError(401, "authentication failed", code=16)
        client = Mock()
        client.post.side_effect = failure
        with self.assertRaises(D2HTTPError) as caught:
            upload_capacity(client)
        self.assertIs(caught.exception, failure)
        self.assertEqual(client.mock_calls, [call.post(QUOTA_ENDPOINT, {})])


class QuotaWaitTests(unittest.TestCase):
    def test_available_capacity_returns_immediately_without_wait_report(self):
        client = Mock()
        client.post.return_value = quota_response("100", "20")
        report, sleep = Mock(), Mock()
        self.assertEqual(wait_for_upload_capacity(client, report, sleep=sleep),
                         {"quota": 100, "used": 20, "remaining": 80})
        report.assert_not_called()
        sleep.assert_not_called()

    def test_reports_each_exhausted_observation_until_provider_reports_capacity(self):
        client = Mock()
        client.post.side_effect = [quota_response("100", "120"), quota_response("100", "100"),
                                   quota_response("100", "0")]
        report, sleep = Mock(), Mock()
        result = wait_for_upload_capacity(client, report, sleep=sleep)
        self.assertEqual(result, {"quota": 100, "used": 0, "remaining": 100})
        self.assertEqual(report.call_args_list, [
            call({"status": "waiting_for_upload_quota", "quota": 100, "used": 120, "remaining": 0, "poll_seconds": 600}),
            call({"status": "waiting_for_upload_quota", "quota": 100, "used": 100, "remaining": 0, "poll_seconds": 600}),
        ])
        self.assertEqual(sleep.call_args_list, [call(600), call(600)])
        self.assertEqual(client.mock_calls, [call.post(QUOTA_ENDPOINT, {})] * 3)

    def test_wait_resumes_when_proto3_omits_zero_uploaded_bytes_after_reset(self):
        reset_response = {"traffic_statistics_quota": {"daily_bytes_upload_quota": "100"}}
        client = Mock()
        client.post.side_effect = [quota_response("100", "100"), reset_response]
        report, sleep = Mock(), Mock()
        result = wait_for_upload_capacity(client, report, required_bytes=25, sleep=sleep)
        self.assertEqual(result, {"quota": 100, "used": 0, "remaining": 100})
        report.assert_called_once_with({"status": "waiting_for_upload_quota", "quota": 100,
                                       "used": 100, "remaining": 0, "required_bytes": 25, "poll_seconds": 600})
        sleep.assert_called_once_with(600)
        self.assertEqual(client.mock_calls, [call.post(QUOTA_ENDPOINT, {})] * 2)
        self.assertEqual(reset_response, {"traffic_statistics_quota": {"daily_bytes_upload_quota": "100"}})

    def test_failed_recheck_is_not_reported_as_available_or_silently_retried(self):
        client = Mock()
        failure = D2HTTPError(429, "quota API rate limited", code=8)
        client.post.side_effect = [quota_response("100", "100"), failure]
        report, sleep = Mock(), Mock()
        with self.assertRaises(D2HTTPError) as caught:
            wait_for_upload_capacity(client, report, poll_seconds=60, sleep=sleep)
        self.assertIs(caught.exception, failure)
        sleep.assert_called_once_with(60)
        self.assertEqual(report.call_count, 1)
        self.assertEqual(client.post.call_count, 2)

    def test_interrupted_sleep_propagates_without_another_request(self):
        client = Mock()
        client.post.return_value = quota_response("100", "100")
        report, sleep = Mock(), Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            wait_for_upload_capacity(client, report, sleep=sleep)
        client.post.assert_called_once_with(QUOTA_ENDPOINT, {})
        self.assertEqual(report.call_count, 1)

    def test_nonpositive_poll_interval_cannot_busy_loop(self):
        for interval in (0, -1):
            with self.subTest(interval=interval):
                client = Mock()
                with self.assertRaisesRegex(ValueError, "positive"):
                    wait_for_upload_capacity(client, Mock(), poll_seconds=interval, sleep=Mock())
                client.post.assert_not_called()

    def test_waits_for_entire_derivative_instead_of_positive_but_insufficient_quota(self):
        client = Mock()
        client.post.side_effect = [quota_response("100", "99"), quota_response("100", "10")]
        report, sleep = Mock(), Mock()
        capacity = wait_for_upload_capacity(client, report, required_bytes=25, sleep=sleep)
        self.assertEqual(capacity["remaining"], 90)
        self.assertEqual(report.call_args.args[0]["required_bytes"], 25)
        sleep.assert_called_once_with(600)

    def test_oversized_derivative_is_reported_instead_of_waiting_forever(self):
        client = Mock()
        client.post.return_value = quota_response("100", "0")
        sleep = Mock()
        with self.assertRaisesRegex(D2Error, "entire daily"):
            wait_for_upload_capacity(client, Mock(), required_bytes=101, sleep=sleep)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
