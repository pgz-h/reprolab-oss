# Build with --workpath and --distpath outside the source repository.
from pathlib import Path
import importlib.metadata
import sysconfig
import sys
import tomllib
from packaging.version import Version
from PyInstaller.utils.hooks import collect_data_files, copy_metadata, collect_submodules
root = Path(SPECPATH).parent
version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
macos_version = ".".join(str(part) for part in Version(version).release)
distributions = ("pywin32",) if sys.platform == "win32" else ()
datas = sum((copy_metadata(name) for name in ("pytest", "pluggy", "iniconfig", "packaging", "pygments", "tomlkit", "PySide6-Essentials", "shiboken6")), [])
datas += sum((copy_metadata(name) for name in distributions), [])
datas += [(str(root / "src/reprolab" / name), "reprolab/" + name)
          for name in ("demo_case", "demo_recheck", "history_cases", "ui_assets")]
datas += [(str(root / "LICENSE"), "licenses/reprolab"), (str(root / "packaging/licenses"), "licenses/qt-pyside")]
for distribution in ("PySide6-Essentials", "shiboken6", "pytest", "pluggy", "iniconfig", "packaging", "pygments", "tomlkit", "pyinstaller", *distributions):
    dist = importlib.metadata.distribution(distribution)
    for item in dist.files or []:
        if ("license" in str(item).lower() or "copying" in str(item).lower()) and ".dist-info/" in str(item):
            datas.append((str(dist.locate_file(item)), "licenses/" + distribution))
for python_license in (Path(sysconfig.get_path("stdlib")) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE.txt"):
    if python_license.is_file():
        datas.append((str(python_license), "licenses/python"))
        break
# External venv workers still execute the regular isolated worker script.
datas += [(str(root / 'src/reprolab' / name), 'reprolab') for name in ('_worker.py', '_project_worker.py', '_vitest_worker.mjs')]
hiddenimports = collect_submodules("pytest") + collect_submodules("_pytest")
if sys.platform == "win32":
    # pywin32 imports this when converting timestamps returned by Win32 APIs.
    hiddenimports.append("win32timezone")
a = Analysis(
    [str(root / "packaging/desktop_entry.py")],
    pathex=[str(root / "src"), str(root / "packaging")],
    binaries=[], datas=datas, hiddenimports=hiddenimports,
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
              "PySide6.QtQuick", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
interpreter_options = [("X utf8", None, "OPTION")]
exe = EXE(pyz, a.scripts, interpreter_options, exclude_binaries=True, name="ReproLab",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, hide_console="hide-early" if sys.platform == "win32" else None,
          target_arch="arm64" if sys.platform == "darwin" else None, codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ReproLab")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="ReproLab.app", bundle_identifier="io.github.pgz-h.reprolab",
                 version=macos_version, info_plist={
                    "CFBundleDisplayName": "ReproLab", "CFBundleShortVersionString": macos_version,
                    # Keep console streams for worker dispatch while allowing GUI keyboard focus.
                    "LSBackgroundOnly": False,
                    "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "14.0",
                    "NSHumanReadableCopyright": "ReproLab contributors · MIT",
                 })
