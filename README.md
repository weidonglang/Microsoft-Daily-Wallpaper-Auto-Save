# Microsoft-Daily-Wallpaper-Auto-Save

一个实用、可扩展的**壁纸自动保存**工具集，包含：

* **Bing 每日壁纸下载器**：并发抓取 4K/2K/1K，自动补齐缺失分辨率，按年/月归档并生成“分类镜像（硬链接/回退复制）”。（基于 Bing 的公开接口 `HPImageArchive.aspx`；历史记录通过社区归档补全）([Microsoft Learn][1])
* **流行壁纸抓取器（多源）**：国内/国外多站点（360 壁纸、Wallhaven、Openverse、Wikimedia）并发检索 + 断点续传 + 内容/感知去重，统一保存到 `popular/<provider>/<res>/`。支持随机/最新/热榜、关键词等筛选；网络异常自动跳过。([wallhaven.cc][2])
* **统一启动器**：`run_wallpapers.py` 可选择运行 **Bing** / **Popular** / **Both**，支持 `profiles.toml` 预设与命令行即时覆盖。

> **致谢与来源**
>
> * Bing 每日图片元数据通过 `HPImageArchive.aspx` 获取（`idx/n/mkt/uhd`；`n` 范围 0–7），历史常需借助第三方归档（例如 `bing.npanuhin.me`）。([Microsoft Learn][1])
> * Wallhaven 官方 API 提供 `sorting`（`date_added/toplist/random`）、`atleast`（最小分辨率）等筛选参数。([wallhaven.cc][2])
> * Openverse API 提供开放授权图片检索，支持访问令牌；其内容以 CC 许可或公有领域为主，但仍需自行核验具体条目许可。([api.openverse.org][3])
> * Wikimedia 通过 MediaWiki Action API 的 `imageinfo` 模块可查询图片原始 URL 与尺寸。([MediaWiki][4])

---

## 目录结构

```
BingWallpapers/
  4k/ 2025/10/ 20251022-贝洛格拉齐克石林.4k.jpg
  2k/ 2025/10/ 20251022-贝洛格拉齐克石林.2k.jpg
  1k/ 2025/10/ 20251022-贝洛格拉齐克石林.1k.jpg
  4k/ 动物/…    4k/ 自然景色/…   4k/ 其他/…   ← 分类镜像（硬链接/复制）

popular/
  wallhaven/
    4k/ *.jpg…
    2k/ *.jpg…
  q360/
    4k/ …
  openverse/
    4k/ …
  wikimedia/
    4k/ …
  popular.sqlite3            ← 下载/去重/条件请求等的记录库
```

---

## 安装

> **Python 版本**：建议 3.11/3.12（3.10+ 也可）。
> **Windows**、**macOS**、**Linux** 通用。

### 方式 A：一把梭（推荐）

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> 你也可以按功能最小化安装（见下）。

### 方式 B：分模块最小化安装

* **Bing 每日壁纸下载器**：

  ```bash
  pip install aiohttp aiosqlite pillow
  ```
* **流行壁纸抓取器**：

  ```bash
  pip install requests tqdm pillow
  # 可选：近似去重（感知哈希）
  pip install imagehash
  # Python 3.10 及更早版本读取 profiles.toml 需要：
  pip install tomli
  ```

---

## 快速开始

### 1）仅运行 Bing 每日壁纸

```bash
python bing_daily_wallpaper.py --years 1 --concurrency 12 --debug
```

* `--dir` 输出目录（默认 `./BingWallpapers`）
* `--mkt` 市场（默认 `zh-CN`，可改 `en-US/ja-JP/...`）
* `--years` 回溯年数（比如 0=仅当日，1=近一年）
* `--no-gen-missing` 不从 4K 自动补 2K/1K

> 说明：Bing 官方接口可获取当日及近几天元数据（`idx/n/mkt/uhd`），但**不提供全量历史**；`n` 的有效范围为 0–7。历史通常通过第三方归档补齐。([Microsoft Learn][1])

### 2）仅运行“流行壁纸抓取器”

```bash
python fetch_popular_wallpapers.py \
  --providers q360 wallhaven \
  --res 4k 2k 1k \
  --limit-per 60 \
  --wh-sorting date_added \
  --dup-mode content --dup-action keep \
  --max-workers 16 --robots on --verbose
```

常用参数：

* `--providers`：`q360 wallhaven openverse wikimedia`（任意组合）

  * **Wallhaven 参数**：`--wh-sorting date_added|toplist|random`、`--wh-toprange 1w|1M|...`、`--wh-seed 20251022`（随机+复现）([wallhaven.cc][2])
* `--res`：`4k 2k 1k`
* `--q`：关键词检索（q360/wallhaven/openverse 支持）
* `--dup-mode`：`url`（URL 级） / `content`（SHA-256 内容级）/ `perceptual`（感知哈希近似）
* `--dup-action`：`keep`（默认，保留新文件） / `skip`（节省磁盘）
* `--robots on|off|strict`：是否遵守 `robots.txt`
* 断点续传、304 条件请求、网络错误自动跳过均为内建默认行为。

> 提示：
>
> * Wallhaven 的 `atleast`（最小分辨率）、`sorting`（最新/热榜/随机）等均受官方 API 支持。([wallhaven.cc][2])
> * Openverse 需令牌可获得更稳配额；其内容以 CC 许可/公有领域为主，但**仍需自行核实每张图片的具体许可**。([api.openverse.org][3])
> * Wikimedia 可用 `imageinfo` 获取原图 URL 与尺寸做筛选。([MediaWiki][4])

### 3）用统一启动器（可二选一/同时跑）

先生成一个模板配置：

```bash
python run_wallpapers.py --init
```

跑 **两个** 子程序（先 Bing 后 Popular）：

```bash
python run_wallpapers.py --target both --profile default --verbose-launcher
```

只跑 **Popular** 并覆盖部分参数（`--` 之后原样透传给子脚本）：

```bash
python run_wallpapers.py --target popular --profile default -- \
  --providers q360 wallhaven --res 4k 2k --limit-per 60 \
  --dup-mode content --dup-action keep --wh-sorting random --wh-seed 20251022
```

---

## 配置：`profiles.toml`（可选但推荐）

```toml
[profiles.default]
  [profiles.default.bing]
  years = 1
  mkt = "zh-CN"
  concurrency = 8
  debug = false

  [profiles.default.popular]
  providers = ["q360", "wallhaven", "openverse"]
  res = ["4k", "2k", "1k"]
  limit_per = 60
  max_workers = 16
  dup_mode = "content"     # url | content | perceptual
  dup_action = "keep"      # keep | skip
  robots = "on"            # on | off | strict
  wh_sorting = "date_added"
  # wh_toprange = "1M"     # toplist 时使用
  # wh_seed = "20251022"   # random 时可复现
  verbose = true
```

运行时使用：

```bash
python run_wallpapers.py --target both --profile default
```

---

## 去重策略 & 数据库说明

* **URL 级**：同一 `(provider, resid, url)` 直接跳过（下载前即可判定）。
* **内容级**：计算 SHA-256，发现已存在内容时，默认 **保留** 新下载文件并忽略重复记录；也可 `--dup-action skip` 丢弃新文件。
* **近似去重**（可选）：感知哈希 pHash，对相似图（默认汉明距离 ≤5）进行合并/忽略（需 `imagehash`）。

所有抓取/去重/条件请求（ETag/Last-Modified）信息保存在 `popular/popular.sqlite3`，支持断点续传与 304。HTTP 条件请求/续传是通用 Web 标准行为，服务端返回 `206 Partial Content` 或 `304 Not Modified` 时可显著减少传输。([wallhaven.cc][2])

---

## 常见问题（FAQ）

* **Bing 为什么有时只有近 8 天？**
  因为 `HPImageArchive.aspx` 的 `n` 参数官方范围是 0–7（最多返回近 8 张）。更早历史需用第三方归档源补齐。([Microsoft Learn][1])

* **Wallhaven “热榜”总是那几张？**
  可改用 `--wh-sorting date_added`（按最新）或 `random`（配 `--wh-seed` 做可复现随机），并调大 `--limit-per` 翻页抓取。([wallhaven.cc][2])

* **Openverse 是否可直接商用？**
  Openverse 聚合了 CC 许可/公有领域内容，但平台本身不保证每条目许可准确性，**发布/分发请务必自行复核**。([docs.openverse.org][5])

---

## 贡献与致谢

* 感谢 Bing 提供每日图片内容接口与社区归档生态。接口参数示例与讨论可见相关 Q&A/帖子。([Microsoft Learn][1])
* 感谢 Wallhaven、Openverse、Wikimedia 开放 API。本文档中参数示例/注意事项依据其官方文档。([wallhaven.cc][2])

---

## 免责声明

* 本项目仅用于**学习与个人收藏**。请遵守各站点 `robots.txt`、图片许可与使用条款。对第三方内容的版权与合规性请**自行负责**。Openverse 条款特别提醒：务必核实单条目许可信息。([docs.openverse.org][5])

---

## 变更摘要（相对旧版）

* 新增 **流行壁纸抓取器（多源）**，支持国内/国外源、并发检索与断点续传，内容/近似去重；失败自动跳过，不中断整体任务。
* 新增 **统一启动器**，支持 `profiles.toml` 与命令行覆盖；一条命令可运行 `bing/popular/both`。
* Bing 模块保留原有并发下载、自动补齐、分类镜像与详尽日志。

---
Here’s a complete, polished **English README** for your repo. Feel free to paste it into `README.md` as-is.

---

# Microsoft-Daily-Wallpaper-Auto-Save

A practical, extensible toolkit for **saving wallpapers automatically**. It includes:

* **Bing Daily Wallpaper Downloader** – fetches 4K/2K/1K with concurrency, auto-fills missing resolutions, archives by year/month, and mirrors category folders via hard links (with safe fallback). Uses Bing’s public `HPImageArchive.aspx` feed (with `idx`, `n`, `mkt`, `uhd` parameters); historical listings are limited and typically supplemented by community archives. ([Stack Overflow][1])
* **Popular Wallpaper Fetcher (multi-source)** – concurrently searches multiple domestic/international sources (360 Wallpapers, Wallhaven, Openverse, Wikimedia), supports resumable downloads, content/perceptual de-duplication, fair scheduling across providers, and robust error handling. Wallhaven query controls like `sorting`, `topRange`, and `atleast` are supported. ([wallhaven.cc][2])
* **Unified Launcher** – `run_wallpapers.py` runs **Bing**, **Popular**, or **Both**, reading `profiles.toml` presets and allowing one-off overrides via CLI.

> **Notes & Sources**
>
> * The `HPImageArchive.aspx` endpoint is a commonly used public feed for Bing’s daily image; `idx` and `n` drive the date window (with `n` effectively limited to 0–7 in practice). ([Stack Overflow][1])
> * Wallhaven API docs explain `sorting` (e.g., `date_added`, `toplist`, `random`), `topRange` (for toplist), and `atleast` (minimum resolution). ([wallhaven.cc][2])
> * Openverse provides an API (token support available) for openly licensed media; verify license per item. ([api.openverse.org][3])
> * Wikimedia’s MediaWiki API (`imageinfo`) returns original image URLs and sizes for filtering. ([MediaWiki][4])
> * Resumable downloads use standard HTTP Range/Conditional requests (`206 Partial Content`; `If-None-Match` → `304 Not Modified`). ([MDN文档][5])

---

## Features

* **Bing module**

  * Parallel downloads; **automatic 2K/1K generation** from 4K (optional).
  * Date-structured output (`YYYY/MM`) and **category mirrors** created via hard links (falls back to copy if needed).
  * Solid logging, manifest, and safety checks.

* **Popular module**

  * Multiple providers: **q360** (CN-friendly), **Wallhaven**, **Openverse**, **Wikimedia**.
  * **Resumable** downloads with `.part` files + image integrity check, **conditional requests** (ETag/Last-Modified).
  * **De-dup** modes: URL / content (SHA-256) / perceptual (pHash); on duplicates, you can **keep** or **skip** the new file.
  * **Fair round-robin scheduling** so one provider can’t starve the others.
  * **Robust**: provider/network errors are handled gracefully and don’t crash the run.

* **Launcher**

  * `run_wallpapers.py` runs **Bing**, **Popular**, or **Both** from a single command.
  * Uses `profiles.toml` presets; anything after `--` is passed verbatim to the chosen script.
  * Shows merged effective options and the final command; supports dry-run.

---

## Directory Layout

```
BingWallpapers/
  4k/ 2025/10/ 20251022-Varna_Bulgaria.4k.jpg
  2k/ 2025/10/ ...
  1k/ 2025/10/ ...
  4k/ Animals/ ...    4k/ Landscapes/ ...  4k/ Other/ ...   ← category mirrors (hard links / fallback copy)

popular/
  wallhaven/
    4k/ *.jpg ...
    2k/ ...
  q360/
    4k/ ...
  openverse/
    4k/ ...
  wikimedia/
    4k/ ...
  popular.sqlite3      ← metadata DB for de-dup & conditional requests
```

---

## Installation

> **Python**: 3.11/3.12 recommended (3.10+ is fine). Works on **Windows/macOS/Linux**.

### One-shot (recommended)

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Minimal per module

* **Bing**:

  ```bash
  pip install aiohttp aiosqlite pillow
  ```
* **Popular**:

  ```bash
  pip install requests tqdm pillow
  # Optional perceptual de-dup:
  pip install imagehash
  # If using Python ≤ 3.10 and you want profiles:
  pip install tomli
  ```

---

## Quick Start

### 1) Only Bing Daily Wallpapers

```bash
python bing_daily_wallpaper.py --years 1 --concurrency 12 --debug
```

Key flags:

* `--dir` output directory (default `./BingWallpapers`)
* `--mkt` market (default `zh-CN`, e.g. `en-US/ja-JP/...`)
* `--years` look-back years (e.g. `0` = today only, `1` = last year)
* `--no-gen-missing` don’t auto-generate 2K/1K from 4K

> Bing’s feed is **not** a full historical API; `idx`/`n` typically cover **only the last ~8 images**. You can supplement older items via community archives. ([Microsoft Learn][6])

### 2) Only Popular Wallpapers (multi-source)

```bash
python fetch_popular_wallpapers.py \
  --providers q360 wallhaven \
  --res 4k 2k 1k \
  --limit-per 60 \
  --wh-sorting date_added \
  --dup-mode content --dup-action keep \
  --max-workers 16 --robots on --verbose
```

Common options:

* `--providers`: `q360 wallhaven openverse wikimedia` (mix & match)

  * **Wallhaven**: `--wh-sorting date_added|toplist|random`, `--wh-toprange 1w|1M|…` (for toplist), `--wh-seed 20251022` (random + reproducible). ([wallhaven.cc][2])
* `--res`: `4k 2k 1k`
* `--q`: keyword search (q360 / wallhaven / openverse)
* `--dup-mode`: `url` / `content` (SHA-256) / `perceptual` (pHash)
* `--dup-action`: `keep` (default) / `skip`
* `--robots on|off|strict`
* Resumable/conditional behavior is automatic (`Range` → `206`, `If-None-Match` → `304`). ([MDN文档][5])

> **Openverse**: tokens are supported and improve stability/quotas; always double-check the license of each item before redistribution. ([api.openverse.org][3])
> **Wikimedia**: we use `imageinfo` to retrieve original URLs and sizes for resolution filters. ([MediaWiki][4])

### 3) Unified Launcher (Bing / Popular / Both)

Generate a template config:

```bash
python run_wallpapers.py --init
```

Run **both** (Bing first, then Popular) using the `default` profile:

```bash
python run_wallpapers.py --target both --profile default --verbose-launcher
```

Run **only Popular** and override a few options (everything after `--` is passed to the Popular script):

```bash
python run_wallpapers.py --target popular --profile default -- \
  --providers q360 wallhaven --res 4k 2k --limit-per 60 \
  --dup-mode content --dup-action keep --wh-sorting random --wh-seed 20251022
```

---

## Configuration: `profiles.toml` (optional but recommended)

```toml
[profiles.default]
  [profiles.default.bing]
  years = 1
  mkt = "zh-CN"
  concurrency = 8
  debug = false

  [profiles.default.popular]
  providers   = ["q360", "wallhaven", "openverse"]
  res         = ["4k", "2k", "1k"]
  limit_per   = 60
  max_workers = 16
  dup_mode    = "content"     # url | content | perceptual
  dup_action  = "keep"        # keep | skip
  robots      = "on"          # on | off | strict
  wh_sorting  = "date_added"  # or toplist/random
  # wh_toprange = "1M"        # when sorting = toplist
  # wh_seed     = "20251022"  # when sorting = random
  verbose     = true
```

Run with:

```bash
python run_wallpapers.py --target both --profile default
```

---

## De-dup Strategy & Database

* **URL-level**: skip before download when `(provider,resid,url)` repeats.
* **Content-level**: SHA-256 digest; by default we **keep** the newly downloaded file but ignore duplicate records (choose `--dup-action skip` to discard new files).
* **Perceptual**: optional pHash similarity check for near-duplicates.

The fetcher stores metadata in `popular/popular.sqlite3` (including ETag/Last-Modified) and supports **resumable** transfers (`Range`) and **conditional** GETs (`304`). ([MDN文档][5])

---

## FAQ

**Why does Bing only provide the most recent ~8 images?**
Because the public `HPImageArchive.aspx` feed’s `n` parameter effectively supports 0–7. For older images, use third-party archives. ([Microsoft Learn][6])

**Wallhaven “Toplist” keeps repeating the same set**
Switch to `--wh-sorting date_added` (newest first) or `random` (optionally add `--wh-seed` for reproducibility), and increase `--limit-per` to traverse more pages. ([wallhaven.cc][2])

**Can I use Openverse images commercially?**
Openverse indexes openly licensed/public-domain works, but you must verify the **specific license and attribution** for each item before use. ([api.openverse.org][3])

---

## Compliance & Attribution

* Respect each site’s **terms** and **robots.txt**.
* Check **licenses** carefully (especially for Openverse/Wikimedia).
* HTTP behaviors (Range/Conditional) follow web standards (`206`, `304`) to reduce bandwidth. ([MDN文档][5])

---

## Changelog & Repository

Your historical changelog and repo are available here:
**GitHub:** `weidonglang/Microsoft-Daily-Wallpaper-Auto-Save` (this repository)

---

## Contributing

Issues and PRs are welcome. If you’d like a ready-made preset in `profiles.toml` (e.g., daily random Wallhaven + CN-friendly sources), open an issue and we’ll add it.

---

## License

This project is provided for educational and personal archival purposes. You are responsible for compliance with third-party content licenses and terms.


