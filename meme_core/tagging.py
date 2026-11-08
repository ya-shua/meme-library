"""标签推断与审核：本地规则推断（0 token）+ AI 两档打标。"""

import json
import re
from pathlib import Path

from .constants import (
    REVIEW_CONFIRMED,
    REVIEW_PENDING_HIGH,
    REVIEW_PENDING_LOW,
    REVIEW_UNCATEGORIZED,
    STANDARD_TAGS,
    TAG_ALIASES,
)


def guess_tag_by_name(file_name: str):
    """按文件名/目录名做本地规则匹配，返回标准 tag 或 None。"""
    stem = Path(file_name).stem.lower()
    tokens = re.split(r"[_\-\s]+", stem)
    tokens.append(stem.replace(" ", ""))
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        for tag, aliases in TAG_ALIASES.items():
            if token in aliases:
                return tag
    return None


class TaggingService:
    def __init__(self, plugin_root: Path, db):
        self.root = plugin_root
        self.db = db

    # ---------- 本地规则推断（0 token，直接入库） ----------
    def local_inference(self, limit=500) -> dict:
        images = self.db.list_images(
            review_status=REVIEW_UNCATEGORIZED, limit=limit, offset=0
        )
        moved = 0
        for img in images:
            tag = guess_tag_by_name(img["file_name"])
            if tag:
                self.db.add_tag(img["id"], tag)
                self.db.confirm_image(img["id"])
                moved += 1
        return {"processed": moved, "remaining": len(images) - moved}

    # ---------- AI 两档打标 ----------
    async def ai_tag(self, provider, limit=50) -> dict:
        if provider is None:
            return {"ok": False, "message": "当前没有可用的模型提供商，无法 AI 打标。"}
        images = self.db.list_images(
            review_status=REVIEW_UNCATEGORIZED, limit=limit, offset=0
        )
        high = low = failed = 0
        for img in images:
            abs_path = self.root / img["file_path"]
            if not abs_path.exists():
                continue
            try:
                resp = await provider.text_chat(
                    prompt="请给这张表情包分类。",
                    system_prompt=self._system_prompt(),
                    image_urls=[str(abs_path)],
                )
                tag, confidence = self._parse_ai_response(resp.completion_text)
            except Exception:
                failed += 1
                continue
            if tag is None:
                failed += 1
                continue
            self.db.add_tag(img["id"], tag)
            if confidence == "high":
                self.db.set_review_status(img["id"], REVIEW_PENDING_HIGH)
                high += 1
            else:
                self.db.set_review_status(img["id"], REVIEW_PENDING_LOW)
                low += 1
        return {"ok": True, "high": high, "low": low, "failed": failed,
                "message": f"AI 打标完成：高置信 {high}，待审核 {low}，失败 {failed}。"}

    # ---------- 心情映射初始推荐（本地，0 token） ----------
    def recommend_moods(self, mood_words: list) -> dict:
        mapping = {}
        for word in mood_words:
            tag = guess_tag_by_name(word)
            if tag:
                mapping[word] = tag
        return mapping

    @staticmethod
    def _system_prompt() -> str:
        tags = "、".join(f"{k}({v})" for k, v in STANDARD_TAGS.items())
        return (
            "你是表情包分类器。从以下分类中选择一个最贴切的："
            f"{tags}。同时判断把握程度。"
            "只输出 JSON，格式为：{\"tag\":\"分类标识\",\"confidence\":\"high或low\"}，"
            "不要输出任何其他内容。"
        )

    @staticmethod
    def _parse_ai_response(text: str):
        if not text:
            return None, None
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None, None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None, None
        tag = data.get("tag")
        if tag not in STANDARD_TAGS:
            return None, None
        confidence = data.get("confidence", "low")
        if confidence not in ("high", "low"):
            confidence = "low"
        return tag, confidence
