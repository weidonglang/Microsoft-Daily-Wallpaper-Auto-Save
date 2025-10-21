# Bing 每日壁纸下载器 · Async + 4K/2K/1K + 分类镜像

一个**并发**下载 Bing 每日壁纸的 Python 脚本，支持：

* **分辨率**：4K / 2K / 1K（缺失时可由 4K**自动下采样补齐**）
* **目录结构**：按 `年/月` 归档，并在同级建立「动物 / 自然景色 / 其他」**平铺分类镜像**（使用**硬链接**；跨盘符自动回退为文件复制）
* **回溯下载**：可一键回补近 N 年历史
* **稳定 & 高速**：`aiohttp` 并发、失败重试、SQLite manifest（WAL 模式）避免重复请求
* **强兜底**：自动从社区归档获取历史条目（遇到大图会在本地**规范化落尺**为 2K/1K）
* **可观测**：`--debug` 输出详细日志（HEAD/GET/重试/生成/落尺/硬链接等）

> ⚙️ 依据：Bing 的公开接口 `HPImageArchive.aspx` 可获取当期/近几天的元数据（参数 `idx/n/mkt/uhd`），但 **`n` 仅 1–8**，官方**不提供**全量历史接口；历史通常借助第三方归档（如 `bing.npanuhin.me`）。非 4K 的分辨率通常依据 `urlbase` 规则拼接，例如 `_1920x1080.jpg`、`_1920x1200.jpg`。([微软学习][1])

---

## 目录结构

主文件（按年月归档）：

```
BingWallpapers/
  4k/  2025/10/ 20251020-鸟喙的故事.4k.jpg
  2k/  2025/10/ 20251020-鸟喙的故事.2k.jpg
  1k/  2025/10/ 20251020-鸟喙的故事.1k.jpg
```

平铺分类镜像（硬链接或复制）：

```
BingWallpapers/
  4k/ 动物/       20251020-鸟喙的故事.4k.jpg
  4k/ 自然景色/   ...
  4k/ 其他/       ...
  2k/ 动物/ ...
  1k/ 自然景色/ ...
```

> 注：Windows 下**硬链接**仅限**同一分区**。若不在同一卷或创建失败，脚本会自动回退为复制。

---

## 安装

```bash
python -m pip install --upgrade pip
pip install aiohttp aiosqlite pillow
```

* Python 3.10+（建议 3.11/3.12）
* 依赖：`aiohttp`、`aiosqlite`、`Pillow`

---

## 使用

```bash
# 最常用：回溯近 1 年，12 并发，显示调试日志
python bing_daily_wallpaper.py --years 1 --concurrency 12 --debug
```

### 命令行参数

* `--dir`：保存根目录（默认 `./BingWallpapers`）
* `--mkt`：市场/地区（默认 `zh-CN`；常见值如 `en-US`、`ja-JP` 等）

  > Bing 官方接口按市场返回当天图片的元数据。可在此处切换不同市场。([codeproject.com][2])
* `--years`：回溯年数（默认 5）
* `--concurrency`：HTTP 并发数（默认 8，建议 6–16）
* `--jobs`：回溯任务并发（默认与 `--concurrency` 相同；若设为 1，日志更接近“同一天 4K→2K→1K 连续输出”，便于肉眼核对）
* `--debug`：打印详细调试日志
* `--no-gen-missing`：不从 4K 自动补齐 2K/1K（默认会补齐）

### 运行示例

```bash
# 回溯近 5 年，8 并发
python bing_daily_wallpaper.py --years 5 --concurrency 8

# 仅当日（不回溯），保守请求，且不启用 4K 下采样补齐
python bing_daily_wallpaper.py --years 0 --concurrency 4 --no-gen-missing

# 切换市场为 en-US
python bing_daily_wallpaper.py --years 1 --mkt en-US --concurrency 12 --debug
```

---

## 工作原理（简述）

1. **今日数据**：调用
   `https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=<MKT>&uhd=1`
   获取当日元数据与 4K 直链；其它分辨率由 `urlbase` 规则尝试拼接。([codeproject.com][2])
2. **历史回溯**：按年份从 `bing.npanuhin.me` 拉取 JSON 清单（或全量清单再按年筛），对每条记录尝试微软源与归档直链；归档直链返回**大图**时，本地**规范化落尺**到 2K/1K 保存。([GitHub][3])
3. **三分辨率策略**：

   * **4K**：优先使用官方直链 `_UHD.jpg`；
   * **2K/1K**：按常见后缀试探（例：`_1920x1200.jpg`、`_1920x1080.jpg`），失败则：

     * 若有 4K：**从 4K 下采样**生成；
     * 若是归档直链：直接下载并在本地**落尺规范**。([Mathematica Stack Exchange][4])
4. **并发 & 去重**：`aiohttp` 限流并发 + SQLite manifest（WAL）记录 `(mkt, 日期, 分辨率)`，避免重复下载；失败任务独立降级，不影响其它条目。
5. **分类镜像**：根据标题/版权等关键词进行**粗分类**（动物 / 自然景色 / 其他），在每个分辨率下创建平铺目录，通过**硬链接**镜像主文件（跨盘符回退为复制）。
6. **日志可观测**：`--debug` 输出 HEAD/Range 试探、GET 重试、规范化落尺、硬链接回退等详细信息。

---

## 性能建议

* 典型环境建议 `--concurrency 8~16`；**归档直链**偶尔响应慢，适当增大并发可提升吞吐。
* 回溯时可设置 `--jobs 1` 获取“按日串行”的更清晰日志；若追求速度可与 `--concurrency` 保持一致。
* Windows 磁盘同卷可享受**硬链接**的零复制镜像；跨卷自动使用复制。

---

## 常见问题（FAQ）

**Q1：为什么只下到了 2K/1K，看起来没 4K？**
A：通常是你**删除了图片文件但保留了 `downloads.sqlite3`**，manifest 仍认为该 `(市场, 日期, 4k)` 已完成，从而跳过 4K。

* 解决：删除 `BingWallpapers/downloads.sqlite3` 后重跑；或启用我们在脚本中提供/建议的**清单自修复**逻辑（启动时剔除已不存在的文件记录）。

**Q2：为什么不是“同一天的 4K/2K/1K 连续”打印？**
A：脚本**并发**处理多个日期；同一日期内部是 4K→2K→1K 的顺序，但不同日期的日志会**交错**输出。把 `--jobs` 设为 `1` 即可更线性。

**Q3：为什么 2K/1K 有时 404？**
A：Bing 公开接口仅覆盖**近几天**且**不保证**所有分辨率；2K/1K 多为“根据 `urlbase` 规则拼接”的**约定俗成**后缀，因此并非每天都可用，我们会自动退回 4K 下采样或归档直链。([微软学习][1])

**Q4：历史能拉多远？**
A：官方接口 `HPImageArchive.aspx` 的 `n` 参数仅 **1–8**，无法获取全历史；本项目借助第三方归档（`bing.npanuhin.me`）补齐历史。([微软学习][1])

---

## 计划任务（可选）

* **Windows 任务计划程序**：新建「每日 09:00」任务，操作指向：

  ```powershell
  python "C:\Path\to\bing_daily_wallpaper.py" --years 0 --concurrency 8
  ```
* **Linux/macOS Cron**：

  ```cron
  0 9 * * * /usr/bin/python3 /path/to/bing_daily_wallpaper.py --years 0 --concurrency 8 >> /path/to/wall.log 2>&1
  ```

---

## 法律与致谢

* **版权**：图片版权归 **Microsoft / Bing** 及摄影师所有。仅供**个人学习/收藏**用途，请遵守相关条款与当地法律。可参考微软 Bing Wallpaper 的产品页与相关服务条款。([Microsoft][5])
* **数据来源**：

  * Bing 公共接口 `HPImageArchive.aspx`（参数 `format/idx/n/mkt/uhd`；`n` 范围 1–8）。([codeproject.com][2])
  * 第三方归档 **Bing-Wallpaper-Archive**（`bing.npanuhin.me`，提供年/全量 JSON 与直链存储）。([GitHub][3])
  * `urlbase + _分辨率.jpg` 的社区约定与示例。([Mathematica Stack Exchange][4])
  * 微软“Recent homepage images”页面（可人工核对当期图片）。([Search - Microsoft Bing][6])

感谢这些项目与社区讨论为本工具提供灵感与参考！

---

## 许可证

建议使用 **MIT License**（或根据你的需要替换）。在仓库根目录添加 `LICENSE` 文件即可。

---

## 开发脚注（How it works, for developers）

* 网络：统一将 Bing 主机**规范为** `https://www.bing.com`，减少重定向/缓存差异；归档直链则**直接 GET**（部分对象存储对 HEAD/Range 支持不一致）。
* 并发：`aiohttp` + 限流信号量；每个 URL 带 429 退避和有限重试，**404 不重试**以节省时间。
* 去重：SQLite（WAL）存 `(mkt, date, res)` 作为唯一键；进程内也有内存缓存防重复。
* 生成与落尺：缺 2K/1K 时从 4K **LANCZOS** 下采样；从归档获得大图时会在保存到 `2k/1k` 前**规范化分辨率**到目标盒（<=2560×1440、<=1920×1080）。
* 分类：以标题/版权等文本做**关键词启发式粗分类**（动物 / 自然景色 / 其他）。
* 链接：分类镜像优先创建**硬链接**；失败（跨盘符/权限）时自动复制。

---

### 参考链接

* Microsoft Q&A：`n` 参数仅 1–8，官方无历史全量接口。([微软学习][1])
* 公开 JSON 示例与参数说明（`format=js&idx=0&n=1&mkt=...`）。([codeproject.com][2])
* `urlbase` + 后缀构造多分辨率的社区实践。([Mathematica Stack Exchange][4])
* 第三方归档项目 **npanuhin/Bing-Wallpaper-Archive**。([GitHub][3])
* Bing “Recent homepage images” 页面。([Search - Microsoft Bing][6])
