"""贴纸桥接：包装宿主内置 stickers，仅覆盖 pick_and_send 走本图库。

其他属性/方法全部委托原对象，保证面板和宿主不受影响；
本图库没有合适图时，回退原 stickers，保留旧能力。
"""

from __future__ import annotations

from core.message import Segment

from .constants import FALLBACK_TAG, PACK_EMOTION, STANDARD_TAGS
from .tagging import guess_tag_by_name


class StickerBridge:
    def __init__(self, original, ctx, db, plugin_root):
        self._original = original
        self._ctx = ctx
        self._db = db
        self._root = plugin_root

    def __getattr__(self, item):
        return getattr(self._original, item)

    def _resolve_tag(self, hint: str):
        hint = str(hint or "").strip()
        if not hint:
            return None
        if hint in STANDARD_TAGS:
            return hint
        tag = self._db.get_mood_tag(hint)
        if tag:
            return tag
        return guess_tag_by_name(hint)

    async def pick_and_send(self, conv_id: str, hint: str = "") -> bool:
        tag = self._resolve_tag(hint)
        rel = None
        if tag:
            rel = self._db.get_random_image_by_tag(tag, PACK_EMOTION)
        if rel is None:
            rel = self._db.get_random_image_by_tag(FALLBACK_TAG, PACK_EMOTION)
        if rel:
            abs_path = self._root / rel
            try:
                await self._ctx.send(
                    conv_id, [Segment("image", {"file": str(abs_path)})]
                )
                return True
            except Exception as exc:
                self._ctx.log.warning("收录库发图失败，回退原 stickers：%s", exc)
        return await self._original.pick_and_send(conv_id, hint)
