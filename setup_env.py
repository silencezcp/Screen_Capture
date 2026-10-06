# -*- coding: utf-8 -*-
"""一键准备运行/打包环境：检查依赖、缺什么就用国内镜像装什么。

用法：
    python setup_env.py              # 检查并补齐依赖（优先用项目内 .venv）
    python setup_env.py --check      # 只检查，不安装
    python setup_env.py --rebuild    # 删掉 .venv 重新创建
    python setup_env.py --no-venv    # 不建虚拟环境，直接装在当前 Python 里

镜像顺序：清华 -> 阿里云 -> 官方源（前一个失败自动换下一个）。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
VENV_PY = VENV / "Scripts" / "python.exe"

MIRRORS = [
    ("清华", "https://pypi.tuna.tsinghua.edu.cn/simple", "pypi.tuna.tsinghua.edu.cn"),
    ("阿里云", "https://mirrors.aliyun.com/pypi/simple/", "mirrors.aliyun.com"),
    ("官方", "https://pypi.org/simple/", ""),
]

# 运行/打包需要的包：导入名 -> pip 安装名（含最低版本）
REQUIREMENTS = [
    ("PyQt6", "PyQt6>=6.4", "界面（必需）"),
    ("PIL", "Pillow>=9.1.0", "截图图像处理（必需）"),
    ("numpy", "numpy", "录屏取帧（必需）"),
    ("wgc_python", "wgc_python>=2.0", "WGC 窗口捕获（可选，缺了退化为 GDI）"),
    ("PyInstaller", "pyinstaller", "打包成 exe（打包时必需）"),
]


def log(message: str = "") -> None:
    print(message, flush=True)


def module_ok(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def run(cmd: list, **kwargs) -> int:
    log("  $ " + " ".join(str(c) for c in cmd))
    try:
        return subprocess.run(cmd, **kwargs).returncode
    except Exception as exc:
        log(f"  执行失败：{exc}")
        return 1


def pip_install(python: Path, spec: str, upgrade: bool = False) -> bool:
    """用镜像列表依次尝试安装一个包。"""
    for label, index, host in MIRRORS:
        cmd = [str(python), "-m", "pip", "install", spec,
               "-i", index, "--timeout", "60", "--disable-pip-version-check"]
        if host:
            cmd += ["--trusted-host", host]
        if upgrade:
            cmd.append("--upgrade")
        log(f"  用{label}镜像安装 {spec} …")
        if run(cmd) == 0:
            return True
        log(f"  {label}镜像失败，换下一个")
    return False


def ensure_venv(rebuild: bool = False) -> Path:
    if rebuild and VENV.exists():
        log(f"删除旧的虚拟环境：{VENV}")
        shutil.rmtree(VENV, ignore_errors=True)
    if not VENV_PY.exists():
        log(f"创建虚拟环境：{VENV}")
        code = run([sys.executable, "-m", "venv", str(VENV)])
        if code != 0 or not VENV_PY.exists():
            raise SystemExit("创建虚拟环境失败，请检查 Python 安装是否完整")
    return VENV_PY


def check(python: Path) -> tuple:
    """返回 (已安装, 缺失) 两个列表。

    注意：只看 ``find_spec`` 不够——PyQt6 装上了也可能因为缺 DLL 而导入失败，
    所以对每个包都做一次真实 import。
    """
    probe = (
        "import json, importlib, traceback\n"
        "names = " + repr([name for name, _spec, _desc in REQUIREMENTS]) + "\n"
        "result = {}\n"
        "for n in names:\n"
        "    try:\n"
        "        importlib.import_module(n)\n"
        "        result[n] = True\n"
        "    except Exception:\n"
        "        result[n] = False\n"
        "print('RESULT:' + json.dumps(result))\n"
    )
    import json

    try:
        result = subprocess.run([str(python), "-c", probe], capture_output=True, text=True,
                                timeout=180)
        line = ""
        for candidate in (result.stdout or "").splitlines():
            if candidate.startswith("RESULT:"):
                line = candidate
        found = json.loads(line[len("RESULT:"):]) if line else {}
    except Exception as exc:
        log(f"依赖检查失败：{exc}")
        return [], [spec for _n, spec, _d in REQUIREMENTS]

    installed, missing = [], []
    for name, spec, desc in REQUIREMENTS:
        (installed if found.get(name) else missing).append(f"{name}（{desc}）")
    return installed, missing


def main() -> int:
    parser = argparse.ArgumentParser(description="检查/补齐运行与打包依赖")
    parser.add_argument("--check", action="store_true", help="只检查，不安装")
    parser.add_argument("--rebuild", action="store_true", help="重建虚拟环境")
    parser.add_argument("--no-venv", action="store_true", help="直接装在当前 Python 里")
    parser.add_argument("--with-packaging", action="store_true", default=True,
                        help="连 PyInstaller 一起检查（默认开启）")
    args = parser.parse_args()

    log("=" * 60)
    log("环境自检：" + sys.version.split()[0] + "  " + sys.executable)
    log("=" * 60)

    if args.no_venv:
        python = Path(sys.executable)
    else:
        python = ensure_venv(rebuild=args.rebuild)
        log(f"使用虚拟环境：{python}")

    installed, missing = check(python)
    log()
    log(f"已安装 {len(installed)} 项：")
    for item in installed:
        log(f"  [OK]   {item}")
    if not missing:
        log()
        log("依赖齐了，可以直接运行：python run.py")
        log("打包 exe：python build_exe.py --onefile")
        return 0

    log()
    log(f"缺少 {len(missing)} 项：")
    for item in missing:
        log(f"  [缺]   {item}")
    if args.check:
        return 1

    log()
    log("开始安装（按 清华 -> 阿里云 -> 官方源 依次尝试）")
    specs = [spec for name, spec, _d in REQUIREMENTS
             if not module_ok_in(python, name)]
    failed = []
    for spec in specs:
        if not pip_install(python, spec):
            failed.append(spec)

    log()
    installed, missing = check(python)
    if failed:
        log(f"以下包没能装上：{', '.join(failed)}")
    if missing:
        log("仍有缺失：" + "，".join(missing))
        log("可以手动重试，例如：")
        log(f"  {python} -m pip install {' '.join(specs)} "
            f"-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com")
        return 1
    log("依赖已全部就绪。")
    log(f"运行：{python} run.py")
    log(f"打包：{python} build_exe.py --onefile")
    return 0


def module_ok_in(python: Path, name: str) -> bool:
    """在目标解释器里检查某个模块是否已装。"""
    try:
        return subprocess.run([str(python), "-c", f"import importlib.util,sys;"
                               f"sys.exit(0 if importlib.util.find_spec({name!r}) else 1)"],
                              capture_output=True).returncode == 0
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
