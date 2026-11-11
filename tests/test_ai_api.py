"""Local HTTP fixtures exercise the API protocol, not real model generation."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest

from test_cli import cli
from test_drafts import prepare, response


@contextmanager
def api_server(body, status=200, delay=0, headers=None):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                             "api_key": self.headers.get("x-api-key"), "anthropic_version": self.headers.get("anthropic-version"),
                             "data": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            time.sleep(delay)
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def api_answer(**changes):
    data = {"model": "fixture-model", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(response())}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
    data.update(changes)
    return data


def config(tmp_path, base_url):
    file = tmp_path / "ai.local.json"
    file.write_text(json.dumps({"base_url": base_url, "model": "requested-model", "api_key": "test-secret-do-not-save"}))
    return file


def test_url_key_model_reach_api_and_generated_case_runs_without_saving_key(tmp_path):
    _, _, _, prepared = prepare(tmp_path)
    with api_server(api_answer()) as (url, requests):
        cfg = config(tmp_path, url)
        result = cli("draft", "generate", prepared["draft_dir"], "--config", cfg, "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert requests[0]["path"] == "/v1/chat/completions"
    assert requests[0]["authorization"] == "Bearer test-secret-do-not-save"
    assert requests[0]["data"]["model"] == "requested-model"
    assert "compute() should return 42" in requests[0]["data"]["messages"][-1]["content"]
    generated = json.loads(result.stdout)
    assert generated["status"] == "draft_generated"
    assert generated["origin"]["model"] == "fixture-model"
    assert generated["origin"]["usage"]["total_tokens"] == 30
    from pathlib import Path
    for path in Path(prepared["draft_dir"]).rglob("*"):
        if path.is_file():
            assert "test-secret-do-not-save" not in path.read_text()
    verified = cli("verify", generated["case"], "--output", tmp_path / "reports", "--json")
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert json.loads(verified.stdout)["status"] == "verified"


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_failure_has_readable_error_and_does_not_echo_service_secrets(tmp_path, status):
    _, _, _, prepared = prepare(tmp_path)
    with api_server({"error": "test-secret-do-not-save"}, status) as (url, requests):
        result = cli("draft", "generate", prepared["draft_dir"], "--config", config(tmp_path, url), "--json")
    assert result.returncode == 2
    assert str(status) in json.loads(result.stdout)["error"]
    assert "test-secret-do-not-save" not in result.stdout + result.stderr
    assert len(requests) == 1


@pytest.mark.parametrize("body", [api_answer(choices=[]), api_answer(choices=[{"finish_reason": "length", "message": {"content": "{}"}}]), api_answer(choices=[{"finish_reason": "stop", "message": {"refusal": "No", "content": None}}])])
def test_invalid_truncated_or_refused_response_cannot_create_case(tmp_path, body):
    _, _, _, prepared = prepare(tmp_path)
    with api_server(body) as (url, _):
        result = cli("draft", "generate", prepared["draft_dir"], "--config", config(tmp_path, url), "--json")
    assert result.returncode == 2
    from pathlib import Path
    assert not (Path(prepared["draft_dir"]) / "cases/case.json").exists()


def test_redirect_is_not_followed_and_timeout_is_reported(tmp_path):
    _, _, _, prepared = prepare(tmp_path)
    with api_server({}, 302, headers={"Location": "https://unrequested.example/v1/chat/completions"}) as (url, requests):
        result = cli("draft", "generate", prepared["draft_dir"], "--config", config(tmp_path, url), "--json")
    assert result.returncode == 2
    assert "302" in result.stdout
    assert len(requests) == 1
    with api_server(api_answer(), delay=0.5) as (url, _):
        result = cli("draft", "generate", prepared["draft_dir"], "--config", config(tmp_path, url), "--ai-timeout", "0.1", "--json")
    assert result.returncode == 2
    assert "超时" in result.stdout


def test_env_configuration_and_missing_settings_are_explicit(tmp_path, monkeypatch):
    from reprolab.ai import load_api_config
    monkeypatch.setenv("REPROLAB_API_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("REPROLAB_AI_MODEL", "my-model")
    monkeypatch.setenv("REPROLAB_API_KEY", "private")
    settings = load_api_config()
    assert settings["endpoint"] == "https://api.example/v1/chat/completions"
    assert settings["model"] == "my-model" and settings["api_key"] == "private"
    monkeypatch.delenv("REPROLAB_API_KEY")
    with pytest.raises(ValueError, match="Key"):
        load_api_config()
    with pytest.raises(ValueError, match="URL"):
        load_api_config(base_url="https://user:password@example.com/v1")


@pytest.mark.parametrize('location', ['transport', 'model_text'])
def test_deeply_nested_model_json_gives_a_safe_error_without_a_traceback(tmp_path, location, monkeypatch):
    monkeypatch.setenv('REPROLAB_API_KEY', 'test-secret-do-not-save')
    _, _, _, prepared = prepare(tmp_path)
    nested = '[' * 10000 + '0' + ']' * 10000
    body = nested.encode() if location == 'transport' else api_answer(
        choices=[{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': nested}}])
    with api_server(body) as (url, _):
        result = cli('draft', 'generate', prepared['draft_dir'], '--config', config(tmp_path, url), '--json')
    assert result.returncode == 2
    assert json.loads(result.stdout)['status'] == 'inconclusive'
    assert 'Traceback' not in result.stderr
    assert 'test-secret-do-not-save' not in result.stdout + result.stderr
    assert not (Path(prepared['draft_dir']) / 'cases/case.json').exists()


def test_deeply_nested_model_config_is_a_readable_error(tmp_path):
    from reprolab.ai import load_api_config
    file = tmp_path / 'nested.json'
    file.write_text('[' * 10000 + '0' + ']' * 10000)
    with pytest.raises(ValueError, match='配置'):
        load_api_config(file)


def test_null_legacy_config_fields_can_still_use_environment_fallbacks(tmp_path, monkeypatch):
    from reprolab.ai import load_api_config
    monkeypatch.setenv('REPROLAB_API_BASE_URL', 'https://example.invalid/v1')
    monkeypatch.setenv('REPROLAB_AI_MODEL', 'synthetic-model')
    monkeypatch.setenv('REPROLAB_API_KEY', 'synthetic-key')
    file = tmp_path / 'legacy.json'
    file.write_text(json.dumps({'base_url': None, 'model': None, 'api_key': None, 'api_key_env': None}))
    settings = load_api_config(file)
    assert settings['endpoint'] == 'https://example.invalid/v1/chat/completions'
    assert settings['model'] == 'synthetic-model' and settings['api_key'] == 'synthetic-key'


@pytest.mark.parametrize('present', [True, False])
def test_cli_explicit_environment_key_overrides_the_saved_profile_without_fallback(tmp_path, monkeypatch, present):
    _, _, _, prepared = prepare(tmp_path)
    monkeypatch.setenv('REPROLAB_API_KEY', 'synthetic-ambient-key')
    if present:
        monkeypatch.setenv('REPROLAB_EXPLICIT_TEST_KEY', 'synthetic-explicit-key')
    else:
        monkeypatch.delenv('REPROLAB_EXPLICIT_TEST_KEY', raising=False)
    with api_server(api_answer()) as (url, requests):
        cfg = config(tmp_path, url)
        data = json.loads(cfg.read_text())
        data['api_key_source'] = 'profile'
        cfg.write_text(json.dumps(data))
        result = cli('draft', 'generate', prepared['draft_dir'], '--config', cfg,
                     '--api-key-env', 'REPROLAB_EXPLICIT_TEST_KEY', '--json')
    if present:
        assert result.returncode == 0, result.stdout + result.stderr
        assert requests[0]['authorization'] == 'Bearer synthetic-explicit-key'
    else:
        assert result.returncode == 2
        assert 'REPROLAB_EXPLICIT_TEST_KEY' in json.loads(result.stdout)['error']
        assert not requests
    assert 'test-secret-do-not-save' not in result.stdout + result.stderr


def test_cli_can_explicitly_select_the_responses_protocol(tmp_path, monkeypatch):
    _, _, _, prepared = prepare(tmp_path)
    monkeypatch.setenv('REPROLAB_API_KEY', 'test-secret-do-not-save')
    body = {'status': 'completed', 'model': 'fixture-model',
            'output': [{'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': json.dumps(response())}]}]}
    with api_server(body) as (url, requests):
        result = cli('draft', 'generate', prepared['draft_dir'], '--config', config(tmp_path, url),
                     '--protocol', 'responses', '--json')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['status'] == 'draft_generated'
    assert requests[0]['path'] == '/v1/responses'


@pytest.mark.parametrize('field', ['base_url', 'model', 'api_key', 'api_key_env'])
def test_invalid_nontext_config_does_not_silently_use_environment_fallback(tmp_path, monkeypatch, field):
    _, _, _, prepared = prepare(tmp_path)
    monkeypatch.setenv('REPROLAB_API_KEY', 'synthetic-ambient-key')
    monkeypatch.setenv('REPROLAB_AI_MODEL', 'synthetic-ambient-model')
    with api_server(api_answer()) as (url, requests):
        monkeypatch.setenv('REPROLAB_API_BASE_URL', url)
        cfg = config(tmp_path, url)
        data = json.loads(cfg.read_text())
        data[field] = 0
        cfg.write_text(json.dumps(data))
        result = cli('draft', 'generate', prepared['draft_dir'], '--config', cfg, '--json')
    assert result.returncode == 2
    assert json.loads(result.stdout)['status'] == 'inconclusive'
    assert not requests and 'Traceback' not in result.stderr


def test_deep_json_is_safely_rejected_on_python_312(tmp_path):
    executable = shutil.which('python3.12')
    if not executable:
        pytest.skip('Python 3.12 is not installed')
    nested = '[' * 10000 + '0' + ']' * 10000
    file = tmp_path / 'nested.json'
    file.write_text(nested)
    script = '''import sys
sys.path.insert(0, sys.argv[1])
from reprolab import ai, drafts, repair_suite
raw = '[' * 10000 + '0' + ']' * 10000
def check(label, call):
    try: call()
    except ValueError: print(label, 'safe')
    else: raise AssertionError(label + ' accepted invalid data')
check('config', lambda: ai.load_api_config(sys.argv[2]))
check('draft', lambda: drafts._validate_response(raw, {'sample'}))
check('suite', lambda: repair_suite._response_json(raw))
check('provider', lambda: ai.generate_api('public', __import__('pathlib').Path(sys.argv[3]), config_file=sys.argv[4]))
'''
    with api_server(nested.encode()) as (url, _):
        cfg = config(tmp_path, url)
        result = subprocess.run([executable, '-I', '-c', script, str(Path(__file__).resolve().parents[1] / 'src'),
                                 str(file), str(tmp_path), str(cfg)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, REPROLAB_API_KEY='test-secret-do-not-save'))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ['config safe', 'draft safe', 'suite safe', 'provider safe']


def message_answer(**changes):
    data = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": "fixture-model",
            "content": [{"type": "text", "text": json.dumps(response())}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 34}}
    data.update(changes)
    return data


@pytest.mark.parametrize("auth_type", ["api-key", "bearer"])
def test_anthropic_messages_protocol_uses_correct_endpoint_payload_auth_and_result(tmp_path, auth_type):
    _, _, _, prepared = prepare(tmp_path)
    with api_server(message_answer()) as (url, requests):
        cfg = config(tmp_path, url.replace("/v1", "/api/anthropic"))
        settings = json.loads(cfg.read_text())
        settings.update(protocol="anthropic", auth_type=auth_type, max_tokens=4096)
        cfg.write_text(json.dumps(settings))
        result = cli("draft", "generate", prepared["draft_dir"], "--config", cfg, "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    request = requests[0]
    assert request["path"] == "/api/anthropic/v1/messages"
    assert request["anthropic_version"] == "2023-06-01"
    assert request["api_key"] == ("test-secret-do-not-save" if auth_type == "api-key" else None)
    assert request["authorization"] == ("Bearer test-secret-do-not-save" if auth_type == "bearer" else None)
    assert request["data"]["max_tokens"] == 4096
    assert request["data"]["system"]
    assert request["data"]["messages"][0]["role"] == "user"
    assert "response_format" not in request["data"]
    generated = json.loads(result.stdout)
    assert generated["status"] == "draft_generated"
    assert generated["origin"]["provider"] == "anthropic-compatible"
    assert generated["origin"]["usage"] == {"input_tokens": 12, "output_tokens": 34}


@pytest.mark.parametrize("body", [message_answer(stop_reason="max_tokens"), message_answer(content=[]),
                                   message_answer(stop_reason="tool_use", content=[{"type": "tool_use", "name": "x", "input": {}}])])
def test_incomplete_anthropic_messages_never_create_a_case(tmp_path, body):
    _, _, _, prepared = prepare(tmp_path)
    with api_server(body) as (url, _):
        result = cli("draft", "generate", prepared["draft_dir"], "--config", config(tmp_path, url),
                     "--protocol", "anthropic", "--json")
    assert result.returncode == 2
    from pathlib import Path
    assert not (Path(prepared["draft_dir"]) / "cases/case.json").exists()


def test_anthropic_config_endpoint_normalization_and_validation(tmp_path):
    from reprolab.ai import load_api_config
    for suffix in ("/api/anthropic", "/api/anthropic/v1", "/api/anthropic/v1/messages"):
        cfg = config(tmp_path, "https://example.invalid" + suffix)
        assert load_api_config(cfg, protocol="anthropic")["endpoint"] == "https://example.invalid/api/anthropic/v1/messages"
    with pytest.raises(ValueError, match="protocol"):
        load_api_config(cfg, protocol="unknown")
    data = json.loads(cfg.read_text())
    data.update(protocol="anthropic", max_tokens=True)
    cfg.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="max_tokens"):
        load_api_config(cfg)


def test_truncated_anthropic_response_explains_limit_without_echoing_body(tmp_path):
    from reprolab.ai import generate_api
    with api_server(message_answer(stop_reason="max_tokens", content=[{"type":"thinking", "thinking":"test-secret-do-not-save"}])) as (url, _):
        cfg = config(tmp_path, url)
        with pytest.raises(ValueError, match="max_tokens") as exc:
            generate_api("synthetic diagnostic", tmp_path, config_file=cfg, protocol="anthropic")
    assert "test-secret-do-not-save" not in str(exc.value)


@pytest.mark.parametrize("fenced", [False, True])
def test_realistic_fenced_response_with_module_level_import_fallback_is_usable(tmp_path, fenced):
    from pathlib import Path
    _, _, _, prepared = prepare(tmp_path)
    code = "try:\n    from sample import compute\nexcept ImportError:\n    from src.sample import compute\n\ndef test_regression():\n    assert compute() == 42, 'expected 42'\n"
    raw = json.dumps(response(code))
    if fenced:
        raw = "```json\n" + raw + "\n```"
    with api_server(message_answer(content=[{"type":"text", "text":raw}])) as (url, _):
        result = cli("draft", "generate", prepared["draft_dir"], "--config", config(tmp_path, url), "--protocol", "anthropic", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (Path(prepared["draft_dir"])/"response.json").read_text() == raw
    case = json.loads(result.stdout)["case"]
    verified = cli("verify", case, "--output", tmp_path/"reports", "--json")
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert json.loads(verified.stdout)["status"] == "verified"
