#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zero-intrusion launcher for bing_daily_wallpaper.py
- 读取 profiles.toml 的参数预设
- 允许命令行覆盖预设
- 子进程方式启动主脚本
"""

from __future__ import annotations
import argparse
import sys
import subprocess
import shlex
from pathlib import Path
from typing import Any, Dict

# 与主脚本 help 对齐：bing_daily_wallpaper.py [-h] [--dir OUT_DIR] [--mkt MKT]
#   [--years YEARS] [--concurrency CONCURRENCY] [--jobs JOBS] [--debug] [--no-gen-missing]
MAIN_ALLOWED_KEYS = [
    "dir",            # 输出目录
    "mkt",            # 市场/地区
    "years",          # 近 N 年
    "concurrency",    # 并发
    "jobs",           # 处理用的工作进程/线程数（你的主脚本支持）
    "debug",          # 调试开关
    "no_gen_missing", # 布尔：不生成/补齐缺失分辨率
]

def _load_profiles(cfg_path: Path) -> Dict[str, Dict[str, Any]]:
    if not cfg_path.exists():
        return {}
    toml_mod = None
    try:
        import tomllib as toml_mod  # 3.11+
    except Exception:
        try:
            import tomli as toml_mod  # pip install tomli
        except Exception:
            toml_mod = None
    if toml_mod is None:
        raise RuntimeError(
            f"Cannot read {cfg_path}: no TOML reader found. "
            f"Use Python 3.11+ (tomllib) or `pip install tomli`."
        )
    with cfg_path.open("rb") as f:
        data = toml_mod.load(f)
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("profiles.toml malformed: [profiles] must be a table")
    return profiles

def _norm_script_path(script_arg: str) -> Path:
    p = Path(script_arg)
    if not p.is_absolute():
        base = Path(__file__).resolve().parent
        p = (base / p).resolve()
    return p

def _merge_profile_and_overrides(profile: Dict[str, Any], cli_ns: argparse.Namespace) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(profile or {})
    cli_d = vars(cli_ns)
    for k in MAIN_ALLOWED_KEYS:
        v = cli_d.get(k, None)
        if v is not None:
            merged[k] = v
    return merged

def _build_main_argv(opts: Dict[str, Any]) -> list[str]:
    argv: list[str] = []
    def add_flag(name: str, value: Any):
        flag = f"--{name.replace('_','-')}"
        if isinstance(value, bool):
            if value:
                argv.append(flag)
        else:
            argv.extend([flag, str(value)])  # 用 extend，避免闭包重绑定问题
    for key in MAIN_ALLOWED_KEYS:
        if key in opts:
            add_flag(key, opts[key])
    return argv

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Launcher for bing_daily_wallpaper.py (profiles + overrides)")
    ap.add_argument("--profile", default="default", help="profile name in profiles.toml")
    ap.add_argument("--config", default="profiles.toml", help="path to profiles TOML")

    # —— 覆盖项：与主脚本参数保持一致 ——
    ap.add_argument("--dir", dest="dir")
    ap.add_argument("--mkt")
    ap.add_argument("--years", type=int)
    ap.add_argument("--concurrency", type=int)
    ap.add_argument("--jobs", type=int)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--no-gen-missing", dest="no_gen_missing", action="store_true")

    # —— 启动器自身参数 ——
    ap.add_argument("--dry-run", action="store_true", help="print final command and exit")
    ap.add_argument("--python", default=sys.executable, help="python interpreter to run main script")
    ap.add_argument("--script", default="bing_daily_wallpaper.py", help="main script path (relative to this file allowed)")
    ap.add_argument("--verbose", action="store_true", help="print merged options before launching")

    args = ap.parse_args(argv)

    profiles = _load_profiles(Path(args.config))
    profile = profiles.get(args.profile, {})
    eff_opts = _merge_profile_and_overrides(profile, args)

    script_path = _norm_script_path(args.script)
    if not script_path.exists():
        ap.error(f"Main script not found: {script_path}")

    cmd = [args.python, str(script_path), *_build_main_argv(eff_opts)]

    if args.verbose or args.dry_run:
        print("== Effective options ==")
        for k in MAIN_ALLOWED_KEYS:
            if k in eff_opts:
                print(f"{k}: {eff_opts[k]}")
        print("\n== Final command ==")
        print(" ".join(shlex.quote(c) for c in cmd))
        if args.dry_run:
            return 0

    completed = subprocess.run(cmd)  # 官方建议常见场景优先用 run()
    return int(completed.returncode)

if __name__ == "__main__":
    raise SystemExit(main())
