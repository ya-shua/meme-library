"""表情包收录库（独立插件，宿主 qqbridge）。

挂载方式：插件目录通过宿主 config.json 的 plugin_paths 被发现。
能力：
  - 包装 services["stickers"]，让 conch 自动配图走本图库（失败回退原服务）
  - 独立 aiohttp 大厅（预览、集合开关、审核、标签、心情映射、Git 下载）
  - 聊天指令手动操作
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from core.message import Segment, make_text  # noqa: E402

from meme_core.constants import (  # noqa: E402
    FALLBACK_TAG,
    PACK_EMOTION,
    PACK_MEME,
    REVIEW_PENDING_HIGH,
    REVIEW_PENDING_LOW,
    STANDARD_TAGS,
)
from meme_core.database import Database  # noqa: E402
from meme_core.downloader import GitDownloader  # noqa: E402
from meme_core.llm import VisionClient  # noqa: E402
from meme_core.preview import RepoPreview  # noqa: E402
from meme_core.sticker_bridge import StickerBridge  # noqa: E402
from meme_core.tagging import TaggingService  # noqa: E402

HELP = (
    "表情包收录库指令：\n"
    "/meme 分类 —— 手动发一张情绪图\n"
    "/梗图 —— 随机发一张梗图\n"
    "/下载 仓库地址 [emotion/meme] —— 下载 Git 表情包\n"
    "/启用 集合名... —— 多选启用集合\n"
    "/列表 —— 集合与启用状态\n"
    "/状态 —— 审核计数\n"
    "/本地 —— 文件名规则自动打标\n"
    "/识别 —— AI 两档打标\n"
    "/入库 high|low —— 整档一键确认"
)


async def setup(ctx) -> None:
    cfg = dict(ctx.config)
    data_dir = PLUGIN_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    db = Database(str(data_dir / "meme_library.db"))
    downloader = GitDownloader(PLUGIN_DIR, db, data_dir / "temp")
    tagging = TaggingService(PLUGIN_DIR, db)
    preview_cfg = cfg.get("preview") or {}
    # 缩略图上限存在插件自己的 data/thumb.json，不往宿主 config 里塞东西
    thumb_cfg = {}
    try:
        thumb_cfg = json.loads((data_dir / "thumb.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        thumb_cfg = {}
    preview = RepoPreview(
        preview_cfg.get("github_token", ""), db=db,
        cache_dir=data_dir / "preview_cache",
        thumb_dir=data_dir / "thumb_cache",
        thumb_max_mb=int(thumb_cfg.get("max_mb") or 128),
    )

    vision_cfg = cfg.get("vision") or {}
    base_url = vision_cfg.get("base_url") or ""
    api_key = vision_cfg.get("api_key") or ""
    if not api_key:
        keys = ctx.cfg.model.keys
        if keys:
            base_url = base_url or ctx.cfg.model.base_url
            api_key = keys[0].get("key", "")
    vision = VisionClient(
        base_url, api_key, vision_cfg.get("model", ""),
        int(vision_cfg.get("timeout_s") or 60),
    )

    # 包装内置 stickers，让 conch 自动配图走本图库
    original_stickers = ctx.services.get("stickers")
    bridge = StickerBridge(original_stickers, ctx, db, PLUGIN_DIR)
    ctx.services["stickers"] = bridge

    # 大厅服务
    from server.hall_server import HallServer

    hall = HallServer(
        plugin_root=PLUGIN_DIR, db=db, downloader=downloader,
        tagging=tagging, vision=vision, preview=preview,
    )
    await hall.start(cfg.get("hall_host", "127.0.0.1"),
                     int(cfg.get("hall_port") or 7802))

    runtime = {"db": db, "downloader": downloader, "tagging": tagging,
               "vision": vision, "preview": preview, "hall": hall,
               "original_stickers": original_stickers}
    ctx.services["meme_library"] = runtime

    ctx.on("message.received", lambda msg: on_message(ctx, runtime, msg), priority=20)
    if cfg.get("inject_prompt", True):
        ctx.on("speech.prompt", _on_prompt_factory(db), priority=50)

    ctx.log.info("表情包收录库就绪，大厅端口 %s", cfg.get("hall_port"))


async def teardown(ctx) -> None:
    runtime = ctx.services.get("meme_library")
    if runtime:
        original = runtime.get("original_stickers")
        if original is not None:
            ctx.services["stickers"] = original
        hall = runtime.get("hall")
        if hall is not None:
            await hall.stop()
        runtime["db"].close()
        ctx.services.pop("meme_library", None)


# ---------------- 事件与指令 ----------------

def _on_prompt_factory(db):
    async def handler(payload):
        """往系统提示里注入可用分类，引导模型按心情输出【表情包：分类】。"""
        stats = db.get_all_tag_stats()
        usable = [f"{tag}({STANDARD_TAGS[tag]})" for tag, c in stats.items() if c > 0]
        if not usable:
            return
        line = (
            "[表情包] 按当前心情，想发图就单独占一行写「【表情包：分类】」，"
            "可用分类：" + "、".join(usable)
        )
        extra = str(payload.get("extra") or "")
        payload["extra"] = extra + ("\n" if extra else "") + line
    return handler


async def on_message(ctx, runtime, msg) -> None:
    text = msg.text
    if not text.startswith("/"):
        return
    parts = text[1:].split()
    if not parts:
        return
    cmd = parts[0]
    args = parts[1:]
    db = runtime["db"]

    if cmd in ("meme", "表情"):
        tag = args[0] if args else FALLBACK_TAG
        if tag not in STANDARD_TAGS:
            from meme_core.tagging import guess_tag_by_name
            tag = guess_tag_by_name(tag) or FALLBACK_TAG
        rel = db.get_random_image_by_tag(tag, PACK_EMOTION)
        if rel is None:
            rel = db.get_random_image_by_tag(FALLBACK_TAG, PACK_EMOTION)
        if rel:
            await ctx.send(msg.conv_id, [Segment("image", {"file": str(PLUGIN_DIR / rel)})])
        else:
            await ctx.send(msg.conv_id, make_text("手里没有可用的情绪图。"))

    elif cmd in ("梗图", "geng"):
        rel = db.get_random_image(PACK_MEME)
        if rel:
            await ctx.send(msg.conv_id, [Segment("image", {"file": str(PLUGIN_DIR / rel)})])
        else:
            await ctx.send(msg.conv_id, make_text("没有可用梗图。"))

    elif cmd in ("下载", "mldl"):
        if not args:
            await ctx.send(msg.conv_id, make_text("用法：/下载 仓库地址 [emotion/meme]"))
            return
        url = args[0]
        pack_type = args[1] if len(args) > 1 and args[1] in (PACK_EMOTION, PACK_MEME) else PACK_EMOTION
        await ctx.send(msg.conv_id, make_text("开始下载，稍等……"))
        result = await runtime["downloader"].download(url, pack_type)
        await ctx.send(msg.conv_id, make_text(result.get("message", "下载结束。")))

    elif cmd in ("启用", "mluse"):
        if not args:
            await ctx.send(msg.conv_id, make_text("用法：/启用 集合名...（可多个）"))
            return
        db.set_enabled_packs(args)
        await ctx.send(msg.conv_id, make_text("已启用：" + "、".join(db.get_enabled_pack_names())))

    elif cmd in ("列表", "mllist"):
        lines = []
        for p in db.list_packs():
            flag = "✓" if p["enabled"] else "·"
            lines.append(f"{flag} {p['name']}（{p['type']}）")
        await ctx.send(msg.conv_id, make_text("\n".join(lines) or "还没有集合。"))

    elif cmd in ("状态", "mlstatus"):
        c = db.review_counts()
        await ctx.send(msg.conv_id, make_text(
            f"已确认 {c['confirmed']}｜高置信待确认 {c['pending_high']}｜"
            f"待审核 {c['pending_low']}｜未分类 {c['uncategorized']}"))

    elif cmd in ("本地", "mllocal"):
        r = runtime["tagging"].local_inference()
        await ctx.send(msg.conv_id, make_text(
            f"本地打标 {r['processed']} 张，剩余 {r['remaining']} 张。"))

    elif cmd in ("识别", "mlai"):
        r = await runtime["tagging"].ai_tag(runtime["vision"])
        await ctx.send(msg.conv_id, make_text(r.get("message", "识别结束。")))

    elif cmd in ("入库", "mlconfirm"):
        target = args[0] if args else "high"
        status = REVIEW_PENDING_LOW if target == "low" else REVIEW_PENDING_HIGH
        n = db.batch_confirm(status)
        await ctx.send(msg.conv_id, make_text(f"已一键入库 {n} 张。"))

    elif cmd in ("帮助", "help", "mlhelp"):
        await ctx.send(msg.conv_id, make_text(HELP))
