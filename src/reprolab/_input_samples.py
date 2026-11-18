"""Bounded JSON values explicitly captured by the pytest worker."""
import hashlib
import json
import math
import re


_VALUE_LIMIT = 16 * 1024
_TOTAL_LIMIT = 1024 * 1024
_MAX_KEYS = 32
_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
ERRORS = {
    'invalid_request': '输入名称或取值函数无效。',
    'duplicate_key': '本次测试重复请求同名输入。',
    'provider_error': '业务输入生成失败。',
    'invalid_value': '业务输入须为有限大小的 JSON 值。',
    'sample_limit': '业务输入记录超过容量限制。',
    'unrecorded_key': '本次请求的输入名称未在基线证据中保存。',
    'replay_mismatch': '本次未完整使用基线中保存的输入。',
}


def _encode(value):
    remaining = 4096
    remaining_text = _VALUE_LIMIT

    def validate(item, depth):
        nonlocal remaining, remaining_text
        remaining -= 1
        if remaining < 0 or depth > 20:
            raise ValueError('输入结构超过复杂度限制。')
        kind = type(item)
        if item is None or kind is bool:
            return
        if kind is str:
            remaining_text -= len(item)
            if remaining_text < 0:
                raise ValueError('输入字符串总长超过 16 KiB。')
            return
        if kind is int and -(2**63) <= item < 2**63:
            return
        if kind is float and math.isfinite(item):
            return
        if kind is list and len(item) <= remaining:
            for elem in item:
                validate(elem, depth + 1)
            return
        if kind is dict and len(item) * 2 <= remaining:
            for key, elem in item.items():
                if type(key) is not str:
                    raise ValueError('输入对象键须为字符串。')
                validate(key, depth + 1)
                validate(elem, depth + 1)
            return
        raise ValueError('输入包含不支持的 JSON 类型或超限值。')

    validate(value, 0)
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(data) > _VALUE_LIMIT:
        raise ValueError('单个输入超过 16 KiB。')
    return data


def sample_digest(value):
    return hashlib.sha256(_encode(value)).hexdigest()


def _cost(identity, key, data, first):
    return len(data) + len(key) + 8 + (len(identity.encode('utf-8')) + 8 if first else 0)


def sample_digests(values, selected):
    if not isinstance(values, dict) or not values.keys() <= set(selected):
        raise ValueError('业务输入样本与所选测试不一致。')
    digests = {}
    size = 0
    for identity, samples in values.items():
        if not isinstance(samples, dict) or not 1 <= len(samples) <= _MAX_KEYS:
            raise ValueError('单个测试须包含 1–32 个业务输入。')
        hashes = {}
        for name, value in samples.items():
            if not isinstance(name, str) or not _KEY.fullmatch(name):
                raise ValueError('业务输入名称无效。')
            data = _encode(value)
            size += _cost(identity, name, data, not hashes)
            if size > _TOTAL_LIMIT:
                raise ValueError('一版业务输入超过 1 MiB。')
            hashes[name] = hashlib.sha256(data).hexdigest()
        digests[identity] = hashes
    return digests


def read_sample_observations(raw, selected):
    record = raw.get('input_samples')
    if record is None:
        return {}, {}
    if not isinstance(record, dict) or record.keys() != {'values', 'errors'}:
        raise ValueError('业务输入观察记录格式无效。')
    errors = record['errors']
    if (not isinstance(errors, dict) or not errors.keys() <= set(selected)
            or any(not isinstance(error, str) or error not in ERRORS for error in errors.values())):
        raise ValueError('业务输入错误记录无效。')
    return sample_digests(record['values'], selected), errors


class InputCapture:
    def __init__(self, replay_values):
        self.replay_values = replay_values
        self.values = {}
        self.errors = {}
        self.size = 0

    def _reject(self, identity, code):
        self.errors.setdefault(identity, code)
        raise ValueError(ERRORS[code])

    def read(self, identity, name, factory):
        if not isinstance(name, str) or not _KEY.fullmatch(name) or not callable(factory):
            self._reject(identity, 'invalid_request')
        recorded = self.values.get(identity, {})
        if name in recorded:
            self._reject(identity, 'duplicate_key')
        if len(recorded) >= _MAX_KEYS:
            self._reject(identity, 'sample_limit')
        if identity in self.replay_values:
            if name not in self.replay_values[identity]:
                self._reject(identity, 'unrecorded_key')
            value = self.replay_values[identity][name]
        else:
            try:
                value = factory()
            except Exception:
                self.errors.setdefault(identity, 'provider_error')
                raise
        try:
            data = _encode(value)
        except (ValueError, TypeError):
            self._reject(identity, 'invalid_value')
        size = self.size + _cost(identity, name, data, not recorded)
        if size > _TOTAL_LIMIT:
            self._reject(identity, 'sample_limit')
        self.size = size
        self.values.setdefault(identity, {})[name] = json.loads(data)
        return json.loads(data)
