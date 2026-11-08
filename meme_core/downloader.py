"""Git 表情包仓库下载与入库：zip 优先，git clone 兜底。"""

import asyncio
import re
import shutil
import zipfile
from pathlib import Path

import aiohttp

from .constants import (
    IMAGE_SUFFIXES,
    NON_MEME_DIRS,
    PACK_EMOTION,
    REPO_SOURCES,
    REVIEW_CONFIRMED,
    REVIEW_UNCATEGORIZED,
    TAG_ALIASES,
)

# 这些目录里的图是封面拼图/文档配图，不是表情，入库时跳过
SKIP_DIRS = NON_MEME_DIRS

_SRC_WORD = re.compile(r"astrbot\s*", re.I)


def _plain(text: str) -> str:
    """入库文案只用我们自己的说法，别人的产品名不往库里带。"""
    return _SRC_WORD.sub("", str(text or "")).strip()


def _pretty(repo: str) -> str:
    """仓库标识转成能看的名字，不露别人的产品名。"""
    name = _plain(repo)
    name = re.sub(r"[-_]meme[-_]?pack", "", name, flags=re.I)
    name = re.sub(r"^[-_]+|[-_]+$", "", name)
    name = re.sub(r"[-_]+", " ", name).strip()
    return f"{name} 表情包" if name else "这个表情包"


def parse_repo_url(url: str):
    """解析 GitHub/Gitee 仓库地址，返回平台、owner、repo、分支。"""
    url = url.strip()
    branch = None
    m_branch = re.search(r"/(?:tree|src)/([^/\s]+)", url)
    if m_branch:
        branch = m_branch.group(1)
    if url.startswith("git@"):
        platform = "github"
        m = re.match(r"git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^#]+?)(?:\.git)?$", url)
        if not m:
            return None
        return {"platform": platform, "owner": m.group("owner"),
                "repo": m.group("repo"), "branch": branch}
    if "gitee.com" in url:
        platform = "gitee"
    elif "github.com" in url:
        platform = "github"
    else:
        return None
    clean = re.sub(r"https?://", "", url)
    clean = clean.split("/tree/")[0].split("/src/")[0].strip("/")
    parts = clean.split("/")
    if len(parts) < 3:
        return None
    owner = parts[1]
    repo = parts[2].removesuffix(".git")
    return {"platform": platform, "owner": owner, "repo": repo, "branch": branch}


def _zip_url(info: dict, branch: str) -> str:
    if info["platform"] == "github":
        return (f"https://codeload.github.com/{info['owner']}/"
                f"{info['repo']}/zip/refs/heads/{branch}")
    return (f"https://gitee.com/{info['owner']}/{info['repo']}/"
            f"repository/archive/{branch}.zip")


def _zip_candidates(info: dict, refs: list) -> list:
    """这些包给的是 tag（v1.0.0），heads 拿不到就换 refs/tags 再试一次。"""
    out = []
    for ref in refs:
        out.append(_zip_url(info, ref))
        if info["platform"] == "github":
            out.append(f"https://codeload.github.com/{info['owner']}/"
                       f"{info['repo']}/zip/refs/tags/{ref}")
    return out


def _read_readme(src_dir: Path) -> str:
    for f in src_dir.iterdir():
        if f.is_file() and f.name.lower().startswith("readme"):
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # 图片
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"[#>*`_~\-]{1,3}", "", text)
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            return " ".join(lines)[:300]
    return ""


def _alias_tag(name: str):
    key = name.lower().strip().replace(" ", "")
    for tag, aliases in TAG_ALIASES.items():
        if key in aliases:
            return tag
    return None


class DownloadCancelled(Exception):
    """用户取消下载时抛出来，用来把半截目录清掉。"""


class GitDownloader:
    def __init__(self, plugin_root: Path, db, temp_dir: Path):
        self.root = plugin_root
        self.db = db
        self.temp_dir = temp_dir
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def download(self, url: str, pack_type: str = PACK_EMOTION,
                       progress=None, ref: str | None = None,
                       stop=None) -> dict:
        def report(stage: str, **extra):
            if progress:
                try:
                    progress({"stage": stage, **extra})
                except Exception:
                    pass

        info = parse_repo_url(url)
        if not info:
            return {"ok": False, "message": "无法识别该仓库地址，仅支持 GitHub/Gitee。"}
        repo = info["repo"]
        target = self.root / "memes" / repo
        if target.exists():
            return {"ok": False, "message": f"已存在同名集合：{repo}，请先处理。"}

        work = self.temp_dir / repo
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)

        pack_id = None
        try:
            report("downloading", bytes=0)
            if ref:
                branches = [ref]
            elif info["branch"]:
                branches = [info["branch"]]
            else:
                branches = ["main", "master"]
            src_dir = await self._download_zip(info, branches, work, report, stop=stop)
            method = "zip"
            if src_dir is None:
                if stop and stop():
                    raise DownloadCancelled
                report("cloning")
                src_dir = await self._clone(url, work)
                method = "clone"
            if src_dir is None:
                shutil.rmtree(work, ignore_errors=True)
                return {"ok": False, "message": "zip 与 git clone 均失败，请检查网络或地址。"}

            report("importing")
            readme = _read_readme(src_dir)
            tag_dirs = self._find_tag_dirs(src_dir)
            has_native_tag = len(tag_dirs) > 0

            # 展示名和说明用我们自己的口径，别把别人的仓库名和 README 原样露出来
            shown, desc = self._meta_for(repo, readme)

            # 移动到正式目录
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_dir), str(target))
            self._clean_git(target)

            pack_id = self.db.create_pack(
                name=repo, display_name=shown, pack_type=pack_type,
                source_url=url, description=desc, has_native_tag=has_native_tag,
            )
            total, tagged = self._import_images(
                target, pack_id, tag_dirs, progress=report, stop=stop,
            )
            report("done", total=total)
            return {
                "ok": True, "name": repo, "method": method,
                "description": readme, "has_native_tag": has_native_tag,
                "pack_type": pack_type, "total": total, "tagged": tagged,
                "untagged": total - tagged, "pack_id": pack_id,
                "message": f"下载成功（{method}），共 {total} 张，{tagged} 张已带标签。",
            }
        except DownloadCancelled:
            # 半截的东西全部清掉，别留脏目录、半截 zip 和空包
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(work.parent / (work.name + "_x"), ignore_errors=True)
            try:
                (self.temp_dir / (repo + ".zip")).unlink(missing_ok=True)
            except Exception:
                pass
            if pack_id:
                try:
                    self.db.delete_pack(pack_id)
                except Exception:
                    pass
                shutil.rmtree(target, ignore_errors=True)
            return {"ok": False, "cancelled": True, "message": "已取消下载。"}

    def _meta_for(self, repo: str, readme: str):
        """给包起个中文名：先在地址库里查，再退到仓库 README 的第一行。"""
        for one in REPO_SOURCES:
            if str(one.get("repo") or "").split("/")[-1] == repo:
                return _plain(one.get("name") or repo), _plain(one.get("note") or readme)
        first = ""
        for line in (readme or "").splitlines():
            line = line.strip().lstrip("#").strip()
            if line and not line.startswith(("![", "|", "[", "http")):
                first = line
                break
        return (_plain(first)[:24] or _pretty(repo)), _plain(readme)

    async def _download_zip(self, info, branches, work: Path, report=None, stop=None):
        zip_path = work.with_suffix(".zip")
        timeout = aiohttp.ClientTimeout(total=3600, sock_connect=30, sock_read=300)
        got = 0
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for url in _zip_candidates(info, branches):
                got = 0
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        size = int(resp.headers.get("Content-Length") or 0)
                        if report:
                            report("downloading", bytes=0, total=size)
                        with open(zip_path, "wb") as fh:
                            async for chunk in resp.content.iter_chunked(256 * 1024):
                                if stop and stop():
                                    fh.close()
                                    raise DownloadCancelled
                                fh.write(chunk)
                                got += len(chunk)
                                if report:
                                    report("downloading", bytes=got, total=size)
                    if got:
                        break
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                    zip_path.unlink(missing_ok=True)
                    continue
        if not zip_path.exists() or zip_path.stat().st_size == 0:
            return None
        if report:
            report("extracting", bytes=got)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(work.parent / (work.name + "_x"))
        except zipfile.BadZipFile:
            return None
        finally:
            zip_path.unlink(missing_ok=True)
        extracted = work.parent / (work.name + "_x")
        subs = [p for p in extracted.iterdir() if p.is_dir()]
        if len(subs) == 1:
            return subs[0]
        return extracted

    async def _clone(self, url, work: Path):
        if not shutil.which("git"):
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth", "1", url, str(work),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=600)
        except (asyncio.TimeoutError, FileNotFoundError, OSError):
            return None
        return work if work.exists() else None

    def _find_tag_dirs(self, src_dir: Path) -> dict:
        """返回 命中分类的目录相对路径 -> 标准tag（相对路径，移动目录后依然成立）。"""
        result = {}
        for d in src_dir.rglob("*"):
            if not d.is_dir() or ".git" in d.parts or d.name.startswith("."):
                continue
            tag = _alias_tag(d.name)
            if tag:
                has_img = any(
                    f.suffix.lower() in IMAGE_SUFFIXES
                    for f in d.rglob("*") if f.is_file()
                )
                if has_img:
                    result[d.relative_to(src_dir).as_posix()] = tag
        return result

    def _import_images(self, target: Path, pack_id: int, tag_dirs: dict,
                       progress=None, stop=None):
        files = []
        for f in target.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if any(p.lower() in SKIP_DIRS for p in f.relative_to(target).parts[:-1]):
                continue
            files.append(f)
        all_count = len(files)
        step = max(1, all_count // 20)   # 每 5% 报一次，别把回调节奏打太快
        total = 0
        tagged = 0
        for idx, f in enumerate(files, 1):
            if stop and stop():
                raise DownloadCancelled
            total += 1
            rel = f.relative_to(self.root).as_posix()
            parent = f.relative_to(target).parent.as_posix()
            if parent == ".":
                parent = ""
            hit_tag = None
            best = -1
            for drel, tag in tag_dirs.items():
                if parent == drel or parent.startswith(drel + "/"):
                    if len(drel) > best:      # 最深的目录优先
                        best = len(drel)
                        hit_tag = tag
            if hit_tag:
                status = REVIEW_CONFIRMED
                tagged += 1
            else:
                status = REVIEW_UNCATEGORIZED
            image_id = self.db.add_image(
                pack_id, rel, f.name, review_status=status,
            )
            if hit_tag:
                self.db.add_tag(image_id, hit_tag)
            if progress and (idx % step == 0 or idx == all_count):
                progress({"stage": "importing", "done": idx, "total": all_count})
        return total, tagged

    @staticmethod
    def _clean_git(target: Path):
        git_dir = target / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir, ignore_errors=True)
