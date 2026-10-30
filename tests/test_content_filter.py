import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

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
        # 总分由程序算：只看胸部不再保底（caliber 2），模型给的总分被忽略
        self.assertEqual(appearance["borderline"]["score"], 3)
        self.assertEqual(appearance["borderline"]["enhancers"], ["湿身"])
        self.assertEqual(appearance["dropped"], ["outfit:比基尼", "enhancers:发光"])

    def test_non_adult_body_fields_and_borderline_forced_empty(self):
        for age in ("疑似未满18岁", "无法判断"):
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
            return cf.borderline_total(item)[:2]

        cases = [
            ({}, (0.0, 0)),
            ({"overall": 1}, (2.0, 0)),                                                                 # 宽松睡衣/日常
            ({"chest": 1, "figure": 1, "overall": 1}, (5.7, 1)),
            ({"chest": 2, "figure": 2, "overall": 1, "camera": 1}, (10.4, 2)),                          # 吊带露乳沟自拍
            ({"chest": 1, "hip": 2, "figure": 2, "overall": 2, "camera": 1, "motion": 1}, (13.0, 3)),   # 瑜伽裤
            ({"chest": 2, "hip": 2, "figure": 3, "overall": 2, "camera": 1, "motion": 1}, (16.7, 4)),   # 健身紧身
            ({"chest": 3, "hip": 3, "figure": 3, "overall": 3, "camera": 2, "motion": 1}, (24.0, 5)),
            # caliber 2：只看胸部不再保底（实测 chest>=3 且其余为 0 的只有 2/15,145 条）
            ({"chest": 3}, (9.0, 2)),
            # 保底改成「胸部与整体暴露同时成立」
            ({"chest": 2, "overall": 3}, (12.0, 4)),
        ]
        for subs, expected in cases:
            self.assertEqual(total(**subs), expected, subs)
        # 胸部权重最高：胸部 1 分比身材 1 分贡献大
        self.assertGreater(total(chest=1)[0], total(hip=1, figure=1)[0])
        # 臀部占大头：同样一共 3 分，臀部高的比曲线高的总分高
        self.assertGreater(total(hip=3, figure=0)[0], total(hip=0, figure=3)[0])
        base = {name: 0 for name in cf.BORDERLINE_WEIGHTS} | {"violation": False}
        self.assertEqual(cf.borderline_total({**base, "violation": True})[1], 5)

    def test_violation_rule_fires_without_model_flag(self):
        """模型端 violation 在 15,145 条里触发 0 次，疑似违规改由分项判定。"""
        def score(**subs):
            item = {"chest": 0, "hip": 0, "figure": 0, "overall": 0, "camera": 0, "motion": 0,
                    "violation": False, "enhancers": [], **subs}
            item["body_raw"], item["body"] = cf.body_score(item["hip"], item["figure"])
            return cf.borderline_total(item)

        # 整体 3 档但加权分没到 17、也没有透视/湿身 → 不升级，只按加权分归档
        weighted, band, low, zone = score(overall=3, camera=3, motion=3)
        self.assertEqual((weighted, band), (12.0, 3))
        self.assertLess(weighted, cf.VIOLATION_WEIGHTED_FLOOR)
        # 整体 3 档 + 透视 → 疑似违规，一票 5 分（加权分只有 6.0，本来落在最低档）
        self.assertEqual(score(overall=3, enhancers=["透视"])[1], 5)
        # 整体 3 档 + 加权分 ≥17 → 同样 5 分（不加规则的话是 4 档）
        bumped = score(chest=2, hip=3, figure=3, overall=3, camera=3)
        self.assertEqual((bumped[0], bumped[1]), (21.0, 5))
        # 遮盖多（overall 1）时就算有湿身也不算疑似违规
        self.assertEqual(score(overall=1, enhancers=["湿身"])[1], 0)
        # 加权分很高但整体没到 3 档 → 不触发，维持加权分归档的 4 档
        self.assertEqual(score(chest=2, hip=3, figure=3, overall=2)[:2], (16.0, 4))

    def test_hysteresis_gives_conservative_band_and_zone(self):
        def parts(**subs):
            item = {"chest": 0, "hip": 0, "figure": 0, "overall": 0, "camera": 0, "motion": 0,
                    "violation": False, **subs}
            item["body_raw"], item["body"] = cf.body_score(item["hip"], item["figure"])
            return cf.borderline_total(item, hysteresis_delta=0.5)

        # weighted 6.0 离 3.0 / 8.5 两个阈值都超过 0.5 → 不是边界区，保守档与总分一致
        self.assertEqual(parts(overall=2, camera=1, motion=1), (6.0, 1, 1, False))
        # weighted 8.7 压在 2 档阈值 8.5 上（差 0.2 ≤ δ）：抖一下就掉档
        self.assertEqual(parts(hip=2, figure=3, overall=2), (8.7, 2, 1, True))
        # 关掉滞回时保守档等于总分，且不报边界
        item = {"chest": 0, "hip": 2, "figure": 3, "overall": 2, "camera": 0, "motion": 0, "violation": False}
        item["body_raw"], item["body"] = cf.body_score(2, 3)
        self.assertEqual(cf.borderline_total(item), (8.7, 2, 2, False))

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

    def test_blocked_frame_is_skipped_and_retried(self):
        calls = []

        async def fake_ask(settings, prompt, images, **kwargs):
            calls.append((prompt, list(images)))
            if len(images) > 1 or images[0] == b"bad":
                raise cf.ModelBlockedError("模型返回空结果（疑似被上游安全策略拦截）")
            return globals()['reply']()

        content_filter = cf.ContentFilter(SETTINGS, self.root)
        with mock.patch.object(cf, "ask_model", fake_ask):
            text, used = asyncio.run(content_filter._ask_with_frame_fallback("打分要求", [b"bad", b"good"]))

        self.assertEqual(used, [1])
        self.assertIn("安全策略拦截", calls[-1][0])
        self.assertIn('"category"', text)

    def test_all_blocked_frames_raise(self):
        async def fake_ask(*args, **kwargs):
            raise cf.ModelBlockedError("模型返回空结果（疑似被上游安全策略拦截）")

        content_filter = cf.ContentFilter(SETTINGS, self.root)
        with mock.patch.object(cf, "ask_model", fake_ask):
            with self.assertRaises(cf.ModelBlockedError):
                asyncio.run(content_filter._ask_with_frame_fallback("打分要求", [b"a", b"b"]))

    def test_model_block_retries_without_extra_frames(self):
        def handler(request):
            body = json.loads(request.content)
            images = [part for part in body["messages"][0]["content"] if part["type"] == "image_url"]
            if len(images) > 1:
                return httpx.Response(200, json={"choices": []})
            return httpx.Response(200, json={"choices": [{"message": {"content": globals()['reply']()}}]})

        content_filter = cf.ContentFilter(SETTINGS, self.root, transport=httpx.MockTransport(handler))
        record = self._image_record(2)
        asyncio.run(content_filter({"id": "u"}, {"desc": "测试"}, record))

        result = record["content_filter"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["frames"], 2)
        self.assertEqual(result["scored_frames"], 1)
        self.assertEqual(result["skipped_frames"], [2])

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


    def test_consistency_flags_only_hard_contradictions(self):
        def verdict_of(*, appearance=None, borderline=None, tags=None):
            data = json.loads(reply())
            person = {"age": "成年", "outfit": [], "exposed_areas": [], "camera_focus": [], "poses": []}
            bands = {"chest": 0, "hip": 0, "figure": 0, "overall": 0, "camera": 0, "motion": 0,
                     "enhancers": [], "violation": False}
            bands.update(borderline or {})
            person.update(appearance or {})
            person["borderline"] = bands
            data["appearance"] = person
            if tags:
                data["tags"] = {**data["tags"], **tags}
            return cf.parse_verdict(json.dumps(data, ensure_ascii=False))

        # 干净结论、以及「有乳沟且胸部给了 1 分」这类自洽组合都不该报
        self.assertEqual(cf.consistency_issues(verdict_of()), [])
        self.assertEqual(cf.consistency_issues(
            verdict_of(appearance={"exposed_areas": ["胸部上缘乳沟"]}, borderline={"chest": 1})), [])
        self.assertEqual(cf.consistency_issues(
            verdict_of(tags={"subject": ["无人物"]})), [])          # 无人物 + 全零分是正常的

        cases = {
            "有乳沟却给胸部 0 分": dict(appearance={"exposed_areas": ["胸部上缘乳沟"]}),
            "镜头聚焦胸臀却给镜头 0 分": dict(appearance={"camera_focus": ["胸部"]}),
            "内衣泳装却算几乎没露出": dict(appearance={"outfit": ["泳装"]}),
            "标了透视却算几乎没露出": dict(borderline={"enhancers": ["透视"]}),
            "判为无人物却有身体描述": dict(tags={"subject": ["无人物"]}, appearance={"outfit": ["日常"]}),
        }
        for name, fields in cases.items():
            self.assertIn(name, cf.consistency_issues(verdict_of(**fields)), name)
        # 同一条检查也覆盖「无人物但擦了擦边分」这种触发方式
        self.assertIn("判为无人物却有身体描述", cf.consistency_issues(
            verdict_of(tags={"subject": ["无人物"]}, borderline={"overall": 2})))

    def test_consistency_skips_works_without_adult_person(self):
        """未判定成年时身体字段已被强制清空，不该因此误报矛盾。"""
        data = json.loads(reply())
        data["appearance"] = {"age": "无法判断", "outfit": ["泳装"], "exposed_areas": ["胸部上缘乳沟"],
                              "camera_focus": ["胸部"], "poses": [], "borderline": None}

        verdict = cf.parse_verdict(json.dumps(data, ensure_ascii=False))

        self.assertEqual(verdict["appearance"]["outfit"], [])
        self.assertEqual(cf.consistency_issues(verdict), [])


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
        records[2]["content_filter"]["appearance"] = {"age": "成年",
                                                     "borderline": {"score": 4, "score_low": 3}}
        records[3]["content_filter"] = {"status": "ok", "caliber": 2, "scoring": "batch", "version": 5,
                                        "appearance": {"age": "成年",
                                                       "borderline": {"score": 3, "score_low": 2}}}
        records[0]["content_filter"]["appearance"] = {"age": "无法判断", "borderline": None}
        def ids(view):
            return sorted(item["aweme_id"] for item in build_archive_page(user, ai_filter=view)["items"])

        self.assertEqual(ids("borderline3"), ["3", "4"])
        self.assertEqual(ids("borderline4"), ["3"])
        # *_strict 用保守档：重跑一遍可能掉档的作品不进这个视图
        self.assertEqual(ids("borderline3_strict"), ["3"])
        self.assertEqual(ids("borderline4_strict"), [])
        item = next(i for i in build_archive_page(user)["items"] if i["aweme_id"] == "3")
        self.assertEqual(item["content_filter"]["appearance"]["borderline"]["score"], 4)

    def test_caliber_and_version_reach_the_frontend(self):
        """口径标号要下发，前端才分得清新旧算法；调试字段仍不下发。"""
        user = self._user()
        user["download_records"][2]["content_filter"].update({"caliber": 2, "version": 5,
                                                             "motion": {"profile": "dynamic"}, "frames": 7})
        item = next(i for i in build_archive_page(user)["items"] if i["aweme_id"] == "3")

        self.assertEqual((item["content_filter"]["caliber"], item["content_filter"]["version"]), (2, 5))
        self.assertNotIn("motion", item["content_filter"])
        self.assertNotIn("frames", item["content_filter"])
        self.assertNotIn("scoring", item["content_filter"])

    def test_inconsistent_view(self):
        user = self._user()
        user["download_records"][2]["content_filter"]["inconsistent"] = ["内衣泳装却算几乎没露出"]

        def view(name):
            return [item["aweme_id"] for item in build_archive_page(user, ai_filter=name)["items"]]

        self.assertEqual(view("inconsistent"), ["3"])
        item = next(i for i in build_archive_page(user)["items"] if i["aweme_id"] == "3")
        self.assertEqual(item["content_filter"]["inconsistent"], ["内衣泳装却算几乎没露出"])
        self.assertEqual(view("flagged"), ["1"])          # 不影响别的视图

    def test_views(self):
        def ids(view):
            return [item["aweme_id"] for item in build_archive_page(self._user(), ai_filter=view)["items"]]

        self.assertEqual(sorted(ids("")), ["1", "2", "3", "4"])
        self.assertEqual(ids("flagged"), ["1"])
        self.assertEqual(ids("error"), ["2"])
        with self.assertRaises(ValueError):
            build_archive_page(self._user(), ai_filter="bogus")


class BatchPromptTests(unittest.TestCase):
    def _media(self, note="图说明", count=2):
        return cf.PreparedMedia([b"a"] * count, note)

    def _meta(self, desc):
        return {"desc": desc, "hashtags": "（无）", "signals": {},
                "signals_text": "（无）", "motion_text": "（未测）"}

    def test_batch_prompt_has_markers_notes_and_array_schema(self):
        content = cf.build_batch_content([(self._meta("作品甲"), self._media("甲说明", 2)),
                                          (self._meta("作品乙"), self._media("乙说明", 1))])
        texts = [part["text"] for part in content if part["type"] == "text"]
        images = [part for part in content if part["type"] == "image_url"]
        joined = "\n".join(texts)

        self.assertIn("【作品 1 开始】", joined)
        self.assertIn("【作品 2 结束】", joined)
        self.assertIn("甲说明", joined)
        self.assertIn("乙说明", joined)
        self.assertIn("数组长度必须为 2", joined)
        self.assertIn('"id"', joined)
        self.assertIn("绝不能把 traffic 等 ad_type 填进 category", joined)
        self.assertIn("年龄只看本作品里的真人", joined)
        self.assertNotIn("只输出一个 JSON 对象", joined)
        self.assertEqual(len(images), 3)


def batch_item(index, **overrides):
    data = json.loads(reply())
    data["id"] = index
    data.update(overrides)
    return data


def batch_payload(count=2, overrides=None):
    overrides = overrides or {}
    return json.dumps([batch_item(index, **overrides.get(index, {}))
                       for index in range(1, count + 1)], ensure_ascii=False)


class BatchParseTests(unittest.TestCase):
    def test_maps_by_id_when_ids_cover_the_batch(self):
        parsed = cf.parse_batch_verdicts(
            "```json\n" + json.dumps([batch_item(2, summary="乙"), batch_item(1, summary="甲")],
                                     ensure_ascii=False) + "\n```", 2)

        self.assertEqual(parsed.errors, {})
        self.assertEqual(parsed.items[1]["summary"], "甲")
        self.assertEqual(parsed.items[2]["summary"], "乙")

    def test_duplicate_ids_do_not_overwrite_and_missing_stays_missing(self):
        text = json.dumps([batch_item(1, summary="甲"), batch_item(1, summary="重复"), batch_item(3)],
                          ensure_ascii=False)

        parsed = cf.parse_batch_verdicts(text, 4)

        self.assertEqual(parsed.items[1]["summary"], "甲")
        self.assertNotIn(2, parsed.items)
        self.assertNotIn(4, parsed.items)

    def test_results_wrapper_is_accepted(self):
        parsed = cf.parse_batch_verdicts(json.dumps({"results": [batch_item(1)]}), 1)

        self.assertIn(1, parsed.items)

    def test_category_ad_type_leak_is_repaired(self):
        parsed = cf.parse_batch_verdicts(batch_payload(1, {1: {"category": "traffic", "ad_type": None}}), 1)

        self.assertEqual(parsed.items[1]["category"], "ad")
        self.assertEqual(parsed.items[1]["ad_type"], "traffic")
        self.assertEqual(parsed.repaired, 1)

    def test_empty_age_without_people_is_repaired(self):
        tags = {"subject": ["无人物"], "scene": [], "content": [], "style": [], "features": []}
        parsed = cf.parse_batch_verdicts(
            batch_payload(1, {1: {"appearance": {**APPEARANCE, "age": ""}, "tags": tags}}), 1)

        self.assertEqual(parsed.items[1]["appearance"]["age"], "无法判断")
        self.assertEqual(parsed.repaired, 1)

    def test_empty_age_with_real_people_is_a_schema_error(self):
        parsed = cf.parse_batch_verdicts(
            batch_payload(1, {1: {"appearance": {**APPEARANCE, "age": ""}}}), 1)

        self.assertNotIn(1, parsed.items)
        self.assertIn("age", parsed.errors[1])


class FakeMediaLoader:
    def __init__(self):
        self.calls = 0

    async def prepare(self, user, record, max_frames):
        self.calls += 1
        return cf.PreparedMedia([b"img-1", b"img-2"], "测试图，共 2 张")


class EvaluateBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _entries(self, count=2):
        return [({"id": "u"}, {"desc": f"作品{index}"}, {"aweme_id": str(index)})
                for index in range(1, count + 1)]

    def _handler(self, calls, batch_content, single_content=None, *,
                 batch_status=200, single_status=200):
        def handler(request):
            body = json.loads(request.content)
            text = body["messages"][0]["content"][0]["text"]
            is_batch = "【作品" in text
            calls.append("batch" if is_batch else "single")
            status = batch_status if is_batch else single_status
            if status != 200:
                return httpx.Response(status, text="upstream down")
            content = batch_content if is_batch else single_content
            if content is None:
                return httpx.Response(200, json={"choices": []})
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        return handler

    def _run(self, handler, *, count=2, counter=None, rescore_risk=True):
        content_filter = cf.ContentFilter(SETTINGS, self.root, transport=httpx.MockTransport(handler))
        return asyncio.run(content_filter.evaluate_batch(self._entries(count), FakeMediaLoader(),
                                                         counter=counter, rescore_risk=rescore_risk))

    def test_two_clean_works_use_one_request(self):
        calls = []
        outcome = self._run(self._handler(calls, batch_payload(2)))

        self.assertEqual(calls, ["batch"])
        self.assertEqual(outcome.requests_used, 1)
        self.assertEqual(outcome.stop_reason, None)
        self.assertEqual([result["status"] for result in outcome.results], ["ok", "ok"])
        self.assertEqual([result["scoring"]["mode"] for result in outcome.results], ["batch", "batch"])
        self.assertEqual([(result["version"], result["caliber"]) for result in outcome.results],
                         [(cf.RESULT_VERSION, cf.CALIBER)] * 2)
        self.assertEqual([result["mode"] for result in outcome.results], ["shadow", "shadow"])

    def test_single_entry_batch_uses_the_single_work_prompt(self):
        calls = []
        outcome = self._run(self._handler(calls, None, reply("normal")), count=1)

        self.assertEqual(calls, ["single"])
        self.assertEqual(outcome.requests_used, 1)
        self.assertEqual(outcome.results[0]["status"], "ok")
        self.assertEqual((outcome.results[0]["version"], outcome.results[0]["caliber"]),
                         (cf.RESULT_VERSION, cf.CALIBER))

    def test_suspicious_age_is_rescored_with_one_more_request(self):
        calls = []
        batch = batch_payload(2, {1: {"appearance": {**APPEARANCE, "age": "无法判断"}}})
        outcome = self._run(self._handler(calls, batch, reply("normal")))

        self.assertEqual(calls, ["batch", "single"])
        self.assertEqual(outcome.requests_used, 2)
        self.assertEqual(outcome.rescored, 1)
        self.assertEqual(outcome.results[0]["appearance"]["age"], "成年")
        self.assertTrue(outcome.results[0]["scoring"]["rescored"])
        self.assertEqual(outcome.results[0]["version"], cf.RESULT_VERSION)

    def test_only_schema_error_item_is_rescored(self):
        calls = []
        batch = batch_payload(2, {2: {"appearance": {**APPEARANCE, "age": ""}}})
        outcome = self._run(self._handler(calls, batch, reply("normal")))

        self.assertEqual(calls, ["batch", "single"])
        self.assertEqual(outcome.schema_errors, 1)
        self.assertEqual(outcome.rescored, 1)
        self.assertFalse(outcome.results[0]["scoring"]["rescored"])
        self.assertTrue(outcome.results[1]["scoring"]["rescored"])

    def test_blocked_batch_falls_back_to_one_single_per_work_without_frame_probing(self):
        calls = []
        outcome = self._run(self._handler(calls, None, None))

        self.assertEqual(calls, ["batch", "single", "single"])
        self.assertEqual(outcome.requests_used, 3)
        self.assertEqual([result["status"] for result in outcome.results], ["error", "error"])

    def test_unparseable_batch_falls_back_to_one_single_per_work(self):
        calls = []
        outcome = self._run(self._handler(calls, "我不输出 JSON 数组", reply("normal")))

        self.assertEqual(calls, ["batch", "single", "single"])
        self.assertEqual([result["status"] for result in outcome.results], ["ok", "ok"])

    def test_budget_stops_before_risk_rescore(self):
        batch = batch_payload(2, {1: {"appearance": {**APPEARANCE, "age": "无法判断"}}})
        calls = []
        counter = cf.RequestCounter(limit=1)
        outcome = self._run(self._handler(calls, batch, reply("normal")), counter=counter)

        self.assertEqual(calls, ["batch"])
        self.assertEqual(outcome.stop_reason, "budget")
        self.assertTrue(outcome.results[0]["scoring"]["rescore_pending"])
        self.assertEqual(counter.used, 1)

    def test_batch_429_stops_without_results(self):
        outcome = self._run(self._handler([], None, batch_status=429))

        self.assertEqual(outcome.stop_reason, "rate_limited")
        self.assertEqual(outcome.results, [None, None])

    def test_batch_503_is_transient(self):
        outcome = self._run(self._handler([], None, batch_status=503))

        self.assertEqual(outcome.stop_reason, "transient")
        self.assertEqual([result["status"] for result in outcome.results], ["error", "error"])

    def test_rescore_429_keeps_the_parsed_batch_result(self):
        calls = []
        batch = batch_payload(2, {1: {"appearance": {**APPEARANCE, "age": "无法判断"}}})
        outcome = self._run(self._handler(calls, batch, batch_status=200, single_status=429))

        self.assertEqual(calls, ["batch", "single"])
        self.assertEqual(outcome.stop_reason, "rate_limited")
        self.assertEqual(outcome.results[0]["status"], "ok")
        self.assertFalse(outcome.results[0]["scoring"]["rescored"])

    def test_high_score_crossing_the_band_is_rescored_but_plain_zone_is_not(self):
        high = batch_item(1, appearance={**APPEARANCE, "borderline": {
            **BORDERLINE, "chest": 2, "hip": 3, "figure": 2, "overall": 1, "camera": 1, "motion": 2}})
        zone = batch_item(1, appearance={**APPEARANCE, "borderline": {
            **BORDERLINE, "chest": 1, "hip": 2, "figure": 2, "overall": 2, "camera": 1, "motion": 0}})
        meta = {"desc": "作品", "signals": {}, "signals_text": "（无）", "motion_text": "（未测）"}

        high_verdict = cf.parse_verdict(json.dumps(high), hysteresis_delta=SETTINGS.hysteresis_delta)
        zone_verdict = cf.parse_verdict(json.dumps(zone), hysteresis_delta=SETTINGS.hysteresis_delta)

        self.assertIn("high_score", cf.batch_rescore_reasons(high_verdict, meta, {}, SETTINGS))
        self.assertEqual(cf.batch_rescore_reasons(zone_verdict, meta, {}, SETTINGS), [])

    def test_age_unknown_without_people_is_not_rescored(self):
        tags = {"subject": ["无人物"], "scene": [], "content": ["情感语录"], "style": [], "features": ["文字卡片"]}
        item = batch_item(1, appearance={**APPEARANCE, "age": "无法判断"}, tags=tags)
        verdict = cf.parse_verdict(json.dumps(item), hysteresis_delta=SETTINGS.hysteresis_delta)

        self.assertEqual(cf.batch_rescore_reasons(verdict, {"desc": "纯文字", "signals": {}}, {}, SETTINGS), [])


if __name__ == "__main__":
    unittest.main()
