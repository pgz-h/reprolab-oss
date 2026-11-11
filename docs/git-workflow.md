# 从两个 Git 版本验证修复

v0.3 直接读取同一个本地仓库里的两个提交，自动准备修复前后的源码副本。

示例：

    python -m reprolab verify-git /path/to/project --before HEAD~1 --after HEAD --test /path/to/regression.py --failure-type ValueError --failure-message "reported bug" --output /path/outside/project/reports

before/after 可以是提交编号、分支或标签，after 默认 HEAD。仓库需要已有提交。本机 work/reprolab/run.sh 入口会把报告放到独立 outputs 目录。

## 行为与边界

- 解析为完整提交编号，再读取原始文件；不执行 checkout，不切换分支，不暂存、提交或清理被测仓库。
- 未提交修改不会进入比较，也不会被覆盖。
- 不执行安装脚本或自动下载依赖，只运行可信本地 Python 代码。
- 报告保留版本名称和实际提交编号，保存的案例可用普通 verify 重放。
- 原始对象读取不受 export-ignore 影响；既有 .env、缓存等排除规则仍适用。
- 当前不支持裸仓库、子模块、符号链接或自动下载 LFS 内容。
- 报告目录必须在被测仓库之外。

## Git 和工作区

Git 仓库范围由 .git 所在的文件夹确定。一个工作区可以有多个独立项目，每个项目各有自己的 .git；总工作区、临时文件和成果目录不必建立仓库。

Git 提交是明确保存的历史检查点，不是自动记录每一次按键。Git 在本机即可使用，配置远程仓库并推送后才会把提交发送到 GitHub 等服务。
