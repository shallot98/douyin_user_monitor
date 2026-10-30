"""D2 HL6 signing and direct HTTP transport; never refresh account tokens."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

SIGN_ALGORITHM = "HL6-HMAC-SHA256"
SIGN_PREFIX = "HL6"
REQUEST_SUFFIX = "hl6_request"
REQUEST_TIMEOUT_SECONDS = 30
NANOSECONDS_PER_SECOND = 1_000_000_000
JSON_CONTENT_TYPE = "application/json; charset=utf-8"
SAFE_ERROR_MESSAGES = frozenset({"file not found", "USER_DAILY_UPLOAD_QUOTA_EXCEEDED", "capacity limit"})
INSECURE_BLOCK_HOSTS = frozenset({"simple-file-proxy.2dland.cn"})


class D2Error(RuntimeError):
    """An explicit, credential-free protocol or transport error."""


class D2HTTPError(D2Error):
    """HTTP failure with numeric provider code and recognized generic text only."""

    def __init__(self, status, diagnostic, *, code=None, message=None):
        super().__init__(diagnostic)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Credentials:
    host: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    access_token: str = field(default="", repr=False)

    @classmethod
    def from_mapping(cls, values):
        if not isinstance(values, dict):
            raise D2Error("Credentials must be a JSON object")
        for name in ("host", "client_id", "client_secret"):
            if not isinstance(values.get(name), str) or not values[name]:
                raise D2Error(f"Credentials field {name} must be a nonempty string")
        host = values["host"]
        parsed = urlsplit("https://" + host)
        if parsed.netloc != host or parsed.username or any(char.isspace() for char in host):
            raise D2Error("Credentials host must contain only the API host, without a URL path")
        token = values.get("access_token", "")
        if not isinstance(token, str):
            raise D2Error("Credentials access_token must be a string")
        return cls(host=host, client_id=values["client_id"], client_secret=values["client_secret"], access_token=token)


def _base36(number):
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    digits = ""
    while number:
        number, remainder = divmod(number, len(alphabet))
        digits = alphabet[remainder] + digits
    return digits or "0"


def _digest(key, value):
    return hmac.new(key, value.encode(), hashlib.sha256).digest()


def request_headers(credentials, endpoint, body, *, timestamp_ns=None):
    """Match the official SDK's POST signer without query parameters."""
    timestamp_ns = time.time_ns() if timestamp_ns is None else timestamp_ns
    current = datetime.fromtimestamp(timestamp_ns // NANOSECONDS_PER_SECOND, timezone.utc)
    stamp = current.strftime("%Y-%m-%dT%H:%M:%SZ")
    date = current.strftime("%Y-%m-%d")
    headers = {
        "host": credentials.host,
        "x-hl-nonce": _base36(timestamp_ns),
        "x-hl-timestamp": stamp,
        "other-header": "other-value",
        "content-type": JSON_CONTENT_TYPE,
    }
    names = sorted(headers)
    signed_headers = ";".join(names)
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in names)
    canonical = "\n".join(("POST", endpoint, "", canonical_headers,
                            signed_headers, hashlib.sha256(body).hexdigest()))
    scope = f"{date}/{credentials.access_token}/{REQUEST_SUFFIX}"
    signing_input = "\n".join((SIGN_ALGORITHM, stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()))
    key = _digest((SIGN_PREFIX + credentials.client_secret).encode(), date)
    key = _digest(key, credentials.access_token)
    key = _digest(key, REQUEST_SUFFIX)
    signature = _digest(key, signing_input).hex()
    headers["authorization"] = (
        f"{SIGN_ALGORITHM} Credential={credentials.client_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: bytes = field(repr=False)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HttpTransport:
    """Use direct connections; do not forward signed requests on redirects."""

    def __init__(self, *, insecure_hosts=()):
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._insecure_hosts = frozenset(
            host.strip().lower() for host in insecure_hosts if isinstance(host, str) and host.strip()
        )
        self._insecure_opener = None
        if self._insecure_hosts:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self._insecure_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context),
                _NoRedirect(),
            )

    def _opener_for(self, url):
        if self._insecure_opener is not None:
            hostname = (urlsplit(url).hostname or "").lower()
            if hostname in self._insecure_hosts:
                return self._insecure_opener
        return self._opener

    def request(self, method, url, *, body=None, headers=None, timeout=REQUEST_TIMEOUT_SECONDS):
        try:
            request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
            with self._opener_for(url).open(request, timeout=timeout) as response:
                return HttpReply(response.status, response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return HttpReply(exc.code, exc.read())
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # urllib exceptions may contain the complete bearer URL.
            raise D2Error(f"D2 transport failed ({type(exc).__name__}); request details withheld") from None


def _safe_error_details(reply):
    try:
        payload = json.loads(reply.body)
    except (ValueError, UnicodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    code = payload.get("code")
    message = payload.get("message")
    # Arbitrary provider text can contain credentials even when it has no URL.
    return {
        "code": code if type(code) is int else None,
        "message": message if isinstance(message, str) and message in SAFE_ERROR_MESSAGES else None,
    }


def check_status(reply, operation, *, allowed=(200, 201)):
    if reply.status in allowed:
        return
    diagnostic = f"D2 {operation}: HTTP {reply.status}"
    if reply.status in (401, 403):
        diagnostic += "; authentication or authorization failed; token refresh disabled"
    raise D2HTTPError(reply.status, diagnostic, **_safe_error_details(reply))


def response_json(reply, operation):
    try:
        return json.loads(reply.body)
    except (ValueError, UnicodeError):
        raise D2Error(f"D2 {operation}: invalid JSON response") from None


def json_body(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


@dataclass(frozen=True)
class D2Client:
    credentials: Credentials = field(repr=False)
    transport: object = field(repr=False)

    def post(self, endpoint, payload):
        body = json_body(payload)
        reply = self.transport.request(
            "POST", f"https://{self.credentials.host}{endpoint}", body=body,
            headers=request_headers(self.credentials, endpoint, body),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        check_status(reply, endpoint, allowed=range(200, 300))
        return response_json(reply, endpoint)
