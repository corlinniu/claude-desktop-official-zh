#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Platform plumbing for patching Claude Desktop on macOS.

Only app-level operations live here: ASAR read/write, Electron integrity,
code signing, backup/restore and the locale config. The localization policy
lives in ``patch_official_zh.py``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

APP_DEFAULT = Path("/Applications/Claude.app")

# 备份不再与 Claude.app 同目录（/Applications），统一放到工作目录（项目根目录）下的 backups/。
BACKUP_DIR = Path(__file__).resolve().parent.parent / "backups"

BACKUP_GLOB = "Claude.backup-before-zh-CN-*.app"

APP_ASAR_REL = Path("Contents/Resources/app.asar")

ELECTRON_FRAMEWORK_REL = Path("Contents/Frameworks/Electron Framework.framework/Versions/A/Electron Framework")

INTEGRITY_DIGEST_SENTINEL = b"AGbevlPCksUGKNL8TSn7wGmJEuJsXb2A"

FRONTEND_ASAR_REL = Path("Contents/Resources/ion-dist.asar")

FRONTEND_DIR_REL = Path("Contents/Resources/ion-dist")

FRONTEND_I18N_REL = FRONTEND_DIR_REL / "i18n"

FRONTEND_ASSETS_REL = FRONTEND_DIR_REL / "assets/v1"

FRONTEND_PROBE_NEEDLE = '"en-US","de-DE","fr-FR","ko-KR","ja-JP"'

ASAR_INTEGRITY_BLOCK_SIZE = 4 * 1024 * 1024

def log(message: str) -> None:
    print(message, flush=True)

def elapsed_since(start: float) -> str:
    return f"{time.perf_counter() - start:.1f}s"

def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check)

def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)

def require_file(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"Missing required file: {path}")

def read_entitlements(path: Path) -> str:
    return run(["codesign", "-d", "--entitlements", "-", str(path)], check=False).stdout

def load_entitlements(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["codesign", "-d", "--entitlements", ":-", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    stdout = result.stdout.rstrip(b"\x00")
    if result.returncode != 0 or not stdout.strip():
        return {}
    try:
        data = plistlib.loads(stdout)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

def require_virtualization_entitlement(app: Path) -> None:
    entitlements = read_entitlements(app)
    if "com.apple.security.virtualization" not in entitlements:
        raise SystemExit(
            "Claude.app does not have the required virtualization entitlement. "
            "Restore or reinstall the official Claude.app first, then run this patcher again."
        )

def quit_claude() -> None:
    run(["osascript", "-e", 'tell application "Claude" to quit'], check=False)

def copy_app(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    start = time.perf_counter()
    log(f"Copying app to temporary workspace: {dst}")
    run(["ditto", str(src), str(dst)])
    log(f"Copied app to temporary workspace in {elapsed_since(start)}")

def find_frontend_assets_dir(app: Path) -> Path | None:
    """Locate the frontend JS bundle directory under the app.

    已知布局（Claude 2.x 至 2.31226.1）是 Contents/Resources/ion-dist/assets/v1；
    新版本若改名或换层级，则在 ion-dist 下递归查找：优先返回命中语言白名单锚点
    的目录（前端主 bundle），其次含 index-*.js 的目录，避免误选 worker 等辅助
    JS 目录（issue #196 同类风险的 Mac 侧防线）。
    """
    known = app / FRONTEND_ASSETS_REL
    if any(known.glob("*.js")):
        return known

    ion_dist = app / "Contents/Resources/ion-dist"
    if not ion_dist.is_dir():
        return None

    needle_hits: list[Path] = []
    index_dirs: list[Path] = []
    for js_path in sorted(ion_dist.rglob("*.js")):
        if not js_path.is_file():
            continue
        if js_path.name.startswith("index-") and js_path.parent not in index_dirs:
            index_dirs.append(js_path.parent)
        if js_path.parent not in needle_hits:
            try:
                if FRONTEND_PROBE_NEEDLE in js_path.read_text(encoding="utf-8"):
                    needle_hits.append(js_path.parent)
            except (OSError, UnicodeDecodeError):
                continue

    if needle_hits:
        return needle_hits[0]
    if index_dirs:
        return index_dirs[0]
    return None

def align4(value: int) -> int:
    return value + ((4 - (value % 4)) % 4)

def read_asar_header(data: bytes, path: Path) -> tuple[int, str, dict[str, Any]]:
    if len(data) < 16:
        raise SystemExit(f"Unsupported app.asar header in {path}")

    size_pickle_payload = struct.unpack_from("<I", data, 0)[0]
    header_size = struct.unpack_from("<I", data, 4)[0]
    if size_pickle_payload != 4 or header_size <= 0 or len(data) < 8 + header_size:
        raise SystemExit(f"Unsupported app.asar size pickle in {path}")

    header_pickle = data[8 : 8 + header_size]
    header_payload_size = struct.unpack_from("<I", header_pickle, 0)[0]
    header_string_size = struct.unpack_from("<i", header_pickle, 4)[0]
    expected_payload_size = align4(4 + header_string_size)
    if header_payload_size != expected_payload_size or header_size != 4 + header_payload_size:
        raise SystemExit(f"Unsupported app.asar header pickle in {path}")

    header_start = 8
    header_end = header_start + header_string_size
    header_string = header_pickle[header_start:header_end].decode("utf-8")
    header = json.loads(header_string)
    if not isinstance(header, dict):
        raise SystemExit(f"Unsupported app.asar header JSON in {path}")
    return header_size, header_string, header

def walk_asar_entries(header: dict[str, Any]):
    """Yield (relative_path, entry) for every file entry in an ASAR header."""
    stack: list[tuple[str, dict[str, Any]]] = [("", header)]
    while stack:
        prefix, node = stack.pop()
        files = node.get("files") if isinstance(node, dict) else None
        if not isinstance(files, dict):
            continue
        for name, child in files.items():
            if not isinstance(child, dict):
                continue
            relative = f"{prefix}/{name}" if prefix else name
            if "files" in child:
                stack.append((relative, child))
            else:
                yield relative, child

def asar_entry(header: dict[str, Any], relative: str) -> dict[str, Any] | None:
    node: Any = header
    for part in relative.split("/"):
        files = node.get("files") if isinstance(node, dict) else None
        if not isinstance(files, dict) or part not in files:
            return None
        node = files[part]
    if not isinstance(node, dict) or "files" in node:
        return None
    return node

def read_asar_entry(data: bytes, header_size: int, header: dict[str, Any], relative: str) -> bytes | None:
    """Read one file entry from an in-memory ASAR archive."""
    entry = asar_entry(header, relative)
    if entry is None or entry.get("unpacked"):
        return None
    offset = int(entry["offset"])
    size = int(entry["size"])
    start = 8 + header_size + offset
    if offset < 0 or size < 0 or start + size > len(data):
        raise SystemExit(f"ASAR 文件边界无效: {relative}")
    return data[start : start + size]

def asar_header_hash(header_string: str) -> str:
    return hashlib.sha256(header_string.encode("utf-8")).hexdigest()

def _asar_integrity(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    blocks = [
        hashlib.sha256(data[offset : offset + ASAR_INTEGRITY_BLOCK_SIZE]).hexdigest()
        for offset in range(0, len(data), ASAR_INTEGRITY_BLOCK_SIZE)
    ]
    if not blocks:
        blocks = [hashlib.sha256(b"").hexdigest()]
    return {
        "algorithm": "SHA256",
        "hash": hashlib.sha256(data).hexdigest(),
        "blockSize": ASAR_INTEGRITY_BLOCK_SIZE,
        "blocks": blocks,
    }

def extract_asar(asar_path: Path, destination: Path, unpacked: set[str] | None = None) -> None:
    """Expand an ASAR archive into *destination*.

    Files flagged ``unpacked`` are copied from the sibling ``.unpacked`` directory;
    their relative paths are recorded in *unpacked* so repacking can preserve them.
    """
    data = asar_path.read_bytes()
    header_size, _header_string, header = read_asar_header(data, asar_path)
    data_start = 8 + header_size
    unpacked_root = Path(str(asar_path) + ".unpacked")
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()

    for relative, entry in walk_asar_entries(header):
        parts = relative.split("/")
        if any(part in ("", ".", "..") or "\\" in part for part in parts):
            raise SystemExit(f"ASAR 包含不安全路径: {relative}")
        target = (destination / Path(*parts)).resolve()
        if target != root and root not in target.parents:
            raise SystemExit(f"ASAR 路径越界: {relative}")
        if entry.get("link"):
            raise SystemExit(f"暂不支持包含软链接的 ASAR: {relative}")

        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.get("unpacked"):
            source = unpacked_root / Path(*parts)
            if not source.is_file():
                raise SystemExit(f"ASAR unpacked 文件缺失: {relative}")
            shutil.copy2(source, target)
            if unpacked is not None:
                unpacked.add(relative)
            continue

        offset = int(entry["offset"])
        size = int(entry["size"])
        if offset < 0 or size < 0 or data_start + offset + size > len(data):
            raise SystemExit(f"ASAR 文件边界无效: {relative}")
        target.write_bytes(data[data_start + offset : data_start + offset + size])

def build_asar(source_dir: Path, destination: Path, unpacked: set[str] | None = None) -> None:
    """Pack *source_dir* into an Electron-compatible ASAR archive."""
    unpacked = unpacked or set()
    file_entries: list[tuple[Path, str, dict[str, Any]]] = []

    def walk(directory: Path, node: dict[str, Any]) -> None:
        for item in sorted(directory.iterdir(), key=lambda path: path.name):
            if item.is_symlink():
                raise SystemExit(f"ASAR 源目录包含不支持的软链接: {item}")
            if item.is_dir():
                child: dict[str, Any] = {"files": {}}
                node["files"][item.name] = child
                walk(item, child)
            elif item.is_file():
                relative = str(item.relative_to(source_dir)).replace(os.sep, "/")
                entry: dict[str, Any] = {"size": item.stat().st_size}
                node["files"][item.name] = entry
                file_entries.append((item, relative, entry))
            else:
                raise SystemExit(f"ASAR 源目录包含不支持的文件类型: {item}")

    header: dict[str, Any] = {"files": {}}
    walk(source_dir, header)

    offset = 0
    for path, relative, entry in file_entries:
        entry["offset"] = str(offset)
        entry["integrity"] = _asar_integrity(path)
        if relative in unpacked:
            # unpacked 文件不写入归档数据区，Electron 从同名 .unpacked 目录读取。
            entry["unpacked"] = True
            continue
        offset += int(entry["size"])

    header_string = json.dumps(header, separators=(",", ":"), ensure_ascii=False)
    header_bytes = header_string.encode("utf-8")
    payload_size = align4(4 + len(header_bytes))
    header_size = 4 + payload_size
    prefix = struct.pack("<II", 4, header_size)
    payload = (
        struct.pack("<II", payload_size, len(header_bytes))
        + header_bytes
        + b"\x00" * (payload_size - 4 - len(header_bytes))
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    unpacked_root = Path(str(destination) + ".unpacked")
    try:
        with temporary.open("wb") as output:
            output.write(prefix)
            output.write(payload)
            for path, relative, _entry in file_entries:
                if relative in unpacked:
                    continue
                with path.open("rb") as source:
                    shutil.copyfileobj(source, output)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    if unpacked:
        unpacked_root.mkdir(parents=True, exist_ok=True)
        for relative in sorted(unpacked):
            source = source_dir / Path(*relative.split("/"))
            target = unpacked_root / Path(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

def get_frontend_bundle_mode(app: Path) -> str:
    if (app / FRONTEND_ASAR_REL).is_file():
        return "asar"
    if (app / FRONTEND_DIR_REL).is_dir():
        return "directory"
    return "missing"

def prepare_frontend_target(app: Path) -> dict[str, Any]:
    """Expose the frontend bundle as a directory so the existing patches work unchanged.

    Claude 4.29+ ships ``ion-dist.asar``; it is expanded into a temporary
    ``ion-dist`` directory inside the patched app copy and repacked in
    :func:`finalize_frontend_target`.
    """
    mode = get_frontend_bundle_mode(app)
    asar = app / FRONTEND_ASAR_REL
    directory = app / FRONTEND_DIR_REL
    if mode == "asar":
        if directory.exists():
            # Claude 4.16336 起同时保留 ion-dist.asar 与一个只含占位页的 ion-dist 目录
            # （占位页提示旧版本已被覆盖，当前版本不会加载）。目录内没有 JS 时按占位页
            # 处理，删除后继续使用 ASAR 布局。
            if any(directory.rglob("*.js")):
                raise SystemExit(f"前端目录与 ASAR 同时存在，无法安全判定布局: {directory}")
            shutil.rmtree(directory)
            print(f"Removed placeholder frontend directory: {directory}")
        unpacked: set[str] = set()
        extract_asar(asar, directory, unpacked)
        print(f"Extracted ion-dist.asar for patching ({len(unpacked)} unpacked file(s))")
        return {"mode": "asar", "work": directory, "asar": asar, "unpacked": unpacked}
    if mode == "directory":
        return {"mode": "directory", "work": directory, "asar": None, "unpacked": set()}
    raise SystemExit(f"未找到前端资源: {directory} 或 {asar}")

def finalize_frontend_target(app: Path, target: dict[str, Any]) -> None:
    if target.get("mode") != "asar":
        return
    work: Path = target["work"]
    asar: Path = target["asar"]
    if not work.is_dir():
        raise SystemExit(f"ASAR 临时工作目录不存在: {work}")

    temporary = asar.with_name(asar.name + ".zh-cn-new")
    print("Rebuilding ion-dist.asar...")
    build_asar(work, temporary, target.get("unpacked") or set())
    try:
        data = temporary.read_bytes()
        _header_size, _header_string, header = read_asar_header(data, temporary)
        paths = {relative for relative, _entry in walk_asar_entries(header)}
        has_i18n = "i18n/en-US.json" in paths
        has_bundle = any(re.search(r"(^|/)assets/(?:[^/]+/)?index-[^/]+\.js$", path) for path in paths)
        if not (has_i18n and has_bundle):
            raise SystemExit("重新打包后的 ion-dist.asar 未通过结构校验。")
        os.replace(temporary, asar)
    finally:
        temporary.unlink(missing_ok=True)

    cleanup_frontend_target(target)
    update_electron_asar_integrity(app, "Resources/ion-dist.asar", asar)
    print("Rebuilt ion-dist.asar")

def cleanup_frontend_target(target: dict[str, Any] | None) -> None:
    if not target or target.get("mode") != "asar":
        return
    work = target.get("work")
    if isinstance(work, Path) and work.exists():
        shutil.rmtree(work, ignore_errors=True)

def compute_integrity_dictionary_digest(integrity: dict[str, Any]) -> str:
    """Digest Electron's package-time integrity dictionary (sorted key + algorithm + hash)."""
    hasher = hashlib.sha256()
    for key in sorted(integrity):
        entry = integrity[key]
        hasher.update(key.encode("utf-8"))
        hasher.update(str(entry.get("algorithm", "")).encode("utf-8"))
        hasher.update(str(entry.get("hash", "")).encode("utf-8"))
    return hasher.hexdigest()

def sync_integrity_digest_slot(app: Path, integrity: dict[str, Any]) -> int:
    """Update the package-time dictionary digest slot inside Electron Framework.

    Returns the number of patched slots (one per Mach-O slice). Older Electron
    builds have no slot; those are left untouched.
    """
    framework = app / ELECTRON_FRAMEWORK_REL
    if not framework.is_file():
        return 0
    data = bytearray(framework.read_bytes())
    digest = bytes.fromhex(compute_integrity_dictionary_digest(integrity))
    updated = 0
    start = 0
    while True:
        index = data.find(INTEGRITY_DIGEST_SENTINEL, start)
        if index < 0:
            break
        slot = index + len(INTEGRITY_DIGEST_SENTINEL)
        if slot + 34 > len(data):
            break
        # used=0 表示打包时未启用字典摘要，保持 fail-open。
        if data[slot] == 1:
            data[slot] = 1
            data[slot + 1] = 1
            data[slot + 2 : slot + 34] = digest
            updated += 1
        start = index + 1
    if updated:
        framework.write_bytes(data)
    return updated

def verify_integrity_digest_slot(app: Path, integrity: dict[str, Any]) -> None:
    framework = app / ELECTRON_FRAMEWORK_REL
    if not framework.is_file():
        return
    data = framework.read_bytes()
    expected = compute_integrity_dictionary_digest(integrity)
    start = 0
    checked = 0
    while True:
        index = data.find(INTEGRITY_DIGEST_SENTINEL, start)
        if index < 0:
            break
        slot = index + len(INTEGRITY_DIGEST_SENTINEL)
        if slot + 34 > len(data):
            break
        if data[slot] == 1:
            actual = data[slot + 2 : slot + 34].hex()
            if actual != expected:
                raise SystemExit(
                    "Electron integrity dictionary digest mismatch in Electron Framework: "
                    f"binary has {actual!r}, expected {expected}."
                )
            checked += 1
        start = index + 1
    if checked:
        print(f"Verified Electron integrity dictionary digest ({checked} slice(s))")

def update_electron_asar_integrity(app: Path, key: str, asar_path: Path) -> bool:
    """Sync Info.plist ElectronAsarIntegrity for *key* when the app declares it."""
    info_plist = app / "Contents/Info.plist"
    with info_plist.open("rb") as f:
        info = plistlib.load(f)
    integrity = info.get("ElectronAsarIntegrity")
    if not isinstance(integrity, dict) or key not in integrity:
        return False
    data = asar_path.read_bytes()
    _header_size, header_string, _header = read_asar_header(data, asar_path)
    expected = asar_header_hash(header_string)
    if integrity[key].get("hash") == expected:
        return False
    integrity[key]["hash"] = expected
    with info_plist.open("wb") as f:
        plistlib.dump(info, f, fmt=plistlib.FMT_BINARY, sort_keys=False)
    sync_integrity_digest_slot(app, integrity)
    return True

def sign_path(
    path: Path,
    entitlements_dir: Path,
    *,
    hardened_runtime: bool = True,
    force_get_task_allow: bool = False,
    designated_requirement: str | None = None,
) -> None:
    entitlements = load_entitlements(path)
    if entitlements is None:
        entitlements = {}
    if entitlements or force_get_task_allow:
        entitlements.pop("com.apple.application-identifier", None)
        entitlements.pop("com.apple.developer.team-identifier", None)
        entitlements.pop("keychain-access-groups", None)
        # Ad-hoc signatures do not have a real Team ID. Under hardened runtime,
        # Electron's main process otherwise fails library validation when it loads
        # bundled frameworks, even when the whole bundle is signed consistently.
        entitlements["com.apple.security.cs.disable-library-validation"] = True
        if force_get_task_allow:
            # Required for Frida/task_for_pid against this process under SIP.
            entitlements["com.apple.security.get-task-allow"] = True
            entitlements.setdefault("com.apple.security.cs.allow-jit", True)

    cmd = [
        "codesign",
        "--force",
        "--sign",
        "-",
        "--timestamp=none",
    ]
    if hardened_runtime:
        cmd.extend(["--options", "runtime", "--preserve-metadata=identifier,flags"])
    if entitlements:
        entitlement_path = entitlements_dir / f"{abs(hash(path.as_posix()))}.plist"
        entitlement_path.write_bytes(plistlib.dumps(entitlements, fmt=plistlib.FMT_XML))
        cmd.extend(["--entitlements", str(entitlement_path)])
    if designated_requirement:
        cmd.extend(["-r", f"=designated => {designated_requirement}"])
    cmd.append(str(path))

    result = run(cmd, check=False)
    if result.returncode != 0:
        print(result.stdout, end="")
        raise SystemExit(f"Failed to re-sign: {path}")

def is_signable_file(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    if path.suffix in {".dylib", ".node", ".so"}:
        return True
    return os.access(path, os.X_OK)

def _collect_sign_targets(app: Path) -> tuple[list[Path], list[Path]]:
    contents = app / "Contents"
    bundle_targets: list[Path] = []
    file_targets: list[Path] = []
    for root, dirs, files in os.walk(contents):
        root_path = Path(root)
        for dirname in dirs:
            path = root_path / dirname
            if path.suffix in {".app", ".framework"}:
                bundle_targets.append(path)
        for filename in files:
            path = root_path / filename
            if is_signable_file(path):
                file_targets.append(path)
    return file_targets, bundle_targets

def app_bundle_identifier(app: Path) -> str | None:
    """Return CFBundleIdentifier of *app*, or None when it cannot be read."""
    info_plist = app / "Contents/Info.plist"
    try:
        with info_plist.open("rb") as f:
            plist = plistlib.load(f)
        value = plist.get("CFBundleIdentifier")
        return value if isinstance(value, str) and value else None
    except Exception:
        return None


def resign_app(app: Path) -> None:
    start = time.perf_counter()
    log("Re-signing patched app with local ad-hoc signature, preserving entitlements")
    entitlements_dir = Path(tempfile.mkdtemp(prefix="claude-zh-cn-entitlements."))
    file_targets, bundle_targets = _collect_sign_targets(app)

    # Sign nested Mach-O files first, then their containing bundles, then the outer app.
    log(f"  signing {len(file_targets)} executable files and {len(bundle_targets) + 1} bundles")
    sorted_file_targets = sorted(file_targets, key=lambda p: len(p.parts), reverse=True)
    for index, path in enumerate(sorted_file_targets, start=1):
        sign_path(path, entitlements_dir)
        if index % 25 == 0 or index == len(sorted_file_targets):
            log(f"  signed {index}/{len(sorted_file_targets)} executable files ({elapsed_since(start)})")
    sorted_bundle_targets = sorted(bundle_targets, key=lambda p: len(p.parts), reverse=True)
    for index, path in enumerate(sorted_bundle_targets, start=1):
        sign_path(path, entitlements_dir)
        if index % 10 == 0 or index == len(sorted_bundle_targets):
            log(f"  signed {index}/{len(sorted_bundle_targets)} nested bundles ({elapsed_since(start)})")
    # ShipIt validates downloaded updates against the installed app's designated
    # requirement. The ad-hoc default DR is a cdhash pin that no official update
    # can satisfy, which is why patched apps silently never install updates.
    # An identifier-only DR is satisfied by any official Claude release, so
    # auto-update keeps working (the update replaces the patch with the
    # official app; re-run the patch afterwards).
    bundle_id = app_bundle_identifier(app)
    if bundle_id:
        sign_path(app, entitlements_dir, designated_requirement=f'identifier "{bundle_id}"')
    else:
        log("  warning: CFBundleIdentifier not found; keeping default ad-hoc DR (auto-update will not install)")
        sign_path(app, entitlements_dir)
    log(f"Re-signed patched app in {elapsed_since(start)}")

def codesign_info(app: Path) -> dict[str, Any]:
    """Return a small codesign summary for *app* (best-effort)."""
    result = run(["codesign", "-dv", "--verbose=4", str(app)], check=False)
    text = (result.stdout or "") + (result.stderr or "")
    flags = ""
    for line in text.splitlines():
        if "flags=" in line:
            flags = line.split("flags=", 1)[-1].strip()
            break
    entitlements = load_entitlements(app) or {}
    return {
        "flags": flags,
        "adhoc": "adhoc" in flags or "Signature=adhoc" in text or "Signature=adhoc" in (result.stderr or ""),
        "hardened_runtime": "runtime" in flags,
        "get_task_allow": bool(entitlements.get("com.apple.security.get-task-allow")),
        "entitlements": entitlements,
        "raw": text,
    }

def clear_quarantine(app: Path) -> None:
    result = run(["xattr", "-dr", "com.apple.quarantine", str(app)], check=False)
    if result.returncode == 0:
        print("Cleared Gatekeeper quarantine attribute")

def set_user_locale(user_home: Path, lang_code: str) -> None:
    """Write the app locale into Claude (+ Claude-3p if present) user config."""
    shell_locale = lang_code
    targets = [
        user_home / "Library/Application Support/Claude/config.json",
        user_home / "Library/Application Support/Claude-3p/config.json",
    ]
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if not (sudo_uid and sudo_gid) and os.geteuid() == 0:
        # macOS `do shell script ... with administrator privileges` does not
        # provide SUDO_UID/GID; preserve the owner of the requested home.
        owner = user_home.stat()
        sudo_uid, sudo_gid = str(owner.st_uid), str(owner.st_gid)

    for config in targets:
        if config.name.startswith("Claude-3p") and not config.parent.exists():
            continue
        config.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if config.exists():
            try:
                data = load_json(config)
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                backup = config.with_suffix(".json.bak-invalid")
                shutil.copy2(config, backup)
                print(f"Existing config was not valid JSON; backed up to {backup}")
                data = {}
        if data.get("locale") == shell_locale:
            print(f"Claude config locale already {shell_locale}: {config}")
            continue
        data["locale"] = shell_locale
        save_json(config, data)
        os.chmod(config, 0o600)
        if sudo_uid and sudo_gid:
            os.chown(config, int(sudo_uid), int(sudo_gid))
        print(f"Set Claude config locale: {config} -> {shell_locale}")

def backup_and_replace(original: Path, patched: Path, dry_run: bool) -> Path:
    start = time.perf_counter()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"Claude.backup-before-zh-CN-{stamp}.app"
    if dry_run:
        print(f"[dry-run] Would move {original} -> {backup}")
        print(f"[dry-run] Would move {patched} -> {original}")
        return backup

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Backing up current app: {backup}")
    shutil.move(str(original), str(backup))
    log(f"Installing patched app: {original}")
    shutil.move(str(patched), str(original))
    log(f"Replaced app in {elapsed_since(start)}")
    return backup

def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()

def find_app_backups(app: Path) -> list[Path]:
    """List backups from the workspace backups/ dir (app kept for call-site compatibility)."""
    if not BACKUP_DIR.is_dir():
        return []
    return sorted(path for path in BACKUP_DIR.glob(BACKUP_GLOB) if path.is_dir())

def bundle_version(app: Path) -> str:
    """Return the app bundle version, or "0" when it cannot be read."""
    info_plist = app / "Contents/Info.plist"
    try:
        with info_plist.open("rb") as f:
            info = plistlib.load(f)
    except Exception:
        return "0"
    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "0"

def version_key(value: str) -> tuple[int, ...]:
    """Numeric sort key for versions like "1.2" or "1.46388.3"."""
    parts = [int(part) for part in re.findall(r"\d+", value)]
    return tuple(parts + [0] * (3 - len(parts)))

def _official_signature(info: dict[str, Any]) -> bool:
    """Classify a codesign_info() result as an unpatched official build."""
    if info.get("adhoc"):
        return False
    return "Authority=Developer ID Application" in info.get("raw", "")

def is_officially_signed(app: Path) -> bool:
    """True when *app* still carries its official Developer ID signature.

    The patcher always re-signs with an ad-hoc signature, so a non-official
    signature means the on-disk app was patched (or Frida-prepared) by this
    toolchain and only a backup restore brings back clean official resources.
    Anything unreadable counts as non-official to stay conservative.
    """
    try:
        return _official_signature(codesign_info(app))
    except Exception:
        return False

def preinstall_should_restore(app: Path) -> bool:
    """True when the installed app needs a backup restore before re-patching.

    Auto-update replaces the patched app with an official build, so an
    officially signed install is never restored: restoring would downgrade
    Claude to a stale backup and delete the fresh version (issue #156).
    """
    return not (app.exists() and is_officially_signed(app))

def select_backup(backups: list[Path]) -> Path:
    """Pick the backup with the highest bundled version (name as tie-breaker).

    Restoring the oldest backup could downgrade Claude below the currently
    installed version; the highest-version backup is the safest official
    snapshot to fall back to.
    """
    return max(backups, key=lambda path: (version_key(bundle_version(path)), path.name))

def prune_stale_backups(app: Path, keep: Path, dry_run: bool) -> None:
    """Delete all zh-CN app backups except *keep*."""
    for path in find_app_backups(app):
        if path == keep:
            continue
        if dry_run:
            print(f"[dry-run] Would delete stale backup: {path}")
            continue
        print(f"Deleting stale backup: {path}")
        remove_path(path)

def restore_backup(app: Path, dry_run: bool) -> Path:
    backups = find_app_backups(app)
    if not backups:
        raise SystemExit(f"No Claude backup found in {BACKUP_DIR}: {BACKUP_GLOB}")

    backup = select_backup(backups)
    extra_backups = [path for path in backups if path != backup]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    current_tmp = app.with_name(f"Claude.restore-current-{stamp}.app")

    if dry_run:
        if app.exists():
            print(f"[dry-run] Would move current app {app} -> {current_tmp}")
        print(f"[dry-run] Would restore backup {backup} -> {app}")
        for extra_backup in extra_backups:
            print(f"[dry-run] Would delete extra backup: {extra_backup}")
        return backup

    if app.exists():
        print(f"Moving current app aside: {current_tmp}")
        shutil.move(str(app), str(current_tmp))

    try:
        print(f"Restoring backup: {backup}")
        shutil.move(str(backup), str(app))
    except Exception:
        if current_tmp.exists() and not app.exists():
            shutil.move(str(current_tmp), str(app))
        raise

    if current_tmp.exists():
        print(f"Removing replaced app: {current_tmp}")
        remove_path(current_tmp)
    for extra_backup in extra_backups:
        print(f"Deleting extra backup: {extra_backup}")
        remove_path(extra_backup)
    return backup

def verify_electron_asar_integrity(app: Path) -> None:
    info_plist = app / "Contents/Info.plist"
    with info_plist.open("rb") as f:
        info = plistlib.load(f)
    integrity = info.get("ElectronAsarIntegrity", {})
    if not isinstance(integrity, dict):
        raise SystemExit("Unsupported ElectronAsarIntegrity shape in Info.plist.")

    targets = [("Resources/app.asar", app / APP_ASAR_REL)]
    frontend_asar = app / FRONTEND_ASAR_REL
    if frontend_asar.is_file():
        targets.append(("Resources/ion-dist.asar", frontend_asar))

    for key, asar_path in targets:
        entry = integrity.get(key)
        # app.asar 必须由官方声明；ion-dist.asar 仅在应用声明了完整性时才校验。
        if not isinstance(entry, dict):
            if key == "Resources/app.asar":
                raise SystemExit(f"Electron {key} integrity entry is missing from Info.plist.")
            continue
        data = asar_path.read_bytes()
        _header_size, header_string, _header = read_asar_header(data, asar_path)
        expected = asar_header_hash(header_string)
        actual = entry.get("hash")
        if actual != expected:
            raise SystemExit(
                f"Electron {key} integrity mismatch: "
                f"Info.plist has {actual!r}, expected {expected}."
            )
        print(f"Verified Electron {key} integrity")
    verify_integrity_digest_slot(app, integrity)

def read_frontend_entry(app: Path, relative: str) -> bytes | None:
    """Read one frontend file whether it lives in ion-dist/ or ion-dist.asar."""
    asar = app / FRONTEND_ASAR_REL
    if asar.is_file():
        data = asar.read_bytes()
        header_size, _header_string, header = read_asar_header(data, asar)
        return read_asar_entry(data, header_size, header, relative)
    path = app / FRONTEND_DIR_REL / relative
    return path.read_bytes() if path.is_file() else None

def verify(app: Path, lang_code: str) -> None:
    start = time.perf_counter()
    raw = read_frontend_entry(app, f"i18n/{lang_code}.json")
    if raw is None:
        raise SystemExit(f"Missing frontend i18n catalog: i18n/{lang_code}.json")
    data = json.loads(raw.decode("utf-8"))
    values = [v for v in data.values() if isinstance(v, str)]
    chinese = sum(1 for v in values if re.search(r"[\u4e00-\u9fff]", v))
    print(f"Verified frontend {lang_code} JSON: {chinese}/{len(values)} strings contain Chinese")

    if read_frontend_entry(app, "i18n/dynamic/en-US.json") is not None:
        if read_frontend_entry(app, f"i18n/dynamic/{lang_code}.json") is None:
            raise SystemExit(
                f"Missing required dynamic i18n catalog: i18n/dynamic/{lang_code}.json; "
                "Claude >= 2.9939.2 fails to load the language without it."
            )
        print(f"Verified frontend dynamic {lang_code} catalog exists")

    verify_electron_asar_integrity(app)

    verify_result = run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], check=False)
    if verify_result.returncode == 0:
        print("Verified app signature")
    else:
        print("App signature verification failed:")
        print(verify_result.stdout, end="")
        raise SystemExit("Refusing to install an app with an invalid signature.")

    entitlements = read_entitlements(app)
    if "com.apple.security.virtualization" in entitlements:
        print("Verified virtualization entitlement")
    else:
        raise SystemExit("Virtualization entitlement is missing after re-signing.")

    result = run(["codesign", "-dv", str(app)], check=False).stdout
    for line in result.splitlines():
        if line.startswith("TeamIdentifier="):
            print(line)
    log(f"Verification finished in {elapsed_since(start)}")
