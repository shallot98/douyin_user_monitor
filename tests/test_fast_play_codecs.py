from __future__ import annotations

import unittest

from douyin_user_monitor.monitor.video_source import (
    select_clearest_video_source,
    select_fast_play_source,
)


def gear(*, name: str, bit_rate: int, short_side: int = 576, **flags) -> dict:
    return {
        "gear_name": name,
        "bit_rate": bit_rate,
        "play_addr": {
            "url_list": [f"https://cdn.example/{name}.mp4"],
            "width": short_side,
            "height": short_side * 2,
        },
        **flags,
    }


class FastPlayCodecTests(unittest.TestCase):
    def test_prefers_browser_compatible_encode_over_smaller_hevc(self):
        video = {
            "bit_rate": [
                gear(name="hevc", bit_rate=492_619, is_h265=1, is_bytevc1=1),
                gear(name="h264_low", bit_rate=884_237, is_h265=0, is_bytevc1=0),
                gear(name="h264_high", bit_rate=1_361_633, is_h265=0, is_bytevc1=0),
            ]
        }
        self.assertEqual(select_fast_play_source(video).gear_name, "h264_low")

    def test_each_codec_flag_can_identify_h264(self):
        for key in ("is_h265", "is_bytevc1"):
            with self.subTest(key=key):
                video = {
                    "bit_rate": [
                        gear(name="hevc", bit_rate=400_000, **{key: 1}),
                        gear(name="h264", bit_rate=900_000, **{key: 0}),
                    ]
                }
                self.assertEqual(select_fast_play_source(video).gear_name, "h264")

    def test_unknown_or_conflicting_codec_does_not_outrank_h264(self):
        video = {
            "bit_rate": [
                gear(name="unknown", bit_rate=100_000),
                gear(name="conflicting", bit_rate=200_000, is_h265=0, is_bytevc1=1),
                gear(name="h264", bit_rate=900_000, is_h265=0, is_bytevc1=0),
            ]
        }
        self.assertEqual(select_fast_play_source(video).gear_name, "h264")

    def test_resolution_floor_applies_within_h264_encodes(self):
        video = {
            "bit_rate": [
                gear(name="hevc", bit_rate=100_000, is_h265=1),
                gear(name="h264_tiny", bit_rate=200_000, short_side=240, is_h265=0),
                gear(name="h264", bit_rate=900_000, is_h265=0),
            ]
        }
        self.assertEqual(select_fast_play_source(video).gear_name, "h264")

    def test_compatible_encode_below_floor_still_preferred_to_hevc(self):
        video = {
            "bit_rate": [
                gear(name="hevc", bit_rate=100_000, is_h265=1),
                gear(name="h264", bit_rate=200_000, short_side=240, is_h265=0),
            ]
        }
        self.assertEqual(select_fast_play_source(video).gear_name, "h264")

    def test_hevc_only_works_require_preparation_from_archive(self):
        video = {
            "bit_rate": [
                gear(name="hevc_small", bit_rate=400_000, is_h265=1),
                gear(name="hevc_large", bit_rate=900_000, is_h265=1),
            ]
        }
        self.assertIsNone(select_fast_play_source(video))

    def test_unknown_or_conflicting_codec_requires_preparation(self):
        self.assertIsNone(select_fast_play_source({"bit_rate": [gear(name="unknown", bit_rate=100_000)]}))
        self.assertIsNone(select_fast_play_source({
            "bit_rate": [gear(name="conflicting", bit_rate=200_000, is_h265=0, is_bytevc1=1)],
        }))

    def test_explicit_top_level_h264_is_available_without_bitrate_list(self):
        video = {"play_addr_h264": {"url_list": ["https://cdn.example/h264.mp4"], "width": 540, "height": 960}}
        self.assertEqual(select_fast_play_source(video).url, "https://cdn.example/h264.mp4")

    def test_archive_quality_selection_is_independent_of_web_codec(self):
        video = {
            "bit_rate": [
                gear(name="hevc_1080", bit_rate=3_000_000, short_side=1080, is_h265=1),
                gear(name="h264_540", bit_rate=900_000, is_h265=0),
            ]
        }
        self.assertEqual(select_clearest_video_source(video).gear_name, "hevc_1080")
        self.assertEqual(select_fast_play_source(video).gear_name, "h264_540")


if __name__ == "__main__":
    unittest.main()
