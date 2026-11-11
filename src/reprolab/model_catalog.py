"""Bounded, explicit discovery for custom OpenAI and Anthropic endpoints."""
from http.client import HTTPException
import json
import math
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .ai import _api_settings, _model_opener

MAX_MODELS = 1000
MAX_LISTING_BYTES = 4 * 1024 * 1024


def model_record(model_id, name=None, *, secret=''):
    """Keep only bounded public labels, including the endpoint's routing alias."""
    name = model_id if name is None else name
    for value in (model_id, name):
        if (not isinstance(value, str) or not value.strip() or len(value) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or (secret and secret in value)):
            return None
    return {'id': model_id.strip(), 'name': name.strip()}


def discover_models(data, *, timeout=20):
    """Read one listing page using the draft profile, before choosing a model."""
    if not math.isfinite(timeout) or not 0.1 <= timeout <= 60:
        raise ValueError('获取模型列表的超时必须在 0.1–60 秒之间。')
    settings = _api_settings(data, require_model=False)
    endpoint = settings['endpoint']
    suffix = {'openai': '/chat/completions', 'responses': '/responses', 'anthropic': '/messages'}[settings['protocol']]
    endpoint = endpoint.removesuffix(suffix) + '/models'
    headers = {'Accept': 'application/json'}
    if settings['auth_type'] == 'bearer':
        headers['Authorization'] = 'Bearer ' + settings['api_key']
    else:
        headers['x-api-key'] = settings['api_key']
    if settings['protocol'] == 'anthropic':
        endpoint += '?limit=1000'
        headers['anthropic-version'] = '2023-06-01'
    request = Request(endpoint, method='GET', headers=headers)
    try:
        with _model_opener(request).open(request, timeout=timeout) as response:
            raw = response.read(MAX_LISTING_BYTES + 1)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        hint = {401: '请检查 API Key', 403: '请检查账号权限',
                404: '该地址可能未提供模型列表，可手动添加模型',
                405: '该服务可能未提供模型列表，可手动添加模型',
                429: '请求过快或额度受限，请稍后再试'}.get(status, '请检查接口地址和服务状态')
        raise ValueError(f'获取模型列表返回 HTTP {status}，{hint}。') from None
    except (TimeoutError, socket.timeout):
        raise ValueError('获取模型列表超时，可以重试或手动添加模型。') from None
    except (URLError, OSError, HTTPException, UnicodeError, ValueError):
        raise ValueError('无法读取模型列表，请检查地址、网络和证书，或手动添加模型。') from None
    if len(raw) > MAX_LISTING_BYTES:
        raise ValueError('模型目录响应超过 4 MiB，可以手动添加需要的模型。')
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError('服务返回的模型目录不是有效 JSON，可以手动添加模型。') from None
    if not isinstance(body, dict):
        raise ValueError('模型目录需要 data 数组或 models 字典。')
    if isinstance(body.get('data'), list):
        rows = [(None, row) for row in body['data']]
    elif isinstance(body.get('models'), dict):
        rows = list(body['models'].items())
    else:
        raise ValueError('模型目录需要 data 数组或 models 字典，可以手动添加模型。')
    models = {}
    partial = body.get('has_more') is True
    for alias, row in rows:
        if not isinstance(row, dict):
            continue
        model_id = alias or row.get('id')
        name = row.get('name') or row.get('display_name') or row.get('displayName') or model_id
        model = model_record(model_id, name, secret=settings['api_key'])
        if model is None or model['id'] in models:
            continue
        if len(models) == MAX_MODELS:
            partial = True
            break
        for target, names in [('context_window', ('contextWindow','context_window','context_length','max_input_tokens')),
                              ('max_tokens', ('maxOutputTokens','max_output_tokens','maxTokens','max_tokens'))]:
            value = next((row[k] for k in names if type(row.get(k)) is int and 0 < row[k] <= 2_000_000_000), None)
            if value is not None: model[target] = value
        modalities = row.get('input') or row.get('inputModalities') or row.get('input_modalities')
        if isinstance(modalities, list) and modalities and all(isinstance(v,str) and v in ('text','image') for v in modalities):
            model['input'] = list(dict.fromkeys(modalities))
        models[model['id']] = model
    return {'models': list(models.values()), 'endpoint': endpoint, 'partial': partial}
