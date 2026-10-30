"""Verify newly published website media with one-byte HTTP Range requests."""

import httpx


class WebsiteVerificationError(RuntimeError):
    """Published metadata has not yet passed the website media endpoint."""


def publication_targets(state, publications):
    users = {user["id"]: user for user in state["users"]}
    targets = []
    for publication in publications:
        records = users[publication["user_id"]]["download_records"]
        record = next(row for row in records if str(row["aweme_id"]) == publication["aweme_id"])
        for kind, field in (("original", "original_file"), ("playback", "playback_file"), ("cover", "cover_file")):
            path = publication.get(field)
            if path:
                targets.append({"user_id": publication["user_id"], "aweme_id": publication["aweme_id"],
                                "kind": kind, "index": record["files"].index(path),
                                "size": publication["evidence"][kind]["size"]})
    return targets


def check_target(client, target):
    path = f"/api/monitor/users/{target['user_id']}/archive/{target['aweme_id']}/media/{target['index']}"
    try:
        with client.stream("GET", path, headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"}) as response:
            expected = "bytes 0-0/" + str(target["size"])
            if response.status_code != 206 or response.headers.get("content-range") != expected:
                raise WebsiteVerificationError(
                    f"Website {target['kind']} Range check failed: HTTP {response.status_code}; "
                    f"Content-Range={response.headers.get('content-range')!r} expected={expected!r}"
                )
            if len(next(response.iter_bytes(chunk_size=1), b"")) != 1:
                raise WebsiteVerificationError("Website media Range response has no byte")
            return {**target, "http_status": response.status_code, "content_range": expected,
                    "content_type": response.headers.get("content-type"), "verified_bytes": 1}
    except httpx.HTTPError as error:
        raise WebsiteVerificationError(f"Website Range request failed ({type(error).__name__})") from None


def verify_published(state, publications, *, client):
    return [check_target(client, target) for target in publication_targets(state, publications)]
