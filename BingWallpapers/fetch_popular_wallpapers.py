#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多源热门壁纸抓取器（国内 + 国外，稳健修复版）
- 国内：360 壁纸 (q360)
- 国外：Wallhaven、Openverse（可选）、Wikimedia（可选；大陆直连常不通）
- 统一目录：popular/<provider>/<res>/
- 稳健：并发检索 + 公平(Round-Robin)调度下载；断点续传；内容级去重(SHA-256)；可选近似去重(pHash)
- 条件请求：If-None-Match / If-Modified-Since 命中 304 直接跳过
- robots.txt 预检（可关）
- 依赖：requests, tqdm；可选：pillow(用于 --exact 和完整性 verify)、imagehash(用于 pHash)

本版关键变化（相对你上一版）：
1) **不再误删新下的文件**：去掉 content_sha256 的唯一索引；数据库写入改为 INSERT OR IGNORE。
2) 新增 `--dup-action {keep,skip}`（默认 keep）。重复时保留文件、不强制删除；你也可切回 skip 以节省磁盘。
3) Wallhaven 默认 `--wh-sorting date_added`（最新），可选 toplist/random；random 支持 seed。
4) 断点续传：Range + 206，下载到 *.part，校验后再原子重命名。
5) 并发检索 + Round-Robin 下载：不会出现“一个源跑完，另一个源才开始”的饥饿现象。
"""

from __future__ import annotations
import argparse
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin
from urllib import robotparser

import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry  # type: ignore
except Exception:
    Retry = None
from tqdm import tqdm

# =========================
#           API 配置（可改/可用环境变量）
# =========================
WALLHAVEN_API_KEY     = os.getenv("WALLHAVEN_API_KEY", "")    # 可选；SFW 不填也能用
OPENVERSE_TOKEN       = os.getenv("OPENVERSE_TOKEN", "")      # 建议；更稳定配额
WIKIMEDIA_USER_AGENT  = os.getenv("WIKIMEDIA_USER_AGENT", "WallpaperFetcher/1.2 (+contact@example.com)")

ENDPOINTS = {
    # 国内：360 壁纸（社区常用接口）
    "q360_categories": "http://cdn.apc.360.cn/index.php",
    "q360_list":       "http://wallpaper.apc.360.cn/index.php",
    "q360_search":     "http://wallpaper.apc.360.cn/index.php",
    # 国外
    "wallhaven_search": "https://wallhaven.cc/api/v1/search",
    "openverse_images": "https://api.openverse.org/v1/images/",
    "wikimedia_api":    "https://commons.wikimedia.org/w/api.php",
}

# =========================
#       常量与默认值
# =========================
UA = "WallpaperFetcher/1.2"
REQ_TIMEOUT = 25
RES_PRESETS = {"1k": (1920, 1080), "2k": (2560, 1440), "4k": (3840, 2160)}

POPULAR_ROOT = Path("popular").resolve()
POPULAR_DB   = POPULAR_ROOT / "popular.sqlite3"

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS fetched(
  provider TEXT NOT NULL,
  resid    TEXT NOT NULL,   -- 1k/2k/4k
  url      TEXT NOT NULL,
  sha1     TEXT NOT NULL,   -- sha1(url)
  path     TEXT NOT NULL,
  width    INTEGER NOT NULL,
  height   INTEGER NOT NULL,
  meta     TEXT NOT NULL,   -- json
  content_sha256 TEXT,
  etag          TEXT,
  last_modified TEXT,
  phash         TEXT,
  PRIMARY KEY(provider, resid, sha1)
);
"""
# 重要：不再创建 content_sha256 的唯一索引，避免“新图顶旧记录/误删”。

# =========================
#     工具 & 数据结构
# =========================
@dataclasses.dataclass
class Item:
    provider: str
    id: str
    url: str
    width: int
    height: int
    filename_hint: str = ""
    meta: Dict = dataclasses.field(default_factory=dict)


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def sha1_of(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def new_session(pool_size: int = 20, retries: int = 3, backoff: float = 0.5) -> requests.Session:
    """带连接池与重试的 Session（对高延迟网络更稳）"""
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    if Retry is not None:
        r = Retry(
            total=retries, connect=retries, read=retries, status=retries,
            backoff_factor=backoff,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=r)
    else:
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    s.mount("http://", adapter); s.mount("https://", adapter)
    return s


def ensure_db(db_path: Path) -> sqlite3.Connection:
    ensure_dir(db_path.parent)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(CREATE_SQL)
    # 兼容旧版本：若存在唯一索引则删除
    try:
        conn.execute("DROP INDEX IF EXISTS uniq_content_sha256;")
    except Exception:
        pass
    conn.commit()
    return conn


def already_have_url(conn: sqlite3.Connection, provider: str, resid: str, url: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM fetched WHERE provider=? AND resid=? AND sha1=? LIMIT 1",
        (provider, resid, sha1_of(url))
    ).fetchone()
    return row is not None


def content_seen(conn: sqlite3.Connection, sha256: str) -> bool:
    row = conn.execute("SELECT 1 FROM fetched WHERE content_sha256=? LIMIT 1", (sha256,)).fetchone()
    return row is not None


def any_phash_similar(conn: sqlite3.Connection, phash_hex: str, max_dist: int = 5) -> bool:
    if not phash_hex:
        return False
    rows = conn.execute("SELECT phash FROM fetched WHERE phash IS NOT NULL").fetchall()
    try:
        import imagehash  # type: ignore
        h0 = imagehash.hex_to_hash(phash_hex)
        for (h1_hex,) in rows:
            if h1_hex:
                if h0 - imagehash.hex_to_hash(h1_hex) <= max_dist:
                    return True
    except Exception:
        pass
    return False


def record(conn: sqlite3.Connection, *, provider: str, resid: str, url: str,
           path: Path, width: int, height: int, meta: Dict,
           content_sha256: Optional[str], etag: Optional[str],
           last_modified: Optional[str], phash: Optional[str]) -> bool:
    """插入记录；若重复（URL 相同）则忽略并返回 False"""
    cur = conn.execute(
        """INSERT OR IGNORE INTO fetched
           (provider,resid,url,sha1,path,width,height,meta,content_sha256,etag,last_modified,phash)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (provider, resid, url, sha1_of(url), str(path), width, height,
         json.dumps(meta, ensure_ascii=False), content_sha256, etag, last_modified, phash)
    )
    conn.commit()
    return cur.rowcount > 0


def pick_filename(item: Item, dest_dir: Path) -> Path:
    ext = os.path.splitext(item.url.split("?")[0])[1].lower()
    if not re.match(r"\.\w{2,5}$", ext):
        ext = ".jpg"
    base = re.sub(r"[^\w\-\.]+", "_", (item.filename_hint or item.id or "img")).strip("_")
    base = base or (item.id or sha1_of(item.url)[:10])
    return dest_dir / f"{base}_{item.width}x{item.height}{ext}"

# robots.txt 预检（RFC 9309）
_ROBOTS_CACHE: Dict[str, robotparser.RobotFileParser] = {}

def robots_allowed(url: str, ua: str, mode: str = "on") -> bool:
    if mode == "off":
        return True
    pr = urlparse(url)
    robots_url = urljoin(f"{pr.scheme}://{pr.netloc}", "/robots.txt")
    rp = _ROBOTS_CACHE.get(robots_url)
    if rp is None:
        rp = robotparser.RobotFileParser()
        rp.set_url(robots_url)
        try:
            rp.read()
            _ROBOTS_CACHE[robots_url] = rp
        except Exception:
            return False if mode == "strict" else True
    try:
        return rp.can_fetch(ua, url)
    except Exception:
        return False if mode == "strict" else True

# 条件请求（If-None-Match / If-Modified-Since）

def fetch_known_headers(conn: sqlite3.Connection, url: str) -> Tuple[Optional[str], Optional[str]]:
    row = conn.execute(
        "SELECT etag,last_modified FROM fetched WHERE url=? ORDER BY rowid DESC LIMIT 1", (url,)
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def conditional_headers(etag: Optional[str], last_mod: Optional[str]) -> Dict[str, str]:
    h = {}
    if etag:
        h["If-None-Match"] = etag
    elif last_mod:
        h["If-Modified-Since"] = last_mod
    return h

# ========= 断点续传 + 完整性校验 =========
try:
    from PIL import Image  # type: ignore
except Exception:
    Image = None


def _head_size(sess: requests.Session, url: str) -> Optional[int]:
    try:
        hr = sess.head(url, headers={"User-Agent": UA}, timeout=REQ_TIMEOUT, allow_redirects=True)
        if hr.ok:
            cl = hr.headers.get("Content-Length")
            if cl and cl.isdigit():
                return int(cl)
    except Exception:
        pass
    return None


def _verify_image_complete(path: Path) -> bool:
    if Image is None:
        return path.exists() and path.stat().st_size > 0
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def download_resumable(sess: requests.Session, url: str, headers: Dict[str, str], dest: Path,
                       *, max_tries: int = 3) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """
    返回 (status, sha256, etag, last_modified)
    status: "downloaded" | "not_modified" | "error"
    - 使用 *.part 临时文件；若中断则下一次用 Range 续传
    - 下载完对比 Content-Length（若可得）；并用 Pillow.verify() 验证图片完整性
    """
    tmp = dest.with_suffix(dest.suffix + ".part")
    expected = _head_size(sess, url)  # 可能为 None
    etag = None
    last_mod = None

    for _ in range(1, max_tries + 1):
        start = tmp.stat().st_size if tmp.exists() else 0
        req_headers = {"User-Agent": UA, **headers}
        if start:
            req_headers["Range"] = f"bytes={start}-"

        with sess.get(url, headers=req_headers, stream=True, timeout=REQ_TIMEOUT) as r:
            if r.status_code == 304:
                return "not_modified", None, None, None
            if r.status_code not in (200, 206):
                r.raise_for_status()
            etag = r.headers.get("ETag")
            last_mod = r.headers.get("Last-Modified")

            mode = "ab" if start and r.status_code == 206 else "wb"
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, mode) as f:
                for chunk in r.iter_content(1 << 16):
                    if not chunk:
                        continue
                    f.write(chunk)

        size_now = tmp.stat().st_size
        if expected is not None and size_now < expected:
            continue
        if not _verify_image_complete(tmp):
            continue

        tmp.replace(dest)
        h = hashlib.sha256()
        with open(dest, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        return "downloaded", h.hexdigest(), etag, last_mod

    with contextlib.suppress(Exception):
        tmp.unlink()
    return "error", None, None, None


def compute_phash(path: Path) -> Optional[str]:
    try:
        import imagehash  # type: ignore
        if Image is None:
            return None
        with Image.open(path) as im:
            h = imagehash.phash(im)
            return str(h)
    except Exception:
        return None


def resize_center_crop(path: Path, target: Tuple[int, int]) -> Tuple[int, int]:
    if Image is None:
        return (0, 0)
    try:
        with Image.open(path) as im:
            tw, th = target
            im = im.convert("RGB")
            scale = max(tw / im.width, th / im.height)
            nw, nh = max(int(im.width * scale),1), max(int(im.height * scale),1)
            im = im.resize((nw, nh), Image.LANCZOS)
            left = (nw - tw)//2; top = (nh - th)//2
            im = im.crop((left, top, left+tw, top+th))
            im.save(path, format="JPEG", quality=92, optimize=True)
            return (tw, th)
    except Exception:
        return (0, 0)

# =========================
#         Providers
# =========================
class ProviderBase:
    name = "base"
    def search(self, *, q: str, target_w: int, target_h: int, limit: int, opts: Optional[dict] = None) -> List[Item]:
        raise NotImplementedError

# —— 360 壁纸（国内源）——
class Qihoo360Provider(ProviderBase):
    name = "q360"
    CATS = ENDPOINTS["q360_categories"]
    LIST = ENDPOINTS["q360_list"]
    SEARCH = ENDPOINTS["q360_search"]

    def _bdm_url(self, raw_url: str, tw: int, th: int, quality: int = 100) -> str:
        return re.sub(r"/bdr/__\d{2}/", f"/bdm/{tw}_{th}_{quality}/", raw_url)

    def search(self, *, q: str, target_w: int, target_h: int, limit: int, opts: Optional[dict] = None) -> List[Item]:
        sess = new_session(pool_size=30, retries=3, backoff=0.3)
        items: List[Item] = []

        def by_category(cid: int, need: int):
            if need <= 0:
                return
            params = {"c":"WallPaper","a":"getAppsByCategory","cid":cid,"start":0,"count":min(100, need),"from":"360chrome"}
            r = sess.get(self.LIST, params=params, timeout=REQ_TIMEOUT); r.raise_for_status()
            for it in (r.json() or {}).get("data", []):
                raw = it.get("url") or ""
                if not raw:
                    continue
                url = self._bdm_url(raw, target_w, target_h, 100)
                items.append(Item(
                    provider=self.name,
                    id=str(it.get("id") or it.get("pid") or ""),
                    url=url, width=target_w, height=target_h,
                    filename_hint=it.get("utag") or it.get("tag") or str(it.get("id") or ""),
                    meta={"cid": cid, "source":"q360", "raw": raw}
                ))

        if q:
            params = {"c":"WallPaper","a":"search","kw":q,"start":0,"count":min(100, limit)}
            r = sess.get(self.SEARCH, params=params, timeout=REQ_TIMEOUT); r.raise_for_status()
            for it in (r.json() or {}).get("data", []):
                raw = it.get("url") or ""
                if not raw:
                    continue
                url = self._bdm_url(raw, target_w, target_h, 100)
                items.append(Item(
                    provider=self.name,
                    id=str(it.get("id") or ""),
                    url=url, width=target_w, height=target_h,
                    filename_hint=it.get("tag") or str(it.get("id") or ""),
                    meta={"kw": q, "source":"q360", "raw": raw}
                ))
        else:
            for cid in (9, 26, 12, 5):  # 风景/动漫/汽车/游戏
                if len(items) >= limit: break
                by_category(cid, limit - len(items))

        return items[:limit]

# —— Wallhaven（国外源；官方 API）——
class WallhavenProvider(ProviderBase):
    name = "wallhaven"
    API = ENDPOINTS["wallhaven_search"]
    def search(self, *, q: str, target_w: int, target_h: int, limit: int, opts: Optional[dict] = None) -> List[Item]:
        opts = opts or {}
        sorting = opts.get("sorting", "date_added")  # 默认最新
        toprange = opts.get("topRange", "1M")
        seed = opts.get("seed", "")

        items: List[Item] = []
        sess = new_session(pool_size=10, retries=3, backoff=0.6)
        if WALLHAVEN_API_KEY:
            sess.headers.update({"X-API-Key": WALLHAVEN_API_KEY})
        params = {
            "q": q or "",
            "purity": "100",
            "categories": "100",
            "atleast": f"{target_w}x{target_h}",
            "order": "desc",
            "page": 1,
            "sorting": sorting,
        }
        if sorting == "toplist":
            params["topRange"] = toprange
        if sorting == "random" and seed:
            params["seed"] = seed

        with tqdm(desc="wallhaven", total=limit, unit="img", leave=False) as bar:
            while len(items) < limit and params["page"] <= 10:
                r = sess.get(self.API, params=params, timeout=REQ_TIMEOUT)
                r.raise_for_status()
                data = r.json()
                for w in data.get("data", []):
                    url = w.get("path")
                    res = w.get("resolution", "")
                    m = re.match(r"(\d+)\s*x\s*(\d+)", res or "")
                    if not url or not m:
                        continue
                    W, H = int(m.group(1)), int(m.group(2))
                    if W >= target_w and H >= target_h:
                        items.append(Item(
                            provider=self.name,
                            id=str(w.get("id") or w.get("short_id") or ""),
                            url=url, width=W, height=H,
                            filename_hint=w.get("id") or "",
                            meta={"resolution": res, "source": "wallhaven"}
                        ))
                        bar.update(1)
                        if len(items) >= limit:
                            break
                if len(items) < limit:
                    params["page"] += 1
        return items[:limit]

# —— Openverse（国外源，开放授权；建议带 token）——
class OpenverseProvider(ProviderBase):
    name = "openverse"
    API = ENDPOINTS["openverse_images"]
    def search(self, *, q: str, target_w: int, target_h: int, limit: int, opts: Optional[dict] = None) -> List[Item]:
        items: List[Item] = []
        sess = new_session(pool_size=10, retries=3, backoff=0.6)
        if OPENVERSE_TOKEN:
            sess.headers.update({"Authorization": f"Bearer {OPENVERSE_TOKEN}"})
        params = {"q": q or "wallpaper", "aspect_ratio": "wide", "size": "large", "page_size": 50}
        page = 1
        with tqdm(desc="openverse", total=limit, unit="img", leave=False) as bar:
            while len(items) < limit and page <= 6:
                r = sess.get(self.API, params={**params, "page": page}, timeout=REQ_TIMEOUT)
                r.raise_for_status()
                data = r.json()
                for it in data.get("results", []) or []:
                    url = it.get("url") or it.get("thumbnail")
                    w = int(it.get("width") or 0); h = int(it.get("height") or 0)
                    if not url or not w or not h:
                        continue
                    if w >= target_w and h >= target_h:
                        items.append(Item(
                            provider=self.name,
                            id=str(it.get("id")),
                            url=url, width=w, height=h,
                            filename_hint=(it.get("title") or it.get("id") or ""),
                            meta={
                                "license": it.get("license"),
                                "license_url": it.get("license_url"),
                                "attribution": it.get("attribution"),
                                "foreign_landing_url": it.get("foreign_landing_url"),
                                "source": "openverse"
                            },
                        ))
                        bar.update(1)
                        if len(items) >= limit:
                            break
                if len(items) < limit:
                    page += 1
        return items[:limit]

# —— Wikimedia（国外源；大陆常不通）——
class WikimediaProvider(ProviderBase):
    name = "wikimedia"
    API = ENDPOINTS["wikimedia_api"]
    FP_CATEGORY = "Category:Featured_pictures_of_landscapes"
    def search(self, *, q: str, target_w: int, target_h: int, limit: int, opts: Optional[dict] = None) -> List[Item]:
        items: List[Item] = []
        sess = new_session(pool_size=6, retries=2, backoff=0.8)
        sess.headers.update({"User-Agent": WIKIMEDIA_USER_AGENT or UA})
        gcmcontinue = None
        with tqdm(desc="wikimedia", total=limit, unit="img", leave=False) as bar:
            while len(items) < limit:
                params = {
                    "action": "query", "format": "json",
                    "generator": "categorymembers",
                    "gcmtitle": self.FP_CATEGORY, "gcmtype": "file", "gcmlimit": "50",
                    "prop": "imageinfo", "iiprop": "url|size",
                }
                if gcmcontinue:
                    params["gcmcontinue"] = gcmcontinue
                r = sess.get(self.API, params=params, timeout=REQ_TIMEOUT)
                r.raise_for_status()
                data = r.json()
                pages = (data.get("query") or {}).get("pages", {})
                for _, page in pages.items():
                    ii = (page.get("imageinfo") or [{}])[0]
                    url = ii.get("url")
                    w, h = int(ii.get("width") or 0), int(ii.get("height") or 0)
                    if not url or not w or not h:
                        continue
                    if w >= target_w and h >= target_h:
                        title = page.get("title", "")
                        pid = str(page.get("pageid"))
                        items.append(Item(
                            provider=self.name,
                            id=pid, url=url, width=w, height=h,
                            filename_hint=title.replace("File:", ""),
                            meta={"title": title, "source": "wikimedia"}
                        ))
                        bar.update(1)
                        if len(items) >= limit:
                            break
                if len(items) >= limit:
                    break
                gcmcontinue = (data.get("continue") or {}).get("gcmcontinue")
                if not gcmcontinue:
                    break
        return items[:limit]

PROVIDER_REGISTRY = {
    "q360": Qihoo360Provider(),
    "wallhaven": WallhavenProvider(),
    "openverse": OpenverseProvider(),
    "wikimedia": WikimediaProvider(),
}

# =========================
#      CLI & 主流程
# =========================

def resolve_targets(res_list: List[str]) -> List[Tuple[str, Tuple[int,int]]]:
    out = []
    for r in res_list:
        if r not in RES_PRESETS:
            raise SystemExit(f"不支持的分辨率档：{r}，可选 {list(RES_PRESETS)}")
        out.append((r, RES_PRESETS[r]))
    return out


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="多源热门壁纸抓取器（统一 popular 目录）")
    ap.add_argument("--providers", nargs="+", default=["q360", "wallhaven", "openverse"],
                    help="来源：q360 wallhaven openverse wikimedia（可任意组合）")
    ap.add_argument("--res", nargs="+", default=["4k"], help="目标分辨率档：1k 2k 4k（可多选）")
    ap.add_argument("--limit-per", type=int, default=40, help="每个来源抓取数量上限")
    ap.add_argument("--q", type=str, default="", help="搜索关键词（q360/wallhaven/openverse 使用）")
    ap.add_argument("--exact", action="store_true", help="将图片裁剪/缩放到精确分辨率（需要 Pillow）")
    ap.add_argument("--dup-mode", choices=["url", "content", "perceptual"], default="content",
                    help="重复判定：url（仅URL）/ content（内容哈希，默认）/ perceptual（pHash近似）")
    ap.add_argument("--dup-action", choices=["keep", "skip"], default="keep",
                    help="发现重复时的动作：keep=保留新文件（默认）；skip=删除新文件")
    ap.add_argument("--phash-distance", type=int, default=5, help="pHash 近似阈值（汉明距离）")
    ap.add_argument("--robots", choices=["on", "off", "strict"], default="on",
                    help="robots.txt 检查：on=默认；off=关闭；strict=读取失败也视为不允许")
    ap.add_argument("--max-workers", type=int, default=12, help="下载线程数（q360可高些，海外源适中）")
    # Wallhaven 策略
    ap.add_argument("--wh-sorting", choices=["date_added","toplist","random"], default="date_added",
                    help="Wallhaven 排序方式（默认最新）")
    ap.add_argument("--wh-toprange", default="1M", help="sorting=toplist 时的时间范围，如 1d, 1w, 1M, 3M, 1y")
    ap.add_argument("--wh-seed", default="", help="sorting=random 时可指定 seed，使结果可复现")
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args(argv)


def interleave_round_robin(groups: List[List[Tuple[Item, Path, Dict[str,str]]]]) -> List[Tuple[Item, Path, Dict[str,str]]]:
    queues: List[Deque[Tuple[Item, Path, Dict[str,str]]]] = [deque(g) for g in groups if g]
    out: List[Tuple[Item, Path, Dict[str,str]]] = []
    while queues:
        for q in list(queues):
            if q:
                out.append(q.popleft())
            else:
                queues.remove(q)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    POPULAR_ROOT.mkdir(parents=True, exist_ok=True)
    conn = ensure_db(POPULAR_DB)
    targets = resolve_targets(args.res)

    total_new_records = 0
    global_sess = new_session(pool_size=max(12, args.max_workers*2), retries=3, backoff=0.6)

    # ========= 并发检索（provider × 分辨率），失败就跳过 =========
    search_jobs = []
    with ThreadPoolExecutor(max_workers=min(8, len(args.providers) * len(targets))) as ex:
        for resid, (tw, th) in targets:
            for name in args.providers:
                prov = PROVIDER_REGISTRY.get(name)
                if not prov:
                    print(f"[warn] 未识别来源：{name}，跳过。可选：{list(PROVIDER_REGISTRY)}")
                    continue
                # provider 特定 opts
                opts = None
                if name == "wallhaven":
                    opts = {"sorting": args.wh_sorting, "topRange": args.wh_toprange, "seed": args.wh_seed}
                fut = ex.submit(prov.search, q=args.q, target_w=tw, target_h=th, limit=args.limit_per, opts=opts)
                search_jobs.append((resid, (tw, th), name, prov, fut))

        # 将每个（provider,res）结果汇总成一个下载任务组
        all_groups: List[List[Tuple[Item, Path, Dict[str,str]]]] = []
        for resid, (tw, th), name, prov, fut in search_jobs:
            dest_dir = POPULAR_ROOT / prov.name / resid
            ensure_dir(dest_dir)
            try:
                items = fut.result()
            except Exception as e:
                print(f"[skip] provider {prov.name} ({resid}) 检索异常：{e}")
                continue

            if args.verbose:
                print(f"\n== {prov.name} -> {resid}({tw}x{th}) 候选 {len(items)} 张 ==")

            seen_urls: set[str] = set()
            group: List[Tuple[Item, Path, Dict[str,str]]] = []
            for it in items:
                if it.url in seen_urls:
                    continue
                seen_urls.add(it.url)

                if not robots_allowed(it.url, UA, mode=args.robots):
                    if args.verbose:
                        print(f"[robots] disallow -> {it.url}")
                    continue

                if args.dup_mode == "url" and already_have_url(conn, prov.name, resid, it.url):
                    if args.verbose:
                        print(f"[skip/url] 已有：{it.url}")
                    continue

                etag0, lm0 = fetch_known_headers(conn, it.url)
                fname = pick_filename(it, dest_dir)
                headers = {"User-Agent": UA, **conditional_headers(etag0, lm0)}
                group.append((it, fname, headers))

            all_groups.append(group)

    # ========= 公平调度（交错各组任务），并行下载 =========
    tasks = interleave_round_robin(all_groups)
    if args.verbose:
        print(f"\n== 排程后任务数：{len(tasks)} ==")

    with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
        futs = [(it, fname, ex.submit(download_resumable, global_sess, it.url, headers, fname))
                for (it, fname, headers) in tasks]

        for it, fname, fut in tqdm(futs, desc="downloading", unit="img", leave=False):
            try:
                status, sha256, etag, last_mod = fut.result(timeout=240)
            except Exception as e:
                if args.verbose:
                    print(f"[fail] {it.url}  -> {e}")
                continue

            if status == "not_modified":
                if args.verbose:
                    print(f"[304] {it.url}")
                continue
            if status != "downloaded" or not fname.exists():
                if args.verbose:
                    print(f"[fail] {it.url}")
                continue

            # 精确裁剪（可选）：使用目标分辨率而非原始
            # 目标分辨率可由文件夹 resid 推断，也可直接通过任务构造时的 (tw,th)。此处取自 resid。
            try:
                resid = Path(fname).parent.name
                target_wh = RES_PRESETS.get(resid)
            except Exception:
                target_wh = None
            final_w, final_h = it.width, it.height
            if args.exact and target_wh:
                w2, h2 = resize_center_crop(fname, target_wh)
                if w2 and h2:
                    final_w, final_h = w2, h2

            # 近似哈希（可选）
            phash_hex = None
            if args.dup_mode == "perceptual":
                phash_hex = compute_phash(fname)

            # 内容级/近似 去重
            dup = False
            if args.dup_mode == "content" and sha256:
                if content_seen(conn, sha256):
                    dup = True
            elif args.dup_mode == "perceptual" and phash_hex:
                if any_phash_similar(conn, phash_hex, max_dist=args.phash_distance):
                    dup = True

            if dup:
                if args.dup_action == "skip":
                    with contextlib.suppress(Exception):
                        fname.unlink()
                    if args.verbose:
                        print(f"[dup-{args.dup_mode}/skip] {it.url}")
                    continue
                else:
                    # keep：保留文件，但不强制删除；尝试入库（不同 URL 会成功，同 URL 被 IGNORE）
                    inserted = record(
                        conn,
                        provider=it.provider, resid=str(Path(fname).parent.name), url=it.url, path=fname,
                        width=final_w, height=final_h, meta=it.meta,
                        content_sha256=sha256, etag=etag, last_modified=last_mod, phash=phash_hex
                    )
                    if inserted:
                        total_new_records += 1
                    if args.verbose:
                        print(f"[dup-{args.dup_mode}/keep] {it.url}")
                    continue

            # 非重复：正常入库
            inserted = record(
                conn,
                provider=it.provider, resid=str(Path(fname).parent.name), url=it.url, path=fname,
                width=final_w, height=final_h, meta=it.meta,
                content_sha256=sha256, etag=etag, last_modified=last_mod, phash=phash_hex
            )
            if inserted:
                total_new_records += 1
            if args.verbose:
                print(f"[ok] {fname.name} <- {it.url}")

    print(f"\n完成：新写入 {total_new_records} 条记录。根目录：{POPULAR_ROOT}")
    if Image is None and args.exact:
        print("提示：未安装 Pillow，无法进行精确裁剪/校验。可：pip install pillow")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[abort] 用户中断 (Ctrl+C)")
        raise SystemExit(130)
    except Exception:
        import traceback; traceback.print_exc()
        raise SystemExit(1)
