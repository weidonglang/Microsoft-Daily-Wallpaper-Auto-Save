# -*- coding: utf-8 -*-
# version: 2025-10-21r6
"""
Bing 每日壁纸 —— 并发下载 + 强兜底 + 调试日志 + 近N年回溯 + 可从4K补齐2K/1K（含分辨率规范化）

目录结构：
  主文件：<4k|2k|1k>/<YYYY>/<MM>/<YYYYMMDD-标题>.<分辨率>.jpg
  镜像（平铺）：<4k|2k|1k>/<动物|自然景色|其他>/<YYYYMMDD-标题>.<分辨率>.jpg
"""

import argparse
import asyncio
import logging
import os
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
import aiosqlite
from PIL import Image

# ---------------- 常量与配置 ----------------
BING_HOST = "https://www.bing.com"
API_PATH  = "/HPImageArchive.aspx"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# 分辨率尝试顺序（2K 先 1920x1200，再 2560x1440，命中率更高）
RES_MAP: Dict[str, List[str]] = {
    "4k": ["UHD"],                         # 3840x2160
    "2k": ["1920x1200", "2560x1440"],
    "1k": ["1920x1080"]
}

# 归档（JSON 清单 + 直链存储）
ARCHIVE_API_BASE = "https://bing.npanuhin.me"

# 分类关键词（简化）
ANIMAL_WORDS = {"animal","wildlife","bird","toucan","lynx","penguin","bear","fox","tiger","lion",
                "猫","狗","动物","鸟","巨嘴鸟","猞猁","企鹅","熊","狐狸","虎","狮"}
NATURE_WORDS = {"nature","landscape","mountain","forest","lake","river","waterfall","desert","ocean","beach",
                "自然","风景","山","森林","湖","河","瀑布","沙漠","海","海滩","峡谷","草原","极光","银河"}

# ---------------- 日志 ----------------
log = logging.getLogger("bingwall")
def setup_logging(debug: bool):
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler()
    fmt = "%(asctime)s [%(levelname)s] %(message)s" if debug else "%(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    log.setLevel(level)
    log.handlers.clear()
    log.addHandler(handler)

# ---------------- 工具函数 ----------------
def safe_filename(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]", "_", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120]

def choose_title(date_str: str, title_or_copyright: Optional[str]) -> str:
    title = re.sub(r"\s*\(©.*?\)\s*$", "", (title_or_copyright or "BingDaily"))
    return safe_filename(f"{date_str}-{title}")

def text_coalesce(*xs) -> str:
    return " ".join([(x if isinstance(x, str) else ("" if x is None else str(x))) for x in xs])

def classify_text(text: str) -> List[str]:
    t = text or ""
    low = t.lower()
    cats = []
    if any((w in low) or (w in t) for w in ANIMAL_WORDS): cats.append("动物")
    if any((w in low) or (w in t) for w in NATURE_WORDS): cats.append("自然景色")
    if not cats: cats = ["其他"]
    return cats

def build_from_urlbase(urlbase: str, suffix: str) -> str:
    return f"{BING_HOST}{urlbase}_{suffix}.jpg"

def swap_suffix_in_bing_url(bing_url: str, new_suffix: str) -> Optional[str]:
    m = re.search(r"(.*?)_(UHD|\d+x\d+)\.jpg(.*)$", bing_url or "")
    if not m: return None
    return f"{m.group(1)}_{new_suffix}.jpg{m.group(3)}"

def mkt_to_country_lang(mkt: str) -> Tuple[str, str]:
    parts = (mkt or "en-US").split("-")
    lang = parts[0].lower() if parts else "en"
    country = parts[1].upper() if len(parts) > 1 else "US"
    return country, lang

def is_archive_storage(url: str) -> bool:
    return isinstance(url, str) and "bing.npanuhin.me" in url

def normalize_bing_host(url: str) -> str:
    """统一为 https://www.bing.com，避免 30x/缓存差异。"""
    if not isinstance(url, str) or not url:
        return url
    return re.sub(r"^https?://(?:www\.)?bing\.com", "https://www.bing.com", url)

# ---------------- Manifest（内存 + SQLite） ----------------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS files(
  mkt   TEXT NOT NULL,
  date  TEXT NOT NULL,
  res   TEXT NOT NULL,   -- 4k/2k/1k
  key   TEXT NOT NULL,   -- mkt:date:res
  path  TEXT NOT NULL,
  size  INTEGER NOT NULL,
  PRIMARY KEY(key)
);
"""

def make_key(mkt: str, date_str: str, res: str) -> str:
    return f"{mkt}:{date_str}:{res}"

class Manifest:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None
        self.cache: set[str] = set()
        self._lock: Optional[asyncio.Lock] = None

    async def __aenter__(self):
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.executescript(CREATE_SQL)
        async with self.conn.execute("SELECT key FROM files") as cur:
            async for (k,) in cur:
                self.cache.add(k)
        await self.conn.commit()
        self._lock = asyncio.Lock()
        log.debug(f"manifest loaded: {len(self.cache)} keys")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.conn:
            await self.conn.close()

    def has(self, key: str) -> bool:
        return key in self.cache

    async def add(self, mkt: str, date_str: str, res: str, path_str: str, size: int):
        key = make_key(mkt, date_str, res)
        if key in self.cache:
            return
        async with self._lock:
            await self.conn.execute(
                "INSERT OR REPLACE INTO files(mkt,date,res,key,path,size) VALUES(?,?,?,?,?,?)",
                (mkt, date_str, res, key, path_str, size)
            )
            await self.conn.commit()
        self.cache.add(key)
        log.debug(f"manifest add: {key} -> {path_str} ({size} bytes)")

# ---------------- HTTP（aiohttp） ----------------
class Http:
    def __init__(self, concurrency: int):
        self.sem = asyncio.Semaphore(concurrency)
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=60)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(limit=0),
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
        )
        log.debug(f"http client ready; concurrency={self.sem._value}")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session:
            await self.session.close()

    async def get_json(self, url: str, params: dict = None, retries: int = 3):
        assert self.session
        for i in range(retries):
            async with self.sem:
                try:
                    url = normalize_bing_host(url)
                    log.debug(f"GET JSON {url} params={params}")
                    async with self.session.get(url, params=params, allow_redirects=True) as r:
                        if r.status == 429:
                            ra = float(r.headers.get("Retry-After", "2"))
                            log.debug(f"429 Retry-After={ra} url={r.request_info.real_url}")
                            await asyncio.sleep(ra)
                            raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status)
                        r.raise_for_status()
                        return await r.json(content_type=None)
                except Exception as e:
                    log.debug(f"get_json err {i+1}/{retries}: {type(e).__name__} {e}")
                    await asyncio.sleep(min(1.5 * (i + 1), 6))
        raise RuntimeError(f"GET JSON failed: {url}")

    async def probe(self, url: str, retries: int = 2) -> bool:
        """先 HEAD；失败再 Range:bytes=0-0 轻探测。"""
        assert self.session
        url = normalize_bing_host(url)
        for i in range(retries):
            async with self.sem:
                try:
                    log.debug(f"HEAD {url}")
                    async with self.session.head(url, allow_redirects=True) as r:
                        if r.status == 200:
                            return True
                except Exception as e:
                    log.debug(f"HEAD fail: {type(e).__name__} {e}")
                try:
                    log.debug(f"PROBE {url} Range=0-0")
                    async with self.session.get(url, headers={"Range": "bytes=0-0"}, allow_redirects=True) as r:
                        if r.status in (200, 206):
                            return True
                except Exception as e:
                    log.debug(f"Range fail: {type(e).__name__} {e}")
                    await asyncio.sleep(min(1.5 * (i + 1), 4))
        return False

    async def download(self, url: str, retries: int = 3) -> Optional[bytes]:
        assert self.session
        url = normalize_bing_host(url)
        for i in range(retries):
            async with self.sem:
                try:
                    log.debug(f"GET {url}")
                    async with self.session.get(url, allow_redirects=True) as r:
                        status = r.status
                        if status == 429:
                            ra = float(r.headers.get("Retry-After", "2"))
                            log.debug(f"429 Retry-After={ra} url={r.request_info.real_url}")
                            await asyncio.sleep(ra)
                            raise aiohttp.ClientResponseError(r.request_info, r.history, status=status)
                        if status >= 400:
                            log.debug(f"GET status={status} url={r.request_info.real_url}")
                        r.raise_for_status()
                        blob = await r.read()
                        log.debug(f"GET ok ({len(blob)} bytes) url={r.request_info.real_url}")
                        return blob
                except aiohttp.ClientResponseError as e:
                    st = getattr(e, "status", None)
                    real = getattr(e, "request_info", None) and e.request_info.real_url
                    log.debug(f"GET err {i+1}/{retries}: status={st} url={real} exc={type(e).__name__}")
                    if st == 404:
                        return None  # 404 不重试，直接放弃该 URL
                except asyncio.TimeoutError:
                    log.debug(f"GET timeout {i+1}/{retries} url={url}")
                except Exception as e:
                    log.debug(f"GET err {i+1}/{retries} url={url} exc={type(e).__name__}: {e}")
                await asyncio.sleep(min(1.5 * (i + 1), 6))
        return None

# ---------------- 写盘与路径 ----------------
async def ensure_dir(d: Path):
    d.mkdir(parents=True, exist_ok=True)

async def save_with_mirrors(content: bytes, primary: Path, mirror_dirs: List[Path]):
    await ensure_dir(primary.parent)
    if not primary.exists() or primary.stat().st_size == 0:
        primary.write_bytes(content)
        print(f"[ok] 主文件：{primary}")
    else:
        print(f"[skip] 已存在：{primary}")

    for d in mirror_dirs:
        await ensure_dir(d)
        mirror = d / primary.name
        if mirror.exists():
            log.debug(f"mirror exists: {mirror}")
            continue
        try:
            os.link(primary, mirror)  # 同盘符硬链接
            print(f"[link] 硬链接：{mirror}")
        except Exception:
            mirror.write_bytes(content)
            print(f"[copy] 复制镜像：{mirror}")

def build_paths(root: Path, res_tag: str, y: str, m: str, fname: str, cats: List[str]) -> Tuple[Path, List[Path]]:
    primary = root / res_tag / y / m / fname
    mirrors = [root / res_tag / cat for cat in cats]  # 平铺分类
    return primary, mirrors

# ---------------- 下采样与分辨率规范化 ----------------
def downscale_from_4k(uhd_bytes: bytes, target: str) -> Optional[bytes]:
    """target in {'2k','1k'}：从4K生成对应分辨率（等比缩小到上限盒）。"""
    try:
        im = Image.open(BytesIO(uhd_bytes)).convert("RGB")
        if target == "2k":
            im.thumbnail((2560, 1440), Image.LANCZOS)
        else:  # 1k
            im.thumbnail((1920, 1080), Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=92, optimize=True, subsampling=2)
        return buf.getvalue()
    except Exception as e:
        log.debug(f"downscale error: {type(e).__name__} {e}")
        return None

def normalize_to_res(blob: bytes, target: str) -> bytes:
    """
    确保输出符合 target（'2k' -> <=2560x1440, '1k' -> <=1920x1080）。
    若输入已不大于目标盒，原样返回；若更大则等比缩小到目标盒内。
    """
    box = (2560, 1440) if target == "2k" else (1920, 1080)
    try:
        im = Image.open(BytesIO(blob)).convert("RGB")
        w, h = im.size
        if w <= box[0] and h <= box[1]:
            return blob
        im.thumbnail(box, Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=92, optimize=True, subsampling=2)
        return buf.getvalue()
    except Exception:
        return blob  # 出错时退回原始字节，避免中断

# ---------------- 单日/单条逻辑 ----------------
async def fetch_and_save_for_meta(http: Http, manifest: Manifest, root: Path, mkt: str, meta: dict,
                                  gen_missing: bool):
    d = meta.get("startdate") or meta.get("fullstartdate") or ""
    y, m = (d[:4], d[4:6]) if len(d) >= 6 else ("unknown", "unknown")
    date8 = d[:8] if len(d) >= 8 else datetime.now().strftime("%Y%m%d")

    title_src = meta.get("title") or meta.get("copyright") or "BingDaily"
    cats = classify_text(text_coalesce(meta.get("title"), meta.get("caption"), meta.get("copyright")))
    base = choose_title(date8, title_src)

    urlbase = meta.get("urlbase")
    uhd_direct = f"{BING_HOST}{meta['url']}" if meta.get("url") else None  # _UHD.jpg

    uhd_blob: Optional[bytes] = None

    for res_tag, suffixes in RES_MAP.items():
        fname = f"{base}.{res_tag}.jpg"
        primary, mirrors = build_paths(root, res_tag, y, m, fname, cats)
        key = make_key(mkt, date8, res_tag)

        # 若 4K 已存在：读盘注入 uhd_blob，供 2K/1K 生成
        if primary.exists() and primary.stat().st_size > 0:
            if res_tag == "4k":
                try:
                    uhd_blob = primary.read_bytes()
                    log.debug(f"loaded 4k from disk for {date8}")
                except Exception as e:
                    log.debug(f"load 4k from disk failed: {type(e).__name__} {e}")
            log.debug(f"skip exist primary {primary}")
            await manifest.add(mkt, date8, res_tag, str(primary), primary.stat().st_size)
            continue
        if manifest.has(key):
            log.debug(f"skip manifest {key}")
            continue

        content = None
        tried_urls: List[str] = []
        for suf in suffixes:
            url = (uhd_direct if (suf == "UHD" and uhd_direct) else (build_from_urlbase(urlbase, suf) if urlbase else None))
            if not url:
                continue
            url = normalize_bing_host(url)
            tried_urls.append(url)
            if is_archive_storage(url):
                content = await http.download(url)
                if content: break
            else:
                ok = await http.probe(url)
                if not ok:
                    log.debug(f"probe fail, force GET {url}")
                    content = await http.download(url)
                    if content: break
                    continue
                content = await http.download(url)
                if content: break

        if not content:
            if gen_missing and res_tag in ("2k","1k") and uhd_blob:
                log.debug(f"generate {res_tag} from 4k for {date8}")
                content = downscale_from_4k(uhd_blob, res_tag)
            else:
                log.debug(f"fail today {res_tag} tried={tried_urls}")
                continue

        # 记录 4K；规范化 2K/1K
        if res_tag == "4k":
            uhd_blob = content
        elif res_tag in ("2k", "1k"):
            before = len(content)
            content = normalize_to_res(content, res_tag)
            if len(content) != before:
                log.debug(f"normalize {res_tag}: {before} -> {len(content)} bytes")

        await save_with_mirrors(content, primary, mirrors)
        await manifest.add(mkt, date8, res_tag, str(primary), len(content))

async def process_archive_item(http: Http, manifest: Manifest, root: Path, mkt: str, it: dict,
                               gen_missing: bool):
    date_str = (it.get("date") or "").replace("-", "")
    ystr, mstr = (date_str[:4], date_str[4:6]) if len(date_str) >= 6 else ("unknown", "unknown")
    base = choose_title(date_str[:8] if date_str else datetime.now().strftime("%Y%m%d"),
                        it.get("title") or it.get("copyright") or "BingDaily")
    cats = classify_text(text_coalesce(it.get("title"), it.get("description"), it.get("copyright")))

    bing_url = it.get("bing_url") or ""  # 源站（老年份常失效）
    storage  = it.get("url") or ""       # 归档直链（至少 ~1k）

    uhd_blob: Optional[bytes] = None

    for res_tag, suffixes in [("4k", RES_MAP["4k"]), ("2k", RES_MAP["2k"]), ("1k", RES_MAP["1k"])]:
        date8 = date_str[:8] if date_str else ""
        fname = f"{base}.{res_tag}.jpg"
        primary, mirrors = build_paths(root, res_tag, ystr, mstr, fname, cats)
        key = make_key(mkt, date8, res_tag)

        # 若 4K 已存在：读盘注入 uhd_blob，供 2K/1K 生成
        if primary.exists() and primary.stat().st_size > 0:
            if res_tag == "4k":
                try:
                    uhd_blob = primary.read_bytes()
                    log.debug(f"loaded 4k from disk for {date8}")
                except Exception as e:
                    log.debug(f"load 4k from disk failed: {type(e).__name__} {e}")
            await manifest.add(mkt, date8, res_tag, str(primary), primary.stat().st_size)
            continue
        if manifest.has(key):
            continue

        urls: List[str] = []
        if res_tag in ("4k","2k") and bing_url:
            for suf in suffixes:
                alt = swap_suffix_in_bing_url(bing_url, suf)
                if alt: urls.append(alt)
        if res_tag == "1k":
            if storage: urls.append(storage)  # 归档直链优先
            if bing_url:
                alt = swap_suffix_in_bing_url(bing_url, "1920x1080")
                if alt: urls.append(alt)
        urls = [normalize_bing_host(u) for u in urls]

        content = None
        for u in urls:
            log.debug(f"try {res_tag}: {u}")
            if is_archive_storage(u):
                content = await http.download(u)  # 直链：直接 GET
                if content: break
            else:
                ok = await http.probe(u)
                if not ok:
                    log.debug(f"probe fail, force GET {u}")
                    content = await http.download(u)
                    if content: break
                    continue
                content = await http.download(u)
                if content: break

        if not content and gen_missing and res_tag in ("2k","1k") and uhd_blob:
            log.debug(f"generate {res_tag} from 4k for {date8}")
            content = downscale_from_4k(uhd_blob, res_tag)

        if not content:
            log.debug(f"fail archive {res_tag} date={date8}")
            continue

        # 记录 4K；规范化 2K/1K
        if res_tag == "4k":
            uhd_blob = content
        elif res_tag in ("2k","1k"):
            before = len(content)
            content = normalize_to_res(content, res_tag)
            if len(content) != before:
                log.debug(f"normalize {res_tag}: {before} -> {len(content)} bytes")

        await save_with_mirrors(content, primary, mirrors)
        await manifest.add(mkt, date8, res_tag, str(primary), len(content))

# ---------------- 今日与回溯 ----------------
async def fetch_today(http: Http, manifest: Manifest, root: Path, mkt: str, gen_missing: bool):
    params = {"format": "js", "idx": "0", "n": "1", "mkt": mkt, "uhd": "1"}
    data = await http.get_json(f"{BING_HOST}{API_PATH}", params=params)
    images = data.get("images") or []
    if not images:
        print("[warn] 今日接口无数据"); return
    meta = images[0]
    if meta.get("copyright"):
        print(f"[info] 今日：{meta['copyright']}")
    await fetch_and_save_for_meta(http, manifest, root, mkt, meta, gen_missing)

async def load_archive_list(http: Http, country: str, lang: str, year: Optional[int]) -> List[dict]:
    if year is not None:
        try:
            return await http.get_json(f"{ARCHIVE_API_BASE}/{country}/{lang}.{year}.json")
        except Exception as e:
            log.debug(f"load year {year} fail: {type(e).__name__} {e}")
    all_data = await http.get_json(f"{ARCHIVE_API_BASE}/{country}/{lang}.json")
    if year is None:
        return all_data
    yprefix = f"{year}-"
    return [r for r in all_data if str(r.get("date","")).startswith(yprefix)]

async def backfill_years(http: Http, manifest: Manifest, root: Path, mkt: str, years: int,
                         gen_missing: bool, jobs: int = 8):
    country, lang = mkt_to_country_lang(mkt)
    cur = datetime.now().year
    targets = [cur - i for i in range(years)]

    all_items: List[dict] = []
    for y in targets:
        try:
            items = await load_archive_list(http, country, lang, y)
            all_items.extend(items)
            log.debug(f"year {y} items={len(items)}")
        except Exception as e:
            print(f"[warn] 年份 {y} 列表获取失败：{e}")

    all_items.sort(key=lambda x: x.get("date",""), reverse=True)

    sem = asyncio.Semaphore(max(1, jobs))

    async def worker(it):
        async with sem:
            try:
                await process_archive_item(http, manifest, root, mkt, it, gen_missing)
            except Exception as e:
                log.exception(f"archive item failed: date={it.get('date')} exc={type(e).__name__}")

    await asyncio.gather(*(worker(it) for it in all_items))

# ---------------- 入口 ----------------
async def main():
    ap = argparse.ArgumentParser(description="Bing 壁纸下载（并发 + 强兜底 + 回溯 N 年 + 可下采样补齐）")
    ap.add_argument("--dir", dest="out_dir", default="BingWallpapers", help="保存根目录（默认 ./BingWallpapers）")
    ap.add_argument("--mkt", dest="mkt", default="zh-CN", help="市场/地区（默认 zh-CN）")
    ap.add_argument("--years", type=int, default=5, help="回溯年数（默认 5）")
    ap.add_argument("--concurrency", type=int, default=8, help="HTTP 并发数（默认 8，建议 6~16）")
    ap.add_argument("--jobs", type=int, default=None, help="回溯任务并发数（默认同 --concurrency）")
    ap.add_argument("--debug", action="store_true", help="输出调试日志")
    ap.add_argument("--no-gen-missing", action="store_true", help="不要从 4K 自动补齐 2K/1K")
    args = ap.parse_args()

    setup_logging(args.debug)
    root = Path(args.out_dir)
    root.mkdir(parents=True, exist_ok=True)

    gen_missing = not args.no_gen_missing
    jobs = args.jobs if args.jobs is not None else args.concurrency

    async with Manifest(root / "downloads.sqlite3") as manifest:
        async with Http(args.concurrency) as http:
            try:
                await fetch_today(http, manifest, root, args.mkt, gen_missing)
            except Exception as e:
                print(f"[warn] 今日获取失败：{e}")
            try:
                await backfill_years(http, manifest, root, args.mkt, args.years, gen_missing, jobs=jobs)
            except Exception as e:
                print(f"[warn] 回溯失败：{e}")

    print(f"[done] 根目录：{root.resolve()}")

if __name__ == "__main__":
    asyncio.run(main())
