"""Per-model reasoning controls; omission preserves service defaults.

Documented aliases are deliberately bounded. Unknown gateways require an explicit
wire-format declaration; a model listing or a successful HTTP response is not a
capability guarantee. See docs/model-connections.md for sources and scope.
"""
from urllib.parse import urlsplit

EFFORT_LABELS = {'default': '服务默认', 'off': '关闭思考 (off)', 'enabled': '开启思考', 'none': '不启用推理 (none)', 'minimal': '最少 (minimal)',
                 'low': '低 (low)', 'medium': '中 (medium)', 'high': '高 (high)',
                 'xhigh': '很高 (xhigh)', 'max': '最高 (max)'}
ADAPTER_LABELS = {'catalog': '模型档位映射', 'auto': '自动识别官方模型', 'openai': 'OpenAI 兼容参数',
                  'deepseek': 'DeepSeek 思考参数', 'anthropic': 'Anthropic effort 参数', 'glm': 'GLM 思考参数',
                  'thinking': '仅思考开关（thinking.type）'}
ADAPTER_LEVELS = {'openai': ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'),
                  'deepseek': ('none', 'low', 'high', 'max'),
                  'anthropic': ('low', 'medium', 'high', 'xhigh', 'max'),
                  'glm': ('low', 'high', 'max'), 'thinking': ('none', 'enabled')}
_OPENAI_MODELS = {
    'gpt-6-astra': ('low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-6.1-sol': ('low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-6-sol': ('none', 'low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-5.6-luna': ('none', 'low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-5.4': ('none', 'low', 'medium', 'high', 'xhigh'),
    'gpt-5.4-2026-03-05': ('none', 'low', 'medium', 'high', 'xhigh'),
    'gpt-6-luna': ('none', 'low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-5.2': ('none', 'low', 'medium', 'high', 'xhigh'),
    'gpt-5.2-2025-12-11': ('none', 'low', 'medium', 'high', 'xhigh'),
}
_CLAUDE_FIVE_LEVELS = {
    'claude-opus-4-7', 'claude-opus-4-8', 'claude-opus-5', 'claude-opus-5-5',
    'claude-sonnet-5', 'claude-sonnet-5-5', 'claude-haiku-5-5',
    'claude-fable-5', 'claude-fable-5-1', 'claude-mythos-5', 'claude-mythos-5-1',
}


def validate_reasoning_map(value):
    if not isinstance(value, dict) or len(value) > 1000:
        raise ValueError('model_reasoning 必须是最多 1000 个模型的设置字典。')
    for model, option in value.items():
        if (not isinstance(model, str) or not model.strip() or len(model) > 200
                or any(ord(c) < 32 or ord(c) == 127 for c in model)
                or not isinstance(option, dict) or set(option) != {'adapter', 'effort'}
                or not isinstance(option['adapter'], str) or option['adapter'] not in ADAPTER_LABELS
                or not isinstance(option['effort'], str) or option['effort'] not in EFFORT_LABELS):
            raise ValueError('模型思考配置无效，需要明确的 adapter 和 effort。')


def allowed_adapters(protocol):
    if protocol == 'anthropic':
        return ('auto', 'deepseek', 'anthropic')
    if protocol == 'responses':
        return ('auto', 'openai', 'deepseek')
    return ('auto', 'openai', 'deepseek', 'glm', 'thinking')


def documented_capability(url, protocol, model):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname if parsed.scheme == 'https' and parsed.port in (None, 443) else None
    except ValueError:
        return None
    if host == 'api.deepseek.com' and model in {'deepseek-flash', 'deepseek-v4-pro'}:
        return 'deepseek', ADAPTER_LEVELS['deepseek']
    if host == 'api.openai.com' and protocol in ('openai', 'responses') and model in _OPENAI_MODELS:
        return 'openai', _OPENAI_MODELS[model]
    if host in {'api.z.ai', 'open.bigmodel.cn'} and protocol == 'openai':
        if model in {'glm-5.3', 'glm-5.3-flash', 'glm-5.3-flashx'}:
            return 'glm', ('low', 'high', 'max')
        if model == 'glm-5.2':
            return 'glm', ('none', 'high', 'max')
        if model in {'glm-5', 'glm-5.1', 'glm-5-turbo', 'glm-5v-turbo', 'glm-4.5', 'glm-4.6', 'glm-4.7'}:
            return 'thinking', ADAPTER_LEVELS['thinking']
    if host == 'api.anthropic.com' and protocol == 'anthropic':
        if model in _CLAUDE_FIVE_LEVELS:
            return 'anthropic', ADAPTER_LEVELS['anthropic']
        if model in {'claude-opus-4-6', 'claude-sonnet-4-6'}:
            return 'anthropic', ('low', 'medium', 'high', 'max')
        if model == 'claude-opus-4-5':
            return 'anthropic', ('low', 'medium', 'high')
    return None


def reasoning_choices(url, protocol, model, adapter='auto'):
    known = documented_capability(url, protocol, model)
    if adapter not in allowed_adapters(protocol):
        return (), '所选参数方式与当前 API 协议不匹配。'
    if known and adapter in ('auto', known[0]):
        return known[1], '按已核对的官方模型文档显示档位；实际可用性以服务为准。'
    if adapter == 'auto':
        return (), '未识别该模型的思考能力；可按服务文档选择兼容设置。'
    if known:
        return (), '当前官方模型应使用自动识别或对应的参数方式。'
    levels = ('none', 'high', 'max') if adapter == 'glm' and model == 'glm-5.2' else ADAPTER_LEVELS[adapter]
    return levels, '自定义兼容设置：请按该模型或网关文档选择，目录返回不代表支持这些档位。'


def reasoning_settings(url, protocol, model, options, definition=None):
    validate_reasoning_map(options)
    option = options.get(model, {'adapter': 'auto', 'effort': 'default'})
    adapter, effort = option['adapter'], option['effort']
    if effort == 'default':
        return {'effort': 'default', 'adapter': 'default', 'source': 'service_default', 'parameters': {}}
    if definition is not None and 'reasoning_levels' in definition:
        mapping = definition['reasoning_levels']
        level = 'off' if effort == 'none' and 'off' in mapping else effort
        if level not in mapping:
            raise ValueError('当前推理档位已从模型卡片移除，请重新选择。')
        wire = mapping[level]
        style = definition.get('reasoning_adapter', 'protocol')
        if style == 'protocol':
            style = 'anthropic' if protocol == 'anthropic' else 'openai'
        if style in ('deepseek', 'glm', 'thinking'):
            parameters = {'thinking': {'type': 'disabled' if level == 'off' else 'enabled'}}
            if level != 'off' and style != 'thinking':
                parameters.update({'output_config': {'effort': wire}} if protocol == 'anthropic'
                                  else {'reasoning': {'effort': wire}} if protocol == 'responses'
                                  else {'reasoning_effort': wire})
            if protocol == 'responses':
                parameters = {'reasoning': {'effort': 'none' if level == 'off' else wire}}
        elif protocol == 'anthropic':
            parameters = {'thinking': {'type': 'disabled'}} if level == 'off' else {'output_config': {'effort': wire}}
        elif protocol == 'responses':
            parameters = {'reasoning': {'effort': 'none' if level == 'off' else wire}}
        else:
            parameters = {'reasoning_effort': 'none' if level == 'off' else wire}
        return {'effort': level, 'adapter': 'catalog', 'source': 'model_declaration',
                'wire_value': wire, 'parameters': parameters}
    levels, hint = reasoning_choices(url, protocol, model, adapter)
    if effort not in levels:
        raise ValueError('所选思考强度不适用于当前模型和协议。' + hint)
    known = documented_capability(url, protocol, model)
    resolved = known[0] if adapter == 'auto' else adapter
    if resolved == 'thinking':
        parameters = {'thinking': {'type': 'disabled' if effort == 'none' else 'enabled'}}
    elif resolved == 'glm':
        parameters = {'thinking': {'type': 'disabled' if effort == 'none' else 'enabled'}}
        if effort != 'none':
            parameters['reasoning_effort'] = effort
    elif resolved == 'deepseek':
        if protocol == 'responses':
            parameters = {'reasoning': {'effort': effort}}
        elif effort == 'none':
            parameters = {'thinking': {'type': 'disabled'}}
        else:
            parameters = {'thinking': {'type': 'enabled'}}
            parameters.update({'output_config': {'effort': effort}} if protocol == 'anthropic'
                              else {'reasoning_effort': effort})
    elif protocol == 'responses':
        parameters = {'reasoning': {'effort': effort}}
    elif resolved == 'anthropic':
        parameters = {'output_config': {'effort': effort}}
    else:
        parameters = {'reasoning_effort': effort}
    return {'effort': effort, 'adapter': resolved,
            'source': 'documented_model' if known else 'user_declared', 'parameters': parameters}


def reasoning_description(settings):
    if settings['effort'] == 'default':
        return '服务默认（不附加思考参数）'
    if settings['adapter'] == 'catalog':
        return EFFORT_LABELS[settings['effort']] + (' → 关闭思考' if settings['effort']=='off' else ' → API '+settings['wire_value'])
    return EFFORT_LABELS[settings['effort']] + ' · ' + ADAPTER_LABELS[settings['adapter']]


def upgrade_profile_models(data):
    """Migrate existing per-model choices in memory; never rewrite old files."""
    from copy import deepcopy
    result = deepcopy(data)
    for model in result.get('models', []):
        model.setdefault('name', model.get('id',''))
        option = result.get('model_reasoning', {}).get(model.get('id'))
        if not option or option.get('effort') == 'default' or 'reasoning_levels' in model:
            continue
        adapter = option.get('adapter', 'auto')
        if adapter == 'auto':
            known = documented_capability(result.get('base_url',''), result.get('protocol','openai'), model['id'])
            if not known:
                continue
            adapter, levels = known
        else:
            levels = ADAPTER_LEVELS.get(adapter, ())
        if not levels:
            continue
        model['reasoning_levels'] = {('off' if level == 'none' else 'high' if level == 'enabled' else level):
                                     (None if level == 'none' else level) for level in levels}
        model['reasoning_adapter'] = adapter
        option['adapter'] = 'catalog'
        if option['effort'] == 'none': option['effort'] = 'off'
        elif option['effort'] == 'enabled': option['effort'] = 'high'
    return result
