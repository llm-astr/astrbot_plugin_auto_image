# -*- coding: utf-8 -*-
"""生图参数关键词模糊匹配：模型 / 画面比例 / 清晰度。

支持从整段自然语言中抓取关键词，例如：
    「用 pro 画一张横版 4K 的图」→ model=nano-banana-pro, ratio=16:9, size=4K
    「gpt 竖屏 高清」           → model=gpt-image-2,   ratio=9:16,  size=2K
"""

from __future__ import annotations

import difflib
import re

# ---------------------------------------------------------------- 模型

# 模型名 -> 别名 / 常见叫法（匹配前统一小写并去除空格、横线等分隔符）
MODEL_ALIASES: dict[str, list[str]] = {
    "nano-banana-fast": ["fast", "快速", "极速", "nbfast", "香蕉fast", "快速香蕉"],
    "nano-banana": ["banana", "香蕉", "标准", "nb", "基础"],
    "nano-banana-pro": ["pro", "专业", "nbpro", "香蕉pro", "专业版"],
    "nano-banana-pro-vt": ["vt", "provt"],
    "nano-banana-pro-cl": ["cl", "procl"],
    "nano-banana-pro-vip": ["vip", "provip", "会员"],
    "nano-banana-pro-4k-vip": ["4kvip", "pro4kvip", "4k会员"],
    "nano-banana-2": ["banana2", "香蕉2", "香蕉二", "二代", "nb2", "2代"],
    "nano-banana-2-cl": ["banana2cl", "nb2cl"],
    "nano-banana-2-2k-cl": ["2kcl", "nb2kcl"],
    "nano-banana-2-4k-cl": ["4kcl", "nb4kcl"],
    "gpt-image-2": ["gpt", "gptimage", "gptimage2", "gpt2", "openai"],
    "gpt-image-2-vip": ["gptvip", "gptimage2vip", "gpt会员"],
}

ALLOWED_MODELS: list[str] = list(MODEL_ALIASES)

# ---------------------------------------------------------------- 比例

ALLOWED_RATIOS: list[str] = [
    "auto", "1:1", "3:2", "2:3", "3:4", "4:3",
    "4:5", "5:4", "9:16", "16:9", "21:9",
]

# 比例 -> 关键词（单字「横/竖/方」单独兜底，见 match_ratio）
RATIO_KEYWORDS: dict[str, list[str]] = {
    "16:9": ["横版", "横向", "横图", "横屏", "宽屏", "横幅", "横着", "壁纸", "landscape"],
    "9:16": ["竖版", "竖向", "竖图", "竖屏", "手机壁纸", "竖着", "全屏手机", "portrait"],
    "1:1": ["方形", "方图", "正方", "头像", "square"],
    "21:9": ["电影", "超宽", "带鱼屏", "宽幅", "宽银幕"],
    "3:4": ["小红书"],
    "4:3": ["经典比例", "传统比例"],
    "3:2": ["相机比例", "单反"],
    "2:3": ["证件照"],
    "4:5": ["ins风", "instagram"],
}

# ---------------------------------------------------------------- 清晰度

ALLOWED_SIZES: list[str] = ["1K", "2K", "4K"]

_SIZE_KEYWORDS_4K = ["超高清", "超清", "极致清晰", "最高清", "顶配音质", "顶配", "最高画质"]
_SIZE_KEYWORDS_2K = ["高清", "高画质", "清晰版"]
_SIZE_KEYWORDS_1K = ["标清", "流畅", "省流", "低清", "普通清晰度"]

_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}

# ---------------------------------------------------------------- 工具


def _norm(s: str) -> str:
    """小写并去除空格、横线、下划线等分隔符"""
    return re.sub(r"[\s_\-/\\·]+", "", (s or "").lower())


def _alias_items() -> list[tuple[str, str]]:
    """(归一化别名, 模型名) 列表"""
    items: list[tuple[str, str]] = []
    for model, aliases in MODEL_ALIASES.items():
        for a in aliases:
            items.append((_norm(a), model))
    return items


def match_model(text: str | None) -> str | None:
    """从文本中模糊匹配模型名，匹配不到返回 None"""
    norm = _norm(text or "")
    if not norm:
        return None

    # 1) 完整命中模型名（忽略大小写与分隔符）
    for model in ALLOWED_MODELS:
        if norm == _norm(model):
            return model

    # 2) 文本中包含完整模型名（长的优先，如 pro-4k-vip 先于 pro）
    for model in sorted(ALLOWED_MODELS, key=lambda m: -len(_norm(m))):
        if _norm(model) in norm:
            return model

    # 3) 文本中包含别名（长的优先，避免 "pro" 抢在 "pro4kvip" 前命中；
    #    短于 3 字节的别名如 cl/vt/nb 只在整段等于它时命中，防止误伤）
    items = _alias_items()
    for alias, model in sorted(items, key=lambda x: -len(x[0])):
        if len(alias.encode("utf-8")) >= 3:
            if alias and alias in norm:
                return model
        elif norm == alias:
            return model

    # 4) difflib 模糊兜底（处理拼写错误，如 bannana / nanobanana）
    alias_map = dict(items)
    choices = list(alias_map.keys()) + [_norm(m) for m in ALLOWED_MODELS]
    hit = difflib.get_close_matches(norm, choices, n=1, cutoff=0.6)
    if hit:
        if hit[0] in alias_map:
            return alias_map[hit[0]]
        for model in ALLOWED_MODELS:
            if _norm(model) == hit[0]:
                return model
    return None


_RATIO_RE = re.compile(r"(\d{1,2})\s*[:：x×]\s*(\d{1,2})")


def match_ratio(text: str | None) -> str | None:
    """从文本中模糊匹配画面比例，匹配不到返回 None"""
    if not text:
        return None

    # 1) 显式 W:H / W x H 写法
    m = _RATIO_RE.search(text)
    if m:
        cand = f"{int(m.group(1))}:{int(m.group(2))}"
        if cand in ALLOWED_RATIOS:
            return cand

    # 2) 自动 / auto
    norm = _norm(text)
    if norm == "auto" or "自动比例" in norm or "自适应" in norm:
        return "auto"

    # 3) 多字关键词
    for ratio, kws in RATIO_KEYWORDS.items():
        for kw in kws:
            if _norm(kw) in norm:
                return ratio

    # 4) 单字兜底：横 / 竖 / 方
    if "横" in text:
        return "16:9"
    if "竖" in text:
        return "9:16"
    if "方" in text:
        return "1:1"
    return None


_SIZE_RE = re.compile(r"([一二两三四1234])\s*[kKＫｋ]")


def match_size(text: str | None) -> str | None:
    """从文本中模糊匹配清晰度（1K/2K/4K），匹配不到返回 None"""
    if not text:
        return None

    # 1) 显式写法：4K / 2k / 四K / 二ｋ
    m = _SIZE_RE.search(text)
    if m:
        n = _CN_NUM.get(m.group(1))
        if n in (1, 2, 4):
            return f"{n}K"

    # 2) 关键词（先 4K 后 2K，避免「超高清」被「高清」截胡）
    for kw in _SIZE_KEYWORDS_4K:
        if kw in text:
            return "4K"
    for kw in _SIZE_KEYWORDS_2K:
        if kw in text:
            return "2K"
    for kw in _SIZE_KEYWORDS_1K:
        if kw in text:
            return "1K"
    return None


def parse_all(text: str | None) -> dict:
    """一次性解析三个参数，返回 {'model':…, 'aspect_ratio':…, 'image_size':…}（无命中则无对应键）"""
    out: dict = {}
    model = match_model(text)
    ratio = match_ratio(text)
    size = match_size(text)
    if model:
        out["model"] = model
    if ratio:
        out["aspect_ratio"] = ratio
    if size:
        out["image_size"] = size
    return out


def strip_param_words(text: str) -> str:
    """从提示词中剔除命中参数的关键词（用于指令模式清理 prompt）"""
    t = text or ""
    # 显式比例与清晰度写法
    t = _RATIO_RE.sub(" ", t)
    t = _SIZE_RE.sub(" ", t)
    # 别名 / 关键词（仅剔除 >=3 字节的词，避免误删正文单字）
    words: set[str] = set()
    for aliases in MODEL_ALIASES.values():
        words.update(aliases)
    for kws in RATIO_KEYWORDS.values():
        words.update(kws)
    words.update(_SIZE_KEYWORDS_4K + _SIZE_KEYWORDS_2K + _SIZE_KEYWORDS_1K)
    for w in sorted(words, key=len, reverse=True):
        if len(w.encode("utf-8")) >= 3:
            t = re.sub(re.escape(w), " ", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip()
