#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Install Claude's official Chinese catalogs into Claude Desktop's bundled frontend.

Claude Desktop's third-party (3p) mode serves the frontend bundled in
``ion-dist.asar``, which ships only the 11 locales listed in its hard-coded
whitelist. Anthropic publishes the catalogs the online app uses as plain static
files under https://claude.ai/i18n/, so this patcher downloads the Chinese one,
merges it against the bundled ``en-US`` catalog (anything the official catalog
does not translate stays English) and registers it as a normal language.

Run from this folder:
    sudo /usr/bin/python3 scripts/patch_official_zh.py --user-home "$HOME"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import patcher_core as core  # noqa: E402

OFFICIAL_I18N_BASE = "https://claude.ai/i18n"
OFFICIAL_LOCALES = {"zh-CN": "zh-Hans", "zh-TW": "zh-Hant", "zh-HK": "zh-Hant"}
LANGUAGE_LABELS = {"zh-CN": "简体中文", "zh-TW": "繁體中文", "zh-HK": "繁體中文"}
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Claude/Desktop Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://claude.ai/",
}

LANG_LIST_RE = re.compile(
    r'\["en-US","de-DE","fr-FR","ko-KR","ja-JP","es-419","es-ES","it-IT","hi-IN","pt-BR","id-ID"'
    r'(?:,"(?:zh-CN|zh-TW|zh-HK|zh-Hans|zh-Hant)")*\]'
)
BASE_LANGUAGE_LIST = (
    '["en-US","de-DE","fr-FR","ko-KR","ja-JP","es-419","es-ES","it-IT","hi-IN","pt-BR","id-ID"'
)


def fetch_catalog(locale: str, kind: str = "", *, required: bool = True) -> dict[str, Any] | None:
    """Download one official catalog; ``kind`` is an optional sub-directory."""
    relative = f"{kind}/{locale}.json" if kind else f"{locale}.json"
    url = f"{OFFICIAL_I18N_BASE}/{relative}"
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        if not required and error.code == 404:
            return None
        raise SystemExit(f"官方语言包下载失败 {url}: HTTP {error.code}")
    except (urllib.error.URLError, OSError) as error:
        raise SystemExit(f"官方语言包下载失败 {url}: {error}")

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"官方语言包不是合法 JSON {url}: {error}")
    if not isinstance(data, dict):
        raise SystemExit(f"官方语言包根节点不是对象: {url}")
    return data


def merge_with_fallback(english: dict[str, Any], official: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Keep every bundled en-US key; use the official string when it has one."""
    merged: dict[str, Any] = {}
    translated = 0
    for key, value in english.items():
        localized = official.get(key)
        if isinstance(localized, str) and localized:
            merged[key] = localized
            translated += 1
        else:
            merged[key] = value
    return merged, translated


def patch_language_whitelist(app: Path, official_locale: str) -> int:
    """Register *official_locale* in every bundled language whitelist."""
    assets_dir = core.find_frontend_assets_dir(app)
    if assets_dir is None:
        raise SystemExit(
            f"找不到前端 JS bundle: {app / 'Contents/Resources/ion-dist'}"
        )

    replacement = f'{BASE_LANGUAGE_LIST},"{official_locale}"]'
    patched_files = 0
    occurrences = 0
    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if not LANG_LIST_RE.search(text):
            continue
        updated, count = LANG_LIST_RE.subn(replacement, text)
        path.write_text(updated, encoding="utf-8")
        patched_files += 1
        occurrences += count
        core.log(f"语言白名单加入 {official_locale}: {path.name} ({count} 处)")

    if not patched_files:
        raise SystemExit("未能修改语言白名单，Claude 的 bundle 格式可能已变化。")
    return occurrences


def install_frontend_locale(app: Path, official_locale: str) -> None:
    i18n_dir = app / core.FRONTEND_I18N_REL
    english_path = i18n_dir / "en-US.json"
    core.require_file(english_path)

    english = core.load_json(english_path)
    if not isinstance(english, dict):
        raise SystemExit(f"前端词库格式异常: {english_path}")

    official = fetch_catalog(official_locale)
    merged, translated = merge_with_fallback(english, official)
    core.save_json(i18n_dir / f"{official_locale}.json", merged)
    core.log(
        f"安装官方前端词库 {official_locale}: {translated}/{len(merged)} 条有官方译文，其余保留英文"
    )

    dynamic_dir = i18n_dir / "dynamic"
    dynamic_english_path = dynamic_dir / "en-US.json"
    if not dynamic_english_path.exists():
        return
    dynamic_english = core.load_json(dynamic_english_path)
    if not isinstance(dynamic_english, dict):
        raise SystemExit(f"前端 dynamic 词库格式异常: {dynamic_english_path}")

    dynamic_official = fetch_catalog(official_locale, "dynamic", required=False)
    if dynamic_official is None:
        core.log("官方未提供 dynamic 词库，使用 en-US 兜底")
        dynamic_official = {}
    merged_dynamic, translated_dynamic = merge_with_fallback(dynamic_english, dynamic_official)
    core.save_json(dynamic_dir / f"{official_locale}.json", merged_dynamic)
    core.log(f"安装官方 dynamic 词库 {official_locale}: {translated_dynamic}/{len(merged_dynamic)} 条")


def patch_app(app: Path, official_locale: str) -> None:
    frontend_target = core.prepare_frontend_target(app)
    try:
        patch_language_whitelist(app, official_locale)
        install_frontend_locale(app, official_locale)
        core.finalize_frontend_target(app, frontend_target)
    finally:
        core.cleanup_frontend_target(frontend_target)


def restore(app: Path, user_home: Path, *, launch: bool, dry_run: bool) -> None:
    if dry_run:
        print("[dry-run] 不会退出 Claude。")
    else:
        core.quit_claude()
    restored = core.restore_backup(app, dry_run)
    if dry_run:
        print(f"[dry-run] 会把 {user_home} 下的 Claude 配置 locale 恢复为 en-US")
        return
    core.set_user_locale(user_home, "en-US")
    print(f"已从备份还原: {restored}")
    if launch:
        core.run(["open", "-a", str(app)], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Claude's official Chinese catalogs into Claude Desktop.")
    parser.add_argument("--app", type=Path, default=core.APP_DEFAULT, help="Claude.app 路径")
    parser.add_argument("--user-home", type=Path, default=Path.home(), help="要写入 locale 配置的用户主目录")
    parser.add_argument(
        "--lang",
        choices=sorted(OFFICIAL_LOCALES),
        default="zh-CN",
        help="中文变体；zh-TW 与 zh-HK 均使用官方 zh-Hant 词库",
    )
    parser.add_argument("--dry-run", action="store_true", help="只准备并验证，不替换 Claude.app")
    parser.add_argument("--launch", action="store_true", help="安装完成后启动 Claude")
    parser.add_argument("--restore", action="store_true", help="还原最近一次备份")
    parser.add_argument(
        "--restore-if-backup-exists",
        action="store_true",
        help="安装前若当前 app 是打过补丁的，先还原官方备份",
    )
    args = parser.parse_args()

    if args.restore:
        restore(args.app, args.user_home, launch=args.launch, dry_run=args.dry_run)
        return 0

    if args.restore_if_backup_exists:
        if not core.find_app_backups(args.app):
            print(f"未找到备份 {core.BACKUP_DIR}: {core.BACKUP_GLOB}，跳过安装前还原。")
            return 0
        if not core.preinstall_should_restore(args.app):
            print(f"当前 app 是官方签名（{core.bundle_version(args.app)}），跳过安装前还原。")
            return 0
        restore(args.app, args.user_home, launch=False, dry_run=args.dry_run)
        print("安装前已完成官方还原。")
        return 0

    official_locale = OFFICIAL_LOCALES[args.lang]
    label = LANGUAGE_LABELS[args.lang]
    if not args.app.exists():
        raise SystemExit(f"未找到 Claude.app: {args.app}")
    core.require_virtualization_entitlement(args.app)

    if args.dry_run:
        print("[dry-run] 不会退出 Claude。")
    else:
        core.quit_claude()

    start = time.perf_counter()
    tmp_root = Path(tempfile.mkdtemp(prefix=f"claude-{official_locale}-official."))
    patched_app = tmp_root / "Claude.app"
    core.copy_app(args.app, patched_app)
    core.log(f"Copied app for patching in {core.elapsed_since(start)}")

    patch_app(patched_app, official_locale)
    core.resign_app(patched_app)
    core.clear_quarantine(patched_app)

    if args.dry_run:
        print(f"[dry-run] 不会写入 {args.user_home} 下的 Claude 配置")
    else:
        core.set_user_locale(args.user_home, official_locale)
    core.verify(patched_app, official_locale)

    backup = core.backup_and_replace(args.app, patched_app, args.dry_run)
    if args.dry_run:
        return 0

    print(f"备份保留在: {backup}")
    core.prune_stale_backups(args.app, keep=backup, dry_run=False)
    if args.launch:
        core.run(["open", "-a", str(args.app)], check=False)
    print(f"完成。若语言未自动切换，请在左下角账号菜单选择 Language -> {label}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
