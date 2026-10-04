#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一条命令把 portable 便携包同步发布到 Gitee + GitCode（可选 GitHub）。

用法::

    python sync_release.py --version 1.0.7 --notes "本次更新说明"
    python sync_release.py --version 1.0.7 --check       # 只检查环境与现状，不上传
    python sync_release.py --version 1.0.7 --github      # 同时发布到 GitHub（默认不发）

做的事：
  1. 把 dist\\应用窗口定时截图工具（或本机部署版）打成 ScreenCaptureTool_v<版本>_portable_win64.zip
  2. 打标签 v<版本> 并推送 main + 标签到 origin（= Gitee）
  3. Gitee + GitCode：建发行版 + 上传附件（令牌取自环境变量 GITEE_TOKEN / GITCODE_TOKEN，\n     或 .tools/gitee_token.txt、.tools/gitcode_token.txt）
  4. GitHub：仅在 --github 时执行（令牌取自 git 凭据管理器，不落盘）
  5. 打印下载地址与 SHA256

注意：Gitee 的 attach_files 接口必须把 access_token 放在 URL 查询参数里，
放在表单里会返回 401（踩过的坑）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / ".tools"
GH_REPO = "silencezcp/Screen_Capture"
GITEE_REPO = "silence95/Screen_Capture"
GITCODE_REPO = "qq_24919633/Screen_Capture"
APP_FOLDER = "应用窗口定时截图工具"


def log(message: str = "") -> None:
    print(message, flush=True)


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(ROOT), text=True, encoding="utf-8",
                          errors="replace", capture_output=True, check=check)


def github_token() -> str:
    """从 git 凭据管理器取 GitHub 令牌（不写入仓库）。"""
    proc = subprocess.run(["git", "credential", "fill"], cwd=str(ROOT), text=True,
                          input="protocol=https\nhost=github.com\n\n",
                          capture_output=True, check=False)
    for line in (proc.stdout or "").splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def gitee_token() -> str:
    token = os.environ.get("GITEE_TOKEN", "").strip()
    if token:
        return token
    path = TOOLS / "gitee_token.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def gitcode_token() -> str:
    """GitCode 已禁用密码推送，只能用访问令牌。"""
    token = os.environ.get("GITCODE_TOKEN", "").strip()
    if token:
        return token
    path = TOOLS / "gitcode_token.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def make_bundle(version: str) -> Path:
    """把 portable 目录打成 zip。"""
    sources = [ROOT / "dist" / APP_FOLDER, Path(os.environ.get("LOCALAPPDATA", "")) / "ScreenCaptureTool" / "app"]
    source = next((p for p in sources if (p / f"{APP_FOLDER}.exe").exists()), None)
    if source is None:
        raise SystemExit("找不到 portable 目录：先运行 build_exe.py（或双击 打包EXE.bat）")
    out_dir = TOOLS / "release"
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = out_dir / f"ScreenCaptureTool_v{version}_portable_win64.zip"
    if bundle.exists():
        bundle.unlink()
    log(f"  打包源：{source}")
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, Path(APP_FOLDER) / path.relative_to(source))
    return bundle


def http_json(url: str, payload: dict | None = None, headers: dict | None = None,
              method: str = "POST") -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "screen-capture-release", **(headers or {})})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def http_get_json(url: str, headers: dict | None = None) -> object:
    request = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "screen-capture-release", **(headers or {})})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def upload_asset(url: str, bundle: Path, headers: dict) -> None:
    boundary = "----screen-capture-release"
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{bundle.name}"\r\n'
            f"Content-Type: application/zip\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = head + bundle.read_bytes() + tail
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "screen-capture-release", **headers})
    with urllib.request.urlopen(request, timeout=1800) as response:
        response.read()


def publish_github(version: str, tag: str, bundle: Path, notes: str, token: str) -> str:
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    release = None
    try:
        release = http_get_json(f"https://api.github.com/repos/{GH_REPO}/releases/tags/{tag}", headers)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    if release is None:
        release = http_json(f"https://api.github.com/repos/{GH_REPO}/releases",
                            {"tag_name": tag, "name": f"{tag} 便携版（Windows x64）",
                             "body": notes, "draft": False, "prerelease": False}, headers)
        log(f"  GitHub 发行版已创建：{release['html_url']}")
    else:
        log(f"  GitHub 发行版已存在：{release['html_url']}")
    existing = {a["name"] for a in release.get("assets", [])}
    if bundle.name in existing:
        log("  GitHub 附件已存在，跳过上传")
    else:
        upload_asset((release["upload_url"].split("{")[0]) + f"?name={bundle.name}", bundle, headers)
        log("  GitHub 附件已上传")
    return f"https://github.com/{GH_REPO}/releases/download/{tag}/{bundle.name}"


def publish_gitee(version: str, tag: str, bundle: Path, notes: str, token: str) -> str:
    if not token:
        log("  跳过 Gitee：没有令牌（设置 GITEE_TOKEN 或写入 .tools/gitee_token.txt）")
        return ""
    releases = http_get_json(f"https://gitee.com/api/v5/repos/{GITEE_REPO}/releases?access_token={token}&per_page=50")
    release = next((r for r in releases if r.get("tag_name") == tag), None)
    if release is None:
        release = http_json("https://gitee.com/api/v5/repos/" + GITEE_REPO + "/releases",
                            {"access_token": token, "tag_name": tag,
                             "name": f"{tag} 便携版（Windows x64）", "body": notes,
                             "target_commitish": "main"})
        log(f"  Gitee 发行版已创建 id={release.get('id')}")
    else:
        log(f"  Gitee 发行版已存在 id={release.get('id')}")
    existing = {a["name"] for a in release.get("assets", [])}
    if bundle.name in existing:
        log("  Gitee 附件已存在，跳过上传")
    else:
        # 注意：Gitee 必须把 access_token 放查询参数，放表单会 401
        upload_asset(f"https://gitee.com/api/v5/repos/{GITEE_REPO}/releases/{release['id']}"
                     f"/attach_files?access_token={token}", bundle, {})
        log("  Gitee 附件已上传")
    return f"https://gitee.com/{GITEE_REPO}/releases/download/{tag}/{bundle.name}"


def publish_gitcode(version: str, tag: str, bundle: Path, notes: str, token: str) -> str:
    """GitCode 的 API 与 Gitee 兼容（/api/v5），令牌同样必须走查询参数。"""
    if not token:
        log("  跳过 GitCode：没有令牌（设置 GITCODE_TOKEN 或写入 .tools/gitcode_token.txt）")
        return ""
    releases = http_get_json(f"https://gitcode.com/api/v5/repos/{GITCODE_REPO}"
                             f"/releases?access_token={token}&per_page=50")
    release = next((r for r in releases if r.get("tag_name") == tag), None)
    if release is None:
        release = http_json(f"https://gitcode.com/api/v5/repos/{GITCODE_REPO}/releases?access_token={token}",
                            {"tag_name": tag,
                             "name": f"{tag} 便携版（Windows x64）", "body": notes,
                             "target_commitish": "main"})
        log(f"  GitCode 发行版已创建：{tag}")
    else:
        log(f"  GitCode 发行版已存在：{tag}")
    has_portable = any(a.get("name") == bundle.name for a in release.get("assets", []))
    if not has_portable:
        # GitCode 的发行版 JSON 没有 id 字段，也不提供附件上传接口（试过 404/405），
        # 所以便携包只能在它的网页上手动拖拽上传。
        log("  GitCode 不支持 API 上传附件，请到网页手动上传便携包：")
        log(f"    页面：https://gitcode.com/{GITCODE_REPO}/releases/{tag}")
        log(f"    文件：{bundle}")
    return f"https://gitcode.com/{GITCODE_REPO}/releases/{tag}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把 portable 包同步发布到 Gitee + GitCode（可选 GitHub）")
    parser.add_argument("--version", required=True, help="版本号，如 1.0.5（会自动加 v 前缀打标签）")
    parser.add_argument("--notes", default="", help="发行说明；留空则用默认说明")
    parser.add_argument("--check", action="store_true", help="只检查打包源与令牌，不上传")
    parser.add_argument("--no-push", action="store_true", help="不推送 git（只发发行版）")
    parser.add_argument("--github", action="store_true",
                        help="同时也发布到 GitHub（默认只发 Gitee）")
    args = parser.parse_args(argv)

    tag = args.version if args.version.startswith("v") else f"v{args.version}"
    version = tag.lstrip("v")
    notes = args.notes or (f"## {tag} 便携版（Windows x64）\n\n"
                           "解压到普通目录（桌面/文档/D:\\Tools）后双击 exe 即可使用。")

    gh_token, gt_token, gc_token = github_token(), gitee_token(), gitcode_token()
    log(f"版本：{tag}")
    if args.github:
        log(f"  GitHub 令牌：{'已获取' if gh_token else '缺失（git 凭据管理器里没有 github.com）'}")
    log(f"  Gitee   令牌：{'已获取' if gt_token else '缺失（设置 GITEE_TOKEN 或 .tools/gitee_token.txt）'}")
    log(f"  GitCode 令牌：{'已获取' if gc_token else '缺失（设置 GITCODE_TOKEN 或 .tools/gitcode_token.txt）'}")
    bundle = make_bundle(version)
    digest = sha256(bundle.read_bytes()).hexdigest().upper()
    log(f"  便携包：{bundle.name}  {bundle.stat().st_size / 1024 / 1024:.1f} MB")
    log(f"  SHA256：{digest}")

    if args.check:
        log("\n--check：只检查，不上传。")
        return 0

    if not args.no_push:
        log("\n推送 git（origin = Gitee + GitCode）…")
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True,
                              capture_output=True).stdout.strip()
        log(f"  当前提交：{head}")
        subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, check=False)
        subprocess.run(["git", "tag", "-f", tag], cwd=ROOT, check=False)
        subprocess.run(["git", "push", "-f", "origin", tag], cwd=ROOT, check=False)

    url_gh = ""
    if args.github and gh_token:
        url_gh = publish_github(version, tag, bundle, notes, gh_token)
    url_gt = publish_gitee(version, tag, bundle, notes, gt_token)
    url_gc = publish_gitcode(version, tag, bundle, notes, gc_token)

    log("\n=== 发布地址（Gitee + GitCode）===")
    if url_gt:
        log(f"  Gitee  ：{url_gt}")
    if url_gc:
        log(f"  GitCode：{url_gc}")
    if url_gh:
        log(f"  GitHub ：{url_gh}")
    log(f"  SHA256：{digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
