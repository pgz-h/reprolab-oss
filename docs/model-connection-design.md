# 模型接入实现与来源

本模块按 DeepSeek Harness 的服务表单、可编辑模型目录和推理映射编辑器重建为原生 Qt 窗口。参考与移植固定在提交 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`。

## 移植的规则

- [DeepSeekModelsEditor.tsx](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/client/ui-settings-models/src/client/DeepSeekModelsEditor.tsx)：K/M 容量解析、显示及逐行校验，移植到 model_definitions.py。
- [ModelReasoningEfforts.tsx](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/client/ui-settings-models/src/client/ModelReasoningEfforts.tsx)：推理档位与 API 值分离、off 空值、切换档位时保留自定义值、追加档位顺序。
- [ModelListEditor.tsx](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/client/ui-settings-models/src/client/ModelListEditor.tsx)：候选多选、保留已编辑模型、配置顺序与取消语义。
- [ModelRow.tsx](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/client/ui-settings-models/src/client/ModelRow.tsx)：卡片字段布局、展开、拖动与键盘排序。

上游版权为 Copyright (c) 2026 DeepSeek，使用 MIT 许可。许可全文随源码与应用保留在 [packaging/licenses/deepseek-harness/LICENSE](../packaging/licenses/deepseek-harness/LICENSE)。图标使用 Heroicons，许可见 [packaging/licenses/heroicons/LICENSE](../packaging/licenses/heroicons/LICENSE)。

## ReproLab 的适配

保留原有 Python 请求客户端、凭据去向确认、私有配置副本、人工审阅与实际测试判定。没有引入上游 Node/Cordis 运行时。模型目录是 `models` 数组，每项可包含 id、name、context_window、max_tokens、input、reasoning_levels 和请求兼容设置。`model_reasoning` 只保存每个模型当前选中的档位。

目录刷新读取当前表单，不先强制保存；编辑字段和模型声明不会自动联网。模型能力由用户或服务目录提供，不用官方型号白名单限制自定义声明。旧配置迁移只发生在内存，明确保存时另存新文件。

窗口使用原生控件并保持单层主滚动。提供模型 ID、名称、容量、输入类型、推理映射和顺序编辑；日常选择器只选择模型与该模型声明的档位。

图片能力和上下文容量保留为声明信息；当前检查流程只发送文本，不增加虚假的多模态入口。输出上限和推理映射接入实际请求、发送前预览与请求来源记录。服务拒绝参数时不静默降级。
