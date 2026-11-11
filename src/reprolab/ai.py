"""Bounded Chat Completions, Responses and Anthropic Messages clients."""
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .model_reasoning import reasoning_settings
from .model_definitions import validate_catalog, selected_definition, MAX_CAPACITY

API_CONFIG_FIELDS = {'base_url', 'model', 'api_key', 'api_key_env', 'json_mode',
                     'protocol', 'auth_type', 'max_tokens', 'provider_id',
                     'display_name', 'models', 'api_key_source', 'model_reasoning'}

RESPONSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        **{key: {"type": "string"} for key in
           ("title", "test_code", "checks", "expected_behavior")},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "expected_failure": {"type": "object", "additionalProperties": False,
                             "properties": {"type": {"type": "string"}, "message_contains": {"type": "string"}},
                             "required": ["type", "message_contains"]},
    },
    "required": ["title", "test_code", "checks", "expected_behavior", "assumptions", "expected_failure"],
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_api_config(config_file=None, *, base_url=None, model=None, api_key_env=None, protocol=None):
    data = {}
    if config_file is not None:
        try:
            data = json.loads(Path(config_file).read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            raise ValueError("无法读取模型配置文件，请提供有效的本机 JSON 配置。") from None
        if not isinstance(data, dict) or set(data) - API_CONFIG_FIELDS:
            raise ValueError("模型配置包含不支持的字段，请检查接口与模型目录配置。")
    return _api_settings(data, base_url=base_url, model=model, api_key_env=api_key_env, protocol=protocol)


def _api_settings(data, *, base_url=None, model=None, api_key_env=None, protocol=None, use_environment=True, require_model=True):
    """Validate a profile without logging its contents; desktop saves can skip environment access."""
    for field in ("base_url", "model", "api_key", "api_key_env"):
        if field in data and data[field] is not None and not isinstance(data[field], str):
            raise ValueError("模型配置中的地址、模型和凭据字段必须是文本。")
    environment = os.environ if use_environment else {}
    url = base_url or data.get("base_url") or environment.get("REPROLAB_API_BASE_URL")
    selected_model = model or data.get("model") or environment.get("REPROLAB_AI_MODEL") or ""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("缺少模型 URL：设置 base_url 或 REPROLAB_API_BASE_URL。")
    try:
        parts = urlsplit(url)
        valid = (bool(parts.hostname) and not parts.username and not parts.password
                 and not parts.query and not parts.fragment
                 and (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"})))
        parts.port
    except ValueError:
        valid = False
    if not valid or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("模型 URL 需要不含凭据/查询参数的 HTTPS 地址；本机服务可用 HTTP。")
    if (not isinstance(selected_model, str) or (require_model and not selected_model.strip())
            or len(selected_model) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in selected_model)):
        raise ValueError("缺少或无效模型名：设置 model 或 REPROLAB_AI_MODEL。")
    env_name = api_key_env if api_key_env is not None else data.get("api_key_env") or "REPROLAB_API_KEY"
    if not isinstance(env_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", env_name):
        raise ValueError("api_key_env 必须是有效环境变量名称。")
    key_source = data.get("api_key_source", "legacy")
    if key_source not in ("legacy", "profile", "environment"):
        raise ValueError("api_key_source 必须为 profile、environment 或 legacy。")
    if api_key_env is not None:
        key = environment.get(env_name)
    elif key_source == "profile" or not use_environment:
        key = data.get("api_key")
    elif key_source == "environment":
        key = environment.get(env_name)
    else:
        key = environment.get(env_name) or data.get("api_key")
    if not isinstance(key, str) or not key.strip() or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError(f"缺少或无效 API Key：请在本机配置 api_key 或环境变量 {env_name}；不要把密钥发到聊天中。")
    if key in url or key in selected_model:
        raise ValueError("模型 URL 或模型名不能包含 API Key。")
    json_mode = data.get("json_mode", True)
    if not isinstance(json_mode, bool):
        raise ValueError("json_mode 必须为 true 或 false。")
    protocol = protocol or data.get("protocol", "openai")
    if protocol not in ("openai", "responses", "anthropic"):
        raise ValueError("protocol 必须为 openai、responses 或 anthropic。")
    auth_type = data.get("auth_type", "api-key" if protocol == "anthropic" else "bearer")
    if auth_type not in ("api-key", "bearer"):
        raise ValueError("auth_type 必须为 api-key 或 bearer。")
    models = data.get('models', [])
    validate_catalog(models, secret=key)
    definition = selected_definition(models, selected_model)
    max_tokens = (definition or {}).get('max_tokens', data.get("max_tokens", 8192))
    if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_CAPACITY:
        raise ValueError("max_tokens 必须为有效范围内的正整数。")
    endpoint = url.rstrip("/")
    if protocol == "anthropic":
        if not endpoint.endswith("/v1/messages"):
            endpoint += "/messages" if endpoint.endswith("/v1") else "/v1/messages"
    elif protocol == "responses":
        if not endpoint.endswith("/responses"):
            endpoint += "/responses" if parts.path.rstrip("/") else "/v1/responses"
    elif not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    return {"endpoint": endpoint, "model": selected_model, "api_key": key, "json_mode": json_mode,
            "protocol": protocol, "auth_type": auth_type, "max_tokens": max_tokens,
            "reasoning": reasoning_settings(url, protocol, selected_model, data.get("model_reasoning", {}), definition),
            "model_definition": definition, "explicit_model_output_limit": bool(definition and "max_tokens" in definition)}


def generate_api(prompt, destination, *, config_file=None, base_url=None, model=None,
                 api_key_env=None, timeout=180, protocol=None, system_prompt=None):
    if not math.isfinite(timeout) or not 0.1 <= timeout <= 600:
        raise ValueError("AI 超时必须是 0.1–600 秒之间的有限数字。")
    settings = load_api_config(config_file, base_url=base_url, model=model, api_key_env=api_key_env, protocol=protocol)
    system = "Return only the requested JSON regression-test draft. Do not claim execution or verification." if system_prompt is None else system_prompt
    if not isinstance(system, str) or not system.strip():
        raise ValueError("模型系统提示必须是非空文本。")
    headers = {"Content-Type": "application/json"}
    if settings["auth_type"] == "bearer":
        headers["Authorization"] = "Bearer " + settings["api_key"]
    else:
        headers["x-api-key"] = settings["api_key"]
    if settings["protocol"] == "anthropic":
        payload = {"model": settings["model"], "stream": False, "max_tokens": settings["max_tokens"],
                   "system": system, "messages": [{"role": "user", "content": prompt}]}
        headers["anthropic-version"] = "2023-06-01"
    elif settings["protocol"] == "responses":
        payload = {"model": settings["model"], "stream": False, "max_output_tokens": settings["max_tokens"],
                   "instructions": system, "input": prompt}
        if settings["json_mode"]:
            payload["text"] = {"format": {"type": "json_object"}}
    else:
        payload = {"model": settings["model"], "stream": False,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if settings["json_mode"]:
            payload["response_format"] = {"type": "json_object"}
    definition = settings['model_definition']
    if definition and 'text' not in definition.get('input', ['text']):
        raise ValueError('当前模型未声明文本输入；本工作流需要支持文本的模型。')
    if settings['protocol'] == 'openai' and settings['explicit_model_output_limit']:
        field = definition.get('output_parameter', 'auto')
        if field == 'auto':
            vendor = urlsplit(settings['endpoint']).hostname
            field = 'max_tokens' if vendor in ('api.deepseek.com','open.bigmodel.cn','api.z.ai') or definition.get('reasoning_adapter') in ('deepseek','glm','thinking') else 'max_completion_tokens'
        payload[field] = settings['max_tokens']
    payload.update(settings["reasoning"]["parameters"])
    request = Request(settings["endpoint"], data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers)
    # Never forward the key to a redirect destination. Do not log error bodies,
    # request headers or arbitrary provider fields that could contain credentials.
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(256 * 1024 + 1)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        hint = {401: "请检查 Key", 403: "请检查账号或模型权限", 429: "额度不足或请求过快", 400: "请检查模型、协议和思考参数是否受支持", 422: "请检查请求参数及思考档位是否受支持"}.get(status, "请检查接口兼容性或服务状态")
        raise ValueError(f"模型服务返回 HTTP {status}，{hint}。未自动重试或生成模板。") from None
    except (TimeoutError, socket.timeout):
        raise ValueError("模型请求超时；上下文已保留，没有验收结论。") from None
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise ValueError("模型请求超时；上下文已保留，没有验收结论。") from None
        raise ValueError("无法连接模型服务；请检查 URL、网络及证书。") from None
    except (OSError, HTTPException, UnicodeError, ValueError):
        raise ValueError("模型连接中断或响应传输无效；请检查 URL、网络及服务状态。") from None
    if len(raw) > 256 * 1024:
        raise ValueError("模型服务响应超过 256 KiB，未生成案例。")
    body = None
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
        if settings["protocol"] == "anthropic":
            if body.get("type") != "message" or body.get("role") != "assistant" or body.get("stop_reason") != "end_turn":
                raise ValueError()
            blocks = body["content"]
            if not isinstance(blocks, list) or any(b.get("type") not in {"text", "thinking", "redacted_thinking"} for b in blocks):
                raise ValueError()
            content = "".join(b["text"] for b in blocks if b.get("type") == "text")
        elif settings["protocol"] == "responses":
            if body.get("status") != "completed" or body.get("error") or body.get("incomplete_details"):
                raise ValueError()
            output = body["output"]
            if not isinstance(output, list):
                raise ValueError()
            texts = []
            for item in output:
                if item.get("type") == "reasoning":
                    continue
                if (item.get("type") != "message" or item.get("role") != "assistant"
                        or item.get("status") != "completed"):
                    raise ValueError()
                blocks = item["content"]
                if not isinstance(blocks, list):
                    raise ValueError()
                for block in blocks:
                    # Refusal and tool output are never accepted as usable text.
                    if block.get("type") != "output_text" or not isinstance(block.get("text"), str):
                        raise ValueError()
                    texts.append(block["text"])
            content = "".join(texts)
        else:
            choices = body["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError()
            choice = choices[0]
            message = choice["message"]
            if choice.get("finish_reason") != "stop" or message.get("refusal") or message.get("tool_calls"):
                raise ValueError()
            content = message["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        if settings["protocol"] == "responses" and isinstance(body, dict):
            details = body.get("incomplete_details")
            if body.get("status") == "incomplete" and isinstance(details, dict) and details.get("reason") == "max_output_tokens":
                raise ValueError(f"模型达到 max_output_tokens={settings['max_tokens']} 上限，响应被截断；请调高输出上限。没有生成可运行案例。") from None
        reason = body.get("stop_reason") if isinstance(body, dict) and settings["protocol"] == "anthropic" else None
        if reason == "max_tokens":
            raise ValueError(f"模型达到 max_tokens={settings['max_tokens']} 上限，响应被截断；可调高输出上限并给生成留出更多时间。没有生成可运行案例。") from None
        if reason in ("tool_use", "pause_turn", "refusal"):
            raise ValueError(f"模型未完成文本草稿（stop_reason={reason}），没有生成可运行案例。") from None
        raise ValueError("模型响应无效、被截断或被拒绝；需要与所选协议一致的完整文本响应。") from None
    # Also refuse an unexpected provider echo of the secret before writing files.
    if settings["api_key"] in content:
        raise ValueError("模型响应异常包含认证信息，未保存响应或草稿。")
    reported = body.get("model")
    reported = reported if isinstance(reported, str) and 0 < len(reported) <= 200 and settings["api_key"] not in reported else settings["model"]
    usage = body.get("usage")
    usage = {k: v for k, v in usage.items() if k in {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"}
             and isinstance(v, int) and not isinstance(v, bool) and v >= 0} if isinstance(usage, dict) else {}
    origin = {"provider": "anthropic-compatible" if settings["protocol"] == "anthropic" else "openai-compatible", "endpoint": settings["endpoint"],
              "requested_model": settings["model"], "model": reported, "usage": usage,
              "reasoning_request": settings["reasoning"], "model_definition": settings["model_definition"],
              "requested_output_limit": settings["max_tokens"] if settings["protocol"] != "openai" or settings["explicit_model_output_limit"] else None}
    (destination / "provider.json").write_text(json.dumps(origin, ensure_ascii=False, indent=2), encoding="utf-8")
    return content, origin
