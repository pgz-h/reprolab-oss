# 第一个真实历史案例：humanize 的 1 字节显示问题

运行：

```bash
.venv/bin/python -m reprolab history
```

输出仍然是离线中文 HTML 报告。现在可以直接看到：

| 内容 | 修复前 | 修复后 |
|---|---|---|
| 输入 | 字符串 `"1"` | 字符串 `"1"` |
| 实际输出 | `1.0 Byte` | `1 Byte` |
| 预期输出 | `1 Byte` | `1 Byte` |

这是 humanize 的已知历史回归，官方在 [PR #250](https://github.com/python-humanize/humanize/pull/250) 修复，并在 [4.12.3](https://github.com/python-humanize/humanize/releases/tag/4.12.3) 发布。ReproLab 分别运行 4.12.2 与 4.12.3 的原始 `filesize.py`，展示同一份检查在两边的结果。

报告还会展示原始修复差异：输出单数字节时，把 `bytes_` 转为整数后再格式化，因此不会多出 `.0`。

## 这个案例说明什么

它说明本工具已经能对一个真实开源模块的历史修复运行验收，并保留可核对来源。它不意味着已支持 humanize 的完整测试环境，不是新发现的 Bug，也不意味着 humanize 已采用本工具。

## 在自己的测试中记录可读结果

使用 pytest 自带的 `record_property`：

```python
def test_example(record_property):
    actual = some_function("input")
    record_property("输入", "input")
    record_property("实际输出", actual)
    record_property("预期输出", "expected")
    assert actual == "expected", "the expected regression condition"
```

这些记录只帮助人理解报告，不决定验收状态。工具仍检查旧版的声明故障以及新版测试是否通过。每次执行最多展示 30 条记录，名称最多 200 字符、值最多 4000 字符。

来源信息使用案例的 `provenance` 字段，参考内置 `history_cases/humanize250/case.json`。它会保存在证据中并进入报告；运行时不会据此访问网络。
