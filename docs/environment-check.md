# 运行前查看环境

当前 0.19.0a3 的项目环境入口支持测试／开发依赖组与 pytest 插件信息，详见[项目环境接入](project-environment.md)。下文保留 doctor 的基础检查范围。

v0.9 增加 doctor 和 --version。安装发行包后，可以先确认工具版本和本地环境：

    reprolab --version
    reprolab doctor
    reprolab doctor --json

## 检查范围

- Python 解释器路径、版本与 pytest 版本；保留所选虚拟环境解释器路径。
- Git 是否可用。缺少 Git 只影响 Git 版本相关命令，独立快照验收仍可用。
- 明确指定的发行包安装记录：`--require requests --require PyYAML`。发行名称可能与 import 名称不同，例如 PyYAML 对应 yaml。
- 可选模型配置：`--config /path/to/ai.local.json`。只检查本地格式与必要字段，不连接模型、不验证 Key 有效性、权限或额度，也不输出 Key。

诊断会启动你选择的可信 Python 解释器读取版本和安装元数据，不导入被测项目、不运行项目测试、不自动安装依赖。没有指定 --config 时，模型配置显示未检查，本地验证不依赖模型配置。

## 选择项目自己的环境

    reprolab doctor --python /path/to/project-env/bin/python --require package-name

--require 只接受发行名称，不接受路径或 `package>=version` 这种安装表达式；最多 20 个。报告会显示找到的版本，Python/pytest 的支持范围另行检查。只存在安装元数据不保证该包的代码可以正常导入。

## 状态含义

ready（退出码 0）表示本次明确检查的必要项目满足要求；attention（退出码 2）表示有项目需要处理。缺少 Git 作为可选能力提示。JSON capabilities 分别说明快照验收、Git 验收和模型配置的状态。

如果模型配置错误，doctor 会提示 attention，但仍会展示本地验收环境是否可用。`ready` 绝不等于 `verified`，也不意味着整个目标项目的所有依赖已经被识别。

doctor 不解析目标项目 requirements/pyproject；项目环境入口读取受支持的静态声明。两者均不提供完整依赖锁。运行环境变动后，重新运行 doctor 与实际验收。

当前本机入口：

    reprolab --version
    reprolab doctor
