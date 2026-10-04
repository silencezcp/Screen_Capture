# -*- coding: utf-8 -*-
"""把「应用窗口定时截图工具」打包成 exe（基于 PyInstaller）。

用法：
    python build_exe.py                  # 目录版（默认，最稳、启动快）
    python build_exe.py --onefile        # 单文件 exe（会解压到 %TEMP%，受限环境可能起不来）
    python build_exe.py --gui-only       # 只打包界面版
    python build_exe.py --cli-only       # 只打包命令行版
    python build_exe.py --clean          # 先清掉 PyInstaller 缓存再打包

前置条件：pip install pyinstaller pyqt5 wgc_python
产物：dist\\应用窗口定时截图工具\\应用窗口定时截图工具.exe（界面版，PyQt5）
      dist\\应用窗口定时截图工具-命令行\\应用窗口定时截图工具-命令行.exe（命令行版）
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
ICON = ROOT / "assets" / "app.ico"
ASSETS = ROOT / "assets"
# 默认输出到 dist；可用环境变量 SCREEN_CAPTURE_DIST 指定别的位置
# （dist 里的程序正在运行时文件会被占用，这时就用它换个目录打包）
DIST = Path(os.environ.get("SCREEN_CAPTURE_DIST") or (ROOT / "dist"))
BUILD = ROOT / "build"
def app_version() -> str:
    """版本号唯一来源：screen_capture/__init__.py 里的 __version__。

    （直接读文件、不 import，避免打包脚本被运行时依赖拖住。）
    """
    try:
        text = (ROOT / "screen_capture" / "__init__.py").read_text(encoding="utf-8")
        found = re.search(r'__version__\s*=\s*["'']([^"'']+)', text)
        if found:
            return found.group(1)
    except Exception:
        pass
    return "0.0.0"


VERSION_TEXT = f"{app_version()}.0"
_parts = [int(p) for p in re.findall(r"\d+", app_version())][:4]
VERSION = tuple(_parts + [0] * (4 - len(_parts)))

GUI_NAME = "应用窗口定时截图工具"
CLI_NAME = "应用窗口定时截图工具-命令行"

# tkinter / Pillow / PyQt5 / WGC 的子模块要显式声明，打包后才不会缺件
HIDDEN_IMPORTS = [
    "PIL.Image", "PIL.ImageTk", "PIL._tkinter_finder",
    "PIL.PngImagePlugin", "PIL.JpegImagePlugin", "PIL.BmpImagePlugin", "PIL.WebPImagePlugin",
    "tkinter", "tkinter.ttk", "tkinter.font", "tkinter.filedialog",
    "tkinter.messagebox", "tkinter.scrolledtext",
    "PyQt5", "PyQt5.sip", "PyQt5.QtCore", "PyQt5.QtGui", "PyQt5.QtWidgets", "PyQt5.QtNetwork",
    "numpy", "wgc_python",
    "screen_capture", "screen_capture.cli", "screen_capture.gui", "screen_capture.gui_qt",
    "screen_capture.engine", "screen_capture.win32", "screen_capture.selftest",
    "screen_capture.capture_wgc", "screen_capture.applog",
]

# 用不到的大块头，排除掉可以显著减小体积
EXCLUDES = [
    "numpy.testing", "pandas", "matplotlib", "scipy", "sympy", "sqlalchemy", "cv2",
    "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebEngineCore", "PyQt5.QtWebEngine",
    "PyQt5.QtQml", "PyQt5.QtQuick", "PyQt5.QtQuickWidgets", "PyQt5.QtMultimedia",
    "PyQt5.QtMultimediaWidgets", "PyQt5.Qt3DCore", "PyQt5.Qt3DRender",
    "PyQt5.QtCharts", "PyQt5.QtDataVisualization", "PyQt5.QtBluetooth",
    "PyQt5.QtNfc", "PyQt5.QtPositioning", "PyQt5.QtLocation", "PyQt5.QtSensors",
    "PyQt5.QtSerialPort", "PyQt5.QtWebSockets", "PyQt5.QtTest", "PyQt5.QtDesigner",
    "PyQt5.QtHelp", "PyQt5.QtSql", "PyQt5.QtXmlPatterns", "PyQt5.QtOpenGL",
    "PyQt5.QtSvg", "PyQt5.QtPrintSupport",
    "PySide2", "PySide6", "wx", "gi",
    "IPython", "jupyter", "notebook", "pytest", "PyInstaller",
]


def log(message: str) -> None:
    print(message, flush=True)


def ensure_pyinstaller() -> bool:
    """确保 PyInstaller 可以被导入；顺带支持放在项目里的 packages 目录。"""
    if importlib.util.find_spec("PyInstaller") is not None:
        return True
    local = ROOT / "packages"
    if local.is_dir():
        sys.path.insert(0, str(local))
        os.environ["PYTHONPATH"] = str(local) + os.pathsep + os.environ.get("PYTHONPATH", "")
        if importlib.util.find_spec("PyInstaller") is not None:
            return True
    return False


def write_version_file(path: Path, description: str) -> Path:
    v = f"({VERSION[0]}, {VERSION[1]}, {VERSION[2]}, {VERSION[3]})"
    template = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={v},
    prodvers={v},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404B0',
        [StringStruct('CompanyName', ''),
         StringStruct('FileDescription', '{description}'),
         StringStruct('FileVersion', '{VERSION_TEXT}'),
         StringStruct('InternalName', 'screen_capture'),
         StringStruct('OriginalFilename', 'screen_capture.exe'),
         StringStruct('ProductName', '应用窗口定时截图工具'),
         StringStruct('ProductVersion', '{VERSION_TEXT}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template, encoding="utf-8")
    return path


def wgc_data_files() -> list:
    """wgc_python 里带的 wgc_python.dll 属于包数据，必须手动带上。"""
    if importlib.util.find_spec("wgc_python") is None:
        log("提示：没有找到 wgc_python，打包后 WGC 捕获不可用（pip install wgc_python）")
        return []
    import wgc_python

    package_dir = Path(wgc_python.__file__).resolve().parent
    files = [f for f in package_dir.iterdir() if f.suffix.lower() in (".dll", ".pyd")]
    if not files:
        log(f"警告：{package_dir} 里没有找到 DLL")
        return []
    return [f"{path}{os.pathsep}wgc_python" for path in files]


def build(name: str, description: str, console: bool, onefile: bool, clean: bool) -> Path:
    version_file = write_version_file(BUILD / f"version_{'console' if console else 'gui'}.txt", description)
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--log-level", "WARN",
        "--name", name,
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--onefile" if onefile else "--onedir",
        "--console" if console else "--windowed",
        "--version-file", str(version_file),
    ]
    if ICON.is_file():
        command += ["--icon", str(ICON)]
    if ASSETS.is_dir():
        # 界面图标和下拉箭头都是运行时要读的文件，必须一起打进去
        command += ["--add-data", f"{ASSETS}{os.pathsep}assets"]
    for item in HIDDEN_IMPORTS:
        command += ["--hidden-import", item]
    for item in EXCLUDES:
        command += ["--exclude-module", item]
    for item in wgc_data_files():
        command += ["--add-data", item]
    if clean:
        command.append("--clean")
    command.append(str(ROOT / "run.py"))

    log(f"\n=== 打包 {'命令行版' if console else '界面版'}：{name} ===")
    log("命令：" + " ".join(f'"{c}"' if " " in c else c for c in command))
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit(f"打包失败（退出码 {result.returncode}）：{name}")
    target = DIST / (f"{name}.exe" if onefile else name)
    if not target.exists():
        raise SystemExit(f"打包似乎成功，但没有找到产物：{target}")
    return target


def human_size(path: Path) -> str:
    if path.is_file():
        return f"{path.stat().st_size / 1024 / 1024:.1f} MB"
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return f"{total / 1024 / 1024:.1f} MB（目录）"


def integrity_label(path: Path) -> str:
    """读文件的完整性标签：Low / Medium / 空（没有显式标签=继承默认）。"""
    try:
        result = subprocess.run(["icacls", str(path)], capture_output=True, text=True,
                                encoding="gbk", errors="replace")
    except Exception:  # pragma: no cover
        return ""
    for line in (result.stdout or "").splitlines():
        if "Mandatory" not in line and "强制" not in line:
            continue
        if "Low" in line or "低" in line:
            return "Low"
        if "Medium" in line or "中" in line:
            return "Medium"
    return ""


def create_shortcut(target: Path, name: str = "应用窗口定时截图工具",
                    desktop: str = "user") -> Optional[Path]:
    """在桌面创建快捷方式。

    做法是「先写一个 UTF-8 的 .ps1，再执行它」，避免把中文直接塞进命令行
    （cmd → PowerShell 的编码很容易把中文变成乱码，最后静默失败）。
    desktop="public" 时建到「公用桌面」，本机所有用户都能看到。
    """
    if not target.exists():
        log(f"  跳过快捷方式：找不到 {target}")
        return None
    script = BUILD / "make_shortcut.ps1"
    script.parent.mkdir(parents=True, exist_ok=True)
    desktop_expr = ("[Environment]::GetFolderPath('CommonDesktopDirectory')"
                    if desktop == "public" else "[Environment]::GetFolderPath('Desktop')")
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"$desktop = {desktop_expr}\n"
        f"$link = Join-Path $desktop '{name}.lnk'\n"
        "$shell = New-Object -ComObject WScript.Shell\n"
        "$sc = $shell.CreateShortcut($link)\n"
        f"$sc.TargetPath = '{target}'\n"
        f"$sc.WorkingDirectory = '{target.parent}'\n"
        f"$sc.IconLocation = '{target},0'\n"
        "$sc.Description = '应用窗口定时截图工具'\n"
        "$sc.Save()\n"
        "Write-Output $link\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    link = None
    for line in (result.stdout or "").splitlines():
        if line.strip().lower().endswith(".lnk"):
            link = Path(line.strip())
    if result.returncode != 0 or link is None:
        log(f"  创建桌面快捷方式失败：{output[:200]}")
        return None
    log(f"  桌面快捷方式：{link}")
    return link


def grant_all_users_read(directory: Path) -> None:
    """给「所有用户」只读+执行权限（多人共用时使用）。

    只读很关键：程序目录不可写时，程序会自动把日志与默认截图目录放到
    每个用户自己的 %LOCALAPPDATA%，多人同时用互不干扰。
    """
    # 注意：**不要**用 /inheritance:r！
    # 实测那样只会把目录授权给 Users，文件（exe/DLL）上没落地，
    # 结果普通用户双击报「Windows 无法访问指定设备、路径或文件」。
    # 正确做法：先把 ACL 重置为继承 ProgramData 的系统默认值，再显式补一条
    # 「所有用户 读取+执行」，目录和文件会一起生效。
    for args in (
        ["icacls", str(directory), "/reset", "/T", "/C"],
        ["icacls", str(directory), "/grant", "*S-1-5-32-545:(OI)(CI)RX", "/T", "/C"],
    ):
        result = subprocess.run(args, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if result.returncode != 0:
            log(f"  设置权限失败：{((result.stdout or '') + (result.stderr or '')).strip()[:200]}")
            return
    # 复核：拿一个文件看看 Users 是否真的能读
    probe = next((p for p in directory.rglob("*.exe")), None)
    check = subprocess.run(["icacls", str(probe or directory)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    ok = "Users" in (check.stdout or "") or "S-1-5-32-545" in (check.stdout or "")
    log(f"  已设置权限：所有用户可读+执行，管理员可写{'' if ok else '（未能确认，请抽查）'}")


def deploy(targets, all_users: bool = False) -> int:
    """把成品复制到普通目录。

    默认装到当前用户的 %LOCALAPPDATA%\\ScreenCaptureTool；
    all_users=True 时装到 %PROGRAMDATA%\\ScreenCaptureTool 并授权所有用户只读，
    再在「公用桌面」建快捷方式 —— 服务器上多人共用一份程序。

    受限环境（例如 DSH 工作区）里目录带了 Low 完整性标签，从中写出的 exe 会继承 Low；
    进程以低完整性启动后，WGC 抓窗口会被系统拒绝（0x80070005）。
    复制到普通目录后新文件继承 Medium 标签，WGC 就正常了。
    """
    if all_users:
        destination = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData") / "ScreenCaptureTool"
    else:
        destination = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ScreenCaptureTool"
    destination.mkdir(parents=True, exist_ok=True)
    log(f"\n=== 部署到 {destination} ===")
    for path in targets:
        if not path.exists():
            continue
        # 注意顺序：命令行版名字里包含界面版名字，必须先判断命令行版
        if CLI_NAME in path.name:
            name = "cli"
        elif GUI_NAME in path.name:
            name = "app"
        else:
            name = "app"
        dest = destination / name
        removed = True
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
            # rmtree 可能因文件被占用而静默失败（杀软/索引器拿着句柄），
            # 所以必须再确认一次，并允许覆盖式复制，别让部署半途抛 FileExistsError。
            removed = not dest.exists()
        if path.is_dir():
            try:
                shutil.copytree(path, dest, dirs_exist_ok=True)
            except OSError as exc:
                log(f"  复制失败：{exc}")
                log("  提示：目标目录可能被正在运行的程序占用，请先关掉它再重新部署。")
                return 1
            log(f"  已复制目录：{dest}   {human_size(dest)}")
            if not removed:
                log("    提示：旧目录未能完全删除，已直接覆盖（残留旧文件不影响使用）")
            label = integrity_label(dest)
        else:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest / path.name)
            log(f"  已复制文件：{dest / path.name}   {human_size(path)}")
            label = integrity_label(dest / path.name)
        if all_users:
            # 多人共用：留下标记 + 授权所有用户只读执行
            (dest / "multi_user.txt").write_text(
                "多人共用安装：日志与默认截图目录按用户分开存放。\n", encoding="utf-8")
            grant_all_users_read(dest)
        log(f"    完整性标签：{label or '无显式标签（默认 Medium，WGC 可用）'}")
    app_dir = destination / "app"
    exe_candidates = list(app_dir.glob("*.exe")) if app_dir.is_dir() else []
    for path in targets:
        if path.is_file():
            exe_candidates.append(path)
    if exe_candidates:
        log("")
        create_shortcut(exe_candidates[0], desktop="public" if all_users else "user")
    log("以后运行部署目录里的 exe 即可（WGC 抓窗口需要 Medium 及以上权限）。")
    return 0


def existing_targets(onefile: bool):
    """不重新打包时，直接找 dist 里已有的产物。"""
    found = []
    for name in (GUI_NAME, CLI_NAME):
        path = DIST / (f"{name}.exe" if onefile else name)
        if path.exists():
            found.append(path)
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="用 PyInstaller 打包成 exe")
    parser.add_argument("--onefile", action="store_true",
                        help="打包成单个 exe（默认是目录版；单文件版需要可写的 %%TEMP%%）")
    parser.add_argument("--gui-only", action="store_true", help="只打包界面版")
    parser.add_argument("--cli-only", action="store_true", help="只打包命令行版")
    parser.add_argument("--clean", action="store_true", help="打包前清理 PyInstaller 缓存")
    parser.add_argument("--keep-build", action="store_true", help="保留 build 中间目录")
    parser.add_argument("--deploy-only", action="store_true", help="不打包，只把 dist 里现有产物复制到普通目录")
    parser.add_argument("--no-deploy", action="store_true", help="打包后不自动部署")
    parser.add_argument("--all-users", action="store_true",
                        help="部署给本机所有用户（PROGRAMDATA + 公用桌面快捷方式，需管理员）")
    args = parser.parse_args(argv)

    if args.deploy_only:
        targets = existing_targets(args.onefile)
        if not targets:
            log("dist 目录里没有可部署的产物，请先运行 python build_exe.py")
            return 1
        code = deploy(targets, all_users=args.all_users)
        if code:
            log("\n部署失败。请关掉正在运行的本程序后重试：python build_exe.py --deploy-only")
        return code

    if not ensure_pyinstaller():
        log("没有找到 PyInstaller，请先安装：pip install pyinstaller")
        return 1

    # 让 PyInstaller 的缓存落在项目内，避免写入用户目录（也方便清理）
    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(BUILD / "pyinstaller-cache"))
    BUILD.mkdir(parents=True, exist_ok=True)

    if not ICON.is_file():
        log(f"提示：没有找到图标 {ICON}，将使用默认图标（可运行 python assets/make_icon.py 生成）。")

    onefile = args.onefile
    targets = []
    if not args.cli_only:
        targets.append(build(GUI_NAME, "应用窗口定时截图工具（图形界面）", console=False,
                             onefile=onefile, clean=args.clean))
    if not args.gui_only:
        targets.append(build(CLI_NAME, "应用窗口定时截图工具（命令行）", console=True,
                             onefile=onefile, clean=args.clean))

    if not args.keep_build:
        shutil.rmtree(BUILD, ignore_errors=True)

    log("\n=== 打包完成 ===")
    for path in targets:
        label = integrity_label(path)
        note = "  ← 带 Low 标签，WGC 会被系统拒绝，需要部署到普通目录" if label == "Low" else ""
        log(f"  {path}   {human_size(path)}{note}")

    need_deploy = any(integrity_label(path) == "Low" for path in targets)
    if not args.no_deploy:
        if need_deploy:
            log("\n检测到产物带 Low 完整性标签：这样的 exe 启动后会被降级为低权限，WGC 抓窗口会被系统拒绝，"
                "因此自动部署一份到普通目录。")
        deploy(targets)

    log("\n建议接着运行一次自检：")
    for path in targets:
        exe = (path / f"{path.name}.exe") if path.is_dir() else path
        log(f'  "{exe}" --selftest')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
