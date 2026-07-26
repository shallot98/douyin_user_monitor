def sanitize_public_error(exc: BaseException, *, limit: int = 200) -> str:
    """Strip newlines and cap length so cookie/token fragments are less likely to leak."""
    text = " ".join(str(exc).split())
    if len(text) > limit:
        return text[:limit] + "..."
    return text
