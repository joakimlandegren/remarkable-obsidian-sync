"""Offline configuration and real serialization tests; synthetic content only."""
import base64
import copy
import io
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fitz
import httpx
import pytest
from PIL import Image
import remarkable_to_obsidian as app
from test_selective_sync import archive, pdf, notebook, note

PROVIDERS = ['anthropic', 'vertex', 'hendrix', 'claude-cli', 'openai-compatible']
CANARY = 'PRIVATE-CANARY-key-note-url-stderr'


@pytest.fixture
def configured(monkeypatch):
    for key in list(os.environ):
        if key.startswith(('RM_', 'ANTHROPIC_', 'CLAUDE_', 'HENDRIX_', 'GOOGLE_', 'CLOUD_')):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('RM_MODEL', 'literal-main-20241022')
    monkeypatch.setenv('RM_OPENAI_BASE_URL', 'http://127.0.0.1:9999/v1')
    return monkeypatch


@pytest.mark.parametrize('backend', PROVIDERS)
@pytest.mark.parametrize('consent', ['', '0', 'true', 'yes'])
def test_denial_precedes_discovery_and_writes(configured, tmp_path, backend, consent):
    configured.setenv('RM_BACKEND', backend)
    configured.setenv('RM_OPENAI_BASE_URL', 'https://example.invalid/v1')
    configured.setenv('RM_ALLOW_REMOTE_LLM', consent)
    configured.setenv('OBSIDIAN_VAULT', str(tmp_path / 'vault'))
    configured.setenv('RM_STATE_FILE', str(tmp_path / 'state.json'))
    with patch.object(sys, 'argv', ['sync']), \
         patch.object(app, 'list_notebooks', side_effect=AssertionError('discovery before denial')) as discovery, \
         patch.object(app, 'load_state', side_effect=AssertionError('state before denial')) as state, \
         patch.object(app, 'create_client', side_effect=AssertionError('client before denial')) as client, \
         patch.object(app.subprocess, 'run', side_effect=AssertionError('spawn before denial')) as spawn:
        with pytest.raises(SystemExit) as error:
            app.main()
    assert error.value.code == 1
    for boundary in (discovery, state, client, spawn):
        boundary.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('backend', [None, '', 'unknown', 'ANTHROPIC', ' anthropic'])
def test_explicit_provider_required(configured, backend):
    if backend is not None:
        configured.setenv('RM_BACKEND', backend)
    configured.setenv('RM_ALLOW_REMOTE_LLM', '1')
    with pytest.raises(app.ProviderError):
        app.resolve_provider_config()


@pytest.mark.parametrize('backend', PROVIDERS)
def test_legacy_conflicts_and_models(configured, backend):
    configured.setenv('RM_BACKEND', backend)
    configured.setenv('RM_ALLOW_REMOTE_LLM', '1')
    config = app.resolve_provider_config()
    assert [config[k] for k in ('model', 'task_model', 'tag_model', 'diagram_model')] == ['literal-main-20241022'] * 4
    for name in ('TASK', 'TAG', 'DIAGRAM'):
        configured.setenv('RM_' + name + '_MODEL', name + '-literal@20241022')
    config = app.resolve_provider_config()
    assert config['task_model'] == 'TASK-literal@20241022'
    assert config['tag_model'] == 'TAG-literal@20241022'
    assert config['diagram_model'] == 'DIAGRAM-literal@20241022'
    configured.setenv('CLAUDE_CODE_USE_VERTEX', '1' if backend != 'vertex' else '0')
    with pytest.raises(app.ProviderError, match='Conflicting'):
        app.resolve_provider_config()


def test_legacy_mapping(configured, caplog):
    configured.setenv('CLAUDE_CODE_USE_VERTEX', '1')
    configured.setenv('RM_ALLOW_REMOTE_LLM', '1')
    assert app.resolve_provider_config()['backend'] == 'vertex'
    assert 'Legacy Vertex' in caplog.text


@pytest.mark.parametrize('key', ['RM_MODEL', 'RM_TASK_MODEL', 'RM_TAG_MODEL', 'RM_DIAGRAM_MODEL'])
@pytest.mark.parametrize('value', ['', ' ', 'two models', 'model\nsecret', '-option'])
def test_invalid_model_fails_before_client(configured, key, value):
    configured.setenv('RM_BACKEND', 'openai-compatible')
    configured.setenv(key, value)
    with patch.object(app, '_http_client', side_effect=AssertionError('constructed')):
        with pytest.raises(app.ProviderError, match='model'):
            app.resolve_provider_config()


@pytest.mark.parametrize('url', [
    '', 'ftp://127.0.0.1', 'http://localhost:123/v1', 'https://localhost./v1',
    'http://127.1/v1', 'https://127.1/v1', 'https://0177.0.0.1', 'https://2130706433',
    'https://0x7f000001', 'http://127.0.0.2', 'https://127.0.0.2',
    'http://127.0.0.1:0', 'http://127.0.0.1:65536', 'http://127.0.0.1:',
    'http://user:secret@127.0.0.1', 'http://127.0.0.1/?key=secret', 'http://127.0.0.1/#secret',
    'http://127.0.0.1?', 'http://127.0.0.1#', 'http://127%2e0.0.1',
    'http://127.0.0.1\\@evil.invalid', 'http://127.0.0.1\n', 'http://[::ffff:127.0.0.1]',
    'https://[0:0:0:0:0:0:0:1]', 'http://example.invalid/v1', 'http://[::1',
    'http://[::1]evil', 'https://[::1]evil:123',
])
def test_hostile_endpoints_never_construct_transport(configured, url):
    configured.setenv('RM_BACKEND', 'openai-compatible')
    configured.setenv('RM_OPENAI_BASE_URL', url)
    configured.setenv('RM_ALLOW_REMOTE_LLM', '1')
    with patch.object(app, '_http_client', side_effect=AssertionError('constructed')):
        with pytest.raises(app.ProviderError):
            app.OpenAICompatibleClient()


@pytest.mark.parametrize('url', ['http://127.0.0.1:123/v1', 'http://[::1]:123/api', 'https://127.0.0.1/v1'])
def test_literal_loopback_requires_no_remote_consent(configured, url):
    configured.setenv('RM_BACKEND', 'openai-compatible')
    configured.setenv('RM_OPENAI_BASE_URL', url)
    assert app.resolve_provider_config()['base_url'] == url


@pytest.mark.parametrize('mode', ['--dry-run', '--list-only', '--retag', '--merge-states'])
def test_non_model_modes_do_not_validate_or_construct(configured, tmp_path, mode):
    configured.setenv('OBSIDIAN_VAULT', str(tmp_path / 'vault'))
    configured.setenv('RM_STATE_FILE', str(tmp_path / 'state.json'))
    arguments = [mode] + ([str(tmp_path / 'file.json')] if mode in ('--list-only', '--merge-states') else [])
    with patch.object(sys, 'argv', ['sync', *arguments]), \
         patch.object(app, 'list_notebooks', return_value=[]), \
         patch.object(app, 'resolve_provider_config', side_effect=AssertionError('model config')), \
         patch.object(app, 'create_client', side_effect=AssertionError('client')):
        app.main()


def test_cli_preserves_literal_model():
    assert app.ClaudeCLIClient._cli_model('literal-model-20241022') == 'literal-model-20241022'


class SerializedProvider:
    """Exercise actual SDK serializers, not mocked messages.create success."""
    def __init__(self, monkeypatch, backend):
        self.backend, self.requests, self.attachments, self.cli = backend, [], [], []
        self.conversions = 0
        monkeypatch.setenv('RM_BACKEND', backend)
        monkeypatch.setenv('RM_ALLOW_REMOTE_LLM', '1')
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'fake-' + CANARY)
        monkeypatch.setenv('HENDRIX_API_KEY', 'fake-' + CANARY)
        monkeypatch.setenv('ANTHROPIC_VERTEX_PROJECT_ID', 'fake-project')
        monkeypatch.setenv('RM_CLAUDE_BIN', '/private-fixture/never-spawned')
        monkeypatch.setattr(app, '_http_client', lambda: httpx.Client(transport=httpx.MockTransport(self.http), trust_env=False, follow_redirects=False, timeout=60))
        from anthropic.lib.vertex._client import AnthropicVertex
        monkeypatch.setattr(AnthropicVertex, '_ensure_access_token', lambda _: 'fake-access-token')
        self.client = app.create_client(app.resolve_provider_config())

    def answer(self, text):
        if 'Classify this handwritten' in text:
            return 'flowchart'
        if 'Convert this flowchart' in text:
            self.conversions += 1
            return 'not-mermaid-' + CANARY if self.conversions % 2 else 'flowchart TD\n A --> B'
        if 'Extract action items' in text or 'suggest 3-5 semantic tags' in text:
            return '[]'
        return 'SELECTED TEXT\n> [Diagram(page=1, top=10, bottom=90): chart]'

    def http(self, request):
        body = json.loads(request.content)
        self.requests.append((str(request.url), body))
        if getattr(self, 'error', False):
            return httpx.Response(400, json={'error': {'type': 'invalid_request_error', 'message': CANARY}})
        if getattr(self, 'redirect', False):
            return httpx.Response(307, headers={'Location': 'https://must-not-follow.invalid/' + CANARY})
        text = json.dumps(body, ensure_ascii=False)
        answer = self.answer(text)
        if self.backend == 'openai-compatible':
            response = {'choices': [{'message': {'content': answer}}]}
        else:
            response = {'id': 'synthetic', 'type': 'message', 'role': 'assistant', 'model': 'synthetic',
                        'content': [{'type': 'text', 'text': answer}], 'stop_reason': 'end_turn',
                        'usage': {'input_tokens': 1, 'output_tokens': 1}}
        return httpx.Response(200, json=response)

    def subprocess(self, arguments, **options):
        if arguments[0] != '/private-fixture/never-spawned':
            raise AssertionError('unexpected subprocess')
        prompt = options['input']
        self.cli.append((arguments, prompt))
        files = sorted(Path(options['cwd']).glob('input-*'))
        self.attachments.append([(p.suffix, p.read_bytes()) for p in files])
        self.requests.append(('cli', {'model': arguments[arguments.index('--model') + 1], 'prompt': prompt}))
        assert 'SELECTED TEXT' not in str(arguments) and CANARY not in str(arguments)
        return SimpleNamespace(returncode=7 if getattr(self, 'error', False) else 0, stdout=self.answer(prompt), stderr=CANARY)

    def close(self):
        client = getattr(self.client, '_client', None)
        if client is not None:
            client.close()


@pytest.mark.parametrize('backend', PROVIDERS)
@pytest.mark.parametrize('kind', ['native', 'pdf'])
@pytest.mark.parametrize('overrides', [False, True])
def test_selected_pipeline_all_provider_routes(configured, tmp_path, caplog, backend, kind, overrides):
    source = archive(tmp_path) if kind == 'native' else pdf(tmp_path)
    if overrides:
        for name in ('TASK', 'TAG', 'DIAGRAM'):
            configured.setenv('RM_' + name + '_MODEL', name.lower() + '-literal@20241022')
    provider = SerializedProvider(configured, backend)
    config = app.resolve_provider_config()
    state = {'select-id': {'pages': {'1': {'hash': 'old', 'markdown': 'EXCLUDED-CACHE-' + CANARY}}}}
    def subprocess(arguments, **options):
        if arguments[0] == 'fake-download':
            shutil.copy2(source, Path(options['cwd']) / source.name)
            return SimpleNamespace(returncode=0)
        return provider.subprocess(arguments, **options)
    def run(selection, dry=False):
        with patch.object(app.subprocess, 'run', side_effect=subprocess):
            return app.sync_notebooks([notebook()], state, str(tmp_path / 'vault'), 'fake-download',
                config['model'], dry, provider.client, str(tmp_path / 'state.json'), blank_detect=False,
                selection=selection, extract_tasks_enabled=True, infer_tags_enabled=True,
                task_model=config['task_model'], tag_model=config['tag_model'], diagram_model=config['diagram_model'])
    try:
        assert run(app.Selection(pages=((2, 2),))).synced
        serialized = json.dumps(provider.requests)
        assert 'EXCLUDED-CACHE' not in serialized and 'Previous page content' not in serialized
        assert 'EXCLUDED' not in note(tmp_path).read_text()
        models = []
        for url, body in provider.requests:
            if backend == 'vertex':
                models.append(url.split('/models/')[1].split(':')[0])
                assert url.startswith('https://europe-west1-aiplatform.googleapis.com/')
            else:
                models.append(body['model'])
        assert models == [config['model'], config['diagram_model'], config['diagram_model'], config['diagram_model'], config['task_model'], config['tag_model']]
        if backend == 'claude-cli':
            first = provider.attachments[0][0][1]
            assert 'ROLE: assistant' in provider.cli[3][1] and 'ROLE: user' in provider.cli[3][1]
        else:
            body = provider.requests[0][1]
            block = body['messages'][1 if backend == 'openai-compatible' else 0]['content'][0]
            first = base64.b64decode(block['image_url']['url'].split(',', 1)[1] if backend == 'openai-compatible' else block['source']['data'])
            retry = provider.requests[3][1]['messages']
            assert [m['role'] for m in retry][-3:] == ['user', 'assistant', 'user']
            if backend == 'openai-compatible':
                assert 'cache_control' not in serialized and 'thinking' not in serialized
        if kind == 'pdf' and backend in ('anthropic', 'vertex', 'claude-cli'):
            with fitz.open(stream=first, filetype='pdf') as document:
                assert len(document) == 1 and 'SELECTED TWO' in document[0].get_text()
                assert 'EXCLUDED' not in document[0].get_text()
        else:
            # Compare decoded pixels to the independently selected source page.
            if kind == 'pdf':
                with fitz.open(source) as document:
                    expected = document[1].get_pixmap(dpi=150).tobytes('png')
            else:
                stroke_file = tmp_path / 'expected.rm'
                from test_selective_sync import stroke
                stroke_file.write_bytes(stroke(50))
                svg = tmp_path / 'expected.svg'
                svg.write_text(app._render_rm_to_svg(stroke_file))
                expected = app._svg_to_png(svg)[0]
            assert Image.open(io.BytesIO(first)).tobytes() == Image.open(io.BytesIO(expected)).tobytes()
        assert CANARY not in caplog.text  # invalid Mermaid labels never become diagnostics
        count = len(provider.requests)
        before = {str(p): p.read_bytes() for p in (tmp_path / 'vault').rglob('*') if p.is_file()}
        saved = copy.deepcopy(state)
        assert run(app.Selection(pages=((1, 3),)), dry=True).dry_runs
        assert state == saved and len(provider.requests) == count
        note(tmp_path).write_text(note(tmp_path).read_text() + '\nUSER EDIT')
        changed = note(tmp_path).read_bytes()
        assert run(app.Selection(pages=((1, 3),))).errored
        assert state == saved and len(provider.requests) == count and note(tmp_path).read_bytes() == changed
        # Legacy output is not adopted, and its attachments stay untouched.
        state['select-id'].pop('output_hash')
        assert run(app.Selection(pages=((1, 3),))).errored
        for path, data in before.items():
            if Path(path) != note(tmp_path):
                assert Path(path).read_bytes() == data
    finally:
        provider.close()


def test_exception_canaries_absent_from_console_and_sync_log(configured, tmp_path, caplog):
    from test_safe_sync import notebook as safe_notebook
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: (_ for _ in ()).throw(RuntimeError(CANARY))))
    page = tmp_path / 'input.rm'; page.write_bytes(b'synthetic')
    with patch.object(app, '_extract_rm_pages', return_value=[page]), \
         patch.object(app, '_render_rm_to_svg', return_value='<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><path d="M1 1 L9 9"/></svg>'):
        summary = app.sync_notebooks([safe_notebook()], {}, str(tmp_path / 'vault'), 'never', 'literal', False,
                                     client, str(tmp_path / 'state.json'), blank_detect=False)
    assert summary.errored
    path = app.write_sync_log(str(tmp_path / 'vault'), summary)
    assert CANARY not in caplog.text + path.read_text() + str(summary)
    # Even a caller-supplied error detail is not copied into the persistent log.
    app.write_sync_log(str(tmp_path / 'vault'), app.SyncSummary([app.SyncResult('safe', '/safe', 'errored', CANARY)]))
    assert CANARY not in path.read_text()


def test_sdk_request_logs_suppressed(caplog):
    logger = logging.getLogger('anthropic._base_client')
    logger.disabled = False; logger.setLevel(logging.DEBUG)
    http = logging.getLogger('httpx'); http.disabled = False; http.setLevel(logging.DEBUG)
    app._quiet_provider_logs()
    logger.debug(CANARY); http.info(CANARY)
    assert CANARY not in caplog.text


@pytest.mark.parametrize('backend', PROVIDERS)
def test_provider_errors_never_fallback_or_disclose(configured, tmp_path, caplog, backend):
    provider = SerializedProvider(configured, backend)
    provider.error = True
    source = pdf(tmp_path)
    def download(_binary, _path, output):
        shutil.copy2(source, output / 'source.pdf')
        return None
    try:
        with patch.object(app, '_extract_rm_pages', side_effect=download), \
             patch.object(app.subprocess, 'run', side_effect=provider.subprocess):
            summary = app.sync_notebooks([notebook()], {}, str(tmp_path / 'vault'), 'never',
                'literal', False, provider.client, str(tmp_path / 'state.json'))
        assert summary.errored and not summary.synced and len(provider.requests) == 1
        path = app.write_sync_log(str(tmp_path / 'vault'), summary)
        assert CANARY not in caplog.text + path.read_text() + str(summary)
        assert not (tmp_path / 'state.json').exists() and not note(tmp_path).exists()
    finally:
        provider.close()


@pytest.mark.parametrize('backend', ['anthropic', 'vertex', 'hendrix'])
def test_sdk_redirects_never_follow(configured, tmp_path, backend):
    provider = SerializedProvider(configured, backend)
    provider.redirect = True
    source = pdf(tmp_path)
    try:
        with pytest.raises(Exception):
            app.transcribe_pdf(provider.client, source, 'literal')
        assert len(provider.requests) == 1
    finally:
        provider.close()


@pytest.mark.parametrize('model', ['model?query', 'model#fragment', 'model%2Fescape', 'model/route'])
def test_vertex_model_path_delimiters_are_rejected(configured, model):
    configured.setenv('RM_BACKEND', 'vertex')
    configured.setenv('RM_ALLOW_REMOTE_LLM', '1')
    configured.setenv('RM_MODEL', model)
    with pytest.raises(app.ProviderError, match='model'):
        app.resolve_provider_config()


def test_main_forwards_selected_models_before_sync(configured, tmp_path):
    configured.setenv('RM_BACKEND', 'openai-compatible')
    for role in ('TASK', 'TAG', 'DIAGRAM'):
        configured.setenv('RM_' + role + '_MODEL', role + '-literal')
    configured.setenv('OBSIDIAN_VAULT', str(tmp_path / 'vault'))
    configured.setenv('RM_STATE_FILE', str(tmp_path / 'state.json'))
    with patch.object(sys, 'argv', ['sync']), patch.object(app, 'list_notebooks', return_value=[]), \
         patch.object(app, 'create_client', return_value=object()), \
         patch.object(app, 'sync_notebooks', return_value=app.SyncSummary()) as sync:
        app.main()
    assert sync.call_args.args[4] == 'literal-main-20241022'
    assert [sync.call_args.kwargs[role + '_model'] for role in ('task', 'tag', 'diagram')] == ['TASK-literal', 'TAG-literal', 'DIAGRAM-literal']


def test_hendrix_does_not_forward_anthropic_bearer_credential(configured):
    configured.setenv('RM_BACKEND', 'hendrix')
    configured.setenv('RM_ALLOW_REMOTE_LLM', '1')
    configured.setenv('HENDRIX_API_KEY', 'selected-fake-key')
    configured.setenv('ANTHROPIC_AUTH_TOKEN', 'wrong-provider-secret')
    captured = []
    def transport(request):
        captured.append(dict(request.headers))
        return httpx.Response(400, json={'error': {'type': 'invalid_request_error', 'message': 'synthetic'}})
    configured.setattr(app, '_http_client', lambda: httpx.Client(transport=httpx.MockTransport(transport)))
    client = app.create_client(app.resolve_provider_config())
    try:
        with pytest.raises(Exception):
            client.messages.create(model='literal', max_tokens=1, messages=[{'role': 'user', 'content': 'synthetic'}])
        assert len(captured) == 1
        assert captured[0]['x-api-key'] == 'selected-fake-key'
        assert 'authorization' not in captured[0]
    finally:
        client._client.close()

