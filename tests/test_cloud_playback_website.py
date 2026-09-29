import httpx
import pytest

from scripts.cloud_playback.website_check import WebsiteVerificationError, check_target, publication_targets

TARGET = {"user_id": "user", "aweme_id": "123", "kind": "playback", "index": 2, "size": 500}


def test_website_check_uses_actual_published_index_and_one_byte():
    def respond(request):
        assert request.url.path == "/api/monitor/users/user/archive/123/media/2"
        assert request.headers["range"] == "bytes=0-0"
        return httpx.Response(206, headers={"Content-Range": "bytes 0-0/500"}, content=b"x")

    with httpx.Client(base_url="http://website.test", transport=httpx.MockTransport(respond)) as client:
        result = check_target(client, TARGET)
    assert result["http_status"] == 206 and result["verified_bytes"] == 1


@pytest.mark.parametrize("status,content_range", [(200, None), (404, None), (206, "bytes 0-0/499")])
def test_bad_status_or_wrong_object_size_is_not_reported_as_published(status, content_range):
    def respond(request):
        headers = {"Content-Range": content_range} if content_range else {}
        return httpx.Response(status, headers=headers, content=b"x")

    with httpx.Client(base_url="http://website.test", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(WebsiteVerificationError, match="Range check failed"):
            check_target(client, TARGET)


def test_target_indices_preserve_original_and_preexisting_file_positions():
    state = {"users": [{"id": "user", "download_records": [{
        "aweme_id": "123", "files": ["original.mp4", "old.fast.mp4", "new.mp4", "new.jpg"],
    }]}]}
    publication = {"user_id": "user", "aweme_id": "123", "playback_file": "new.mp4", "cover_file": "new.jpg",
                   "evidence": {"playback": {"size": 500}, "cover": {"size": 50}}}
    targets = publication_targets(state, [publication])
    assert [item["index"] for item in targets] == [2, 3]
    assert [item["size"] for item in targets] == [500, 50]
