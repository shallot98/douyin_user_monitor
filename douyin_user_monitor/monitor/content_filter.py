"""作品 AI 内容标注与筛选：详细标签 + 广告 / 纯文字图片 / 截图判定。

新作品：下载完成、生成下载记录后调用（本地文件）。
历史作品：scripts/content_filter_backfill.py 批量补打（本地没有就从网盘读，见 content_filter_media）。
结论写进 record["content_filter"]，version 标识结构版本，补打任务据此判断要不要重做。

流程：接口字段 / 标题取广告信号 → 画面运动（仅本地文件，ffprobe 读包大小不解码）→
只解关键帧抽图 → 一次视觉模型调用 → 严格解析 JSON。

第一阶段（shadow）只打标签，不移动、不删除文件。任何失败都记成 status=error 并保留原因，
绝不默认成通过或筛掉，也不向上抛异常（向上抛会丢掉这条下载记录，下一轮重复下载）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from PIL import Image

from douyin_user_monitor.monitor.errors import sanitize_public_error
from douyin_user_monitor.monitor.playback_media import run_media_tool
from douyin_user_monitor.monitor.timeutils import cn_now

logger = logging.getLogger(__name__)

# 5：胸部权重 3、身材 2（09-30 按权重 胸2/身材2.75 打的 v4 作废，全部重打）
RESULT_VERSION = 5
CATEGORIES = ("normal", "ad", "text_only", "screenshot")
AD_TYPES = ("selling", "sponsored", "traffic", "local_promo")
TAG_GROUPS = ("subject", "scene", "content", "style", "features")
MAX_TAGS_PER_GROUP = 6
MAX_TAG_CHARS = 12
# “性感”已由 borderline 分档拆细；“氛围感”是模型的万能填充词，没有区分度
BANNED_STYLE_TAGS = frozenset({"性感", "氛围感"})

# 人物外观：固定选项，便于筛选统计；模型给出选项外的值会被丢弃并记进 dropped
AGE_ADULT = "成年"
AGE_OPTIONS = (AGE_ADULT, "疑似未成年", "无法判断")
OUTFIT_OPTIONS = ("日常", "紧身", "瑜伽裤紧身裤", "丝袜", "吊带背心", "短裙短裤", "露背装", "露脐装", "泳装", "内衣", "睡衣",
                  "Cosplay", "运动服", "正装", "汉服古装")
EXPOSED_AREA_OPTIONS = ("肩颈", "手臂", "腰腹", "背部", "腿部", "胸部上缘乳沟", "臀部轮廓")
CAMERA_FOCUS_OPTIONS = ("面部", "全身", "上半身", "胸部", "腰臀", "腿脚", "手部")
POSE_OPTIONS = ("站立展示", "坐姿", "躺卧", "舞蹈", "扭动", "贴近镜头", "转身展示背面", "行走",
                "弹奏演奏", "运动健身")
ENHANCER_OPTIONS = ("紧身贴合", "透视", "湿身", "油光", "丝袜")
# 擦边分档：字段 → 最大值（都从 0 起）
BORDERLINE_SCALES = {"chest": 3, "hip": 3, "figure": 3, "overall": 3, "camera": 3, "motion": 3}
# 身材 = 臀部与曲线合成的一个分（臀部占大头）；模型仍分别打两项，界面/筛选/排序只用合成后的 body
BODY_HIP_SHARE = 0.65
# 总分由程序按分项加权算，不再让模型直接给（模型给的总分常与分项对不上）。
# 胸部最重要，其次臀部、身材展示；换算线按试打样本校准，见 borderline_total()
# 整体暴露合并了原来的“着装暴露”（两项试打中 79% 同分，是同一件事），权重取两者之和
# 每件事只在一个分项里算：衣服露多少→胸部/整体；衣服勾勒轮廓→身材展示；镜头→镜头强调；姿势动作→动作展示
# 胸部最重要，身材第二（身材 = 臀部 65% + 曲线 35%）
BORDERLINE_WEIGHTS = {"chest": 3.0, "body": 2.0, "overall": 2.0, "camera": 1.0, "motion": 1.0}
# 加权分 >= 阈值 即达到对应总分（1~5）。2026-09-30 按胸部 3 / 身材 2 的权重用 33 个样本校准：
# 宽松睡衣/日常≈2→0，吊带露乳沟自拍≈10.4→2，紧身/瑜伽裤勾勒臀型≈13→3，健身紧身/泳装露出多≈16.4~17.4→4
BORDERLINE_TOTAL_THRESHOLDS = ((5, 20.0), (4, 16.0), (3, 11.5), (2, 8.5), (1, 3.0))
# 未判定为成年时，这些与身体相关的字段一律清空（无论模型填了什么）
BODY_LIST_FIELDS = ("outfit", "exposed_areas", "camera_focus")
MODE_SHADOW = "shadow"

# 非关键帧小于“每百万像素这么多字节”视为画面没动（按分辨率归一化）。
# 实测 1080x1920：纯图片视频非关键帧均值 ~600B（~270B/MP），正常视频 p10 ~4.5KB（~2200B/MP）
STATIC_BYTES_PER_MEGAPIXEL = 725
MIN_STATIC_PACKET_BYTES = 64
STATIC_RATIO = 0.9
PARTLY_STATIC_RATIO = 0.5
FRAME_SHORT_SIDE = 512
FRAME_JPEG_QUALITY = 80
MAX_OUTPUT_TOKENS = 700
# 网盘读取：单遍顺序读最多这么多个关键帧再均匀挑，避免长视频整片下载
STREAM_KEYFRAME_CAP = 12
STREAM_RW_TIMEOUT_US = 30_000_000
SCALE_FILTER = (f"scale='if(lt(iw,ih),{FRAME_SHORT_SIDE},-2)'"
                f":'if(lt(iw,ih),-2,{FRAME_SHORT_SIDE})'")
JPEG_SOI = b"\xff\xd8\xff"

AD_KEYWORDS = (
    "广告", "商单", "合作", "赞助", "好物", "推荐", "种草", "链接", "小黄车", "下单", "优惠", "折扣",
    "团购", "探店", "券", "直播间", "加微信", "vx", "私信", "橱窗", "带货", "同款", "品牌",
)

PROMPT = """你是抖音作品审核与标注员。请看图并完成两件事：判断是否需要筛掉，并给作品打详细标签。
{image_note}

作品标题：{desc}
话题标签：{hashtags}
广告线索（接口元数据/标题关键词，仅供参考）：{signals}
画面运动情况（按视频码流估算）：{motion}

一、分类 category：
- ad：打广告。ad_type 细分：
  selling=带货（展示/推荐商品、挂小黄车、口播下单、价格优惠）；
  sponsored=品牌商单/软广（为某品牌或产品做推广，未必挂链接）；
  traffic=引流（引导加微信/私信、直播预告、推广小程序或游戏）；
  local_promo=探店/团购/本地生活推广。
- text_only：整个作品基本就是一张或几张以文字为主的图片（文字卡片、语录、长文图），没有实拍内容。
- screenshot：主体是聊天记录、网页、APP 界面等截图。
- normal：其他正常内容。真人出镜、实拍、舞蹈、穿搭、vlog 即使带字幕或话题也属于 normal。
广告线索只是线索，要结合画面判断；普通带字幕的实拍视频不是 text_only。

二、标签 tags（按画面实际内容，每组 0~6 个中文短词，每个不超过 6 个字，看不出就留空，不要编造）：
- subject 主体：如 单人女生、单人男生、多人、情侣、儿童、宠物、无人物
- scene 场景：如 室内、卧室、车内、户外、街拍、海边、健身房、舞台、商场
- content 内容类型（作品在做什么；滤镜、特效属于 features，不要写在这里）：如 舞蹈、手势舞、穿搭、变装、口播、剧情、vlog、自拍、美妆、美食、旅行、健身、才艺、游戏、搞笑、情感语录
- style 风格（只写有区分度的词，没有明显风格就留空；不要写“性感”“氛围感”）：如 甜美、清新、酷飒、可爱、文艺、搞笑、复古、御姐、元气
- features 画面特征：如 露脸、特写、字幕、滤镜、特效、横屏、黑白、手机截图、文字卡片、商品展示、价格标签

三、appearance 人物外观（按可见事实填写，只能从给定选项里选；画面没有人物就全部留空）：
- age 年龄判断，必选其一：{age_options}
  重要：只要不能确定是成年人（疑似未成年或无法判断），outfit、exposed_areas、camera_focus 必须为空，borderline 必须为 null。
- outfit 着装（多选）：{outfit_options}
- exposed_areas 暴露部位（多选）：{exposed_options}
- camera_focus 镜头重点（多选）：{focus_options}
- poses 动作姿态（多选）：{pose_options}
- borderline 擦边程度（仅成年人物；全部为 0~3 的整数，按平台内容审核的尺度分档，只看可见程度，不描述身体细节；不需要给总分）：
  每一件事只在一个分项里算，不要重复计分：衣服露了多少只算在胸部和整体暴露；衣服把身体轮廓勾勒出来只算在身材；镜头怎么拍只算在镜头强调；姿势和动作只算在动作展示。
  chest 胸部暴露（只看胸部实际露出多少，不管穿的是什么）：0=看不到胸部，领口正常；1=锁骨下方少量露出，乳沟很浅或看不出；2=乳沟清楚可见；3=乳沟很深，或胸部上半部/侧面大部分露出
  【身材】分两项分别打分（程序会合成一个身材分，臀部占大头），只看衣物把身体轮廓勾勒出来的程度，不看镜头和姿势：
  hip 身材·臀部：0=看不出臀部轮廓；1=下装能看出臀部轮廓；2=衣物紧贴、臀型清晰（如紧身裤、瑜伽裤、紧身短裤）；3=臀部部分露出，或下装极短、臀部下缘露出
  figure 身材·曲线（腰腹和胸部的线条，不含臀部；只看展示程度，不评价身材本身好坏）：0=宽松或普通衣着，看不出线条；1=能看出身体轮廓；2=贴身衣物明显勾勒腰腹和胸部线条；3=衣物极度贴身、身体线条完全显现，或标题话题明确强调身材
  overall 整体暴露（画面里实际露出的皮肤有多少；透视能看到的部分也算露出；只看露出多少，不管衣服叫什么）：0=很少，基本只露脸和手；1=少量，露手臂、小腿，或短袖短款；2=较多，露大腿、腰腹、背部，或低领、露背、露脐、超短、局部透视；3=大面积，躯干和腿大部分露出，如以泳装、内衣为主或布料很少
  睡衣/家居服同样按实际露出和贴身程度打分：宽松、遮盖多的睡衣 overall 0~1、figure 0~1；吊带睡裙、短款、透视或贴身的睡衣再按对应档位往上给
  camera 镜头强调（所有和拍法有关的都算在这里）：0=自然拍摄，包括正常的自拍/上半身构图；1=展示身材的构图（全身展示、摆拍）；2=局部特写胸、臀、腿等部位，或低角度仰拍、从背后拍；3=持续或反复聚焦胸部/臀部特写、跟拍，或镜头沿身体上下移动
  motion 动作展示（所有和姿势、动作有关的都算在这里）：0=自然，包括正常的瑜伽、健身、常规舞蹈动作；1=展示身材的摆姿（侧身、叉腰、收腹挺胸、转身展示背面）；2=挑逗性动作（翘臀、扭胯、俯身、甩臀、贴近镜头）；3=持续强烈的挑逗性舞蹈或动作
  enhancers 增强因素（多选）：{enhancer_options}
  violation 是否疑似违规（疑似裸露或违规走光）：true/false，只标记，不描述

四、summary：一句话（不超过 40 字）客观描述画面内容，不描写身体细节。

只输出一个 JSON 对象，不要任何解释或代码块：
{{"category": "normal|ad|text_only|screenshot", "ad_type": "selling|sponsored|traffic|local_promo|null", "confidence": 0到1的小数, "reason": "一句话中文理由", "tags": {{"subject": [], "scene": [], "content": [], "style": [], "features": []}}, "appearance": {{"age": "成年|疑似未成年|无法判断", "outfit": [], "exposed_areas": [], "camera_focus": [], "poses": [], "borderline": {{"chest": 0, "hip": 0, "figure": 0, "overall": 0, "camera": 0, "motion": 0, "enhancers": [], "violation": false}}}}, "summary": "一句话描述"}}"""


class ContentFilterError(RuntimeError):
    """检测链路中可解释的失败（抽帧失败、模型返回无法解析等）。"""


@dataclass
class PreparedMedia:
    images: List[bytes]
    image_note: str
    motion: Optional[Dict[str, Any]] = None
    # 视频作品是否带了封面；图文作品为 None
    cover_used: Optional[bool] = None
    # local=本机下载目录 | archive=网盘
    source: str = "local"


# ---- 元数据 ----------------------------------------------------------------

def extract_hashtags(aweme_detail: Dict[str, Any]) -> List[str]:
    extras = aweme_detail.get("text_extra")
    if isinstance(extras, list):
        tags = [str(item["hashtag_name"]) for item in extras if isinstance(item, dict) and item.get("hashtag_name")]
        if tags:
            return tags[:15]
    # 历史作品没存 text_extra，退回从标题里的 #话题 解析
    return re.findall(r"#([^\s#]{1,30})", str(aweme_detail.get("desc") or ""))[:15]


def extract_ad_signals(aweme_detail: Dict[str, Any]) -> Dict[str, Any]:
    """从作品详情里取与广告相关的字段；只保留有值的，写进记录保持精简。"""
    signals: Dict[str, Any] = {}
    if aweme_detail.get("is_ads") is True:
        signals["is_ads"] = True
    if aweme_detail.get("is_from_ad_auth") is True:
        signals["is_from_ad_auth"] = True
    promotions = aweme_detail.get("promotions")
    if isinstance(promotions, list) and promotions:
        signals["promotions"] = len(promotions)
    anchors = aweme_detail.get("anchors")
    if isinstance(anchors, list) and anchors:
        signals["anchors"] = [str(a.get("title") or a.get("keyword") or a.get("type") or "")[:40]
                              for a in anchors[:3] if isinstance(a, dict)]
    anchor_info = aweme_detail.get("anchor_info")
    if isinstance(anchor_info, dict) and anchor_info:
        signals["anchor_info"] = str(anchor_info.get("title") or anchor_info.get("type") or "有")[:40]
    if aweme_detail.get("commerce_config_data"):
        signals["commerce_config"] = True
    text = f"{aweme_detail.get('desc') or ''} {' '.join(extract_hashtags(aweme_detail))}".lower()
    hits = [word for word in AD_KEYWORDS if word.lower() in text]
    if hits:
        signals["keywords"] = hits[:8]
    return signals


# ---- 画面运动 ----------------------------------------------------------------

def static_packet_threshold(width: int, height: int) -> int:
    return max(MIN_STATIC_PACKET_BYTES, int(STATIC_BYTES_PER_MEGAPIXEL * width * height / 1_000_000))


def classify_motion(packets: List[tuple[int, bool]], *, width: int = 1080, height: int = 1920) -> Dict[str, Any]:
    """packets: (字节数, 是否关键帧)。按非关键帧中“几乎没变化”的比例分档。"""
    nonkey = [size for size, key in packets if not key]
    keyframes = len(packets) - len(nonkey)
    if not nonkey:
        return {"profile": "static", "static_ratio": 1.0, "keyframes": keyframes}
    threshold = static_packet_threshold(width, height)
    ratio = sum(1 for size in nonkey if size < threshold) / len(nonkey)
    if ratio >= STATIC_RATIO:
        profile = "static"
    elif ratio >= PARTLY_STATIC_RATIO:
        profile = "partly_static"
    else:
        profile = "dynamic"
    return {"profile": profile, "static_ratio": round(ratio, 3), "keyframes": keyframes,
            "avg_nonkey_bytes": int(sum(nonkey) / len(nonkey))}


async def probe_motion(video: Path) -> Dict[str, Any]:
    """只读包大小不解码；要读完整个文件，所以只用于本地文件。"""
    output = await run_media_tool((
        "ffprobe", "-v", "error", "-threads", "1", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:packet=size,flags", "-of", "json", str(video),
    ))
    try:
        data = json.loads(output)
        stream = data["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        packets = [(int(p["size"]), "K" in str(p.get("flags", ""))) for p in data.get("packets", [])]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ContentFilterError("ffprobe 输出无法解析") from exc
    if not packets:
        raise ContentFilterError("ffprobe 未读到视频包")
    return classify_motion(packets, width=width, height=height)


async def probe_duration(video: Path) -> float:
    output = await run_media_tool((
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video),
    ))
    try:
        return float(output.decode().strip())
    except ValueError as exc:
        raise ContentFilterError("ffprobe 未读到视频时长") from exc


# ---- 抽帧 ----------------------------------------------------------------

def _dedupe(frames: List[bytes]) -> List[bytes]:
    seen, unique = set(), []
    for data in frames:
        digest = hashlib.md5(data).digest()
        if data and digest not in seen:
            seen.add(digest)
            unique.append(data)
    return unique


def split_jpeg_stream(data: bytes) -> List[bytes]:
    """mjpeg image2pipe 输出是首尾相接的 JPEG；按 SOI 标记切开。"""
    starts = [match.start() for match in re.finditer(re.escape(JPEG_SOI), data)]
    return [data[start:end] for start, end in zip(starts, [*starts[1:], len(data)])]


async def extract_video_frames(video: Path, duration: float, count: int) -> List[bytes]:
    """本地文件：在均匀分布的时间点各取最近的关键帧（只解关键帧，不精确 seek），按内容去重。"""
    frames = []
    for index in range(count):
        position = duration * (index + 0.5) / count
        frames.append(await run_media_tool((
            "ffmpeg", "-v", "error", "-threads", "1", "-skip_frame", "nokey", "-noaccurate_seek",
            "-ss", f"{position:.3f}", "-i", str(video), "-frames:v", "1", "-vf", SCALE_FILTER,
            "-q:v", "5", "-f", "image2pipe", "-c:v", "mjpeg", "-",
        )))
    frames = _dedupe(frames)
    if not frames:
        raise ContentFilterError("未能从视频抽出关键帧")
    return frames


async def extract_stream_keyframes(url: str, count: int) -> List[bytes]:
    """网盘直链：一个进程顺序读，只解关键帧，最多读 STREAM_KEYFRAME_CAP 个再均匀挑。

    每次 seek 都要重新建连（网关首字节 1~3s），实测单遍读比多次 seek 快 3~4 倍。
    """
    output = await run_media_tool((
        "ffmpeg", "-v", "error", "-rw_timeout", str(STREAM_RW_TIMEOUT_US), "-threads", "1",
        "-skip_frame", "nokey", "-i", url, "-vsync", "vfr", "-vf", SCALE_FILTER,
        "-frames:v", str(STREAM_KEYFRAME_CAP), "-q:v", "5", "-f", "image2pipe", "-c:v", "mjpeg", "-",
    ))
    frames = _dedupe(split_jpeg_stream(output))
    if not frames:
        raise ContentFilterError("未能从网盘视频抽出关键帧")
    return pick_evenly(frames, count)


def shrink_image_bytes(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        scale = FRAME_SHORT_SIDE / min(image.size)
        if scale < 1:
            image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=FRAME_JPEG_QUALITY)
        return buffer.getvalue()


def shrink_image(path: Path) -> bytes:
    return shrink_image_bytes(path.read_bytes())


def pick_evenly(items: List[Any], count: int) -> List[Any]:
    if len(items) <= count:
        return list(items)
    step = len(items) / count
    return [items[int(step * index + step / 2)] for index in range(count)]


# ---- 模型输出 ----------------------------------------------------------------

def _parse_tags(raw: Any) -> Dict[str, List[str]]:
    if not isinstance(raw, dict):
        raise ContentFilterError("模型未返回 tags 对象")
    tags: Dict[str, List[str]] = {}
    for group in TAG_GROUPS:
        values = raw.get(group, [])
        if not isinstance(values, list):
            raise ContentFilterError(f"tags.{group} 不是列表")
        cleaned = []
        for value in values:
            text = str(value).strip()[:MAX_TAG_CHARS]
            if text and text not in cleaned and not (group == "style" and text in BANNED_STYLE_TAGS):
                cleaned.append(text)
        tags[group] = cleaned[:MAX_TAGS_PER_GROUP]
    return tags


def _pick_options(raw: Any, options: tuple[str, ...], field: str, dropped: List[str]) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ContentFilterError(f"appearance.{field} 不是列表")
    picked = []
    for value in raw:
        text = str(value).strip()
        if text in options:
            if text not in picked:
                picked.append(text)
        elif text:
            dropped.append(f"{field}:{text}"[:40])
    return picked


def _scale_value(raw: Dict[str, Any], field: str, maximum: int) -> int:
    value = raw.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value \
            or not 0 <= int(value) <= maximum:
        raise ContentFilterError(f"borderline.{field} 须为 0~{maximum} 的整数：{value!r}")
    return int(value)


def _parse_borderline(raw: Any, dropped: List[str]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ContentFilterError("成年人物缺少 borderline 擦边分档")
    borderline: Dict[str, Any] = {field: _scale_value(raw, field, maximum)
                                  for field, maximum in BORDERLINE_SCALES.items()}
    borderline["enhancers"] = _pick_options(raw.get("enhancers"), ENHANCER_OPTIONS, "enhancers", dropped)
    violation = raw.get("violation", False)
    if not isinstance(violation, bool):
        raise ContentFilterError(f"borderline.violation 须为布尔值：{violation!r}")
    borderline["violation"] = violation
    borderline["body_raw"], borderline["body"] = body_score(borderline["hip"], borderline["figure"])
    borderline["weighted"], borderline["score"] = borderline_total(borderline)
    return borderline


def body_score(hip: int, figure: int) -> tuple[float, int]:
    """臀部、曲线 → (身材原始分 0~3, 取整后的身材分)。四舍五入（.5 进位），便于筛选和排序。"""
    raw = round(BODY_HIP_SHARE * hip + (1 - BODY_HIP_SHARE) * figure, 2)
    return raw, int(raw + 0.5)


def borderline_total(borderline: Dict[str, Any]) -> tuple[float, int]:
    """分项 → (加权分, 总分 0~5)。胸部 3 分至少 4 分；疑似违规直接 5 分。"""
    # 身材用原始分参与加权，不用取整后的，避免 1.35 和 1.65 被算成一样
    values = {**borderline, "body": borderline.get("body_raw", borderline.get("body", 0))}
    weighted = round(sum(weight * values[name] for name, weight in BORDERLINE_WEIGHTS.items()), 2)
    score = next((total for total, threshold in BORDERLINE_TOTAL_THRESHOLDS if weighted >= threshold), 0)
    if borderline["chest"] >= 3:
        score = max(score, 4)
    if borderline["violation"]:
        score = 5
    return weighted, score


def _parse_appearance(raw: Any) -> Dict[str, Any]:
    """只收固定选项；未判定为成年时强制清空身体相关字段和擦边分档（不依赖模型自觉）。"""
    if not isinstance(raw, dict):
        raise ContentFilterError("模型未返回 appearance 对象")
    age = str(raw.get("age") or "").strip()
    if age not in AGE_OPTIONS:
        raise ContentFilterError(f"appearance.age 不在选项内：{age!r}")
    dropped: List[str] = []
    appearance: Dict[str, Any] = {
        "age": age,
        "outfit": _pick_options(raw.get("outfit"), OUTFIT_OPTIONS, "outfit", dropped),
        "exposed_areas": _pick_options(raw.get("exposed_areas"), EXPOSED_AREA_OPTIONS, "exposed_areas", dropped),
        "camera_focus": _pick_options(raw.get("camera_focus"), CAMERA_FOCUS_OPTIONS, "camera_focus", dropped),
        "poses": _pick_options(raw.get("poses"), POSE_OPTIONS, "poses", dropped),
        "borderline": None,
    }
    if age == AGE_ADULT:
        appearance["borderline"] = _parse_borderline(raw.get("borderline"), dropped)
    else:
        for field in BODY_LIST_FIELDS:
            appearance[field] = []
    if dropped:
        appearance["dropped"] = dropped[:10]
    return appearance


def parse_verdict(text: str) -> Dict[str, Any]:
    """解析模型输出；字段不合法直接报错，不做猜测性修补。"""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ContentFilterError(f"模型未返回 JSON：{(text or '')[:120]}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ContentFilterError(f"模型 JSON 无法解析：{match.group(0)[:120]}") from exc
    category = str(data.get("category") or "").strip()
    if category not in CATEGORIES:
        raise ContentFilterError(f"模型返回未知分类：{category!r}")
    ad_type = data.get("ad_type")
    ad_type = str(ad_type).strip() if ad_type not in (None, "", "null") else None
    if category == "ad" and ad_type not in AD_TYPES:
        raise ContentFilterError(f"广告分类缺少有效 ad_type：{ad_type!r}")
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ContentFilterError("模型 confidence 不是数字") from exc
    if not 0 <= confidence <= 1:
        raise ContentFilterError(f"模型 confidence 超出范围：{confidence}")
    return {
        "category": category,
        "ad_type": ad_type if category == "ad" else None,
        "confidence": round(confidence, 3),
        "reason": str(data.get("reason") or "").strip()[:200],
        "tags": _parse_tags(data.get("tags")),
        "appearance": _parse_appearance(data.get("appearance")),
        "summary": str(data.get("summary") or "").strip()[:80],
    }


# ---- 本地媒体 ----------------------------------------------------------------

def first_video_file(record: Dict[str, Any]) -> str:
    if record.get("video_file"):
        return str(record["video_file"])
    for name in record.get("files", []):
        if str(name).endswith(".mp4") and not str(name).endswith(".fast.mp4"):
            return str(name)
    return ""


def image_files(record: Dict[str, Any]) -> List[str]:
    cover = str(record.get("cover_file") or "")
    return [str(name) for name in record.get("files", []) if str(name) != cover]


class LocalMediaLoader:
    """新作品：文件刚下载完，一定在本机下载目录里。"""

    def __init__(self, download_root: Path):
        self._download_root = download_root.resolve()

    def resolve(self, relative: Any) -> Path:
        name = str(relative or "").strip()
        if not name:
            raise ContentFilterError("记录缺少媒体文件路径")
        path = (self._download_root / name).resolve()
        if not path.is_relative_to(self._download_root):
            raise ContentFilterError(f"媒体文件不在下载目录内：{name}")
        if not path.is_file():
            raise ContentFilterError(f"媒体文件不存在：{name}")
        return path

    def optional(self, relative: Any) -> Optional[Path]:
        if not str(relative or "").strip():
            return None
        try:
            return self.resolve(relative)
        except ContentFilterError:
            return None

    async def prepare(self, user: Dict[str, Any], record: Dict[str, Any], max_frames: int) -> PreparedMedia:
        media_type = record.get("media_type")
        if media_type == "video":
            video = self.resolve(first_video_file(record))
            motion = await probe_motion(video)
            duration = safe_seconds(record.get("video_duration_ms")) or await probe_duration(video)
            images = await extract_video_frames(video, duration, max_frames)
            # 封面只是辅助线索：缺失时不用，但如实标出 cover_used
            cover = self.optional(record.get("cover_file"))
            if cover is not None:
                images.insert(0, await asyncio.to_thread(shrink_image, cover))
            return PreparedMedia(images, video_note(cover is not None), motion=motion, cover_used=cover is not None)
        if media_type == "image":
            pictures = [self.resolve(name) for name in image_files(record)]
            if not pictures:
                raise ContentFilterError("图文作品没有图片文件")
            chosen = pick_evenly(pictures, max_frames)
            images = [await asyncio.to_thread(shrink_image, path) for path in chosen]
            return PreparedMedia(images, image_post_note(len(pictures), len(images)))
        raise ContentFilterError(f"不支持的作品类型：{media_type!r}")


def video_note(with_cover: bool) -> str:
    if with_cover:
        return "第 1 张是封面（作者可能自定义，仅作参考），其余是视频中截取的关键帧。"
    return "以下是视频中截取的关键帧。"


def image_post_note(total: int, shown: int) -> str:
    return f"这是图文作品，共 {total} 张图，下面是其中 {shown} 张。"


# ---- 主流程 ----------------------------------------------------------------

class ContentFilter:
    def __init__(self, settings, download_root: Path, *, transport: Optional[httpx.AsyncBaseTransport] = None,
                 concurrency: int = 1):
        self._settings = settings
        self._local = LocalMediaLoader(download_root)
        self._transport = transport
        # 新作品默认逐个排队，避免多个用户同步时并发占 CPU / 刷模型额度；补打任务可调大
        self._slots = asyncio.Semaphore(concurrency)

    @property
    def settings(self):
        return self._settings

    def applies_to(self, record: Dict[str, Any]) -> bool:
        if not self._settings.enabled:
            return False
        return record.get("source") != "history" or self._settings.include_history

    async def __call__(self, user: Dict[str, Any], aweme_detail: Dict[str, Any], record: Dict[str, Any]) -> None:
        """下载流程入口：本地文件，结论写回 record。"""
        if not self.applies_to(record):
            return
        record["content_filter"] = await self.evaluate(user, aweme_detail, record, self._local)

    async def evaluate(self, user: Dict[str, Any], aweme_detail: Dict[str, Any], record: Dict[str, Any],
                       loader) -> Dict[str, Any]:
        """返回结论字典，从不抛出业务异常（取消除外）。"""
        started = time.monotonic()
        result: Dict[str, Any] = {"version": RESULT_VERSION, "mode": self._settings.mode,
                                  "model": self._settings.model}
        try:
            async with self._slots:
                started = time.monotonic()  # 只计处理耗时，不含排队
                result.update(await self._evaluate(user, aweme_detail, record, loader))
            result["status"] = "ok"
            rejected = (result["category"] in self._settings.reject_categories
                        and result["confidence"] >= self._settings.min_confidence)
            result["verdict"] = "reject" if rejected else "keep"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 失败要落到记录里，不能丢掉整条下载记录
            result["status"] = "error"
            result["error"] = sanitize_public_error(exc)
            logger.warning("内容筛选失败 aweme=%s: %s", record.get("aweme_id"), result["error"])
        result["checked_at"] = cn_now()
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result

    async def _evaluate(self, user, aweme_detail, record, loader) -> Dict[str, Any]:
        if not self._settings.api_key:
            raise ContentFilterError("未配置 DYMON_FILTER_API_KEY")
        signals = extract_ad_signals(aweme_detail)
        media: PreparedMedia = await loader.prepare(user, record, self._settings.max_frames)
        if media.motion:
            motion_text = json.dumps(media.motion, ensure_ascii=False)
        elif record.get("media_type") == "image":
            motion_text = "（图文作品，不适用）"
        else:
            motion_text = "（未测）"
        prompt = PROMPT.format(
            image_note=media.image_note,
            desc=str(aweme_detail.get("desc") or "").strip()[:300] or "（无）",
            hashtags="、".join(extract_hashtags(aweme_detail)) or "（无）",
            signals=json.dumps(signals, ensure_ascii=False) if signals else "（无）",
            motion=motion_text,
            age_options="、".join(AGE_OPTIONS),
            outfit_options="、".join(OUTFIT_OPTIONS),
            exposed_options="、".join(EXPOSED_AREA_OPTIONS),
            focus_options="、".join(CAMERA_FOCUS_OPTIONS),
            pose_options="、".join(POSE_OPTIONS),
            enhancer_options="、".join(ENHANCER_OPTIONS),
        )
        verdict = parse_verdict(await self._ask_model(prompt, media.images))
        result = {**verdict, "frames": len(media.images), "signals": signals, "motion": media.motion,
                  "media_source": media.source}
        if media.cover_used is not None:
            result["cover_used"] = media.cover_used
        return result

    async def _ask_model(self, prompt: str, images: List[bytes]) -> str:
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for data in images:
            encoded = base64.b64encode(data).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        body = {"model": self._settings.model, "temperature": 0, "max_tokens": MAX_OUTPUT_TOKENS,
                "messages": [{"role": "user", "content": content}]}
        # 用 httpx 自身超时，不套 asyncio.wait_for（取消会泄漏连接池，见 openlist 连接池事故）
        async with httpx.AsyncClient(timeout=self._settings.timeout_seconds, transport=self._transport,
                                     trust_env=False) as client:
            response = await client.post(
                f"{self._settings.api_base.rstrip('/')}/chat/completions", json=body,
                headers={"Authorization": f"Bearer {self._settings.api_key}"},
            )
        if response.status_code != 200:
            raise ContentFilterError(f"模型接口 HTTP {response.status_code}：{response.text[:160]}")
        try:
            return response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ContentFilterError(f"模型接口返回格式异常：{response.text[:160]}") from exc


def safe_seconds(duration_ms: Any) -> float:
    try:
        return max(0.0, float(duration_ms) / 1000)
    except (TypeError, ValueError):
        return 0.0
