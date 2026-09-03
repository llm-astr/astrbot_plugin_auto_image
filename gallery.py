# -*- coding: utf-8 -*-
"""提示词图库：图片 + 预设提示词（关键词）的持久化存储与匹配。

用途：用户为每张图库图片设置一个或多个「预设提示词」（关键词）；
当生图 / 改图请求文本中包含某关键词时，对应图片自动作为参考图附加，
并在提示词中按参考图序数强调「第几张是什么」（见 presets.py 的 GalleryPreset）。

存储位置：优先 AstrBot 插件数据目录（data/plugin_data/<插件名>/prompt_gallery，
插件更新不会丢失）；老版本 AstrBot 没有该 API 时回退到插件目录下。
索引文件 index.json 与图片文件同目录，结构：
    [{"id": "...", "file": "<id>.jpg", "keywords": ["..."], "ts": 169...}, ...]
列表顺序即用户添加顺序，也就是参考图「第 N 张」的序数依据。
"""

import json
import re
import time
import uuid
from pathlib import Path

MAX_ITEMS = 60                # 图库容量上限
MAX_KEYWORDS_PER_ITEM = 10    # 每张图最多绑定的预设提示词数量
MAX_KEYWORD_LEN = 30          # 单个预设提示词最大长度
MAX_IMAGE_BYTES = 15 * 1024 * 1024  # 单张图片大小上限 15MB
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
EXT_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
MIME_EXT = {v: k for k, v in EXT_MIME.items() if k != ".jpeg"}


def sniff_ext(data: bytes) -> str:
    """按魔数识别图片格式，无法识别返回空串"""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""


def default_gallery_dir() -> Path:
    """图库目录：优先 AstrBot 插件数据目录（更新插件不丢数据），失败回退插件目录"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return (
            Path(get_astrbot_plugin_data_path())
            / "astrbot_plugin_auto_image"
            / "prompt_gallery"
        )
    except Exception:
        return Path(__file__).parent / "prompt_gallery"


def sanitize_keywords(raw) -> list[str]:
    """清洗关键词：去空白 / 去重 / 限量限长，非法输入返回空列表"""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for kw in raw:
        kw = str(kw or "").strip()
        if not kw or len(kw) > MAX_KEYWORD_LEN:
            continue
        if kw not in out:
            out.append(kw)
        if len(out) >= MAX_KEYWORDS_PER_ITEM:
            break
    return out


def guess_ext(filename: str = "", content_type: str = "") -> str:
    """根据文件名 / Content-Type 推断安全的图片扩展名，无法识别时返回空串"""
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix in ALLOWED_EXTS:
        return suffix
    ct = str(content_type or "").split(";")[0].strip().lower()
    return MIME_EXT.get(ct, "")


class PromptGallery:
    """提示词图库存储：线程无关、全量落盘（图库规模小，直接整体读写 index.json）"""

    def __init__(self, gallery_dir):
        self.dir = Path(gallery_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.dir / "index.json"
        self._items: list[dict] = []
        self._load()

    # ---------------- 查询 ----------------

    def list_items(self) -> list[dict]:
        """全部条目（按添加顺序），每条 {id, file, keywords, ts, has_image}"""
        out = []
        for it in self._items:
            out.append(
                {
                    "id": it["id"],
                    "file": it.get("file", ""),
                    "keywords": list(it.get("keywords", [])),
                    "ts": it.get("ts", 0),
                    "has_image": bool(it.get("file"))
                    and (self.dir / it["file"]).is_file(),
                }
            )
        return out

    def get(self, item_id: str) -> dict | None:
        for it in self._items:
            if it["id"] == item_id:
                return it
        return None

    def nth(self, index: int) -> dict | None:
        """按 1 起始的序号取条目（QQ 指令用）"""
        if 1 <= index <= len(self._items):
            return self._items[index - 1]
        return None

    def image_path(self, item: dict) -> Path:
        return self.dir / item.get("file", "")

    def brief(self) -> str:
        """图库概况，用于日志排查：「关键词1/关键词2」(图片缺失!)、…"""
        parts = []
        for it in self._items:
            kw = "/".join(it.get("keywords", [])) or "未设置"
            has = bool(it.get("file")) and (self.dir / it["file"]).is_file()
            parts.append(f"「{kw}」" + ("" if has else "(图片缺失!)"))
        return "、".join(parts)

    def match(self, text: str) -> list[dict]:
        """按预设提示词匹配图库条目（按添加顺序，每条最多命中一次）。

        返回的条目附带 matched_keyword 字段（实际命中的那个关键词），
        供提示词序数标注使用；没有有效图片文件的条目不参与匹配。
        """
        text = (text or "").lower()
        if not text.strip():
            return []
        hits: list[dict] = []
        for it in self._items:
            if not it.get("file") or not (self.dir / it["file"]).is_file():
                continue
            for kw in it.get("keywords", []):
                if kw and kw.lower() in text:
                    hit = dict(it)
                    hit["matched_keyword"] = kw
                    hits.append(hit)
                    break
        return hits

    # ---------------- 增改删 ----------------

    def create(self, keywords=None) -> dict:
        """创建条目（图片可随后通过 attach_* 补充）"""
        if len(self._items) >= MAX_ITEMS:
            raise ValueError(f"图库已满（最多 {MAX_ITEMS} 张），请先删除不用的条目")
        item = {
            "id": uuid.uuid4().hex[:12],
            "file": "",
            "keywords": sanitize_keywords(keywords or []),
            "ts": time.time(),
        }
        self._items.append(item)
        self._save()
        return item

    def attach_bytes(self, item_id: str, data: bytes, ext: str) -> dict:
        """把图片字节写入图库并绑定到条目（覆盖旧图）"""
        if ext not in ALLOWED_EXTS:
            raise ValueError("不支持的图片格式（仅 jpg / png / webp / gif）")
        if not data:
            raise ValueError("图片数据为空")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("图片超过 15MB 上限")
        item = self.get(item_id)
        if item is None:
            raise KeyError("图库条目不存在")
        name = f"{item_id}{ext}"
        (self.dir / name).write_bytes(data)
        self._attach_name(item, name)
        return item

    def candidate_path(self, item_id: str, ext: str) -> Path:
        """计算条目图片的目标路径（供上传 handler 直接落盘后调用 attach_saved）"""
        if not re.fullmatch(r"[0-9a-f]{12}", str(item_id or "")):
            raise KeyError("图库条目不存在")
        if ext not in ALLOWED_EXTS:
            raise ValueError("不支持的图片格式（仅 jpg / png / webp / gif）")
        if self.get(item_id) is None:
            raise KeyError("图库条目不存在")
        return self.dir / f"{item_id}{ext}"

    def attach_saved(self, item_id: str, path) -> dict:
        """确认已落盘的图片文件（校验大小后绑定到条目）"""
        item = self.get(item_id)
        if item is None:
            raise KeyError("图库条目不存在")
        p = Path(path)
        size = p.stat().st_size if p.is_file() else 0
        if size <= 0:
            raise ValueError("图片数据为空")
        if size > MAX_IMAGE_BYTES:
            try:
                p.unlink()
            except OSError:
                pass
            raise ValueError("图片超过 15MB 上限")
        self._attach_name(item, p.name)
        return item

    def set_keywords(self, item_id: str, keywords) -> dict:
        item = self.get(item_id)
        if item is None:
            raise KeyError("图库条目不存在")
        kws = sanitize_keywords(keywords)
        if not kws:
            raise ValueError("至少需要 1 个有效的预设提示词")
        item["keywords"] = kws
        self._save()
        return item

    def remove(self, item_id: str) -> bool:
        item = self.get(item_id)
        if item is None:
            return False
        self._items = [it for it in self._items if it["id"] != item_id]
        file = item.get("file", "")
        if file:
            try:
                (self.dir / file).unlink(missing_ok=True)
            except OSError:
                pass
        self._save()
        return True

    # ---------------- 内部 ----------------

    def _attach_name(self, item: dict, name: str) -> None:
        old = item.get("file", "")
        if old and old != name:
            try:
                (self.dir / old).unlink(missing_ok=True)
            except OSError:
                pass
        item["file"] = name
        self._save()

    def _load(self) -> None:
        try:
            raw = json.loads(self.index_file.read_text(encoding="utf-8"))
        except Exception:
            raw = []
        if not isinstance(raw, list):
            return
        items: list[dict] = []
        for it in raw:
            try:
                item_id = str(it["id"])
                if not re.fullmatch(r"[0-9a-f]{12}", item_id):
                    continue
                file = str(it.get("file") or "")
                # 文件名只允许 <id>.<ext> 形态，防止索引被篡改后越界读写
                if file and not re.fullmatch(rf"{item_id}\.(jpg|jpeg|png|webp|gif)", file):
                    file = ""
                items.append(
                    {
                        "id": item_id,
                        "file": file,
                        "keywords": sanitize_keywords(it.get("keywords") or []),
                        "ts": float(it.get("ts") or 0),
                    }
                )
            except Exception:
                continue
        self._items = items[:MAX_ITEMS]

    def _save(self) -> None:
        try:
            self.index_file.write_text(
                json.dumps(self._items, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
