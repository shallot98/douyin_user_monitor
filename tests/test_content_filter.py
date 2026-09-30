import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx
from PIL import Image

from douyin_user_monitor.monitor import content_filter as cf
from douyin_user_monitor.monitor.archive_feed import build_archive_page
from douyin_user_monitor.settings import ContentFilterSettings

SETTINGS = ContentFilterSettings(
    enabled=True, mode="shadow", api_base="http://model.test/v1", api_key="k", model="vision-test",
    timeout_seconds=5, max_frames=3, reject_categories=("ad", "text_only", "screenshot"),
    min_confidence=0.8, include_history=True,
)
HAS_FFMPEG = shutil.which("ffmpeg") is not None
TAGS = {"subject": ["单人女生"], "scene": ["室内"], "content": ["穿搭"], "style": [], "features": ["露脸"]}
BORDERLINE = {"chest": 0, "hip": 0, "figure": 1, "overall": 1, "camera": 0, "motion": 0,
              "enhancers": [], "violation": False}
APPEARANCE = {"age": "成年", "outfit": ["吊带背心"], "exposed_areas": ["肩颈"],
              "camera_focus": ["上半身"], "poses": ["站立展示"], "borderline": BORDERLINE}


def reply(category="normal", ad_type=None, confidence=0.9, reason="实拍"):
    return json.dumps({"category": category, "ad_type": ad_type, "confidence": confidence, "reason": reason,
                       "tags": TAGS, "appearance": APPEARANCE, "summary": "女生在室内展示穿搭"}, ensure_ascii=False)


def model_transport(reply, *, status=200, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, text="upstream down")
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})
    return httpx.MockTransport(handler)


def make_video(path: Path, *, static: bool) -> None:
    # static：纯色图循环；dynamic：testsrc 每帧都在变
    source = ("color=c=white:s=320x568:d=4:r=25" if static
              else "testsrc2=s=320x568:d=4:r=25,noise=alls=40:allf=t+u")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", source, "-c:v", "libx264",
                    "-g", "25", "-pix_fmt", "yuv420p", "-y", str(path)], check=True)


class SignalAndParseTests(unittest.TestCase):
    def test_ad_signals_only_keep_present_fields(self):
        detail = {"is_ads": True, "promotions": [{"id": 1}], "anchors": None, "is_from_ad_auth": False,
                  "desc": "今天分享一个好物，链接在下方", "text_extra": [{"hashtag_name": "好物推荐"}]}

        signals = cf.extract_ad_signals(detail)

        self.assertEqual(signals["is_ads"], True)
        self.assertEqual(signals["promotions"], 1)
        self.assertNotIn("is_from_ad_auth", signals)
        self.assertNotIn("anchors", signals)
        self.assertIn("好物", signals["keywords"])
        self.assertIn("链接", signals["keywords"])

    def test_plain_work_has_no_signals(self):
        self.assertEqual(cf.extract_ad_signals({"desc": "跳舞", "is_ads": False, "promotions": []}), {})

    def test_motion_profiles(self):
        static = [(50000, True)] + [(300, False)] * 99
        dynamic = [(50000, True)] + [(20000, False)] * 99
        partly = [(50000, True)] + [(300, False)] * 60 + [(20000, False)] * 39

        self.assertEqual(cf.classify_motion(static)["profile"], "static")
        self.assertEqual(cf.classify_motion(dynamic)["profile"], "dynamic")
        self.assertEqual(cf.classify_motion(partly)["profile"], "partly_static")
        self.assertEqual(cf.classify_motion([(50000, True)])["profile"], "static")

    def test_parse_verdict_accepts_fenced_json(self):
        verdict = cf.parse_verdict(f"```json\n{reply('ad', 'selling', 0.93, '带货')}\n```")

        self.assertEqual(verdict["category"], "ad")
        self.assertEqual(verdict["ad_type"], "selling")
        self.assertEqual(verdict["tags"]["content"], ["穿搭"])
        self.assertEqual(verdict["tags"]["style"], [])
        self.assertEqual(verdict["summary"], "女生在室内展示穿搭")

    def test_parse_verdict_drops_ad_type_for_non_ads(self):
        verdict = cf.parse_verdict(reply("normal", "selling"))

        self.assertIsNone(verdict["ad_type"])

    def test_tags_are_trimmed_deduped_and_capped(self):
        data = json.loads(reply())
        data["tags"] = {"subject": ["多人", "多人", " "], "scene": [], "content": [f"c{i}" for i in range(9)],
                        "style": ["超级超级超级超级长的风格描述词"], "features": []}

        tags = cf.parse_verdict(json.dumps(data))["tags"]

        self.assertEqual(tags["subject"], ["多人"])
        self.assertEqual(len(tags["content"]), cf.MAX_TAGS_PER_GROUP)
        self.assertLessEqual(len(tags["style"][0]), cf.MAX_TAG_CHARS)

    def test_parse_verdict_rejects_invalid_output(self):
        base = json.loads(reply())
        bad_tags = {**base, "tags": {**base["tags"], "scene": "室内"}}
        no_tags = {key: value for key, value in base.items() if key != "tags"}
        for text in ("没有 JSON", '{"category":"cat","confidence":0.9}', '{"category":"ad","confidence":0.9}',
                     '{"category":"normal","confidence":"high"}', '{"category":"normal","confidence":1.5}',
                     json.dumps(bad_tags), json.dumps(no_tags)):
            with self.assertRaises(cf.ContentFilterError, msg=text):
                cf.parse_verdict(text)

    def _with_appearance(self, **appearance):
        data = json.loads(reply())
        data["appearance"] = {**APPEARANCE, **appearance}
        return json.dumps(data, ensure_ascii=False)

    def test_appearance_keeps_only_fixed_options(self):
        text = self._with_appearance(outfit=["泳装", "比基尼"], exposed_areas=["胸部上缘乳沟", "腰腹"],
                                     poses=["扭动", "扭动"],
                                     borderline={**BORDERLINE, "chest": 3, "enhancers": ["湿身", "发光"]})

        appearance = cf.parse_verdict(text)["appearance"]

        self.assertEqual(appearance["outfit"], ["泳装"])
        self.assertEqual(appearance["exposed_areas"], ["胸部上缘乳沟", "腰腹"])
        self.assertEqual(appearance["poses"], ["扭动"])
        self.assertEqual(appearance["borderline"]["chest"], 3)
        # 总分由程序算：胸部 3 分至少 4 分（模型不再给总分）
        self.assertEqual(appearance["borderline"]["score"], 4)
        self.assertEqual(appearance["borderline"]["enhancers"], ["湿身"])
        self.assertEqual(appearance["dropped"], ["outfit:比基尼", "enhancers:发光"])

    def test_non_adult_body_fields_and_borderline_forced_empty(self):
        for age in ("疑似未成年", "无法判断"):
            text = self._with_appearance(age=age, outfit=["泳装"], exposed_areas=["腰腹"], camera_focus=["胸部"],
                                         poses=["舞蹈"], borderline={**BORDERLINE, "chest": 3})

            appearance = cf.parse_verdict(text)["appearance"]

            self.assertEqual(appearance["outfit"], [], age)
            self.assertEqual(appearance["exposed_areas"], [], age)
            self.assertEqual(appearance["camera_focus"], [], age)
            self.assertIsNone(appearance["borderline"], age)
            self.assertEqual(appearance["poses"], ["舞蹈"], age)

    def test_invalid_appearance_rejected(self):
        for bad in ({"age": "中年"}, {"outfit": "泳装"}, {"borderline": None},
                    {"borderline": {**BORDERLINE, "figure": 4}}, {"borderline": {**BORDERLINE, "chest": "高"}},
                    {"borderline": {**BORDERLINE, "camera": 1.5}}, {"borderline": {**BORDERLINE, "motion": True}},
                    {"borderline": {**BORDERLINE, "violation": "否"}},
                    {"borderline": {k: v for k, v in BORDERLINE.items() if k != "overall"}}):
            with self.assertRaises(cf.ContentFilterError, msg=bad):
                cf.parse_verdict(self._with_appearance(**bad))

    def test_borderline_total_is_weighted_by_program(self):
        def total(**subs):
            item = {"chest": 0, "hip": 0, "figure": 0, "overall": 0, "camera": 0, "motion": 0, "violation": False, **subs}
            item["body_raw"], item["body"] = cf.body_score(item["hip"], item["figure"])
            return cf.borderline_total(item)

        cases = [
            ({}, (0.0, 0)),
            ({"overall": 1}, (2.0, 0)),                                                                 # 宽松睡衣/日常
            ({"chest": 1, "figure": 1, "overall": 1}, (5.7, 1)),
            ({"chest": 2, "figure": 2, "overall": 1, "camera": 1}, (10.4, 2)),                          # 吊带露乳沟自拍
            ({"chest": 1, "hip": 2, "figure": 2, "overall": 2, "camera": 1, "motion": 1}, (13.0, 3)),   # 瑜伽裤
            ({"chest": 2, "hip": 2, "figure": 3, "overall": 2, "camera": 1, "motion": 1}, (16.7, 4)),   # 健身紧身
            ({"chest": 3, "hip": 3, "figure": 3, "overall": 3, "camera": 2, "motion": 1}, (24.0, 5)),
            ({"chest": 3}, (9.0, 4)),                                                                   # 胸部 3 分保底 4
        ]
        for subs, expected in cases:
            self.assertEqual(total(**subs), expected, subs)
        # 胸部权重最高：胸部 1 分比身材 1 分贡献大
        self.assertGreater(total(chest=1)[0], total(hip=1, figure=1)[0])
        # 臀部占大头：同样一共 3 分，臀部高的比曲线高的总分高
        self.assertGreater(total(hip=3, figure=0)[0], total(hip=0, figure=3)[0])
        base = {name: 0 for name in cf.BORDERLINE_WEIGHTS} | {"violation": False}
        self.assertEqual(cf.borderline_total({**base, "violation": True})[1], 5)

    def test_model_supplied_total_is_ignored(self):
        text = self._with_appearance(borderline={**BORDERLINE, "score": 5})

        borderline = cf.parse_verdict(text)["appearance"]["borderline"]

        self.assertEqual((borderline["weighted"], borderline["score"]), (2.7, 0))  # 身材曲线 1、整体 1
        self.assertEqual((borderline["body_raw"], borderline["body"]), (0.35, 0))

    def test_vague_style_word_removed(self):
        data = json.loads(reply())
        data["tags"]["style"] = ["性感", "氛围感", "甜美"]

        self.assertEqual(cf.parse_verdict(json.dumps(data))["tags"]["style"], ["甜美"])

    def test_split_jpeg_stream(self):
        first, second = b"\xff\xd8\xffAAA\xff\xd9", b"\xff\xd8\xffBB\xff\xd9"

        self.assertEqual(cf.split_jpeg_stream(first + second), [first, second])
        self.assertEqual(cf.split_jpeg_stream(b""), [])

    def test_hashtags_fall_back_to_desc(self):
        self.assertEqual(cf.extract_hashtags({"desc": "今天 #穿搭 #甜妹 分享"}), ["穿搭", "甜妹"])

    def test_pick_evenly(self):
        self.assertEqual(cf.pick_evenly([1, 2], 3), [1, 2])
        self.assertEqual(cf.pick_evenly(list(range(10)), 3), [1, 5, 8])


class ContentFilterRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _run(self, record, reply=None, *,
             settings=SETTINGS, status=200, detail=None):
        seen = []
        content_filter = cf.ContentFilter(settings, self.root, transport=model_transport(reply or globals()['reply'](), status=status, seen=seen))
        asyncio.run(content_filter({"id": "u"}, detail or {"desc": "测试"}, record))
        return record.get("content_filter"), seen

    def _image_record(self, count=2):
        folder = self.root / "nick" / "图片" / "work"
        folder.mkdir(parents=True)
        files = []
        for index in range(1, count + 1):
            Image.new("RGB", (1080, 1440), "white").save(folder / f"{index}.jpg")
            files.append(f"nick/图片/work/{index}.jpg")
        Image.new("RGB", (1080, 1440), "white").save(folder / "cover.jpg")
        files.append("nick/图片/work/cover.jpg")
        return {"aweme_id": "1", "media_type": "image", "source": "incremental", "files": files,
                "cover_file": "nick/图片/work/cover.jpg"}

    def test_image_post_rejected_with_downscaled_images(self):
        result, seen = self._run(self._image_record(),
                                 reply("text_only", None, 0.95, "文字卡片"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["verdict"], "reject")
        self.assertEqual(result["category"], "text_only")
        self.assertEqual(result["frames"], 2)  # 封面不算进图文图片
        images = [part for part in seen[0]["messages"][0]["content"] if part["type"] == "image_url"]
        self.assertEqual(len(images), 2)
        self.assertEqual(seen[0]["model"], "vision-test")

    def test_low_confidence_is_kept(self):
        result, _ = self._run(self._image_record(1), reply("ad", "sponsored", 0.5, "疑似"))

        self.assertEqual(result["verdict"], "keep")
        self.assertEqual(result["category"], "ad")

    def test_category_outside_reject_list_is_kept(self):
        settings = replace(SETTINGS, reject_categories=("text_only",))
        result, _ = self._run(self._image_record(1), reply("ad", "selling", 0.99, "带货"),
                              settings=settings)

        self.assertEqual(result["verdict"], "keep")

    def test_model_http_error_is_recorded_not_raised(self):
        result, _ = self._run(self._image_record(1), status=503)

        self.assertEqual(result["status"], "error")
        self.assertNotIn("verdict", result)
        self.assertIn("503", result["error"])

    def test_missing_api_key_is_recorded(self):
        result, seen = self._run(self._image_record(1), settings=replace(SETTINGS, api_key=""))

        self.assertEqual(result["status"], "error")
        self.assertIn("DYMON_FILTER_API_KEY", result["error"])
        self.assertEqual(seen, [])

    def test_missing_media_file_is_recorded(self):
        record = {"aweme_id": "1", "media_type": "video", "source": "incremental",
                  "files": ["nick/a.mp4"], "video_file": "nick/a.mp4"}

        result, _ = self._run(record)

        self.assertEqual(result["status"], "error")
        self.assertIn("不存在", result["error"])

    def test_path_escape_is_refused(self):
        outside = self.root.parent / "outside.jpg"
        record = {"aweme_id": "1", "media_type": "image", "source": "incremental", "files": ["../outside.jpg"]}

        result, _ = self._run(record)

        self.assertFalse(outside.exists())
        self.assertIn("不在下载目录内", result["error"])

    def test_disabled_or_history_excluded_skips(self):
        record = self._image_record(1)
        self._run(record, settings=replace(SETTINGS, enabled=False))
        self.assertNotIn("content_filter", record)

        record["source"] = "history"
        self._run(record, settings=replace(SETTINGS, include_history=False))
        self.assertNotIn("content_filter", record)

    @unittest.skipUnless(HAS_FFMPEG, "需要 ffmpeg")
    def test_video_frames_motion_and_cover(self):
        folder = self.root / "nick"
        folder.mkdir()
        make_video(folder / "a.mp4", static=False)
        Image.new("RGB", (320, 568), "black").save(folder / "a.cover.jpg")
        record = {"aweme_id": "1", "media_type": "video", "source": "incremental",
                  "files": ["nick/a.mp4", "nick/a.cover.jpg"], "video_file": "nick/a.mp4",
                  "cover_file": "nick/a.cover.jpg", "video_duration_ms": 4000}

        result, seen = self._run(record, detail={"desc": "好物分享", "is_ads": True})

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["motion"]["profile"], "dynamic")
        self.assertEqual(result["signals"]["is_ads"], True)
        self.assertGreaterEqual(result["frames"], 2)  # 封面 + 至少一张关键帧
        self.assertTrue(result["cover_used"])
        prompt = seen[0]["messages"][0]["content"][0]["text"]
        self.assertIn("好物分享", prompt)
        self.assertIn("is_ads", prompt)

    @unittest.skipUnless(HAS_FFMPEG, "需要 ffmpeg")
    def test_missing_cover_is_optional_and_reported(self):
        (self.root / "nick").mkdir()
        make_video(self.root / "nick" / "a.mp4", static=False)
        record = {"aweme_id": "1", "media_type": "video", "source": "incremental", "files": ["nick/a.mp4"],
                  "video_file": "nick/a.mp4", "cover_file": "nick/gone.cover.jpg", "video_duration_ms": 4000}

        result, _ = self._run(record)

        self.assertEqual(result["status"], "ok", result)
        self.assertFalse(result["cover_used"])

    @unittest.skipUnless(HAS_FFMPEG, "需要 ffmpeg")
    def test_static_video_detected(self):
        make_video(self.root / "s.mp4", static=True)

        motion = asyncio.run(cf.probe_motion(self.root / "s.mp4"))

        self.assertEqual(motion["profile"], "static")


class ArchiveFeedFilterTests(unittest.TestCase):
    def _user(self):
        base = {"media_type": "video", "files": ["n/a.mp4"], "publish_time": "2026-09-01T00:00:00+08:00"}
        return {"download_records": [
            {**base, "aweme_id": "1", "content_filter": {"status": "ok", "verdict": "reject", "category": "ad",
                                                          "ad_type": "selling", "confidence": 0.9, "reason": "带货",
                                                          "signals": {"is_ads": True}}},
            {**base, "aweme_id": "2", "content_filter": {"status": "error", "error": "HTTP 503"}},
            {**base, "aweme_id": "3", "content_filter": {"status": "ok", "verdict": "keep", "category": "normal"}},
            {**base, "aweme_id": "4"},
        ]}

    def test_item_exposes_summary_without_debug_fields(self):
        items = build_archive_page(self._user())["items"]
        flagged = next(item for item in items if item["aweme_id"] == "1")

        self.assertEqual(flagged["content_filter"]["verdict"], "reject")
        self.assertNotIn("signals", flagged["content_filter"])
        self.assertIsNone(next(item for item in items if item["aweme_id"] == "4")["content_filter"])

    def test_borderline_views(self):
        user = self._user()
        records = user["download_records"]
        records[2]["content_filter"]["appearance"] = {"age": "成年", "borderline": {"score": 4}}
        records[3]["content_filter"] = {"status": "ok", "appearance": {"age": "成年", "borderline": {"score": 3}}}
        records[0]["content_filter"]["appearance"] = {"age": "无法判断", "borderline": None}
        ids = lambda view: sorted(item["aweme_id"] for item in build_archive_page(user, ai_filter=view)["items"])

        self.assertEqual(ids("borderline3"), ["3", "4"])
        self.assertEqual(ids("borderline4"), ["3"])
        item = next(i for i in build_archive_page(user)["items"] if i["aweme_id"] == "3")
        self.assertEqual(item["content_filter"]["appearance"]["borderline"]["score"], 4)

    def test_views(self):
        ids = lambda view: [item["aweme_id"] for item in build_archive_page(self._user(), ai_filter=view)["items"]]

        self.assertEqual(sorted(ids("")), ["1", "2", "3", "4"])
        self.assertEqual(ids("flagged"), ["1"])
        self.assertEqual(ids("error"), ["2"])
        with self.assertRaises(ValueError):
            build_archive_page(self._user(), ai_filter="bogus")


if __name__ == "__main__":
    unittest.main()
