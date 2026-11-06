"""Actual loopback HTTP and private fake-CLI boundaries, never real integrations."""
import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import remarkable_to_obsidian as app

try:
    import privacy_test_guard as guard
except ImportError:
    guard = SimpleNamespace(connections=set(), executables=set())

CANARY = 'PRIVATE-BOUNDARY-note-key-url-stderr'


@contextmanager
def server(status=200, response=None, redirect=False, delay=0):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            requests.append((self.path, dict(self.headers), json.loads(body)))
            if delay:
                time.sleep(delay)
            self.send_response(status)
            if redirect:
                self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/redirect-' + CANARY)
            self.end_headers()
            payload = {'choices': [{'message': {'content': 'synthetic result'}}]} if response is None else response
            try:
                self.wfile.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    http.daemon_threads = False
    address = ('127.0.0.1', http.server_port)
    guard.connections.add(address)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{address[1]}/v1', requests
    finally:
        http.shutdown(); http.server_close(); thread.join(timeout=2)
        guard.connections.discard(address)
        assert not thread.is_alive()


def local_client(monkeypatch, url):
    monkeypatch.setenv('RM_BACKEND', 'openai-compatible')
    monkeypatch.setenv('RM_MODEL', 'literal-model-20241022')
    monkeypatch.setenv('RM_OPENAI_BASE_URL', url)
    monkeypatch.delenv('RM_ALLOW_REMOTE_LLM', raising=False)
    return app.create_client(app.resolve_provider_config())


def request(client, **overrides):
    return client.messages.create(model='literal-model-20241022', max_tokens=123,
                                  system=[{'type': 'text', 'text': 'SYSTEM-' + CANARY, 'cache_control': {'type': 'ephemeral'}}],
                                  messages=[{'role': 'user', 'content': 'TEXT-' + CANARY}], **overrides)


def test_actual_loopback_ignores_proxies_and_serializes_text(monkeypatch, caplog):
    with server() as (url, requests), server() as (proxy_url, proxy_requests):
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            monkeypatch.setenv(key, proxy_url)
        monkeypatch.setenv('NO_PROXY', '')
        monkeypatch.setenv('no_proxy', '')
        monkeypatch.setenv('RM_OPENAI_API_KEY', 'fake-' + CANARY)
        client = local_client(monkeypatch, url)
        try:
            assert client._client.timeout.read == 60.0
            assert request(client).content[0].text == 'synthetic result'
        finally:
            client._client.close()
        assert not proxy_requests and len(requests) == 1
        path, headers, payload = requests[0]
        assert path == '/v1/chat/completions'
        assert headers['Authorization'] == 'Bearer fake-' + CANARY
        assert payload == {'model': 'literal-model-20241022', 'max_tokens': 123,
                           'messages': [{'role': 'system', 'content': 'SYSTEM-' + CANARY},
                                        {'role': 'user', 'content': 'TEXT-' + CANARY}]}
        assert CANARY not in caplog.text


def test_local_key_optional_and_images_ordered_with_retry_history(monkeypatch):
    monkeypatch.delenv('RM_OPENAI_API_KEY', raising=False)
    with server(response={'choices': [{'message': {'content': [{'type': 'text', 'text': 'one'}, {'type': 'text', 'text': 'two'}]}}]}) as (url, requests):
        client = local_client(monkeypatch, url)
        try:
            images = [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                       'data': base64.b64encode(value).decode()}} for value in (b'first-image', b'second-image')]
            result = client.messages.create(model='literal', max_tokens=4, system='system', thinking={'type': 'disabled'},
                messages=[{'role': 'user', 'content': images}, {'role': 'assistant', 'content': 'bad diagram'},
                          {'role': 'user', 'content': 'retry'}])
            assert result.content[0].text == 'one\ntwo'
        finally:
            client._client.close()
        _, headers, body = requests[0]
        assert 'Authorization' not in headers
        assert [m['role'] for m in body['messages']] == ['system', 'user', 'assistant', 'user']
        assert [base64.b64decode(b['image_url']['url'].split(',')[1]) for b in body['messages'][1]['content']] == [b'first-image', b'second-image']
        assert 'thinking' not in body


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_redirects_never_forward_prompt_or_key(monkeypatch, caplog, status):
    with server(status=status, response=CANARY.encode(), redirect=True) as (url, requests):
        client = local_client(monkeypatch, url)
        try:
            with pytest.raises(app.ProviderError) as error:
                request(client)
            assert CANARY not in str(error.value) + caplog.text
        finally:
            client._client.close()
        assert len(requests) == 1 and requests[0][0] == '/v1/chat/completions'


@pytest.mark.parametrize('response', [b'not-json-' + CANARY.encode(), {}, {'choices': []},
    {'choices': [{'message': {'content': None}}]}, {'choices': [{'message': {'content': ''}}]},
    {'choices': [{'message': {'content': [{'type': 'image', 'text': CANARY}]}}]},
    {'choices': [{'message': {'content': [{'type': 'text', 'text': 42}]}}]}])
def test_malformed_response_is_safe(monkeypatch, caplog, response):
    with server(response=response) as (url, requests):
        client = local_client(monkeypatch, url + '/' + CANARY)
        try:
            with pytest.raises(app.ProviderError) as error:
                request(client)
            assert error.value.__suppress_context__ or error.value.__context__ is None
            assert CANARY not in str(error.value) + caplog.text
        finally:
            client._client.close()
        assert len(requests) == 1


def test_timeout_is_finite_and_safe_without_fallback(monkeypatch, caplog):
    with server(delay=0.15) as (url, requests):
        client = local_client(monkeypatch, url)
        client._client.timeout = httpx.Timeout(0.03)
        try:
            with pytest.raises(app.ProviderError) as error:
                request(client)
            assert CANARY not in str(error.value) + caplog.text
        finally:
            client._client.close()
        assert len(requests) == 1


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    executable = tmp_path / 'private-fake-cli'
    capture = tmp_path / 'capture.json'
    # Reviewed fixture: reads stdin and private temporary attachments; no network,
    # authentication, shell commands, real CLI or external filesystem access.
    executable.write_text(f'#!{sys.executable}\n' + '''import base64, json, os, pathlib, sys, time
prompt = sys.stdin.read()
files = [(p.suffix, base64.b64encode(p.read_bytes()).decode()) for p in sorted(pathlib.Path.cwd().glob('input-*'))]
pathlib.Path(os.environ['FIXTURE_CAPTURE']).write_text(json.dumps({'argv': sys.argv[1:], 'stdin': prompt, 'files': files, 'unrelated': [k for k in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'HENDRIX_API_KEY', 'RM_OPENAI_API_KEY') if k in os.environ]}))
mode = os.environ.get('FIXTURE_MODE', 'success')
if mode == 'timeout': time.sleep(0.5)
if mode == 'error':
    sys.stderr.write('PRIVATE-BOUNDARY-note-key-url-stderr')
    sys.exit(7)
sys.stdout.write('synthetic cli output' if mode != 'empty' else '')
''')
    executable.chmod(0o700)
    guard.executables.add(str(executable.resolve()))
    monkeypatch.setenv('FIXTURE_CAPTURE', str(capture))
    monkeypatch.setenv('RM_ALLOW_REMOTE_LLM', '1')
    monkeypatch.setenv('RM_BACKEND', 'claude-cli')
    monkeypatch.setenv('RM_MODEL', 'literal-model-20241022')
    monkeypatch.setenv('RM_CLAUDE_BIN', str(executable))
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'HENDRIX_API_KEY', 'RM_OPENAI_API_KEY'):
        monkeypatch.setenv(key, 'unrelated-synthetic-secret')
    try:
        yield app.create_client(app.resolve_provider_config()), capture
    finally:
        guard.executables.discard(str(executable.resolve()))


def test_fake_cli_prompt_stdin_model_literal_and_response(fake_cli):
    client, capture = fake_cli
    result = client.messages.create(model='literal-model-20241022', system='system-' + CANARY, messages=[
        {'role': 'user', 'content': [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': base64.b64encode(b'synthetic-image').decode()}}, {'type': 'text', 'text': 'prompt-' + CANARY}]},
        {'role': 'assistant', 'content': 'bad answer'}, {'role': 'user', 'content': 'retry'}])
    assert result.content[0].text == 'synthetic cli output'
    captured = json.loads(capture.read_text())
    assert CANARY not in str(captured['argv'])
    assert captured['argv'][captured['argv'].index('--model') + 1] == 'literal-model-20241022'
    assert CANARY in captured['stdin']
    assert captured['unrelated'] == []
    assert captured['stdin'].index('ROLE: assistant') < captured['stdin'].rindex('ROLE: user')
    assert captured['files'] == [['.png', base64.b64encode(b'synthetic-image').decode()]]


@pytest.mark.parametrize('mode', ['error', 'empty', 'timeout'])
def test_fake_cli_failures_never_disclose(monkeypatch, fake_cli, caplog, mode):
    client, capture = fake_cli
    monkeypatch.setenv('FIXTURE_MODE', mode)
    client.timeout = 0.2 if mode == 'timeout' else 2
    with pytest.raises(app.ProviderError) as error:
        client.messages.create(model='literal', system=CANARY, messages=[{'role': 'user', 'content': CANARY}])
    assert CANARY not in str(error.value) + caplog.text
    assert CANARY not in str(json.loads(capture.read_text())['argv'])
