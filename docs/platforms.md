# 系统、架构与安装包

一个软件版本通常对应多种下载文件。文件名常按“软件版本 + 操作系统 + 芯片架构 + 包格式”命名，不要求每个项目提供全部组合。

| 系统 | 常见架构 | 常见包格式 | 说明 |
| --- | --- | --- | --- |
| Windows | x64、ARM64 | `.exe`、`.msi`、便携 `.zip` | x64 对应多数 Intel／AMD 电脑；ARM64 对应 Windows ARM 电脑 |
| macOS | arm64、x86_64、universal | `.dmg`、`.pkg`、应用 `.zip` | arm64 为 Apple 芯片，x86_64 为 Intel；universal 可同时包含两种架构 |
| Linux | x86_64／amd64、aarch64／arm64 | `.AppImage`、`.deb`、`.rpm`、`.tar.gz` | `.deb` 常见于 Debian／Ubuntu，`.rpm` 常见于 Fedora／RHEL 系；AppImage 为跨发行版分发方式之一 |

`x64`、`x86_64`、`amd64` 在常见桌面下载中指同一类 64 位 x86 架构；`ARM64`、`arm64`、`aarch64` 通常指 64 位 ARM。名字相近不代表不同系统的程序可以互相运行。

Linux 的 Ubuntu、Debian、Fedora 等是发行版；`.deb`、`.rpm`、AppImage 是安装或分发格式。Linux 包仍须核对发行版、系统库和架构兼容性，AppImage 也不保证支持所有 Linux 系统。

## ReproLab 当前状态

| 目标 | 当前材料 | 验证状态 |
| --- | --- | --- |
| macOS arm64 | 应用构建入口与共用源码 | 验证范围见本版记录 |
| Windows x64 | 适配源码、Windows 构建与实际 exe 验收脚本、GitHub Actions | 开发分支通过 Windows x64 Actions 原生检查与实际 exe 验收，提供未签名的 Actions 候选包 |
| Windows ARM64、macOS Intel | 尚无专用交付 | 未验证 |
| Linux x86_64／ARM64 | 通用源码；目录约定已覆盖 Linux | 未构建 AppImage／deb／rpm，未验证桌面运行 |

首发候选提供 Python wheel、源码和 macOS arm64 应用。Windows x64 候选包见对应的 Actions 运行记录。Linux 发行包需要在目标发行版和架构上另行验证。

Windows 候选步骤见[Windows 指南](windows.md)，当前实际检查见[验证状态](release-readiness.md)。

格式参考：[AppImage 官方说明](https://docs.appimage.org/introduction/index.html)、[Debian 软件包说明](https://www.debian.org/doc/manuals/debian-faq/pkg-basics.en.html)。
