"""Preserve known upstream rejection reasons without echoing response payloads."""

ARGUS_REJECTIONS = frozenset({
    "Blocked by ArgusSecurityPlugin Uifid Not Found",
    "Blocked by ArgusSecurityPlugin Signature Not Found",
})


def status_error_message(response):
    message = f"HTTP Status Code {response.status_code}"
    reason = response.text.strip()
    if reason in ARGUS_REJECTIONS:
        return f"{message}: {reason}"
    return message
