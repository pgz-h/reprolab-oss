# 案例管理与批量运行

## 查看已经保存的案例

    python -m reprolab cases /path/to/cases
    python -m reprolab cases /path/to/cases --json

只读取这个文件夹顶层的 JSON 文件，不递归扫描子目录，不执行任何项目代码。“可读取”表示基本配置能读出来，不代表环境已准备好或验收已通过。错误配置也会列出来，方便处理。

## 一次运行多个案例

    python -m reprolab batch /path/to/cases --output /path/to/reports

每个案例顺序运行一次。一个案例的错误不会阻止后续案例。所有案例默认使用自己保存的 Python 环境，也可以用 --python 统一覆盖；--timeout 是每个案例每个版本的上限，默认 15 秒。

一次最多 50 个案例。没有案例时明确报错；符号链接案例不自动执行。报告目录必须放在被测源码之外。

## 总览报告

每次批次建立独立目录：

    reports/batch-运行编号/
      summary.html
      summary.json
      runs/各案例运行编号/...

总览显示总数、已处理数、耗时、每种结论的数量、错误原因，以及详细报告链接。实际开始后先保存总览，之后每完成一个案例就更新一次。运行中打开页面需要手动刷新。

- 全部验证通过：退出码 0。
- 有未复现或修复后仍失败的案例：退出码 1。
- 有无法验收或配置/执行错误的案例：退出码 2。
- 用户中断：退出码 130，保留已完成结果，未运行案例没有验收结论。

这些结论仍只覆盖各案例的测试，不代表整个项目没有其他问题。错误配置、环境问题和实际未通过会分别显示。

## 在当前本机工作区使用

进入 /path/to/reprolab-work 后：

    ./run.sh cases cases
    ./run.sh batch cases

启动入口会自动把报告放进独立 outputs/reprolab/reports，源码和工作文件仍分开。

## AI 草稿案例

v0.6 生成目录内的 cases/case.json 可直接用于 verify；cases 和 batch 可读取该 cases/ 子目录。独立 Git 测试使用 test 字段，与项目 test_target 二选一。草稿已生成不代表验收通过，批次结论继续来自实际测试运行。
