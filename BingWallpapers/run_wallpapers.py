#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键驱动两个程序的启动器（profiles + 可选择目标 + 覆盖）
- 目标可选：bing / popular / both
- 读取 profiles.toml（支持每个 profile 下分别给 bing/popular 配置）
- 命令行可用 -- 覆盖（"--" 之后的参数将原样传给目标脚本）
- 提供 --init 生成示例 profiles.toml

默认脚本路径：
  - Bing:     bing_daily_wallpaper.py
  - Popular:  fetch_popular_wallpapers_fixed.py （若不存在则退回 fetch_popular_wallpapers.py）

使用示例：
  # 生成示例 profiles.toml
  python run_wallpapers.py --init

  # 按 default profile 跑两个程序（先 Bing 后 Popular）
  python run_wallpapers.py --target both --verbose-launcher

  # 只跑 Popular，并覆盖部分参数（-- 之后原样传给 popular 脚本）
  python run_wallpapers.py --target popular -- --providers q360 wallhaven --res 4k 2k --limit-per 60

  # dry-run 仅查看最终命令
  python run_wallpapers.py --target bing --dry-run -- --years 2 --mkt zh-CN
"""
from __future__ import annotations
import argparse
import sys
import subprocess
import shlex
from pathlib import Path
from typing import Any, Dict, List, Tuple

# —— 允许的键（用于 profiles.toml） ——
BING_ALLOWED_KEYS = [
    "dir", "mkt", "years", "concurrency", "jobs", "debug", "no_gen_missing",
]
POPULAR_ALLOWED_KEYS = [
    "providers", "res", "limit_per", "q", "exact", "dup_mode", "dup_action",
    "phash_distance", "robots", "max_workers", "wh_sorting", "wh_toprange",
    "wh_seed", "verbose",
]

EXAMPLE_TOML = """
[profiles.default]
# 这是一个示例 profile；你可以复制粘贴并改名为自己的环境。

  [profiles.default.bing]
  mkt = "zh-CN"
  years = 1
  concurrency = 8
  # dir = "./bing_out"         # 可选：输出目录
  # jobs = 4
  # debug = false
  # no_gen_missing = false

  [profiles.default.popular]
  providers = ["q360", "wallhaven", "openverse"]
  res = ["4k", "2k", "1k"]
  limit_per = 60
  max_workers = 16
  dup_mode = "content"         # url | content | perceptual
  dup_action = "keep"          # keep | skip
  robots = "on"                # on | off | strict
  wh_sorting = "date_added"    # date_added | toplist | random
  # wh_toprange = "1M"         # 当 sorting = toplist 时使用
  # wh_seed = ""               # 当 sorting = random 时可设置
  verbose = true

# 你可以添加更多 profile：
# [profiles.fast]
#   [profiles.fast.bing]
#   ...
#   [profiles.fast.popular]
#   ...
""".lstrip()

def _find_toml_loader():
    try:
        import tomllib as toml_mod  # Python 3.11+
        return toml_mod
    except Exception:
        try:
            import tomli as toml_mod  # pip install tomli
            return toml_mod
        except Exception:
            return None

def _load_profiles(cfg_path: Path) -> Dict[str, Dict[str, Any]]:
    if not cfg_path.exists():
        return {}
    toml_mod = _find_toml_loader()
    if toml_mod is None:
        raise RuntimeError(
            f"Cannot read {cfg_path}: no TOML reader found. Use Python 3.11+ (tomllib) or `pip install tomli`."
        )
    with cfg_path.open("rb") as f:
        data = toml_mod.load(f)
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("profiles.toml malformed: [profiles] must be a table")
    return profiles

# —— 构造最终 argv ——

def _norm_script_path(script_arg: str) -> Path:
    p = Path(script_arg)
    if not p.is_absolute():
        base = Path(__file__).resolve().parent
        p = (base / p).resolve()
    return p

def _split_opts_for(prog: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    """从 profile 里选出某个程序的键值。"""
    allowed = BING_ALLOWED_KEYS if prog == "bing" else POPULAR_ALLOWED_KEYS
    sub = profile.get(prog, {}) if isinstance(profile.get(prog, {}), dict) else {}
    # 兼容旧格式：如果用户直接把键放在上层，也容忍一下
    for k, v in profile.items():
        if k in allowed and k not in sub:
            sub[k] = v
    return sub

def _build_argv_from_opts(opts: Dict[str, Any]) -> List[str]:
    argv: List[str] = []
    def flag(name: str) -> str:
        return f"--{name.replace('_','-')}"

    for k, v in opts.items():
        if v is None:
            continue
        # 列表：按一次 flag + 多值 方式输出（argparse nargs+）
        if isinstance(v, (list, tuple)):
            if len(v) == 0:
                continue
            argv.append(flag(k))
            argv.extend(str(x) for x in v)
        # 布尔：True 输出开关，False 忽略
        elif isinstance(v, bool):
            if v:
                argv.append(flag(k))
        else:
            argv.extend([flag(k), str(v)])
    return argv

# —— 运行一个子程序 ——

def _run_one(name: str, script_path: Path, opts: Dict[str, Any], passthrough: List[str], *,
             python: str, dry_run: bool, verbose_launcher: bool) -> int:
    if not script_path.exists():
        print(f"[error] {name} 脚本不存在: {script_path}")
        return 127
    argv = [python, str(script_path), *_build_argv_from_opts(opts), *passthrough]
    if verbose_launcher or dry_run:
        print(f"\n== {name} :: Effective options ==")
        for k in sorted(opts.keys()):
            print(f"{k}: {opts[k]}")
        print("\n== Final command ==")
        print(" ".join(shlex.quote(s) for s in argv))
    if dry_run:
        return 0
    completed = subprocess.run(argv)
    return int(completed.returncode)


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Launcher for Bing + Popular wallpaper scripts")

    ap.add_argument("--target", choices=["bing", "popular", "both"], default="both",
                    help="选择要运行的程序（默认 both）")
    ap.add_argument("--profile", default="default", help="profile name in profiles.toml")
    ap.add_argument("--config", default="profiles.toml", help="path to profiles TOML")
    ap.add_argument("--python", default=sys.executable, help="python interpreter to run child scripts")
    ap.add_argument("--script-bing", default="bing_daily_wallpaper.py",
                    help="path to bing script (relative to this file allowed)")
    ap.add_argument("--script-popular", default="",
                    help="path to popular script (default: fetch_popular_wallpapers_fixed.py or fetch_popular_wallpapers.py)")
    ap.add_argument("--dry-run", action="store_true", help="print final command(s) and exit")
    ap.add_argument("--continue-on-error", action="store_true", default=True,
                    help="遇到错误是否继续运行后续程序（默认 True）")
    ap.add_argument("--stop-on-error", action="store_true",
                    help="遇到错误立即停止（与 --continue-on-error 相反）")
    ap.add_argument("--verbose-launcher", action="store_true", help="打印合并后的选项与最终命令")
    ap.add_argument("--init", action="store_true", help="在当前目录生成示例 profiles.toml（若已存在则跳过）")

    # 捕获 "--" 之后的所有内容，原样传给子程序
    ap.add_argument("passthrough", nargs=argparse.REMAINDER, help="use `--` then args passed to child script")

    args = ap.parse_args(argv)

    # 处理 stop/continue 逻辑
    continue_on_error = True
    if args.stop_on_error:
        continue_on_error = False
    elif args.continue_on_error is False:  # 显式关闭
        continue_on_error = False

    # 初始化 profiles.toml
    cfg_path = Path(args.config)
    if args.init:
        if cfg_path.exists():
            print(f"[init] 已存在 {cfg_path}，跳过生成。")
        else:
            cfg_path.write_text(EXAMPLE_TOML, encoding="utf-8")
            print(f"[init] 写入示例 {cfg_path}")
        if args.dry_run:
            return 0

    profiles = _load_profiles(cfg_path)
    profile = profiles.get(args.profile, {})
    if not isinstance(profile, dict):
        print(f"[warn] 找不到 profile '{args.profile}'，将使用空配置")
        profile = {}

    # 规范化脚本路径
    bing_script = _norm_script_path(args.script_bing)
    pop_arg = args.script_popular.strip()
    if pop_arg:
        popular_script = _norm_script_path(pop_arg)
    else:
        # 自动探测 popular 脚本
        base = Path(__file__).resolve().parent
        cand1 = (base / "fetch_popular_wallpapers_fixed.py").resolve()
        cand2 = (base / "fetch_popular_wallpapers.py").resolve()
        popular_script = cand1 if cand1.exists() else cand2

    # 解析 profile
    bing_opts = _split_opts_for("bing", profile)
    popular_opts = _split_opts_for("popular", profile)

    # 提取 passthrough（去掉开头的 "--"）
    passthrough = list(args.passthrough or [])
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

    rc = 0
    if args.target in ("bing", "both"):
        rc = _run_one("bing", bing_script, bing_opts, passthrough, python=args.python,
                      dry_run=args.dry_run, verbose_launcher=args.verbose_launcher)
        if rc != 0 and not continue_on_error:
            return rc

    if args.target in ("popular", "both"):
        rc2 = _run_one("popular", popular_script, popular_opts, passthrough, python=args.python,
                       dry_run=args.dry_run, verbose_launcher=args.verbose_launcher)
        if rc == 0:
            rc = rc2

    return int(rc)


if __name__ == "__main__":
    raise SystemExit(main())
