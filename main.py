# -*- coding: utf-8 -*-
"""
AstrBot 插件：LLM 自主生图 / 改图（Grsai API）

- 注册 LLM 函数工具：bot 在对话中自主决定文生图 / 图生图（改图），无需指令
- 生图参数（模型 / 比例 / 1K-4K 清晰度）支持关键词模糊匹配，如「pro」「横版」「4K」
- 开始生成提示可切换：插件固定提示语（默认）/ 由 bot 以正常回话方式表达
- 可配置识别到生图意图后的等待时间，便于接收用户随后发送的参考图，防止遗漏
- 保留 /生图 /改图 /生图模型 指令，用于精确控制
"""

import asyncio
import sys
import uuid
import time
from collections import OrderedDict
from pathlib import Path

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.event import MessageChain
except ImportError:  # 兼容旧版本 AstrBot
    from astrbot.core.message.message_event_result import MessageChain

if __package__:
    from .client import (
        IMAGE_SIZE_MODELS,
        MAX_REF_IMAGES,
        GrsaiAPIError,
        GrsaiClient,
        download_image,
        to_data_url,
    )
    from .fuzzy import ALLOWED_MODELS, match_model, match_ratio, match_size, strip_param_words
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from client import (
        IMAGE_SIZE_MODELS,
        MAX_REF_IMAGES,
        GrsaiAPIError,
        GrsaiClient,
        download_image,
        to_data_url,
    )
    from fuzzy import ALLOWED_MODELS, match_model, match_ratio, match_size, strip_param_words

# 会话图片缓存：改图工具自动取最近图片
RECENT_TTL = 600      # 缓存 10 分钟
RECENT_MAX = 8        # 每个会话最多记 8 张
EDIT_REF_COUNT = 4    # 改图时最多携带最近 4 张

NO_IMAGE_MSG = (
    "当前会话中没有找到可供修改的图片，"
    "请提示用户先发送一张图片、或回复引用一张图片后重试。"
)


class _RecentImages:
    """按会话缓存用户最近发送的图片（时间有限、数量有限）"""

    def __init__(self, ttl: int, max_per_session: int):
        self.ttl = ttl
        self.max = max_per_session
        self._data: OrderedDict[str, list[tuple[float, str]]] = OrderedDict()

    def add(self, session: str, src: str) -> None:
        now = time.monotonic()
        lst = self._data.setdefault(session, [])
        lst.append((now, src))
        self._data[session] = [(t, s) for t, s in lst if now - t <= self.ttl][-self.max:]
        self._data.move_to_end(session)
        if len(self._data) > 200:
            self._data.popitem(last=False)

    def latest(self, session: str, n: int = 1) -> list[str]:
        now = time.monotonic()
        lst = [
            (t, s)
            for t, s in self._data.get(session, [])
            if now - t <= self.ttl
        ]
        return [s for _, s in lst[-n:]]


@register(
    "astrbot_plugin_auto_image",
    "Kimi",
    "LLM 自主生图/改图：bot 根据对话自主调用文生图/图生图，参数支持关键词模糊匹配（Grsai API）",
    "1.1.0",
    "https://github.com/llm-astr/astrbot_plugin_auto_image",
)
class AutoImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.temp_dir = Path(__file__).parent / "temp"
        self.temp_dir.mkdir(exist_ok=True)
        self._recent = _RecentImages(RECENT_TTL, RECENT_MAX)

    # ---------------- 工具方法 ----------------

    def _client(self) -> GrsaiClient:
        api_key = self.config.get("api_key", "")
        if not api_key:
            raise GrsaiAPIError(
                "尚未配置 API Key，请在 AstrBot 管理面板的插件配置中填写 Grsai API Key"
            )
        return GrsaiClient(api_key, self.config.get("api_node", "cn"))

    def _resolve_params(self, *texts: str) -> tuple[str, str, str | None]:
        """按优先级从若干文本中模糊匹配参数，回退到插件默认值"""
        joined = " ".join(t for t in texts if t)
        model = match_model(joined) or self.config.get("model", "nano-banana-fast")
        ratio = match_ratio(joined) or self.config.get("aspect_ratio", "auto")
        size = match_size(joined) or self.config.get("image_size", "1K")
        if model not in IMAGE_SIZE_MODELS:
            size = None  # fast / 基础版 / gpt-image 系列不支持清晰度参数
        return model, ratio, size

    def _wait_seconds(self) -> int:
        """识别到生图意图后的自定义等待时间（秒），仅 LLM 工具模式生效"""
        try:
            return max(0, min(60, int(self.config.get("wait_before_generate", 0))))
        except (TypeError, ValueError):
            return 0

    def _collect_refs(self, event: AstrMessageEvent) -> list[str]:
        """收集参考图：当前消息 / 回复引用 优先，其次会话最近图片"""
        refs = self._extract_image_sources(event)
        if not refs:
            refs = self._recent.latest(event.unified_msg_origin, EDIT_REF_COUNT)
        return refs

    @staticmethod
    def _extract_image_sources(event: AstrMessageEvent) -> list[str]:
        """从当前消息 / 被回复的消息中提取参考图来源（URL 或本地路径）"""
        sources: list[str] = []

        def _pick(comp) -> None:
            src = comp.url or comp.file
            if src:
                sources.append(src)

        for comp in event.get_messages():
            if isinstance(comp, Image):
                _pick(comp)
            elif isinstance(comp, Reply):
                for sub in comp.chain or []:
                    if isinstance(sub, Image):
                        _pick(sub)
        return sources[:MAX_REF_IMAGES]

    @staticmethod
    def _parse_flags(text: str) -> tuple[str, dict]:
        """解析可选精确参数：-m 模型 -ar 比例 -s 清晰度，返回 (提示词, 参数)"""
        opts: dict = {}
        tokens = text.split()
        prompt_tokens: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("-m", "--model") and i + 1 < len(tokens):
                opts["model"] = tokens[i + 1]
                i += 2
            elif tok in ("-ar", "--ratio") and i + 1 < len(tokens):
                opts["aspect_ratio"] = tokens[i + 1]
                i += 2
            elif tok in ("-s", "--size") and i + 1 < len(tokens):
                opts["image_size"] = tokens[i + 1]
                i += 2
            else:
                prompt_tokens.append(tok)
                i += 1
        return " ".join(prompt_tokens).strip(), opts

    async def _send(self, event: AstrMessageEvent, comps: list) -> None:
        """主动发送一条消息（工具内部无法 yield，统一走 event.send）"""
        await event.send(MessageChain(chain=comps))

    async def _send_progress(
        self,
        event: AstrMessageEvent,
        model: str,
        ratio: str,
        size: str | None,
        ref_count: int,
        wait: int,
    ) -> None:
        """发送固定格式的「开始生成」提示（progress_msg_mode=fixed 时）"""
        tip = f"🎨 已收到生图请求（{model} / {ratio}" + (f" / {size}" if size else "") + "）"
        if wait > 0:
            tip += f"，等待 {wait} 秒接收参考图后提交……"
        else:
            tip += (f"，参考图 {ref_count} 张" if ref_count else "") + "，提交生成中请稍候……"
        try:
            await self._send(event, [Plain(tip)])
        except Exception:
            pass

    async def _generate_and_send(
        self,
        event: AstrMessageEvent,
        prompt: str,
        params_text: str,
        ref_sources: list[str] | None = None,
        ref_collector=None,
        wait: int = 0,
        require_ref: bool = False,
    ) -> str:
        """统一生图流程：提示 → (可选等待) → 收集参考图 → 提交任务 → 发送图片。

        返回结果说明文本（LLM 工具场景下会回传给 LLM，指令场景下直接发给用户）。
        """
        try:
            client = self._client()
        except GrsaiAPIError as e:
            return str(e)

        model, ratio, size = self._resolve_params(params_text, prompt)
        ref_sources = list(ref_sources or [])
        fixed_mode = self.config.get("progress_msg_mode", "fixed") == "fixed"

        # 1) 尽早发送开始提示（fixed 模式）；llm 模式下由 bot 正常回话表达，插件不插话
        if fixed_mode:
            await self._send_progress(event, model, ratio, size, len(ref_sources), wait)

        # 2) 自定义等待：给用户随后发送的参考图留出到达时间，防止遗漏
        if wait > 0:
            await asyncio.sleep(wait)
            if ref_collector is not None:
                ref_sources = ref_collector() or ref_sources

        if require_ref and not ref_sources:
            return NO_IMAGE_MSG

        # 3) 参考图并行下载并转成 base64 data URL，避免 API 拉不动平台图链
        ref_data_urls: list[str] = []
        if ref_sources:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            ) as session:
                results = await asyncio.gather(
                    *(to_data_url(session, src) for src in ref_sources[:MAX_REF_IMAGES]),
                    return_exceptions=True,
                )
            errors = [r for r in results if isinstance(r, Exception)]
            ref_data_urls = [r for r in results if isinstance(r, str)]
            if errors:
                logger.warning(f"[auto-image] {len(errors)} 张参考图处理失败: {errors[0]}")
            if not ref_data_urls and errors:
                return f"⚠️ 参考图处理失败: {errors[0]}"
            # 等待模式下补充一条参考图确认，避免用户对参考图是否生效存疑
            if fixed_mode and wait > 0 and ref_data_urls:
                try:
                    await self._send(
                        event,
                        [Plain(f"📎 已携带 {len(ref_data_urls)} 张参考图，提交生成中……")],
                    )
                except Exception:
                    pass

        # 4) 提交任务并轮询结果
        try:
            image_urls = await client.generate(
                model=model,
                prompt=prompt,
                aspect_ratio=ratio,
                image_size=size,
                urls=ref_data_urls,
                timeout=int(self.config.get("poll_timeout", 300)),
                interval=float(self.config.get("poll_interval", 4)),
            )
        except GrsaiAPIError as e:
            return f"⚠️ {e}"
        except Exception as e:
            logger.exception("[auto-image] 生图异常")
            return f"⚠️ 生图出现异常: {e}"

        # 5) 下载并发送图片
        sent = 0
        for img_url in image_urls:
            local = self.temp_dir / f"{uuid.uuid4().hex}.jpg"
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as session:
                    await download_image(session, img_url, local)
                await self._send(event, [Image.fromFileSystem(str(local))])
                sent += 1
            except Exception as e:
                logger.warning(f"[auto-image] 图片下载失败，改为发送 URL: {e}")
                try:
                    await self._send(event, [Image.fromURL(img_url)])
                    sent += 1
                except Exception:
                    pass
            finally:
                try:
                    local.unlink(missing_ok=True)
                except OSError:
                    pass

        if sent:
            return (
                f"图片已生成并发送给用户（共 {sent} 张，模型 {model}，比例 {ratio}"
                + (f"，清晰度 {size}" if size else "")
                + (f"，携带参考图 {len(ref_data_urls)} 张" if ref_data_urls else "")
                + "。请用文字简单向用户确认即可，不要重复发送图片链接。"
            )
        return "⚠️ 图片生成成功但发送失败，可稍后重试。"

    # ---------------- 会话图片缓存（供改图取用） ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def record_recent_images(self, event: AstrMessageEvent):
        """被动记录会话中的图片，供改图工具自动取用"""
        try:
            for src in self._extract_image_sources(event):
                self._recent.add(event.unified_msg_origin, src)
        except Exception:
            pass

    # ---------------- LLM 自主工具 ----------------

    @filter.llm_tool(name="auto_text_to_image")
    async def tool_text_to_image(
        self, event: AstrMessageEvent, prompt: str, params: str = ""
    ):
        """根据文字描述生成一张全新的图片（文生图）。当用户想让你画图、生成图片、设计海报/壁纸/头像/表情包等新图像时，调用此工具。

        Args:
            prompt(string): 对目标图片的详细描述，请基于用户意图扩充细节（主体、风格、构图、色彩、氛围等），描述越丰富效果越好。
            params(string): 用户对生成参数的要求原文，如模型、画面比例、清晰度，例如 "用pro模型画横版4K"；用户没有特殊要求时留空，系统会自动模糊匹配并回退到默认参数。
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return "缺少图片描述，请根据用户需求补充 prompt 后重新调用。"
        logger.info(f"[auto-image] LLM 触发文生图: {prompt[:80]} params={params!r}")
        return await self._generate_and_send(
            event,
            prompt,
            params or "",
            ref_sources=self._extract_image_sources(event),
            ref_collector=lambda: self._collect_refs(event),
            wait=self._wait_seconds(),
        )

    @filter.llm_tool(name="auto_image_edit")
    async def tool_image_edit(
        self, event: AstrMessageEvent, prompt: str, params: str = ""
    ):
        """修改或编辑一张已有的图片（图生图 / 改图），例如换风格、改颜色、添加或去除元素、换背景、动漫化等。原图会自动取自用户当前消息携带的图片、回复引用的图片、或本会话中最近发送的图片，无需也无法手动指定图片地址。

        Args:
            prompt(string): 对修改要求的详细描述，说明要改成什么样。
            params(string): 用户对生成参数的要求原文（模型/比例/清晰度），例如 "竖版 高清"；没有特殊要求时留空。
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return "缺少修改要求描述，请根据用户需求补充 prompt 后重新调用。"

        refs = self._collect_refs(event)
        wait = self._wait_seconds()
        if not refs and wait <= 0:
            return NO_IMAGE_MSG

        logger.info(f"[auto-image] LLM 触发改图: {prompt[:80]} refs={len(refs)}")
        return await self._generate_and_send(
            event,
            prompt,
            params or "",
            ref_sources=refs,
            ref_collector=lambda: self._collect_refs(event),
            wait=wait,
            require_ref=True,
        )

    # ---------------- 指令（精确控制 / 兜底） ----------------

    @filter.command("生图", alias={"draw", "绘图"})
    async def draw(self, event: AstrMessageEvent):
        """AI 生图。用法：/生图 <提示词> [参数关键词]，回复一张图片即为图生图"""
        raw = event.message_str.strip()
        body = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
        prompt, opts = self._parse_flags(body)

        if not prompt:
            yield event.plain_result(
                "用法：/生图 <提示词> [参数]\n"
                "参数可直接写中文关键词，自动模糊匹配：\n"
                "· 模型：fast / 香蕉 / pro / 香蕉2 / gpt …\n"
                "· 比例：横版 / 竖版 / 方图 / 16:9 / 9:16 …\n"
                "· 清晰度：标清 / 高清 / 超清（1K / 2K / 4K）\n"
                "示例：/生图 一只在月球上喝茶的橘猫 横版 4K pro\n"
                "精确参数：-m nano-banana-pro -ar 16:9 -s 4K\n"
                "回复一张图片发送 /生图 即为图生图；也可用 /改图。\n"
                "发送 /生图模型 查看全部可用模型。"
            )
            return

        # 精确 flag 优先，其次对提示词做关键词模糊匹配
        model = match_model(opts.get("model", "")) or match_model(prompt)
        ratio = match_ratio(opts.get("aspect_ratio", "")) or match_ratio(prompt)
        size = match_size(opts.get("image_size", "")) or match_size(prompt)
        prompt = strip_param_words(prompt)
        if not prompt:
            yield event.plain_result("提示词不能为空，请描述要生成的画面。")
            return

        params_text = " ".join(
            filter(None, [model or "", ratio or "", size or ""])
        )
        refs = self._extract_image_sources(event)
        result = await self._generate_and_send(event, prompt, params_text, refs)
        yield event.plain_result(result)

    @filter.command("改图", alias={"edit", "图生图"})
    async def edit(self, event: AstrMessageEvent):
        """AI 改图。用法：/改图 <修改要求> [参数关键词]，自动取当前/回复/最近发送的图片"""
        raw = event.message_str.strip()
        body = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
        prompt, opts = self._parse_flags(body)

        if not prompt:
            yield event.plain_result(
                "用法：/改图 <修改要求> [参数]\n"
                "示例：/改图 改成吉卜力风格 超清\n"
                "会自动使用当前消息、回复引用、或最近发送的图片作为原图。"
            )
            return

        refs = self._collect_refs(event)
        if not refs:
            yield event.plain_result(
                "没有找到可供修改的图片，请先发送图片或回复引用一张图片。"
            )
            return

        model = match_model(opts.get("model", "")) or match_model(prompt)
        ratio = match_ratio(opts.get("aspect_ratio", "")) or match_ratio(prompt)
        size = match_size(opts.get("image_size", "")) or match_size(prompt)
        prompt = strip_param_words(prompt) or body

        params_text = " ".join(
            filter(None, [model or "", ratio or "", size or ""])
        )
        result = await self._generate_and_send(event, prompt, params_text, refs)
        yield event.plain_result(result)

    @filter.command("生图模型", alias={"models"})
    async def list_models(self, event: AstrMessageEvent):
        """查看可用生图模型与关键词叫法"""
        lines = ["可用生图模型（支持模糊叫法）："]
        for m in ALLOWED_MODELS:
            lines.append(f"· {m}")
        lines.append("")
        lines.append("模糊叫法示例：")
        lines.append("· 模型：fast / 香蕉 / pro / 香蕉2 / gpt")
        lines.append("· 比例：横版(16:9) / 竖版(9:16) / 方图(1:1) / 电影(21:9)")
        lines.append("· 清晰度：标清(1K) / 高清(2K) / 超清(4K)")
        lines.append("注：仅 pro / 2 系列支持 1K-4K 清晰度参数。")
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        """插件卸载时清理临时文件"""
        try:
            for f in self.temp_dir.glob("*.jpg"):
                f.unlink()
        except Exception:
            pass
