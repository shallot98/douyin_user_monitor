from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from crawlers.douyin.web.web_sign import SIGNATURE_PARAM, sign_web_request


class WebSignTests(unittest.TestCase):
    def test_signed_request_preserves_parameters_and_input_headers(self):
        url = "https://www.douyin.com/aweme/v1/web/aweme/post/?count=18&msToken=&a_bogus=a%2Bb%2Fc%3D"
        original = {"Cookie": "sessionid=test-session; UIFID=visitor-one", "Referer": "https://www.douyin.com/"}
        signed, headers = sign_web_request(url, original, timestamp=1800000000)
        query = parse_qs(urlsplit(signed).query, keep_blank_values=True)

        self.assertEqual(query["a_bogus"], ["a+b/c="])
        self.assertEqual(query["msToken"], [""])
        self.assertEqual(query["uifid"], ["visitor-one"])
        self.assertEqual(query["timestamp"], ["1800000000"])
        self.assertEqual(query[SIGNATURE_PARAM], [headers[SIGNATURE_PARAM]])
        self.assertEqual(headers["x-secsdk-web-expire"], "1800000000")
        self.assertEqual(headers["Cookie"], original["Cookie"])
        self.assertNotIn(SIGNATURE_PARAM, original)

    def test_signature_changes_with_account_and_time(self):
        url = "https://www.douyin.com/aweme/v1/web/aweme/post/?count=18"
        signatures = {
            sign_web_request(url, {"Cookie": cookie}, timestamp=stamp)[1][SIGNATURE_PARAM]
            for cookie, stamp in (("UIFID=one", 100), ("UIFID=two", 100), ("UIFID=one", 101))
        }
        self.assertEqual(len(signatures), 3)

    def test_query_encoding_matches_observed_official_sdk_output(self):
        url = "https://www.douyin.com/aweme/v1/web/aweme/post/?aid=6383&q=a+b%2Bc~!*%27()%E4%B8%AD%E6%96%87&blank="
        signed, _ = sign_web_request(url, {"Cookie": "UIFID=visitor"}, timestamp=100)
        self.assertTrue(urlsplit(signed).query.startswith(
            "aid=6383&q=a%20b%2Bc~!*'()%E4%B8%AD%E6%96%87&blank=&uifid=visitor&timestamp=100&"
        ))

    def test_accepts_sdk_visitor_cookie_spellings(self):
        url = "https://www.douyin.com/aweme/v1/web/aweme/post/?count=18"
        for name in ("uifid", "uifid_temp", "uifidtemp", "UIFID", "UIFID_TEMP", "UIFIDTEMP"):
            with self.subTest(name=name):
                _, headers = sign_web_request(url, {"Cookie": f"{name}=visitor"}, timestamp=100)
                self.assertEqual(headers["uifid"], "visitor")

    def test_missing_visitor_fails_without_exposing_cookie(self):
        with self.assertRaisesRegex(ValueError, "UIFID") as caught:
            sign_web_request("https://www.douyin.com/aweme/v1/web/aweme/post/", {"Cookie": "sessionid=private-value"})
        self.assertNotIn("private-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
