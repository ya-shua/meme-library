"""远程仓库预览：只读远程文件树 + 图片代理，不落盘。

大厅「先看后下」就靠这个模块：
  - inspect(): 拉远程文件树，给出简介、是否自带标签、目录分组、图片总数
  - files():   按分组/分页列出图片，缩略图走本地代理 URL
  - thumb():   服务端转发远程图片，结果只进内存 LRU，绝不写盘

预览阶段不占本地空间，也不改动本地库。
"""

from __future__ import annotations

import base64
import asyncio
import hashlib
import io
import json
import re
import time
from collections import OrderedDict
from urllib.parse import quote

import aiohttp

from .constants import IMAGE_SUFFIXES, NON_MEME_DIRS, REPO_SOURCES, SIZE_DIRS, TAG_ALIASES
from .downloader import parse_repo_url

# Pillow 是可选依赖：装了就能把原图压成小缩略图（快几十倍），没装就退回直传原图。
try:
    from PIL import Image as _PILImage
    HAS_PIL = True
except Exception:  # pragma: no cover - 单纯没装
    _PILImage = None
    HAS_PIL = False

Image = _PILImage

TREE_TTL = 600
THUMB_MAX_ITEMS = 1200
THUMB_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_THUMB_MB = 128
THUMB_SIDE = 420
THUMB_QUALITY = 82
ROOT_GROUP = "（根目录）"


def refresh_pil() -> bool:
    """一键装完 Pillow 之后不用重启，直接重新探测一次。"""
    global Image, HAS_PIL
    try:
        from PIL import Image as img
        Image = img
        HAS_PIL = True
    except Exception:
        HAS_PIL = False
    return HAS_PIL


def _resample():
    try:
        return Image.Resampling.LANCZOS
    except AttributeError:  # 老版本 Pillow
        return Image.LANCZOS

# 社区索引；本机 raw 不通，所以首选 jsDelivr 的 gcore 节点
INDEX_TTL = 3600
COVER_TTL = 86400  # 封面一天一换

# 能断点续传的 GitHub 镜像：只读 zip 尾部的中央目录，就能拿到整包文件清单，
# 完全不碰 api.github.com，所以既不吃限流、也不受 50MB 限制。
ZIP_MIRRORS = ("https://gh-proxy.com", "https://cdn.gh-proxy.com")
INDEX_URLS = (
    "https://gcore.jsdelivr.net/gh/anka-afk/astrbot-meme-pack-index@main/community-index.json",
    "https://raw.githubusercontent.com/anka-afk/astrbot-meme-pack-index/main/community-index.json",
)

_RAW_RE = re.compile(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)")


def _cdn_url(url: str) -> str:
    """把 raw.githubusercontent 的地址换成 gcore.jsdelivr，直连不通的机器也能出图。"""
    m = _RAW_RE.match(url)
    if not m:
        return url
    owner, repo, ref, path = m.groups()
    return f"https://gcore.jsdelivr.net/gh/{owner}/{repo}@{quote(ref)}/{quote(path)}"


def _judge_meme_pack(entries: list, images: list, native: list) -> dict:
    """判断这个仓库到底是不是表情包仓库，给前端一个明确的结论。"""
    blobs = [e for e in entries if e.get("type") == "blob"]
    file_total = len(blobs)
    image_total = len(images)

    def _is_native(path: str) -> bool:
        segs = path.split("/")[:-1]
        return not any(s.lower() in NON_MEME_DIRS for s in segs)

    core = [i for i in images if _is_native(i["path"])]
    image_core = len(core)
    tag_dirs = len(native)
    ratio = image_core / file_total if file_total else 0
    has_manifest = any(
        str(e.get("path", "")).rsplit("/", 1)[-1].lower() == "manifest.json" for e in blobs
    )

    stats = {
        "file_total": file_total,
        "image_total": image_total,
        "image_core": image_core,
        "tag_dirs": tag_dirs,
    }

    if image_total < 5:
        reason = (f"翻了 {file_total} 个文件，只有 {image_total} 张图"
                  if image_total else f"翻了 {file_total} 个文件，一张图都没有，是代码或文档仓库")
        return {**stats, "is_meme_pack": False,
                "verdict": f"看起来不是表情包仓库：{reason}，撑不起一个表情包"}

    if has_manifest and image_core >= 5:
        return {**stats, "is_meme_pack": True,
                "verdict": "带包内索引，这是成套整理好的表情包"}
    if tag_dirs >= 2 and image_core >= 5:
        return {**stats, "is_meme_pack": True,
                "verdict": f"识别到 {tag_dirs} 个表情分类目录"}
    if image_core >= 12 and ratio >= 0.3:
        return {**stats, "is_meme_pack": True,
                "verdict": f"{image_core} 张表情图，占全部文件的 {ratio:.0%}"}
    if image_total >= 100:
        return {**stats, "is_meme_pack": True,
                "verdict": f"{image_total} 张图，图量够一个表情包库了"}

    if image_total < 12:
        reason = f"翻了 {file_total} 个文件，只有 {image_total} 张图，太少，不像表情包"
    else:
        reason = (f"{image_total} 张图只占全部文件的 {ratio:.0%}，"
                  f"也没有分类目录或包内索引，更像普通图片/插画仓库")
    return {**stats, "is_meme_pack": False,
            "verdict": f"看起来不是表情包仓库：{reason}"}


class PreviewError(Exception):
    """预览失败，message 直接给前端看。"""


_SRC_WORD = re.compile(r"astrbot\s*", re.I)


def _plain(text: str) -> str:
    """对外文案只用我们自己的说法，别人的产品名一律不往界面上带。"""
    return _SRC_WORD.sub("", str(text or "")).strip()


def _pretty_name(repo: str) -> str:
    """仓库标识转成能看的名字：去掉前缀和打包后缀，不露别人的产品名。"""
    name = _plain(repo)
    name = re.sub(r"[-_]meme[-_]?pack", "", name, flags=re.I)
    name = re.sub(r"^[-_]+|[-_]+$", "", name)
    name = re.sub(r"[-_]+", " ", name).strip()
    return f"{name} 表情包" if name else "这个表情包"


def _alias_tag(name: str):
    key = name.lower().strip().replace(" ", "")
    for tag, aliases in TAG_ALIASES.items():
        if key in aliases:
            return tag
    return None


class RepoPreview:
    def __init__(self, github_token: str = "", db=None, cache_dir=None,
                 thumb_dir=None, thumb_max_mb: int = DEFAULT_THUMB_MB):
        self.github_token = (github_token or "").strip()
        self.db = db
        self.cache_dir = cache_dir
        self.thumb_dir = thumb_dir
        self.thumb_max_mb = max(8, int(thumb_max_mb or DEFAULT_THUMB_MB))
        self._thumb_writes = 0
        self._cache: dict[str, tuple[float, dict, dict, list]] = {}
        self._thumbs: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
        self._thumb_bytes = 0
        # 限速链路上并发拉原图会互相拖死，这里只放 6 张同时走
        self._thumb_gate = asyncio.Semaphore(6)
        self._index_cache: tuple[float, dict] | None = None
        self._cover_cache: dict[str, tuple[float, str]] = {}
        self._tree_source = ""  # jsdelivr / api / zip

    # ---------------- 缩略图磁盘缓存 ----------------
    def _thumb_path(self, key: str):
        if not self.thumb_dir:
            return None
        digest = hashlib.md5(key.encode("utf-8")).hexdigest()
        return self.thumb_dir / f"{digest}.jpg"

    def _remember_thumb(self, key: str, data: bytes, ctype: str) -> None:
        self._thumbs[key] = (data, ctype)
        self._thumb_bytes += len(data)
        while self._thumbs and (len(self._thumbs) > THUMB_MAX_ITEMS
                                or self._thumb_bytes > THUMB_MAX_BYTES):
            _, (old, _) = self._thumbs.popitem(last=False)
            self._thumb_bytes -= len(old)

    def _prune_thumbs(self) -> None:
        """磁盘缓存超过上限就按最久没动过的先删。"""
        if not self.thumb_dir or not self.thumb_dir.exists():
            return
        rows, total = [], 0
        for f in self.thumb_dir.glob("*.jpg"):
            try:
                st = f.stat()
            except OSError:
                continue
            rows.append((st.st_mtime, st.st_size, f))
            total += st.st_size
        limit = self.thumb_max_mb * 1024 * 1024
        if total <= limit:
            return
        rows.sort()
        for _, size, f in rows:
            if total <= limit:
                break
            try:
                f.unlink()
            except OSError:
                continue
            total -= size

    @staticmethod
    def _shrink(data: bytes) -> bytes:
        """把原图缩成小缩略图（最长边 420、JPEG 82）。动图只取第一帧。"""
        im = Image.open(io.BytesIO(data))
        try:
            if getattr(im, "is_animated", False):
                im.seek(0)
        except Exception:
            pass
        im.load()
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((THUMB_SIDE, THUMB_SIDE), _resample())
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=THUMB_QUALITY, optimize=True, progressive=True)
        return buf.getvalue()

    def thumb_stats(self) -> dict:
        used, count = 0, 0
        if self.thumb_dir and self.thumb_dir.exists():
            for f in self.thumb_dir.glob("*.jpg"):
                try:
                    used += f.stat().st_size
                    count += 1
                except OSError:
                    continue
        return {"has_pil": HAS_PIL, "used_mb": round(used / 1048576, 1),
                "max_mb": self.thumb_max_mb, "count": count}

    def set_thumb_max(self, mb: int) -> None:
        self.thumb_max_mb = max(8, int(mb))
        self._prune_thumbs()

    # ---------------- 文件清单磁盘缓存 ----------------
    # GitHub 没登录一小时只给 60 次，一个包只查一次，之后 24 小时都吃本地缓存。
    def _cache_file(self, info: dict):
        if not self.cache_dir:
            return None
        safe = re.sub(r"[^\w.-]+", "_",
                      f"{info['platform']}_{info['owner']}_{info['repo']}_{info['branch'] or 'auto'}")
        return self.cache_dir / f"{safe}.json"

    def _read_tree_cache(self, info: dict):
        f = self._cache_file(info)
        if not f or not f.exists():
            return None
        try:
            if time.time() - f.stat().st_mtime > 86400:
                return None
            data = json.loads(f.read_text(encoding="utf-8"))
            entries = data.get("entries") or []
            branch = str(data.get("branch") or "")
            if entries and branch:
                self._tree_source = "disk"
                return branch, entries, bool(data.get("truncated"))
        except (OSError, ValueError):
            return None
        return None

    def _cached_tree(self, repo: str):
        """按仓库名扫磁盘缓存（不看 branch 后缀），省掉一次大包的清单请求。"""
        if not self.cache_dir:
            return None
        owner, _, name = repo.partition("/")
        safe_o = re.sub(r"[^\w.-]+", "_", owner)
        safe_n = re.sub(r"[^\w.-]+", "_", name)
        for f in self.cache_dir.glob(f"github_{safe_o}_{safe_n}_*.json"):
            try:
                if time.time() - f.stat().st_mtime > 86400:
                    continue
                data = json.loads(f.read_text(encoding="utf-8"))
                entries = data.get("entries") or []
                if entries:
                    return str(data.get("branch") or ""), entries
            except (OSError, ValueError):
                continue
        return None

    def _write_tree_cache(self, info: dict, branch: str, entries: list):
        f = self._cache_file(info)
        if not f:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"branch": branch, "entries": entries,
                                     "saved": time.time()}, ensure_ascii=False),
                         encoding="utf-8")
        except OSError:
            pass

    # ---------------- 基础 ----------------
    def _headers(self) -> dict:
        h = {"User-Agent": "meme-library/1.0"}
        if self.github_token:
            h["Authorization"] = f"Bearer {self.github_token}"
        return h

    @staticmethod
    def _timeout(sec: int = 30) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=sec)

    async def _json(self, session, url: str):
        async with session.get(url, headers=self._headers()) as resp:
            text = await resp.text()
            if resp.status == 403:
                raise PreviewError(
                    "GitHub 接口被限流了（没登录每小时只有 60 次）。"
                    "可以等一小时，或者在插件 config.json 填 preview.github_token 再重启；"
                    "也可以直接点「收录」，那条路走 zip 下载，不受这个限流影响。"
                )
            if resp.status == 404:
                raise PreviewError("GitHub 上找不到这个仓库，确认下地址对不对、仓库是不是公开的")
            if resp.status != 200:
                raise PreviewError(f"仓库接口返回 HTTP {resp.status}：{text[:140]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise PreviewError("仓库接口返回的不是 JSON") from exc

    @staticmethod
    def _key(info: dict) -> str:
        return f"{info['platform']}|{info['owner']}|{info['repo']}|{info['branch'] or ''}"

    # ---------------- 仓库树：zip 中央目录（免 API 额度、任意大小） --------------
    @staticmethod
    def _cr_total(value: str) -> int:
        try:
            return int(str(value).rsplit("/", 1)[-1])
        except (ValueError, AttributeError, IndexError):
            return 0

    async def _tree_from_zip(self, session, repo: str, refs: list):
        """Range 读 zip 末尾 → 解出中央目录 → 得到所有文件路径。
        只在一个镜像上试；镜像不认 Range（返回整包 200）就立刻放弃，绝不能把几百 MB 拖下来。"""
        short = aiohttp.ClientTimeout(total=12, sock_connect=6)
        for one in refs:
            for kind in ("heads", "tags"):
                url = (f"{ZIP_MIRRORS[0]}/https://github.com/{repo}"
                       f"/archive/refs/{kind}/{quote(one)}.zip")
                try:
                    async with session.get(url, headers={"Range": "bytes=-400000"},
                                           timeout=short) as resp:
                        if resp.status != 206:
                            resp.close()   # 不读 body，直接断
                            continue
                        tail = await resp.read()
                        total = self._cr_total(resp.headers.get("Content-Range"))
                        if not total or total < len(tail):
                            continue
                    entries = await self._central_dir(session, url, tail, total)
                    if entries:
                        return entries
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    continue
        return None

    async def _central_dir(self, session, url: str, tail: bytes, total: int):
        i = tail.rfind(b"PK\x05\x06")
        if i < 0:
            return None
        cd_size = int.from_bytes(tail[i + 12:i + 16], "little")
        cd_off = int.from_bytes(tail[i + 16:i + 20], "little")
        if cd_size <= 0 or cd_off <= 0 or cd_off + cd_size > total:
            return None
        base = total - len(tail)
        if cd_off >= base:
            blob = tail[cd_off - base: cd_off - base + cd_size]
        else:
            try:
                async with session.get(url, headers={
                    "Range": f"bytes={cd_off}-{cd_off + cd_size - 1}"
                }) as resp:
                    if resp.status != 206:
                        return None
                    blob = await resp.read()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return None
        if len(blob) < cd_size:
            return None
        out = []
        pos = 0
        while pos + 46 <= len(blob):
            if blob[pos:pos + 4] != b"PK\x01\x02":
                break
            raw_size = int.from_bytes(blob[pos + 24:pos + 28], "little")
            name_len = int.from_bytes(blob[pos + 28:pos + 30], "little")
            extra_len = int.from_bytes(blob[pos + 30:pos + 32], "little")
            comment_len = int.from_bytes(blob[pos + 32:pos + 34], "little")
            name = blob[pos + 46: pos + 46 + name_len].decode("utf-8", "ignore")
            pos += 46 + name_len + extra_len + comment_len
            if not name or name.endswith("/"):
                continue
            # GitHub 打的包外面套了一层 "<repo>-<version>/"
            path = name.split("/", 1)[1] if "/" in name else name
            if path:
                out.append({"type": "blob", "path": path, "size": raw_size})
        return out or None

    # ---------------- 仓库树与 README ----------------
    async def _tree_jsdelivr(self, session, repo: str, ref: str):
        """jsDelivr 的文件树接口：不占 GitHub 额度，小包（<50MB）秒回。
        返回 (树, 失败原因)，失败原因 None / "oversize" / "other"。"""
        url = f"https://data.jsdelivr.com/v1/packages/gh/{repo}@{quote(ref)}"
        try:
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status == 403:
                    return None, "oversize"
                if resp.status != 200:
                    return None, "other"
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, json.JSONDecodeError, asyncio.TimeoutError, ValueError):
            return None, "other"

        out: list = []

        def walk(nodes, prefix):
            for n in nodes or []:
                name = str(n.get("name") or "")
                if not name:
                    continue
                path = f"{prefix}{name}"
                if n.get("type") == "directory":
                    walk(n.get("files"), path + "/")
                else:
                    out.append({"type": "blob", "path": path,
                                "size": n.get("size") or 0})

        walk(data.get("files"), "")
        return (out or None), None

    async def _branch_and_tree(self, session, info: dict):
        p, o, r = info["platform"], info["owner"], info["repo"]
        branch = info["branch"]
        if p == "github":
            refs = [branch] if branch else ["main", "master"]
            oversize = False
            for ref in refs:
                tree, why = await self._tree_jsdelivr(session, f"{o}/{r}", ref)
                if tree:
                    self._tree_source = "jsdelivr"
                    return ref, tree, False
                if why == "oversize":
                    oversize = True
            # 大包（>50MB）镜像的 Range 也不稳，直接去 API，别在这儿耗几十秒
            if not oversize:
                tree = await self._tree_from_zip(session, f"{o}/{r}", refs)
                if tree:
                    self._tree_source = "zip"
                    return refs[0], tree, False
        # 最后才动 GitHub 接口：一个包只发一次 tree 请求，别再额外查默认分支
        last = None
        for ref in ([branch] if branch else ["main", "master"]):
            tree_api = (f"https://api.github.com/repos/{o}/{r}/git/trees/{quote(ref)}?recursive=1"
                        if p == "github" else
                        f"https://gitee.com/api/v5/repos/{o}/{r}/git/trees/{quote(ref)}?recursive=1")
            try:
                data = await self._json(session, tree_api)
            except PreviewError as exc:
                last = exc
                if "限流" in str(exc):
                    raise
                continue
            self._tree_source = "api"
            entries = data.get("tree") or []
            if entries:
                return ref, entries, bool(data.get("truncated"))
        if last:
            raise last
        raise PreviewError("仓库里好像没有内容，确认下地址和分支")

    async def _readme(self, session, info: dict, branch: str) -> str:
        p, o, r = info["platform"], info["owner"], info["repo"]
        text = ""
        if p == "github" and self._tree_source in ("jsdelivr", "zip"):
            # 走 CDN 直取 README，别为了简介再烧一次 API 额度
            for name in ("README.md", "readme.md", "Readme.md", "README_zh.md"):
                try:
                    async with session.get(
                        f"https://gcore.jsdelivr.net/gh/{o}/{r}@{quote(branch)}/{name}"
                    ) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            break
                except aiohttp.ClientError:
                    continue
            if text:
                return self._clean_readme(text)
        try:
            if p == "github":
                data = await self._json(session, f"https://api.github.com/repos/{o}/{r}/readme")
                text = base64.b64decode(data.get("content") or "").decode("utf-8", errors="ignore")
            else:
                listing = await self._json(
                    session, f"https://gitee.com/api/v5/repos/{o}/{r}/contents/?ref={quote(branch)}")
                target = next((x for x in listing
                               if str(x.get("name", "")).lower().startswith("readme")), None)
                if target and target.get("content"):
                    text = base64.b64decode(target["content"]).decode("utf-8", errors="ignore")
        except (PreviewError, ValueError, TypeError, KeyError):
            return ""
        return self._clean_readme(text)

    @staticmethod
    def _clean_readme(text: str) -> str:
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"[#>*`~]{1,3}", "", text)
        lines = [ln.strip().lstrip("-").strip() for ln in text.splitlines() if ln.strip()]
        return " ".join(lines)[:300]

    # ---------------- 图片整理 ----------------
    @staticmethod
    def _collect(entries: list) -> dict:
        images = []
        for e in entries:
            if e.get("type") != "blob":
                continue
            path = str(e.get("path") or "")
            if not path.lower().endswith(IMAGE_SUFFIXES):
                continue
            parts = path.split("/")
            if any(seg.startswith(".") for seg in parts):
                continue
            # previews/assets/docs 这些是封面拼图、徽标，不是表情；入库也会跳，预览必须一致
            if parts[0].lower() in NON_MEME_DIRS:
                continue
            dirname = "/".join(parts[:-1])
            tag = None
            if dirname:
                for seg in dirname.split("/"):
                    tag = _alias_tag(seg)
                    if tag:
                        break
            images.append({"path": path, "name": parts[-1], "dir": dirname,
                           "tag": tag, "local_id": e.get("id")})
        # 有些仓库把同一批图存了大中小三份（large/ thumb/ media/），只留原图那份
        keep = {(i["tag"], i["name"]) for i in images
                if i["dir"].split("/")[0].lower() not in SIZE_DIRS}
        images = [i for i in images
                  if i["dir"].split("/")[0].lower() not in SIZE_DIRS
                  or (i["tag"], i["name"]) not in keep]
        groups: dict[str, int] = {}
        for img in images:
            g = img["tag"] or img["dir"] or ROOT_GROUP
            groups[g] = groups.get(g, 0) + 1
        return {"images": images, "groups": groups}

    # ---------------- 对外：社区表情包索引 ----------------
    async def index(self) -> dict:
        """拉社区索引 community-index.json，转成本机能打开的精简条目。"""
        if self._index_cache and time.time() - self._index_cache[0] < INDEX_TTL:
            return self._index_cache[1]

        payload = None
        async with aiohttp.ClientSession(timeout=self._timeout(30)) as session:
            for url in INDEX_URLS:
                try:
                    async with session.get(url, headers=self._headers()) as resp:
                        if resp.status != 200:
                            continue
                        payload = json.loads((await resp.text()).lstrip("\ufeff"))
                        break
                except (aiohttp.ClientError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
        if not payload:
            raise PreviewError("拉不到表情包索引（网络不通或索引换地址了）")

        items = []
        for p in payload.get("packs") or []:
            src = p.get("source") or {}
            repo = str(src.get("repo") or "").strip()
            if src.get("type") != "github" or "/" not in repo:
                continue
            ref = str(src.get("ref") or "main")
            cover = ""
            for raw in p.get("previews") or []:
                cover = _cdn_url(str(raw))
                if cover:
                    break
            items.append({
                "id": p.get("id") or repo,
                "name": p.get("name") or repo,
                "maintainer": p.get("maintainer") or repo.split("/")[0],
                "description": p.get("description") or "",
                "license": p.get("license") or "",
                "verified": bool(p.get("verified")),
                "tags": [str(t) for t in (p.get("tags") or [])][:6],
                "cover": self._local_cover(repo) or cover,
                "repo": repo,
                "ref": ref,
                "url": f"https://github.com/{repo}",
            })

        result = {"ok": True, "generated_at": payload.get("generated_at") or "",
                  "total": len(items), "items": items}
        self._index_cache = (time.time(), result)
        return result

    # ---------------- 对外：包封面（用包里真实的一张表情） ----------------
    def _local_cover(self, repo: str) -> str:
        """库里有这个包，就拿库里第一张图当封面（本地取，零网络）。"""
        if self.db is None:
            return ""
        try:
            pack = self.db.get_pack(repo.split("/")[-1])
            if not pack:
                return ""
            images = self.db.list_images(pack["id"], limit=1)
            if not images:
                return ""
            return f"/api/img?id={images[0]['id']}"
        except Exception:
            return ""

    BAD_COVER_WORDS = ("qr", "qrcode", "logo", "banner", "poster", "avatar",
                       "icon", "cover", "icon_", "favicon")

    @staticmethod
    def _is_bad_cover(p: str) -> bool:
        low = p.lower()
        stem = low.rsplit("/", 1)[-1]
        return any(w in stem for w in RepoPreview.BAD_COVER_WORDS)

    @staticmethod
    def _pick_cover(tree: list, repo: str, ref: str) -> str:
        fallback = ""
        for e in tree or []:
            if e.get("type") != "blob":
                continue
            p = str(e.get("path") or "")
            if not p.lower().endswith(IMAGE_SUFFIXES):
                continue
            if RepoPreview._is_bad_cover(p):
                continue  # 二维码、logo、封面拼图这类不是"一张表情"
            if p.split("/")[0].lower() in NON_MEME_DIRS:
                if not fallback:
                    fallback = p
                continue
            return f"https://gcore.jsdelivr.net/gh/{repo}@{quote(ref)}/{quote(p)}"
        if fallback:
            return f"https://gcore.jsdelivr.net/gh/{repo}@{quote(ref)}/{quote(fallback)}"
        return ""

    COVER_NAMES = ("previews/cover.jpg", "previews/cover.png", "previews/cover.jpeg",
                   "previews/cover.webp", "previews/cover.gif",
                   "cover.jpg", "cover.png")

    async def _probe_cover_file(self, session, repo: str, ref: str) -> str:
        """取不到文件清单的大包，直接探常见的封面文件名——单文件走 jsDelivr 不会被 50MB 限制拦。
        注意 jsDelivr 不认 HEAD，必须 GET；用 Range 只取首字节，拿到响应立刻关掉不读 body。"""
        for name in self.COVER_NAMES:
            url = f"https://gcore.jsdelivr.net/gh/{repo}@{quote(ref)}/{quote(name)}"
            try:
                async with session.get(url, headers={"Range": "bytes=0-0"},
                                       allow_redirects=True) as r:
                    if r.status in (200, 206, 416):
                        r.close()
                        return url
            except Exception:
                continue
        return ""

    @staticmethod
    def _first_meme_path(node) -> str:
        """在 manifest.json 里递归找第一个像图的表情文件路径，跳过封面类文件。"""
        if isinstance(node, str):
            low = node.lower()
            if low.endswith(IMAGE_SUFFIXES) and "/" in node \
                    and not low.startswith("previews/") \
                    and not RepoPreview._is_bad_cover(node):
                return node
            return ""
        if isinstance(node, dict):
            for v in node.values():
                p = RepoPreview._first_meme_path(v)
                if p:
                    return p
            return ""
        if isinstance(node, list):
            for v in node:
                p = RepoPreview._first_meme_path(v)
                if p:
                    return p
        return ""

    async def _cover_from_manifest(self, session, repo: str, ref: str) -> str:
        """大包取不到文件清单时，manifest.json 是单文件、能直接下，从里面挑一张真表情。
        这个包自带的 previews/cover.jpg 往往是九宫格拼图，用户要的是单张表情。"""
        for name in ("manifest.json", "memesdata.json"):
            url = f"https://gcore.jsdelivr.net/gh/{repo}@{quote(ref)}/{quote(name)}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status != 200:
                        continue
                    data = await r.json(content_type=None)
            except Exception:
                continue
            path = self._first_meme_path(data)
            if path:
                return f"https://gcore.jsdelivr.net/gh/{repo}@{quote(ref)}/{quote(path)}"
        return ""

    async def store_cover(self, repo: str, ref: str) -> str:
        """封面用包里真实的一张表情：已收录的直接从本地库取，没收录的从文件清单里挑一张。"""
        local = self._local_cover(repo)
        if local:
            return local
        refs = [ref] if ref else ["main", "master"]
        cached = self._cached_tree(repo)
        if cached:
            branch, entries = cached
            url = self._pick_cover(entries, repo, branch or refs[0])
            if url:
                return url
        try:
            async with aiohttp.ClientSession(timeout=self._timeout(25)) as session:
                for one in refs:
                    tree, _why = await self._tree_jsdelivr(session, repo, one)
                    if tree:
                        url = self._pick_cover(tree, repo, one)
                        if url:
                            return url
                tree = await self._tree_from_zip(session, repo, refs)
                if tree:
                    url = self._pick_cover(tree, repo, refs[0])
                    if url:
                        return url
                # 清单全失败（多半整包 >50MB）：先从 manifest 挑一张真表情，再退到自带封面
                for one in refs:
                    url = await self._cover_from_manifest(session, repo, one)
                    if url:
                        return url
                for one in refs:
                    url = await self._probe_cover_file(session, repo, one)
                    if url:
                        return url
        except Exception:
            return ""
        return ""

    # ---------------- 对外：概览 ----------------
    def _local_tree(self, repo: str):
        """库里有这个包就直接从数据库造清单，一张图都不用联网，也就不吃 GitHub 额度。"""
        if self.db is None:
            return None
        try:
            pack = self.db.get_pack(repo.split("/")[-1])
            if not pack:
                return None
            rows = self.db.list_images(pack["id"], limit=99999)
            if not rows:
                return None
            return [{"type": "blob", "path": str(r["file_path"]), "id": r["id"]}
                    for r in rows]
        except Exception:
            return None

    async def inspect(self, url: str) -> dict:
        info = parse_repo_url(url)
        if not info:
            raise PreviewError("只支持 GitHub / Gitee 仓库地址，比如 https://github.com/owner/repo")
        key = self._key(info)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < TREE_TTL:
            return hit[1]

        local = self._local_tree(info["repo"])
        if local:
            branch, entries, truncated, readme = (info["branch"] or "local"), local, False, ""
            self._tree_source = "local"
        else:
            async with aiohttp.ClientSession(timeout=self._timeout(35)) as session:
                try:
                    cached = self._read_tree_cache(info)
                    if cached:
                        branch, entries, truncated = cached
                        readme = ""
                    else:
                        branch, entries, truncated = await self._branch_and_tree(session, info)
                        if self._tree_source in ("api", "jsdelivr", "zip"):
                            self._write_tree_cache(info, branch, entries)
                        readme = await self._readme(session, info, branch)
                except aiohttp.ClientError as exc:
                    raise PreviewError(f"连不上仓库：{exc}") from exc
        collected = self._collect(entries)
        images = collected["images"]
        native = sorted({t for t in (i["tag"] for i in images) if t})
        info = {**info, "branch": branch}
        judge = _judge_meme_pack(entries, images, native)
        repo_name = info["repo"]
        shown_name = _pretty_name(repo_name)
        shown_desc = _plain(readme or "（这个仓库没写简介）")
        for one in REPO_SOURCES:
            if str(one.get("repo") or "").split("/")[-1] == repo_name:
                shown_name = _plain(one.get("name") or shown_name)
                shown_desc = _plain(one.get("note") or shown_desc)
                break
        summary = {
            "ok": True,
            "platform": info["platform"],
            "owner": info["owner"],
            "repo": repo_name,
            "branch": branch,
            "url": url.strip(),
            "name": shown_name,
            "description": shown_desc,
            "has_native_tag": bool(native),
            "native_tags": native,
            "total": len(images),
            "groups": collected["groups"],
            "truncated": truncated,
            **judge,
        }
        self._cache[key] = (time.time(), summary, info, images)
        return summary

    def _lookup(self, url: str):
        info = parse_repo_url(url)
        if not info:
            raise PreviewError("仓库地址无法识别")
        hit = self._cache.get(self._key(info))
        if not hit:
            raise PreviewError("先把仓库预览一遍，再点图")
        return hit[1], hit[2], hit[3]

    # ---------------- 对外：图片列表 ----------------
    async def files(self, url: str, group: str = "", page: int = 1, size: int = 60) -> dict:
        summary, info, images = self._lookup(url)
        if group:
            if group == ROOT_GROUP:
                images = [i for i in images if not i["dir"]]
            else:
                images = [i for i in images
                          if i["tag"] == group or i["dir"] == group
                          or i["dir"].startswith(group + "/")]
        total = len(images)
        start = (max(1, page) - 1) * size
        rows = [dict(i) for i in images[start:start + size]]
        for r in rows:
            if r.get("local_id"):
                r["thumb"] = f"/api/img?id={r['local_id']}"   # 本地的图直接读库
            else:
                r["thumb"] = "/api/repo/thumb?" + "&".join([
                    f"platform={info['platform']}",
                    f"owner={quote(info['owner'])}",
                    f"repo={quote(info['repo'])}",
                    f"branch={quote(info['branch'])}",
                    f"path={quote(r['path'])}",
                ])
        return {"items": rows, "total": total, "page": page, "size": size,
                "group": group, "branch": info["branch"]}

    # ---------------- 对外：缩略图 ----------------
    async def thumb(self, platform: str, owner: str, repo: str,
                    branch: str, path: str) -> tuple[bytes, str]:
        if platform not in ("github", "gitee"):
            raise PreviewError("平台不支持")
        if not path.lower().endswith(IMAGE_SUFFIXES):
            raise PreviewError("不是图片")
        if any(seg.startswith(".") for seg in path.split("/")):
            raise PreviewError("路径不合法")
        key = f"{platform}|{owner}|{repo}|{branch}|{path}"
        hit = self._thumbs.get(key)
        if hit:
            self._thumbs.move_to_end(key)
            return hit

        # 1) 磁盘上的缩略图：重启之后也还在这儿，直接读
        tf = self._thumb_path(key)
        if tf and tf.exists():
            try:
                cached = tf.read_bytes()
            except OSError:
                cached = b""
            if cached:
                self._remember_thumb(key, cached, "image/jpeg")
                return cached, "image/jpeg"

        # 2) 没有就回源拉原图
        async with self._thumb_gate:
            data, ctype = await self._fetch_image(platform, owner, repo, branch, path)

        out, out_type = data, ctype
        if HAS_PIL:
            try:
                small = self._shrink(data)
            except Exception:
                small = b""
            if small:
                out, out_type = small, "image/jpeg"
                if tf:
                    try:
                        tf.parent.mkdir(parents=True, exist_ok=True)
                        tf.write_bytes(small)
                        self._thumb_writes += 1
                        if self._thumb_writes % 30 == 1:
                            self._prune_thumbs()
                    except OSError:
                        pass
        self._remember_thumb(key, out, out_type)
        return out, out_type

    async def _fetch_image(self, platform, owner, repo, branch, path) -> tuple[bytes, str]:
        if platform == "github":
            # 本机 raw.githubusercontent.com 解析不了，走 jsDelivr 的 gcore 节点（有 CDN 缓存，快很多）
            urls = [
                f"https://gcore.jsdelivr.net/gh/{owner}/{repo}@{branch}/{quote(path)}",
                f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{quote(path)}",
                f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(path)}?ref={quote(branch)}",
            ]
        else:
            urls = [
                f"https://gitee.com/{owner}/{repo}/raw/{branch}/{quote(path)}",
                f"https://gitee.com/api/v5/repos/{owner}/{repo}/contents/{quote(path)}?ref={quote(branch)}",
            ]
        async with aiohttp.ClientSession(timeout=self._timeout(45)) as session:
            for idx, u in enumerate(urls):
                try:
                    async with session.get(u, headers=self._headers()) as resp:
                        if resp.status != 200:
                            continue
                        body = await resp.read()
                        ctype = (resp.headers.get("Content-Type") or "").lower()
                        if idx == len(urls) - 1 and "json" in ctype:
                            payload = json.loads(body.decode("utf-8", errors="ignore"))
                            body = base64.b64decode(payload.get("content") or "")
                            ctype = "image/png"
                        if not body:
                            continue
                        return body, (ctype if ctype.startswith("image/") else "image/png")
                except (aiohttp.ClientError, json.JSONDecodeError, ValueError):
                    continue
        raise PreviewError("远程图片取不到（文件可能已删除，或网络不通）")
