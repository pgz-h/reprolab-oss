"""Explicit project variables: portable public values and unrecorded local inputs."""
import os
import re

from .runtime import child_environment

MAX_VARIABLES = 64
MAX_VALUE_BYTES = 8192


def validate_variables(values=None, names=None):
    values = {} if values is None else values
    names = [] if names is None else names
    if not isinstance(values, dict) or not isinstance(names, list) or len(values) + len(names) > MAX_VARIABLES:
        raise ValueError('运行变量需要普通值对象和本机名称列表，合计最多 64 项。')
    if any(not isinstance(name, str) or len(name) > 128 or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name)
           for name in [*values, *names]):
        raise ValueError('变量名须为最多 128 字符的字母、数字或下划线，不能以数字开头。')
    if len(names) != len(set(names)) or set(values) & set(names):
        raise ValueError('运行变量名称不能重复，也不能同时声明为普通值和本机值。')
    reserved = [name for name in [*values, *names] if name.upper() == 'PATH'
                or name.upper().startswith(('PYTHON', 'PYTEST', 'LD_', 'DYLD_', 'REPROLAB_'))]
    if reserved:
        raise ValueError('不能覆盖解释器、插件加载或执行控制变量：' + '、'.join(sorted(reserved)))
    for name, value in values.items():
        validate_value(name, value)
    if sum(len(value.encode('utf-8')) for value in values.values()) > 64 * 1024:
        raise ValueError('普通运行变量总大小不能超过 64 KiB。')
    return dict(sorted(values.items())), sorted(names)


def validate_value(name, value):
    if not isinstance(value, str) or '\x00' in value or len(value.encode('utf-8')) > MAX_VALUE_BYTES:
        raise ValueError('运行变量 ' + str(name) + ' 需要不含 NUL 的字符串，最多 8 KiB；未显示变量值。')


def variable_record(profile):
    profile = profile or {}
    values, names = validate_variables(profile.get('environment'), profile.get('pass_env'))
    return {'schema_version': 1, 'values': values, 'local_names': names}


def resolve_variables(profile, local_values=None):
    record = variable_record(profile)
    supplied = local_values or {}
    if not isinstance(supplied, dict):
        raise ValueError('本机运行变量需要名称和值的对象。')
    env = child_environment()
    env.update(record['values'])
    missing = []
    for name in record['local_names']:
        value = supplied.get(name, os.environ.get(name))
        if value is None:
            missing.append(name)
        else:
            validate_value(name, value)
            env[name] = value
    if missing:
        raise ValueError('缺少本机运行变量：' + '、'.join(missing) + '。请在“运行变量”中填写本次值，或由启动进程环境提供；尚未执行测试。')
    if sum(len(env[name].encode('utf-8')) for name in [*record['values'], *record['local_names']]) > 64 * 1024:
        raise ValueError('运行变量总大小不能超过 64 KiB；未显示变量值。')
    return env, record


def compare_variable_records(before, after):
    # Missing records predate explicit variables; only an empty declaration can be inferred.
    def record(report):
        expected = variable_record(report.get('pytest_profile'))
        actual = report.get('runtime_environment')
        return expected if actual == expected or actual is None and not expected['values'] and not expected['local_names'] else None
    left, right = record(before), record(after)
    if left is None or right is None:
        return None
    if left != right:
        return False
    return None if left['local_names'] else True


def describe_variables(record):
    if record is None:
        return '没有记录自定义运行变量。'
    values, names = record['values'], record['local_names']
    text = '普通变量：' + ('、'.join(values) or '无') + '；本机变量：' + ('、'.join(names) or '无') + '。'
    if names:
        text += '本机值不写入配置或运行条件记录，跨轮值是否相同未知；重放须重新提供。项目日志可能自行输出敏感值。'
    return text
