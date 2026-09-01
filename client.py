# -*- coding: utf-8 -*-
"""Grsai 生图 API 客户端（提交任务 + 轮询结果）与图片工具函数。

与 astrbot_plugin_grsai_image 使用同一后端接口。
"""

import asyncio
import base64
import mimetypes
import time
from pathlib import Path

import aiohttp

# API 节点
NODES = {
    "cn": "https://grsai.dakka.com.cn",
    "global": "https://api.grsai.com",
}

# 全部生图模型（不含视频模型）
IMAGE_MODELS = [
    "nano-banana-fast",
    "nano-banana",
    "nano-banana-pro",
    "nano-banana-pro-vt",
    "nano-banana-pro-cl",
    "nano-banana-pro-vip",
    "nano-banana-pro-4k-vip",
    "nano-banana-2",
    "nano-banana-2-cl",
    "nano-banana-2-2k-cl",
    "nano-banana-2-4k-cl",
    "gpt-image-2",
    "gpt-image-2-vip",
]

# 支持 imageSize（1K/2K/4K）参数的模型
IMAGE_SIZE_MODELS = {
    "nano-banana-pro",
    "nano-banana-pro-vt",
    "nano-banana-pro-cl",
    "nano-banana-pro-vip",
    "nano-banana-pro-4k-vip",
    "nano-banana-2",
    "nano-banana-2-cl",
    "nano-banana-2-2k-cl",
    "nano-banana-2-4k-cl",
}

# 参考图限制（最多 8 张，单张小于 10MB）
MAX_REF_IMAGES = 8
MAX_REF_IMAGE_SIZE = 10 * 1024 * 1024


class GrsaiAPIError(Exception):
    """Grsai API 调用失败"""


class GrsaiClient:
    """Grsai 生图 API 客户端（提交任务 + 轮询结果）"""

    def __init__(self, api_key: str, node: str = "cn"):
        self.api_key = api_key.strip()
        self.base_url = NODES.get(node, NODES["cn"])

    @property
    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    @staticmethod
    def _endpoint_for(model: str) -> str:
        if model.startswith("nano-banana"):
            return "/v1/draw/nano-banana"
        return "/v1/draw/completions"

    async def submit(
        self,
        session: aiohttp.ClientSession,
        model: str,
        prompt: str,
        aspect_ratio: str = "auto",
        image_size: str | None = None,
        urls: list[str] | None = None,
        variants: int = 1,
    ) -> str:
        """提交生图任务，返回任务 id"""
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "aspectRatio": aspect_ratio,
            "variants": variants,
            "urls": urls or [],
            "webHook": "-1",
        }
        if model in IMAGE_SIZE_MODELS and image_size:
            payload["imageSize"] = image_size

        url = self.base_url + self._endpoint_for(model)
        async with session.post(url, headers=self._headers, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise GrsaiAPIError(f"任务提交失败 HTTP {resp.status}: {text[:200]}")
            data = await resp.json()

        if data.get("code") != 0:
            raise GrsaiAPIError(f"任务提交失败: {data.get('msg') or data}")
        return data["data"]["id"]

    async def query_result(self, session: aiohttp.ClientSession, task_id: str) -> dict:
        """查询任务结果，返回 data 字段"""
        url = f"{self.base_url}/v1/draw/result"
        async with session.post(url, headers=self._headers, json={"id": task_id}) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise GrsaiAPIError(f"结果查询失败 HTTP {resp.status}: {text[:200]}")
            data = await resp.json()

        code = data.get("code")
        if code == -22:
            # 服务端生成超时
            return {"status": "failed", "failure_reason": "生成超时"}
        if code != 0:
            raise GrsaiAPIError(f"结果查询失败: {data.get('msg') or data}")
        return data["data"] or {}

    async def generate(
        self,
        model: str,
        prompt: str,
        aspect_ratio: str = "auto",
        image_size: str | None = None,
        urls: list[str] | None = None,
        timeout: int = 300,
        interval: float = 4.0,
    ) -> list[str]:
        """完整流程：提交任务并轮询直到完成，返回图片 URL 列表"""
        client_timeout = aiohttp.ClientTimeout(total=timeout + 60, sock_connect=15)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            task_id = await self.submit(
                session, model, prompt, aspect_ratio, image_size, urls
            )

            deadline = time.monotonic() + timeout
            while True:
                if time.monotonic() > deadline:
                    raise GrsaiAPIError(f"等待生成结果超时（{timeout} 秒）")
                result = await self.query_result(session, task_id)
                status = result.get("status")
                if status == "succeeded":
                    results = result.get("results") or []
                    image_urls = [r["url"] for r in results if r.get("url")]
                    if not image_urls and result.get("url"):
                        image_urls = [result["url"]]
                    if not image_urls:
                        raise GrsaiAPIError("任务成功但未返回图片地址")
                    return image_urls
                if status == "failed":
                    reason = (
                        result.get("error")
                        or result.get("failure_reason")
                        or "未知原因"
                    )
                    raise GrsaiAPIError(f"生成失败: {reason}")
                await asyncio.sleep(interval)


async def to_data_url(session: aiohttp.ClientSession, src: str) -> str:
    """把参考图（http URL / 本地路径）转成 base64 data URL。

    Grsai API 对 QQ 等平台的多媒体链接经常拉取失败
    （image upload failed），统一先转成 data URL 再提交。
    """
    mime = ""
    if src.startswith("file://"):
        src = src[7:]
    if src.startswith(("http://", "https://")):
        async with session.get(src, headers={"User-Agent": "Mozilla/5.0"}) as resp:
            if resp.status != 200:
                raise GrsaiAPIError(f"参考图下载失败 HTTP {resp.status}")
            data = await resp.read()
            mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    else:
        path = Path(src)
        if not path.exists():
            raise GrsaiAPIError(f"参考图本地文件不存在: {src}")
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or ""

    if len(data) > MAX_REF_IMAGE_SIZE:
        raise GrsaiAPIError("参考图大小超过 10MB 限制")
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


async def download_image(session: aiohttp.ClientSession, url: str, path: Path) -> Path:
    """下载图片到本地路径"""
    async with session.get(url) as resp:
        if resp.status != 200:
            raise GrsaiAPIError(f"图片下载失败 HTTP {resp.status}")
        path.write_bytes(await resp.read())
    return path
