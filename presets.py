# -*- coding: utf-8 -*-
"""预设系统：识别特定消息模式，自动补充参考图来源 / 提示词。

规划：每个预设一个类，注册进 PRESETS 列表即生效。
- id / name / config_key：预设标识与开关（config_key 对应插件配置项，配置缺省视为开启）
- match(event) -> PresetHit | None：命中时返回要补充的参考图来源与提示词

后续新增预设：在下方新建 Preset 子类并追加到 PRESETS 即可，main.py 无需改动；
需要外部依赖（如图库存储）的预设，由 main.py 实例化后调用 register_preset 注册。
"""

import re

try:
    from astrbot.api import logger
except Exception:  # 独立测试环境
    import logging

    logger = logging.getLogger("auto-image.presets")


class PresetHit:
    """预设命中结果。

    refs 为参考图来源列表 [(url, file), ...]；labels 与 refs 一一对应，
    是对每张参考图的文字说明（如「预设提示词「猫娘」对应的图库图片」），
    main.py 会把所有命中预设的 labels 按全局顺序合成「第 N 张是…」的序数
    提示附加到提示词末尾，帮助生图模型理解每张参考图的含义。
    record 为 False 时，命中补充的参考图不会写入会话最近图片缓存
    （图库类预设图片长期有效，无需再进缓存，避免污染「我发的图」语义取图）。
    """

    def __init__(
        self,
        preset_id: str,
        refs: list | None = None,
        prompt_hint: str = "",
        labels: list | None = None,
        record: bool = True,
    ):
        self.preset_id = preset_id
        self.refs = list(refs or [])
        self.labels = list(labels or [])
        self.prompt_hint = prompt_hint
        self.record = record


class Preset:
    """预设基类"""

    id = "base"
    name = "基础预设"
    config_key = ""  # 插件配置项名；空串 = 总是开启

    def match(self, event, extra_text: str = "") -> PresetHit | None:
        """event 为消息事件；extra_text 为额外匹配文本（如 LLM 写好的生成提示词）"""
        raise NotImplementedError


# ---------------- 通用工具 ----------------


def _self_id(event) -> str:
    try:
        return str(event.get_self_id())
    except Exception:
        return ""


def _at_targets(event) -> list[str]:
    """消息中所有 @ 目标的 QQ 号（按出现顺序，排除 @全体成员 与 bot 自己）。

    At 组件在不同平台/版本字段可能不同，这里按 ``qq`` 属性鸭子类型识别。
    """
    self_qq = _self_id(event)
    out: list[str] = []
    try:
        messages = event.get_messages()
    except Exception:
        return out
    for comp in messages or []:
        if type(comp).__name__ != "At" and not hasattr(comp, "qq"):
            continue
        qq = str(getattr(comp, "qq", "") or "").strip()
        if not qq or qq.lower() == "all" or (self_qq and qq == self_qq):
            continue
        if qq not in out:
            out.append(qq)
    return out


# ---------------- 预设①：@某人 +「用他/她/它 画/做」→ 头像参考图 ----------------


class AvatarRefPreset(Preset):
    """消息包含「用他画/用她画/用它画/用他做/用她做/用它做」且 @ 了某人时，
    提取第一个被@者（非 bot 自己）的 QQ 头像作为默认参考图。
    """

    id = "avatar_ref"
    name = "@某人头像参考图"
    config_key = "preset_avatar_ref"
    TRIGGER = re.compile(r"用\s*[他她它]\s*[画做]")
    AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"

    def match(self, event, extra_text: str = "") -> PresetHit | None:
        text = getattr(event, "message_str", "") or ""
        if not self.TRIGGER.search(text):
            return None
        targets = _at_targets(event)
        if not targets:
            return None
        qq = targets[0]
        return PresetHit(
            self.id,
            refs=[(self.AVATAR_URL.format(qq=qq), "")],
            labels=[f"QQ 号 {qq} 的头像（消息里「用他/她/它」指的就是这个人）"],
        )


# ---------------- 预设②：提示词图库 → 关键词命中自动带图 ----------------

# 人物外观特征词（子串匹配，CJK）：命中预设②时，含这些词的提示词子句会被拦截
_FEATURE_TERMS_CJK = [
    # 发色 / 发型
    "金发", "银发", "白发", "黑发", "红发", "蓝发", "粉发", "紫发", "绿发", "棕发",
    "灰发", "橙发", "黄发", "渐变发", "长发", "短发", "双马尾", "单马尾", "高马尾",
    "低马尾", "马尾辫", "卷发", "直发", "波浪卷", "刘海", "发色", "发型", "呆毛",
    "麻花辫", "丸子头", "波波头", "披肩发", "鬓发",
    # 眼睛
    "眼睛", "眼眸", "眸子", "瞳孔", "瞳色", "大眼", "红瞳", "蓝瞳", "金瞳", "绿瞳",
    "紫瞳", "粉瞳", "灰瞳", "异色瞳", "琥珀瞳",
    # 耳 / 尾 / 角 / 翼 / 其他身体特征
    "猫耳", "兔耳", "狐耳", "犬耳", "狼耳", "兽耳", "耳朵", "尖耳", "垂耳", "尾巴",
    "猫尾", "狐尾", "兔尾", "龙尾", "龙角", "犄角", "触角", "翅膀", "羽翼", "蝠翼",
    "胡须", "猫须", "兽爪", "肉垫",
    # 面部 / 皮肤
    "脸颊", "红晕", "脸红", "肤色", "皮肤", "面容", "五官", "鼻梁", "鼻子", "嘴唇",
    "睫毛", "眉毛", "雀斑", "酒窝", "虎牙", "泪痣",
    # 服饰
    "连衣裙", "女仆装", "女仆风", "短裙", "长裙", "百褶裙", "制服", "校服", "水手服",
    "和服", "浴衣", "泳装", "比基尼", "婚纱", "西装", "礼服", "卫衣", "夹克", "旗袍",
    "洛丽塔", "发饰", "发夹", "蝴蝶结", "丝带", "丝袜", "长袜", "过膝袜", "短袜",
    "皮鞋", "靴子", "手套", "围巾", "眼镜", "墨镜", "帽子", "头饰", "项链", "耳环",
    "耳坠", "围裙", "吊带裙", "衬衫", "外套", "风衣", "披风", "斗篷", "腰带", "发带",
    # 体型
    "身材", "体型", "身高", "娇小", "高挑", "丰满", "纤瘦", "苗条", "长腿", "锁骨",
]

# 人物外观特征词（英文 tag，词边界匹配避免误伤，如 chair 不含 hair）
_FEATURE_TERMS_EN = re.compile(
    r"\b("
    r"blonde|blond|silver\s+hair|white\s+hair|black\s+hair|brown\s+hair|red\s+hair|"
    r"blue\s+hair|pink\s+hair|purple\s+hair|green\s+hair|gr[ae]y\s+hair|orange\s+hair|"
    r"gradient\s+hair|long\s+hair|short\s+hair|medium\s+hair|twintails|twin\s+tails|"
    r"ponytail|curly\s+hair|wavy\s+hair|straight\s+hair|bangs|ahoge|braids?|hair\s+bun|"
    r"bob\s+cut|hair\s+ornament|hairclip|hair\s+ribbon|hair\s+bow|hairband|"
    r"\w+\s+eyes|heterochromia|"
    r"cat\s+ears?|animal\s+ears|bunny\s+ears|rabbit\s+ears|fox\s+ears|dog\s+ears|"
    r"wolf\s+ears|kemonomimi|ears|tail|horns?|wings|whiskers|fangs?|freckles|blush|"
    r"dress|maid(\s+outfit)?|skirt|miniskirt|pleated\s+skirt|uniform|kimono|yukata|"
    r"swimsuit|bikini|wedding\s+dress|suit|hoodie|jacket|lolita|ribbon|stockings|"
    r"thighhighs|socks|boots|gloves|scarf|glasses|hat|necklace|earrings|apron|"
    r"shirt|coat|cape|cloak|belt"
    r")\b",
    re.IGNORECASE,
)

# 改动意图词：含这些词的子句是「修改要求」而非「静态描述」，一律保留
_CHANGE_VERBS_CJK = [
    "改成", "改为", "换成", "变换", "变成", "变为", "替换", "更换", "穿上", "戴上",
    "换上", "加上", "添加", "去掉", "去除", "移除", "删掉", "删除", "不要", "别再",
    "取消", "保留",
]
_CHANGE_VERBS_EN = re.compile(
    r"\b(change|replace|swap|turn\s+into|add|remove|without|wear|wearing|keep)\b",
    re.IGNORECASE,
)

_CLAUSE_RE = re.compile(r"[^，。；！？,;!?:：\n]+[，。；！？,;!?:：\n]*")


def strip_character_features(prompt: str) -> tuple[str, list[str]]:
    """拦截提示词中的人物外观特征描述：按子句拆分，命中特征词且不含改动
    意图词的子句被移除。返回 (清理后的提示词, 被移除的子句列表)。

    防护：若拦截后内容过短（几乎被清空），回退为原提示词。
    """
    if not prompt or not prompt.strip():
        return prompt, []
    kept: list[str] = []
    dropped: list[str] = []
    for clause in _CLAUSE_RE.findall(prompt):
        body = clause.strip("，。；！？,;!?:：\n ")
        if not body:
            continue
        has_feature = any(t in clause for t in _FEATURE_TERMS_CJK) or bool(
            _FEATURE_TERMS_EN.search(clause)
        )
        has_change = any(v in clause for v in _CHANGE_VERBS_CJK) or bool(
            _CHANGE_VERBS_EN.search(clause)
        )
        if has_feature and not has_change:
            dropped.append(body)
        else:
            kept.append(clause)
    cleaned = "".join(kept).strip()
    core = cleaned.strip("，。；！？,;!?:：\n ")
    if len(core) < 4:  # 几乎清空 → 回退原文，交给「以图为准」提示兜底
        return prompt, []
    return cleaned, dropped


class GalleryPreset(Preset):
    """生图 / 改图要求文本中包含图库条目的「预设提示词」时，
    自动把对应图库图片作为参考图附加（按图库添加顺序，即全局序数依据），
    并通过 labels 在提示词中强调「第几张是哪个预设提示词对应的图片」。

    图库图片来自插件数据目录，长期有效，因此 record=False：不写入会话
    最近图片缓存，避免污染「我发的图」等语义取图。
    """

    id = "prompt_gallery"
    name = "提示词图库"
    config_key = "preset_prompt_gallery"
    MAX_MATCH = 6  # 单次最多附加的图库参考图数量

    def __init__(self, gallery):
        self.gallery = gallery

    def match(self, event, extra_text: str = "") -> PresetHit | None:
        # 同时匹配用户消息原文与（生图/改图入口传入的）实际生成提示词：
        # 用户措辞与预设提示词有出入时，LLM 扩写的提示词里往往仍包含该词
        text = (getattr(event, "message_str", "") or "") + " " + (extra_text or "")
        if not text.strip():
            return None
        items = self.gallery.match(text)[: self.MAX_MATCH]
        if not items:
            # 仅在生成入口（带 extra_text）且图库非空时提示一次，便于排查未命中
            total = self.gallery.list_items()
            if extra_text and total:
                logger.info(
                    f"[auto-image] 图库共 {len(total)} 张（{self.gallery.brief()}），"
                    "但本次生图/改图未命中任何预设提示词。"
                    "匹配为子串包含：你发的话或生成提示词里需要完整出现某个预设提示词"
                )
            return None
        refs: list = []
        labels: list[str] = []
        for it in items:
            kw = it["matched_keyword"]
            refs.append(("", str(self.gallery.image_path(it))))
            labels.append(
                f"预设提示词「{kw}」对应的参考图，「{kw}」的人物形象与外观特征直接以此图为准"
            )
        return PresetHit(self.id, refs=refs, labels=labels, record=False)


# ---------------- 注册表 ----------------

PRESETS: list[Preset] = [
    AvatarRefPreset(),
]


def register_preset(preset: Preset) -> None:
    """注册需要外部依赖的预设（按 id 去重替换，热重载重复注册安全）。

    如 GalleryPreset 需要图库存储实例，由 main.py 在插件初始化时构建并注册：
        register_preset(GalleryPreset(self.gallery))
    """
    for i, p in enumerate(PRESETS):
        if p.id == preset.id:
            PRESETS[i] = preset
            return
    PRESETS.append(preset)


def match_presets(event, config=None, extra_text: str = "") -> list[PresetHit]:
    """依次匹配所有已启用的预设，返回命中结果列表（单个预设异常不影响其他）。

    extra_text：额外的匹配文本（生图/改图入口传入的实际生成提示词），
    供需要匹配文本的预设（如提示词图库）使用。
    """
    hits: list[PresetHit] = []
    for p in PRESETS:
        if p.config_key and config is not None:
            try:
                if not config.get(p.config_key, True):
                    continue
            except Exception:
                pass
        try:
            hit = p.match(event, extra_text)
        except Exception:
            hit = None
        if hit is not None:
            hits.append(hit)
    return hits
