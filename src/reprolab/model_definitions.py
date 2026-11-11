"""Editable provider model catalog.

Capacity parsing, level value transitions and catalog adoption are adapted from
DeepSeek Harness (MIT), copyright (c) 2026 DeepSeek. See
packaging/licenses/deepseek-harness/LICENSE and docs/model-connection-design.md.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

LEVELS = ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
MAX_CAPACITY = 2_000_000_000
MODEL_FIELDS = {'id', 'name', 'context_window', 'max_tokens', 'input', 'reasoning_levels', 'reasoning_adapter', 'output_parameter'}


def parse_capacity(text):
    value = text.strip()
    if not value:
        return None
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([kKmM])?', value)
    if not match:
        raise ValueError('请输入正整数或 K/M 容量，例如 128K、1M。')
    number = Decimal(match[1]) * {'k': 1000, 'm': 1_000_000, '': 1}[(match[2] or '').lower()]
    if number != number.to_integral_value() or not 1 <= number <= MAX_CAPACITY:
        raise ValueError('容量必须是有效范围内的正整数。')
    return int(number)


def format_capacity(value):
    if value is None:
        return ''
    if type(value) is not int:
        return str(value)
    if value > 0 and value % 1_000_000 == 0:
        return f'{value // 1_000_000}M'
    if value > 0 and value % 1000 == 0:
        return f'{value // 1000}K'
    return str(value)


def default_level_value(level):
    return None if level == 'off' else level


def changed_level_value(previous, value, new):
    custom = isinstance(value, str) and value and value != previous and previous != 'off' and new != 'off'
    return value if custom else default_level_value(new)


def next_level(used):
    highest = max((LEVELS.index(level) for level in used if level in LEVELS), default=-1)
    return next((level for i, level in enumerate(LEVELS) if i > highest and level not in used),
                next((level for level in LEVELS if level not in used), None))


def validate_model(model, *, secret=''):
    if not isinstance(model, dict) or set(model) - MODEL_FIELDS:
        raise ValueError('模型字段无效。')
    for field in ('id', 'name'):
        value = model.get(field, model.get('id') if field == 'name' else None)
        if (not isinstance(value, str) or not value.strip() or len(value) > 200
                or any(ord(c) < 32 or ord(c) == 127 for c in value) or (secret and secret in value)):
            raise ValueError('模型 ID 和显示名称需要有效文本。')
    for field in ('context_window', 'max_tokens'):
        if field in model and (type(model[field]) is not int or not 1 <= model[field] <= MAX_CAPACITY):
            raise ValueError(('上下文窗口' if field == 'context_window' else '最大输出 token 数') + '需要有效正整数。')
    inputs = model.get('input', ['text'])
    if not isinstance(inputs, list) or not inputs or any(i not in ('text', 'image') for i in inputs) or len(set(inputs)) != len(inputs):
        raise ValueError('至少选择一种输入类型。')
    mapping = model.get('reasoning_levels')
    if mapping is not None:
        if not isinstance(mapping, dict) or not mapping or len(mapping) > len(LEVELS):
            raise ValueError('推理等级需要至少一个有效档位。')
        for level, value in mapping.items():
            if level not in LEVELS:
                raise ValueError('推理档位无效。')
            if level == 'off':
                if value is not None:
                    raise ValueError('off 档位不能填写 API 参数值。')
            elif (not isinstance(value, str) or not value.strip() or len(value) > 100
                  or any(ord(c) < 33 or ord(c) > 126 for c in value) or (secret and secret in value)):
                raise ValueError('每个推理档位都需要非空 API 参数值。')
        if not any(level != 'off' for level in mapping):
            raise ValueError('启用推理等级后，至少保留一个非 off 档位。')
    if 'reasoning_levels' in model and mapping is None:
        raise ValueError('关闭推理等级时应移除档位声明。')
    if model.get('output_parameter', 'auto') not in ('auto', 'max_tokens', 'max_completion_tokens'):
        raise ValueError('输出上限参数无效。')
    if model.get('reasoning_adapter', 'protocol') not in ('protocol', 'openai', 'deepseek', 'anthropic', 'glm', 'thinking'):
        raise ValueError('推理参数格式无效。')


def validate_catalog(models, *, secret=''):
    if not isinstance(models, list) or len(models) > 1000:
        raise ValueError('模型目录最多保存 1000 项。')
    seen = set()
    for i, model in enumerate(models, 1):
        try:
            validate_model(model, secret=secret)
            identity = model['id'].strip()
            if identity in seen:
                raise ValueError('模型 ID 重复。')
            seen.add(identity)
        except ValueError as exc:
            raise ValueError(f'模型 {i}：{exc}') from None


def adopt_models(existing, available, selected):
    """Keep tuned rows and their order; append only newly selected candidates."""
    offered = {model['id'] for model in available}
    kept = [deepcopy(model) for model in existing if model['id'] not in offered or model['id'] in selected]
    kept_ids = {model['id'] for model in kept}
    return kept + [deepcopy(model) for model in available if model['id'] in selected and model['id'] not in kept_ids]


def selected_definition(models, identity):
    return next((model for model in models if model['id'] == identity), None)
