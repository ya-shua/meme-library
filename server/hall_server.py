"""独立 aiohttp 大厅：静态页面 + JSON API + 图片文件服务。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime

from aiohttp import web

from meme_core.constants import (
    PLUGIN_VERSION,
    PACK_EMOTION,
    PACK_MEME,
    REPO_SOURCES,
    REVIEW_PENDING_HIGH,
    REVIEW_PENDING_LOW,
    STANDARD_TAGS,
)


class HallServer:
    def __init__(self, plugin_root, db, downloader, tagging, vision, preview=None):
        self.root = plugin_root
        self.db = db
        self.downloader = downloader
        self.tagging = tagging
        self.vision = vision
        self.preview = preview
        self.runner = None
        self.jobs: dict[str, dict] = {}
        self.trash_dir = (plugin_root / "data" / "trash")
        self.trash_max = 50   # 回收站最多留 50 张，超了从最旧的开始清
        self.started_at = time.time()   # 契约接口用：进程/服务启动时刻
        self.port = None                # start() 里填真实监听端口
        self._heal_dead_jobs()   # 上次没跑完的任务标成中断，别一直挂在“下载中”

    def _heal_dead_jobs(self) -> None:
        try:
            for row in self.db.list_downloads(200):
                if not row.get("done"):
                    row["done"] = 1
                    row["ok"] = 0
                    row["finished"] = row.get("created") or 0
                    row["message"] = "上次没跑完（程序重启过），重新收一次吧"
                    self.db.save_download(row)
        except Exception:
            pass

    async def start(self, host: str, port: int) -> None:
        app = web.Application()
        # 对接契约（只读、无隐私）：任意宿主据此判断「接得对不对、版本对不对、能用到哪些能力」
        app.add_routes([
            web.get("/api/health", self.health),
            web.get("/api/manifest", self.manifest),
        ])
        app.add_routes(
            [
                web.get("/api/overview", self.overview),
                web.get("/api/packs", self.packs),
                web.post("/api/packs/enable", self.packs_enable),
                web.post("/api/packs/delete", self.packs_delete),
                web.get("/api/repo/inspect", self.repo_inspect),
                web.get("/api/store/repos", self.store_repos),
                web.get("/api/store/index", self.store_index),
                web.get("/api/store/cover", self.store_cover),
                web.get("/api/repo/files", self.repo_files),
                web.get("/api/repo/thumb", self.repo_thumb),
                web.get("/api/images", self.images),
                web.get("/api/tag/counts", self.tag_counts),
                web.post("/api/images/delete", self.images_delete),
                web.get("/api/trash", self.trash_list),
                web.get("/api/trash/img", self.trash_img),
                web.post("/api/trash/restore", self.trash_restore),
                web.post("/api/trash/purge", self.trash_purge),
                web.get("/api/img", self.img_file),
                web.post("/api/tags", self.tags_save),
                web.post("/api/tags/batch", self.tags_batch),
                web.post("/api/review/confirm", self.review_confirm),
                web.post("/api/review/batch", self.review_batch),
                web.post("/api/review/revert", self.review_revert),
                web.get("/api/moods", self.moods_list),
                web.post("/api/moods", self.moods_save),
                web.post("/api/moods/delete", self.mood_delete),
                web.post("/api/download", self.download),
                web.get("/api/download/status", self.download_status),
                web.post("/api/download/cancel", self.download_cancel),
                web.post("/api/download/delete", self.download_delete),
                web.post("/api/download/clear", self.download_clear),
                web.get("/api/download/tasks", self.download_tasks),
                web.post("/api/tagging/local", self.run_local),
                web.post("/api/tagging/ai", self.run_ai),
                web.get("/api/constants", self.constants),
                web.get("/api/thumb/status", self.thumb_status),
                web.post("/api/thumb/settings", self.thumb_settings),
                web.post("/api/deps/install", self.deps_install),
                web.get("/api/deps/status", self.deps_status),
            ]
        )
        self.view_dir = self.root / "views" / "hall"
        app.add_routes([
            web.get("/", self.serve_view),
            web.get("/app.js", self.serve_view),
            web.get("/style.css", self.serve_view),
        ])

        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, host, port)
        self.port = port   # 契约接口报「实际监听端口」，由这里填
        await site.start()

    async def serve_view(self, request):
        name = "index.html" if request.path == "/" else request.path.lstrip("/")
        base = self.view_dir.resolve()
        target = (self.view_dir / name).resolve()
        if target != base and base not in target.parents:
            return web.json_response({"error": "forbidden"}, status=403)
        if not target.exists():
            return web.json_response({"error": "not found"}, status=404)
        return web.FileResponse(target)

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    # ---------------- 对接契约（只读，给任意宿主判「能不能接」） ----------------

    def _metadata(self) -> dict:
        """仓库根 metadata.json。只读；读坏/读不到就给空对象，不让契约接口 500。"""
        try:
            return json.loads((self.root / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    async def health(self, request):
        """探活：活没活、是哪份、什么版本、跑多久了。不含任何密钥/用户数据。"""
        return web.json_response({
            "ok": True,
            "id": self._metadata().get("id", ""),
            "version": PLUGIN_VERSION,
            "uptime_s": int(time.time() - self.started_at),
        })

    async def manifest(self, request):
        """清单：元数据原文 + 本次运行的真实端口/pid/启动时刻。"""
        return web.json_response({
            "ok": True,
            "metadata": self._metadata(),
            "runtime": {
                "port": self.port,
                "pid": os.getpid(),
                "started_at": datetime.fromtimestamp(self.started_at).astimezone().isoformat(
                    timespec="seconds"),
            },
        })

    # ---------------- handlers ----------------
    async def overview(self, request):
        return web.json_response({
            "review": self.db.review_counts(),
            "tag_stats": self.db.get_all_tag_stats(),
            "standard_tags": STANDARD_TAGS,
        })

    async def constants(self, request):
        return web.json_response({
            "standard_tags": STANDARD_TAGS,
            "pack_types": {"emotion": "情绪包", "meme": "梗图包"},
        })

    async def packs(self, request):
        packs = self.db.list_packs()
        for p in packs:
            p["image_count"] = self.db.count_images(pack_id=p["id"])
        return web.json_response(packs)

    async def packs_enable(self, request):
        body = await request.json()
        names = body.get("names") or []
        self.db.set_enabled_packs(names)
        return web.json_response({"enabled": self.db.get_enabled_pack_names()})

    async def packs_delete(self, request):
        """删集合：先删库里的记录，再删 memes 下对应的文件夹。"""
        body = await request.json()
        pack_id = int(body.get("id") or 0)
        pack = self.db.get_pack_by_id(pack_id)
        if not pack:
            return web.json_response({"ok": False, "message": "没有这个集合"}, status=404)
        self.db.delete_pack(pack_id)
        memes_root = (self.root / "memes").resolve()
        target = (memes_root / str(pack["name"])).resolve()
        if memes_root in target.parents and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        return web.json_response({"ok": True, "name": pack["name"]})

    # ---------------- 在线预览（不下载） ----------------
    async def store_repos(self, request):
        """资源广场的地址库：地址抄在 constants 里，不再依赖别人的索引服务。"""
        items = []
        for s in REPO_SOURCES:
            repo = s["repo"]
            items.append({
                "name": s["name"],
                "repo": repo,
                "ref": s["ref"],
                "url": f"https://github.com/{repo}",
                "note": s["note"],
                "author": repo.split("/")[0],
                "exists_locally": bool(self.db.get_pack(repo.split("/")[-1])),
            })
        return web.json_response({"ok": True, "total": len(items), "items": items})

    async def store_index(self, request):
        """社区表情包索引，给资源广场的「社区包」区块用。"""
        if self.preview is None:
            return web.json_response({"ok": False, "message": "预览模块没装"}, status=500)
        try:
            data = await self.preview.index()
        except Exception as exc:
            return web.json_response({"ok": False, "message": str(exc)}, status=200)
        for it in data.get("items", []):
            it["exists_locally"] = bool(self.db.get_pack(it["repo"].split("/")[-1]))
        return web.json_response(data)

    async def store_cover(self, request):
        """拿包里的一张真实表情当卡片封面（不占 GitHub API 额度）。"""
        if self.preview is None:
            return web.json_response({"ok": False, "url": ""})
        repo = str(request.query.get("repo") or "").strip()
        ref = str(request.query.get("ref") or "main").strip()
        if "/" not in repo:
            return web.json_response({"ok": False, "url": "", "message": "repo 参数要 owner/repo"})
        try:
            url = await self.preview.store_cover(repo, ref)
        except Exception as exc:
            return web.json_response({"ok": False, "url": "", "message": str(exc)})
        return web.json_response({"ok": bool(url), "url": url,
                                  "message": "" if url else "包里没翻到图，先用官方封面"})

    async def repo_inspect(self, request):
        if self.preview is None:
            return web.json_response({"ok": False, "message": "预览模块没装"}, status=500)
        url = str(request.query.get("url") or "").strip()
        if not url:
            return web.json_response({"ok": False, "message": "给个仓库地址"}, status=400)
        try:
            data = await self.preview.inspect(url)
        except Exception as exc:  # PreviewError 或网络问题
            return web.json_response({"ok": False, "message": str(exc)}, status=200)
        data["exists_locally"] = bool(self.db.get_pack(data["repo"]))
        return web.json_response(data)

    async def repo_files(self, request):
        if self.preview is None:
            return web.json_response({"ok": False, "message": "预览模块没装"}, status=500)
        q = request.query
        url = str(q.get("url") or "").strip()
        group = str(q.get("group") or "")
        try:
            page = max(1, int(q.get("page", 1)))
            size = min(120, max(1, int(q.get("size", 60)))
                       ) if q.get("size") else 60
        except ValueError:
            page, size = 1, 60
        try:
            data = await self.preview.files(url, group=group, page=page, size=size)
        except Exception as exc:
            return web.json_response({"ok": False, "message": str(exc)}, status=200)
        return web.json_response({"ok": True, **data})

    async def repo_thumb(self, request):
        if self.preview is None:
            return web.json_response({"error": "预览模块没装"}, status=500)
        q = request.query
        try:
            data, ctype = await self.preview.thumb(
                str(q.get("platform") or ""), str(q.get("owner") or ""),
                str(q.get("repo") or ""), str(q.get("branch") or "main"),
                str(q.get("path") or ""),
            )
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=404)
        return web.Response(body=data, content_type=ctype.split("/")[-1],
                            headers={"Cache-Control": "max-age=600"})

    # ---------------- 缩略图加速 ----------------
    def _thumb_cfg(self) -> dict:
        f = self.root / "data" / "thumb.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_thumb_cfg(self, max_mb: int) -> None:
        f = self.root / "data" / "thumb.json"
        cfg = self._thumb_cfg()
        cfg["max_mb"] = max_mb
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    async def thumb_status(self, request):
        if self.preview is None:
            return web.json_response({"has_pil": False, "used_mb": 0,
                                      "max_mb": 128, "count": 0})
        return web.json_response(self.preview.thumb_stats())

    async def thumb_settings(self, request):
        if self.preview is None:
            return web.json_response({"error": "预览模块没装"}, status=500)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            mb = int(body.get("max_mb") or 0)
        except (TypeError, ValueError):
            mb = 0
        if not 8 <= mb <= 4096:
            return web.json_response({"error": "上限填 8 到 4096 之间"}, status=400)
        self.preview.set_thumb_max(mb)
        self._save_thumb_cfg(mb)
        return web.json_response({"ok": True, **self.preview.thumb_stats()})

    async def deps_install(self, request):
        """一键把 Pillow 装进当前宿主解释器，装完即时生效，不用重启。"""
        job_id = "pillow"
        cur = self.jobs.get(job_id)
        if cur and cur.get("status") == "running":
            return web.json_response({"id": job_id, "status": "running"})
        self.jobs[job_id] = {"status": "running", "log": "", "ts": time.time()}

        async def run():
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install",
                    "--disable-pip-version-check", "--no-warn-script-location",
                    "-i", "https://pypi.tuna.tsinghua.edu.cn/simple", "pillow",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await proc.communicate()
                ok = proc.returncode == 0
            except Exception as exc:
                out, ok = str(exc).encode("utf-8"), False
            from meme_core import preview as pv
            pv.refresh_pil()
            self.jobs[job_id].update({
                "status": "done" if ok else "fail",
                "log": out.decode("utf-8", "replace")[-600:],
                "has_pil": pv.HAS_PIL,
            })

        asyncio.create_task(run())
        return web.json_response({"id": job_id, "status": "running"})

    async def deps_status(self, request):
        job_id = request.query.get("id") or "pillow"
        job = self.jobs.get(job_id)
        if not job:
            return web.json_response({"status": "none", "log": ""})
        data = {k: v for k, v in job.items() if k != "ts"}
        if self.preview is not None:
            data["has_pil"] = self.preview.thumb_stats()["has_pil"]
        return web.json_response(data)

    async def tag_counts(self, request):
        """各标准分类的实际图片数（不限审核状态），供管理页左侧分类栏用。"""
        pack_id = request.query.get("pack_id")
        pack_id = int(pack_id) if pack_id else None
        counts = {t: self.db.count_images(pack_id=pack_id, tag=t) for t in STANDARD_TAGS}
        counts["uncategorized"] = self.db.count_images(
            pack_id=pack_id, review_status="uncategorized")
        counts["__all__"] = self.db.count_images(pack_id=pack_id)
        return web.json_response(counts)

    async def images(self, request):
        q = request.query
        page = max(1, int(q.get("page", 1)))
        size = min(120, max(1, int(q.get("size", 60))))
        pack_id = q.get("pack_id")
        pack_id = int(pack_id) if pack_id else None
        status = q.get("status") or None
        tag = q.get("tag") or None
        rows = self.db.list_images(
            pack_id=pack_id, review_status=status, tag=tag,
            limit=size, offset=(page - 1) * size,
        )
        total = self.db.count_images(pack_id=pack_id, review_status=status, tag=tag)
        for r in rows:
            r["tags"] = self.db.get_tags(r["id"])
        return web.json_response({"items": rows, "total": total,
                                  "page": page, "size": size})

    async def img_file(self, request):
        image_id = int(request.query.get("id", 0))
        img = self.db.get_image(image_id)
        if not img:
            return web.json_response({"error": "not found"}, status=404)
        target = (self.root / img["file_path"]).resolve()
        memes_root = (self.root / "memes").resolve()
        if memes_root not in target.parents or not target.exists():
            return web.json_response({"error": "forbidden"}, status=403)
        return web.FileResponse(target)

    async def tags_save(self, request):
        body = await request.json()
        image_id = int(body.get("id"))
        tags = [t for t in (body.get("tags") or []) if t in STANDARD_TAGS]
        self.db.set_tags(image_id, tags)
        return web.json_response({"id": image_id, "tags": tags})

    async def tags_batch(self, request):
        """批量打标：mode=set 覆盖，mode=add 追加（一图多标签）。"""
        body = await request.json()
        ids = [int(i) for i in (body.get("ids") or [])]
        tags = [t for t in (body.get("tags") or []) if t in STANDARD_TAGS]
        mode = body.get("mode", "add")
        changed = 0
        for image_id in ids:
            if not self.db.get_image(image_id):
                continue
            if mode == "set":
                self.db.set_tags(image_id, tags)
            else:
                origin = set(self.db.get_tags(image_id))
                self.db.set_tags(image_id, sorted(origin | set(tags)))
            changed += 1
        return web.json_response({"ok": True, "count": changed, "tags": tags})

    async def images_delete(self, request):
        """删图：先进回收站，库里记录照旧删掉，文件挪到 data/trash。"""
        body = await request.json()
        ids = [int(i) for i in (body.get("ids") or [])]
        memes_root = (self.root / "memes").resolve()
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        removed = 0
        for image_id in ids:
            img = self.db.get_image(image_id)
            if not img:
                continue
            target = (self.root / img["file_path"]).resolve()
            if memes_root not in target.parents or not target.exists():
                self.db.delete_image(image_id)
                removed += 1
                continue

            pack = self.db.get_pack_by_id(img["pack_id"]) if img.get("pack_id") else None
            stamp = time.strftime("%Y%m%d%H%M%S")
            tname = "%s_%05d_%s" % (stamp, image_id, target.name)
            tpath = self.trash_dir / tname
            try:
                shutil.move(str(target), str(tpath))
            except OSError:
                self.db.delete_image(image_id)
                removed += 1
                continue

            tags = ",".join(self.db.get_tags(image_id))
            pack_name = (pack.get("display_name") or pack.get("name")) if pack else "已删除的包"
            self.db.add_trash(
                img["pack_id"], pack_name, img["file_path"], img.get("file_name"),
                "data/trash/" + tname, tags, img.get("review_status"),
                time.strftime("%Y-%m-%d %H:%M"),
            )
            self.db.delete_image(image_id)
            removed += 1

        self._prune_trash()
        return web.json_response({"ok": True, "count": removed,
                                  "trash_total": len(self.db.list_trash())})

    # ---------------- 回收站 ----------------
    def _prune_trash(self):
        """超过上限就从最旧的开始清干净。"""
        rows = self.db.list_trash()
        for r in rows[self.trash_max:]:
            try:
                p = (self.root / r["trash_path"]).resolve()
                if p.exists():
                    p.unlink()
            except OSError:
                pass
            self.db.drop_trash(r["id"])

    async def trash_list(self, request):
        rows = self.db.list_trash()
        items = [
            {
                "id": r["id"],
                "file_name": r.get("file_name") or "",
                "pack_name": r.get("pack_name") or "",
                "deleted_at": r.get("deleted_at") or "",
                "review_status": r.get("review_status") or "",
                "tags": [t for t in (r.get("tags") or "").split(",") if t],
            }
            for r in rows
        ]
        return web.json_response({"ok": True, "total": len(items),
                                  "max": self.trash_max, "items": items})

    async def trash_img(self, request):
        tid = int(request.query.get("id", 0))
        row = self.db.get_trash(tid)
        if not row:
            return web.json_response({"error": "not found"}, status=404)
        target = (self.root / row["trash_path"]).resolve()
        if self.trash_dir.resolve() not in target.parents or not target.exists():
            return web.json_response({"error": "forbidden"}, status=403)
        return web.FileResponse(target)

    async def trash_restore(self, request):
        """复原：文件搬回原路径，库里重新加一条，标签也一起还回来。"""
        body = await request.json()
        ids = [int(i) for i in (body.get("ids") or [])]
        memes_root = (self.root / "memes").resolve()
        restored = 0
        skipped = 0
        for tid in ids:
            row = self.db.get_trash(tid)
            if not row:
                skipped += 1
                continue
            src = (self.root / row["trash_path"]).resolve()
            dst = (self.root / row["file_path"]).resolve()
            pack = self.db.get_pack_by_id(row["pack_id"]) if row.get("pack_id") else None
            if not pack or memes_root not in dst.parents:
                skipped += 1      # 原包已经删了，图没法还回去
                continue
            if not src.exists():
                self.db.drop_trash(tid)
                skipped += 1
                continue
            if dst.exists():      # 同名占着位置，加个时间戳
                dst = dst.with_name("%d_%s" % (int(time.time()), dst.name))
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            except OSError:
                skipped += 1
                continue

            rel = str(dst.relative_to(self.root)).replace("\\", "/")
            new_id = self.db.add_image(
                row["pack_id"], rel, row.get("file_name") or "",
                row.get("review_status") or "uncategorized",
            )
            tags = [t for t in (row.get("tags") or "").split(",") if t]
            if new_id and tags:
                self.db.set_tags(new_id, tags)
            self.db.drop_trash(tid)
            restored += 1
        return web.json_response({"ok": True, "count": restored, "skipped": skipped})

    async def trash_purge(self, request):
        """彻底删：给了 ids 就删这几张，不给就清空。"""
        body = await request.json()
        ids = [int(i) for i in (body.get("ids") or [])]
        if ids:
            rows = [self.db.get_trash(i) for i in ids]
        else:
            rows = self.db.list_trash()
        purged = 0
        for r in rows:
            if not r:
                continue
            try:
                p = (self.root / r["trash_path"]).resolve()
                if p.exists():
                    p.unlink()
            except OSError:
                pass
            self.db.drop_trash(r["id"])
            purged += 1
        return web.json_response({"ok": True, "count": purged})

    async def review_confirm(self, request):
        body = await request.json()
        self.db.confirm_image(int(body.get("id")))
        return web.json_response({"ok": True})

    async def review_batch(self, request):
        body = await request.json()
        status = body.get("status", REVIEW_PENDING_HIGH)
        if status not in (REVIEW_PENDING_HIGH, REVIEW_PENDING_LOW):
            status = REVIEW_PENDING_HIGH
        n = self.db.batch_confirm(status)
        return web.json_response({"ok": True, "count": n})

    async def review_revert(self, request):
        body = await request.json()
        to = body.get("to", REVIEW_PENDING_HIGH)
        if to not in (REVIEW_PENDING_HIGH, REVIEW_PENDING_LOW, "uncategorized"):
            to = REVIEW_PENDING_HIGH
        self.db.revert_image(int(body.get("id")), to)
        return web.json_response({"ok": True})

    async def moods_list(self, request):
        from meme_core.constants import TAG_ALIASES
        rows = self.db.list_moods()
        return web.json_response({
            "custom": {r["mood_word"]: r["tag"] for r in rows},
            "builtin": TAG_ALIASES,
        })

    async def moods_save(self, request):
        body = await request.json()
        mappings = body.get("mappings") or {}
        clean = {str(k): str(v) for k, v in mappings.items() if v in STANDARD_TAGS}
        self.db.bulk_upsert_moods(clean)
        return web.json_response({"ok": True, "moods": self.db.list_moods()})

    async def mood_delete(self, request):
        body = await request.json()
        word = str(body.get("word") or "")
        if word:
            self.db.delete_mood(word)
        return web.json_response({"ok": True})

    async def download(self, request):
        """下载改为后台任务：立刻返回任务号，前端轮询进度。"""
        body = await request.json()
        url = str(body.get("url") or "").strip()
        pack_type = body.get("type", PACK_EMOTION)
        if pack_type not in (PACK_EMOTION, PACK_MEME):
            pack_type = PACK_EMOTION
        if not url:
            return web.json_response({"ok": False, "message": "缺少仓库地址"}, status=400)

        # 清理一小时前的旧任务
        now = time.time()
        for jid in [k for k, v in self.jobs.items() if now - v["created"] > 3600]:
            self.jobs.pop(jid, None)

        job_id = uuid.uuid4().hex[:10]
        job = {
            "id": job_id, "url": url, "type": pack_type, "created": now,
            "ref": str(body.get("ref") or "").strip(),
            "title": str(body.get("title") or "").strip(),
            "stage": "queued", "bytes": 0, "total": 0, "done_items": 0,
            "done": False, "ok": None, "cancel": False, "finished": None,
            "pack_id": None, "pack_name": "",
            "message": "准备中……",
        }
        self.jobs[job_id] = job
        self._save_job(job)
        asyncio.create_task(self._run_download(job))
        return web.json_response({"ok": True, "task_id": job_id,
                                  "message": "已开始下载，稍等。"})

    def _save_job(self, job: dict) -> None:
        """任务状态落库，刷新或重启后「下载」页还能看到记录。"""
        try:
            self.db.save_download({
                "id": job["id"], "url": job["url"], "ref": job.get("ref") or "",
                "title": job.get("title") or "", "pack_type": job["type"],
                "created": job["created"], "finished": job.get("finished"),
                "stage": job.get("stage"), "bytes": job.get("bytes") or 0,
                "total": job.get("total") or 0,
                "done_items": job.get("done_items") or 0,
                "done": job.get("done"), "ok": job.get("ok"),
                "cancel": job.get("cancel"), "pack_id": job.get("pack_id"),
                "pack_name": job.get("pack_name") or "",
                "message": job.get("message") or "",
            })
        except Exception:
            pass

    def _job_public(self, job: dict) -> dict:
        return {
            "id": job["id"], "title": job.get("title") or "",
            "url": job.get("url") or "", "type": job.get("type") or "",
            "created": job.get("created") or 0,
            "finished": job.get("finished"),
            "stage": job.get("stage") or "", "bytes": job.get("bytes") or 0,
            "total": job.get("total") or 0,
            "done_items": job.get("done_items") or 0,
            "done": bool(job.get("done")), "ok": job.get("ok"),
            "cancel": bool(job.get("cancel")),
            "pack_id": job.get("pack_id"),
            "pack_name": job.get("pack_name") or "",
            "message": job.get("message") or "",
        }

    async def _run_download(self, job: dict) -> None:
        last_save = [0.0]

        def progress(info: dict) -> None:
            job["stage"] = info.get("stage", job["stage"])
            job["bytes"] = info.get("bytes", job["bytes"])
            if info.get("total") is not None:
                job["total"] = info["total"]
            if info.get("done") is not None:
                job["done_items"] = info["done"]
            if time.time() - last_save[0] > 3:
                last_save[0] = time.time()
                self._save_job(job)

        try:
            result = await self.downloader.download(
                job["url"], job["type"], progress=progress,
                ref=job.get("ref") or None,
                stop=lambda: bool(job.get("cancel")))
            job["done"] = True
            job["ok"] = bool(result.get("ok"))
            job["message"] = result.get("message", "")
            job["result"] = result
            job["pack_id"] = result.get("pack_id")
            job["pack_name"] = result.get("name") or ""
            job["finished"] = time.time()
            if result.get("cancelled"):
                job["cancel"] = True
        except Exception as exc:  # 任何异常都要落到任务状态里
            job["done"] = True
            job["ok"] = False
            job["message"] = f"下载异常：{exc}"
            job["finished"] = time.time()
        self._save_job(job)

    async def download_cancel(self, request):
        body = await request.json()
        jid = str(body.get("id") or "")
        job = self.jobs.get(jid)
        if not job:
            return web.json_response({"ok": False, "message": "任务不存在"}, status=404)
        if job.get("done"):
            return web.json_response({"ok": False, "message": "这个任务已经结束了"})
        job["cancel"] = True
        job["message"] = "正在取消……"
        self._save_job(job)
        return web.json_response({"ok": True})

    async def download_delete(self, request):
        """删记录。还在跑的不让删，先取消，免得有任务没记录。"""
        body = await request.json()
        ids = [str(i) for i in (body.get("ids") or [])]
        if not ids:
            return web.json_response({"ok": False, "message": "没给要删的记录"})
        removed = 0
        skipped = 0
        for jid in ids:
            job = self.jobs.get(jid)
            if job and not job.get("done"):
                skipped += 1
                continue
            self.jobs.pop(jid, None)
            removed += self.db.drop_download(jid)
        return web.json_response({"ok": True, "count": removed, "skipped": skipped})

    async def download_clear(self, request):
        """清空记录：只清结束了的，正在下的留着。"""
        for jid, job in list(self.jobs.items()):
            if job.get("done"):
                self.jobs.pop(jid, None)
        n = self.db.clear_downloads()
        return web.json_response({"ok": True, "count": n,
                                  "live": sum(1 for j in self.jobs.values()
                                              if not j.get("done"))})

    async def download_tasks(self, request):
        """下载页数据：内存里的进行中 + 库里的历史，合成一份。"""
        merged: dict[str, dict] = {}
        try:
            for row in self.db.list_downloads(100):
                merged[row["id"]] = {
                    "id": row["id"], "title": row.get("title") or "",
                    "url": row.get("url") or "", "type": row.get("pack_type") or "",
                    "created": row.get("created") or 0,
                    "finished": row.get("finished"),
                    "stage": row.get("stage") or "",
                    "bytes": row.get("bytes") or 0,
                    "total": row.get("total") or 0,
                    "done_items": row.get("done_items") or 0,
                    "done": bool(row.get("done")),
                    "ok": None if row.get("ok") is None else bool(row.get("ok")),
                    "cancel": bool(row.get("cancel")),
                    "pack_id": row.get("pack_id"),
                    "pack_name": row.get("pack_name") or "",
                    "message": row.get("message") or "",
                }
        except Exception:
            pass
        for job in self.jobs.values():
            merged[job["id"]] = self._job_public(job)

        items = sorted(merged.values(),
                       key=lambda x: x.get("created") or 0, reverse=True)[:60]
        return web.json_response({"ok": True, "items": items,
                                  "live": sum(1 for i in items if not i["done"])})

    async def download_status(self, request):
        job = self.jobs.get(str(request.query.get("id") or ""))
        if not job:
            return web.json_response({"ok": False, "message": "任务不存在"}, status=404)
        return web.json_response({
            "ok": True, "id": job["id"], "stage": job["stage"],
            "bytes": job["bytes"], "total": job.get("total") or 0,
            "done_items": job.get("done_items") or 0,
            "done": job["done"], "success": job["ok"],
            "message": job["message"],
            "result": job.get("result"),
        })

    async def run_local(self, request):
        return web.json_response(self.tagging.local_inference())

    async def run_ai(self, request):
        body = await request.json()
        limit = int(body.get("limit", 50))
        result = await self.tagging.ai_tag(self.vision, limit=limit)
        return web.json_response(result)
