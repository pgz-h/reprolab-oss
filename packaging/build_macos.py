"""Build release artifacts outside the sole source repository."""
import argparse
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tomllib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument('--skip-dmg', action='store_true', help='Build the app and ZIP without creating a disk image.')
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    work, output = args.work_root.resolve(), args.output_dir.resolve()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error("This desktop build is currently verified only on macOS arm64.")
    if work.is_relative_to(source) or output.is_relative_to(source):
        parser.error("Build and release directories must be outside the source repository.")
    version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    names = ["ReproLab.app", f"ReproLab-{version}-macOS-arm64.zip", f"ReproLab-{version}-macOS-arm64.dmg"]
    if any((output / name).exists() for name in names):
        parser.error("Release artifacts already exist; choose a new output directory.")
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONPYCACHEPREFIX=str(work / "pycache"))
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm",
                    "--workpath", str(work / "pyinstaller"), "--distpath", str(work / "dist"),
                    str(source / "packaging/desktop.spec")], cwd=source, env=env, check=True)
    app = output / "ReproLab.app"
    subprocess.run(["ditto", str(work / "dist/ReproLab.app"), str(app)], check=True)
    bundle_info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if bundle_info.get("LSBackgroundOnly") or bundle_info.get("LSUIElement"):
        raise RuntimeError("Desktop bundle must allow a regular foreground application and keyboard focus.")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(output / names[1])], check=True)
    if args.skip_dmg:
        print(output)
        return
    image_source = work / "dmg-content"
    if image_source.exists():
        shutil.rmtree(image_source)  # Only this script's generated staging directory.
    image_source.mkdir()
    (image_source / "Applications").symlink_to("/Applications")
    subprocess.run(["ditto", str(app), str(image_source / "ReproLab.app")], check=True)
    shutil.copyfile(source / "docs/desktop.md", image_source / "使用说明.md")
    subprocess.run(["hdiutil", "create", "-volname", f"ReproLab {version}",
                    "-srcfolder", str(image_source), "-format", "UDZO",
                    str(output / names[2])], check=True)
    print(output)


if __name__ == "__main__":
    main()
