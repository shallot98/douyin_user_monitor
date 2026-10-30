"""Sign protected Douyin Web requests using the current account's visitor ID.

Algorithm: Douyin runtime_bundler_34.js, @byted/secsdk-strategy 1.0.40.
Adapted from Evil0ctal/Douyin_TikTok_Download_API (Apache-2.0),
commit a40370473991321dd4771ee3eb25789db8340cce,
src/dtk/signing/native/websign.py. Verified against the live API on 2026-09-15.
"""

import hashlib
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

WEB_SIGN_SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"
SIGNATURE_PARAM = "x-secsdk-web-signature"
EXPIRE_HEADER = "x-secsdk-web-expire"
UIFID_COOKIE_NAMES = ("uifid", "uifid_temp", "uifidtemp", "UIFID", "UIFID_TEMP", "UIFIDTEMP")
WEB_SIGN_SAFE_CHARS = "-_.!~*'()"


def _visitor_id(cookie):
    cookies = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
    for name in UIFID_COOKIE_NAMES:
        if cookies.get(name):
            return cookies[name]
    raise ValueError("Cookie 缺少 UIFID 或 UIFID_TEMP，无法生成抖音网页请求签名")


def sign_web_request(endpoint, headers, *, timestamp=None):
    visitor = _visitor_id(headers.get("Cookie", ""))
    stamp = str(int(time.time() if timestamp is None else timestamp))
    url = urlsplit(endpoint)
    pairs = parse_qsl(url.query, keep_blank_values=True)
    pairs.extend((("uifid", visitor), ("timestamp", stamp)))
    # Match the SDK's encodeURIComponent serialization, including spaces as %20.
    query = urlencode(pairs, quote_via=quote, safe=WEB_SIGN_SAFE_CHARS)
    signature = hashlib.md5(f"{visitor}_{stamp}_{WEB_SIGN_SALT}_{query}".encode()).hexdigest()
    signed_query = f"{query}&{SIGNATURE_PARAM}={signature}"
    signed_headers = {
        **headers,
        "uifid": visitor,
        SIGNATURE_PARAM: signature,
        EXPIRE_HEADER: stamp,
    }
    return urlunsplit(url._replace(query=signed_query)), signed_headers
