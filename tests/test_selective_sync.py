"""Offline selection boundaries using real synthetic archives, stroke files and PDFs."""
import base64
import copy
import json
import shutil
import struct
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import fitz
import pytest
import remarkable_to_obsidian as app


def notebook(**changes):
    return {"id": "select-id", "name": "Selected", "path": "/Work/Selected", "version": 1,
            "modified": "unchanged", "tags": [], **changes}


def stroke(x):
    return (b'reMarkable .lines file, version=5' + b' ' * 10 + struct.pack('<II', 1, 1)
            + struct.pack('<IIIfII', 0, 0, 0, 1.0, 0, 2)
            + struct.pack('<ffffff', x, 10, 0, 0, 1, 1)
            + struct.pack('<ffffff', x + 10, 20, 0, 0, 1, 1))


def archive(root, *, tags=None, page_tags=None, order=('a', 'b', 'c'), modern=False, content=None):
    path = root / 'input.zip'
    metadata = {"fileType": "notebook", "tags": tags or [], "pageTags": page_tags or []}
    if modern:
        metadata['cPages'] = {'pages': [
            {'id': pid, 'idx': {'value': f'a{i}'}} for i, pid in reversed(list(enumerate(order)))]}
    else:
        metadata['pages'] = list(order)
    if content is not None:
        metadata = content
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('book.content', json.dumps(metadata))
        for i, pid in enumerate(('a', 'b', 'c')):
            z.writestr(f'book/{pid}.rm', stroke(30 + i * 20))
    return path


def run(root, source, state, selection=None, dry=False, nb=None, **options):
    calls = []
    def download(command, **kwargs):
        assert command[1] == 'get'
        shutil.copy2(source, Path(kwargs['cwd']) / source.name)
        return MagicMock(returncode=0)
    client = MagicMock()
    def response(**kwargs):
        calls.append(kwargs)
        return MagicMock(content=[MagicMock(text='SELECTED TEXT')])
    client.messages.create.side_effect = response
    with patch.object(app.subprocess, 'run', side_effect=download) as downloads:
        result = app.sync_notebooks([nb or notebook()], state, str(root / 'vault'), 'forbidden',
                                    'fake', dry, client, str(root / 'state.json'),
                                    blank_detect=False, selection=selection, **options)
    return result, calls, downloads.call_count


def note(root):
    return root / 'vault/Remarkable Notes/Work/Selected.md'


@pytest.mark.parametrize('value', ['0', '-1', '3-1', '1-', '1,,2', '', '1.2', 'a', '1-2-3'])
def test_invalid_ranges(value):
    with pytest.raises(ValueError):
        app.parse_page_ranges(value)


def test_ranges_preserve_order_without_expanding_unbounded_ranges():
    ranges = app.parse_page_ranges('3-5, 1,3,999999999999')
    selection = app.Selection(pages=ranges)
    assert [i for i in range(1, 8) if selection.matches(i, set())] == [1, 3, 4, 5]


@pytest.mark.parametrize('modern', [False, True])
def test_native_tags_associate_pages_and_exclusion_wins(tmp_path, modern):
    source = archive(tmp_path, modern=modern, page_tags=[{'name': ' Work ', 'pageId': 'a'},
                    {'name': 'work', 'pageId': 'c'}, {'name': 'Private', 'pageId': 'c'}])
    state = {}
    result, calls, _ = run(tmp_path, source, state,
                           app.Selection(include_tags=('WORK',), exclude_tags=(' private ',)))
    assert result.synced and len(calls) == 1
    assert 'Page 1 of 3' in str(calls)
    assert list(state['select-id']['pages']) == ['0']
    text = note(tmp_path).read_text()
    assert 'page 1.png' in text and 'page 2.png' not in text and 'page 3.png' not in text


@pytest.mark.parametrize('include,exclude,pages,expected', [
    (('whole',), (), None, ['0', '1', '2']),
    ((), ('private',), None, ['0', '2']),
    (('whole', 'other'), ('private',), '2-3', ['2']),
    (('missing',), (), None, []),
    (('whole',), ('whole',), None, []),
    ((), (), '9-10', []),
])
def test_include_any_notebook_tags_exclusions_and_range_intersection(tmp_path, include, exclude, pages, expected):
    source = archive(tmp_path, tags=[{'name': ' Whole '}], page_tags=[{'name': 'private', 'pageId': 'b'}])
    state = {}
    selection = app.Selection(include_tags=include, exclude_tags=exclude,
                              pages=app.parse_page_ranges(pages) if pages else ())
    result, calls, _ = run(tmp_path, source, state, selection)
    if expected:
        assert result.synced
        assert list(state['select-id']['pages']) == expected
        assert len(calls) == len(expected)
    else:
        assert result.skipped and 'no pages' in result.skipped[0].detail.lower()
        assert not state and not calls and not note(tmp_path).exists()


def test_selection_changes_and_tag_removal_recheck_unchanged_source(tmp_path):
    source = archive(tmp_path, page_tags=[{'name': 'work', 'pageId': 'b'}])
    state = {}
    selection = app.Selection(include_tags=('work',))
    assert run(tmp_path, source, state, selection)[0].synced
    before = note(tmp_path).read_bytes()
    original = copy.deepcopy(state)
    assert run(tmp_path, source, state, selection)[1] == []
    source = archive(tmp_path)
    result, calls, _ = run(tmp_path, source, state, selection)
    assert result.skipped and not calls
    assert note(tmp_path).read_bytes() == before and state == original
    result, calls, _ = run(tmp_path, source, state, app.Selection(pages=((1, 1),)))
    assert result.synced and len(calls) == 1
    assert list(state['select-id']['pages']) == ['0']
    result, calls, _ = run(tmp_path, source, state)  # full sync after partial
    assert result.synced and len(calls) == 2
    assert list(state['select-id']['pages']) == ['0', '1', '2']
    assert run(tmp_path, source, state)[1] == []


def test_reorder_reuses_stable_page_identity_and_updates_original_numbers(tmp_path):
    source = archive(tmp_path)
    state = {}
    selection = app.Selection(pages=((1, 2),))
    assert run(tmp_path, source, state, selection)[0].synced
    source = archive(tmp_path, order=('b', 'a', 'c'), modern=True)
    result, calls, _ = run(tmp_path, source, state, selection)
    assert result.synced and not calls
    assert [p['id'] for p in state['select-id']['pages'].values()] == ['b', 'a']


def test_excluded_cached_text_never_becomes_context_enrichment_or_assets(tmp_path):
    source = archive(tmp_path)
    state = {}
    assert run(tmp_path, source, state, app.Selection(pages=((1, 3),)))[0].synced
    # A legacy/full-cache transcription might contain excluded previous-page text.
    state['select-id']['pages']['1']['markdown'] = 'EXCLUDED SECRET'
    state['select-id']['pages']['1'].pop('context_free', None)
    with patch.object(app, 'extract_tasks', return_value=[]) as tasks, \
         patch.object(app, 'infer_tags', return_value=[]) as infer, \
         patch.object(app, 'extract_diagram_crops', side_effect=lambda md, *a, **k: (md, [])) as diagrams, \
         patch.object(app, 'save_source_pages', wraps=app.save_source_pages) as sources:
        result, calls, _ = run(tmp_path, source, state, app.Selection(pages=((2, 2),)),
                               extract_tasks_enabled=True, infer_tags_enabled=True)
    assert result.synced and len(calls) == 1
    assert 'EXCLUDED SECRET' not in str(calls) + str(tasks.call_args) + str(infer.call_args)
    assert 'EXCLUDED SECRET' not in note(tmp_path).read_text()
    assert diagrams.call_args.args[1][0] is None and diagrams.call_args.args[1][2] is None
    assert sources.call_args.args[2][0] is None and sources.call_args.args[2][2] is None
    assert 'page 2.png' in note(tmp_path).read_text()


def test_preview_selection_is_unresolved_download_model_write_free(tmp_path):
    source = archive(tmp_path)
    state = {}
    before = sorted(str(p) for p in tmp_path.rglob('*'))
    result, calls, downloads = run(tmp_path, source, state, app.Selection(include_tags=('work',)), dry=True)
    assert result.dry_runs and 'unresolved' in result.dry_runs[0].detail.lower()
    assert not calls and not downloads and not state
    assert before == sorted(str(p) for p in tmp_path.rglob('*'))


@pytest.mark.parametrize('content', [
    {'fileType': 'notebook'},
    {'fileType': 'epub', 'pages': ['a']},
    {'pages': ['a', 'a']},
    {'cPages': {'pages': [{'id': 'a'}]}},
    {'cPages': {'pages': [{'id': 'a', 'idx': {'value': 'a'}}, {'id': 'b', 'idx': {'value': 'a'}}]}},
    {'pages': ['a'], 'pageTags': [{'name': 'private'}]},
    {'pages': ['a'], 'pageTags': [{'name': 'private', 'pageId': 'unknown'}]},
    {'pages': ['missing']},
    {'cPages': {'pages': [{'id': 'a', 'idx': {'value': 'a0'}},
                         {'id': 'a', 'deleted': {'value': True}}]},
     'pageTags': [{'name': 'private', 'pageId': 'a'}]},
])
def test_unsupported_mapping_fails_closed(tmp_path, content):
    source = archive(tmp_path, content=content)
    state = {}
    result, calls, _ = run(tmp_path, source, state, app.Selection(exclude_tags=('private',)))
    assert result.errored and not result.synced and not calls and not state
    assert not note(tmp_path).exists()


def pdf(root):
    path = root / 'input.pdf'
    with fitz.open() as doc:
        for text in ('EXCLUDED ONE', 'SELECTED TWO', 'EXCLUDED THREE'):
            doc.new_page().insert_text((72, 72), text)
        doc.save(path)
    return path


def test_pdf_range_subsets_model_payload_and_export(tmp_path):
    source = pdf(tmp_path)
    state = {}
    result, calls, _ = run(tmp_path, source, state, app.Selection(pages=((2, 2),)))
    assert result.synced and len(calls) == 1
    block = calls[0]['messages'][0]['content'][0]
    assert block['type'] == 'document'
    payload = block['source']
    with fitz.open(stream=base64.b64decode(payload['data']), filetype='pdf') as doc:
        assert len(doc) == 1 and 'SELECTED TWO' in doc[0].get_text()
        assert 'EXCLUDED' not in ''.join(p.get_text() for p in doc)
    exports = list((tmp_path / 'vault/Attachments/reMarkable').glob('*.pdf'))
    assert len(exports) == 1
    with fitz.open(exports[0]) as doc:
        assert len(doc) == 1 and 'SELECTED TWO' in doc[0].get_text()
    assert 'Page 2 of 3' in str(calls) and 'Page 2' in note(tmp_path).read_text()
    assert run(tmp_path, source, state, app.Selection(pages=((2, 2),)))[1] == []
    assert len(run(tmp_path, source, state)[1]) == 2


def test_selected_pdf_uses_gateway_document_conversion(tmp_path):
    result, calls, _ = run(tmp_path, pdf(tmp_path), {}, app.Selection(pages=((2, 2),)))
    assert result.synced
    gateway = object.__new__(app.HendrixClient)
    gateway._client = MagicMock()
    gateway._client.messages.create.return_value = MagicMock(content=[MagicMock(type='text', text='selected')])
    gateway.create(**calls[0])
    content = gateway._client.messages.create.call_args.kwargs['messages'][0]['content']
    images = [block for block in content if block['type'] == 'image']
    assert len(images) == 1 and images[0]['source']['media_type'] == 'image/png'
    assert not any(block['type'] == 'document' for block in content)


def test_literal_page_values_survive_selection_expansion_and_reorder(tmp_path):
    source = archive(tmp_path)
    state = {}
    text = 'API example: page=42; code page=123; literal page=1'
    with patch.object(app, 'transcribe_page', return_value=text):
        assert run(tmp_path, source, state, app.Selection(pages=((1, 1),)))[0].synced
        assert state['select-id']['pages']['0']['markdown'] == text
        assert run(tmp_path, source, state, app.Selection(pages=((1, 2),)))[0].synced
        assert [p['markdown'] for p in state['select-id']['pages'].values()] == [text, text]
        source = archive(tmp_path, order=('b', 'a', 'c'), modern=True)
        assert run(tmp_path, source, state, app.Selection(pages=((1, 2),)))[0].synced
        assert [p['markdown'] for p in state['select-id']['pages'].values()] == [text, text]
    assert note(tmp_path).read_text().count(text) == 2


def test_tagged_pdf_without_metadata_fails_closed(tmp_path):
    result, calls, _ = run(tmp_path, pdf(tmp_path), {}, app.Selection(exclude_tags=('private',)))
    assert result.errored and not calls and not note(tmp_path).exists()


def test_stat_or_inferred_tags_are_not_selection_evidence(tmp_path):
    source = archive(tmp_path)
    result, calls, _ = run(tmp_path, source, {}, app.Selection(include_tags=('work',)),
                           nb=notebook(tags=['work', 'inbox', 'handwritten']))
    assert result.skipped and not calls


@pytest.mark.parametrize('mapping', [None, [0, 1, 2], [2, 1, 0]])
def test_ordinary_pdf_archive_identity_mapping(tmp_path, mapping):
    original = pdf(tmp_path)
    source = tmp_path / 'pdf.zip'
    content = {'fileType': 'pdf', 'pages': ['a', 'b', 'c'],
               'pageCount': 3, 'originalPageCount': 3}
    if mapping is not None:
        content['redirectionPageMap'] = mapping
    with zipfile.ZipFile(source, 'w') as z:
        z.writestr('book.content', json.dumps(content))
        z.writestr('book.pdf', original.read_bytes())
    state = {}
    result, calls, _ = run(tmp_path, source, state, app.Selection(pages=((2, 2),)))
    if mapping == [0, 1, 2]:
        assert result.synced and len(calls) == 1
    else:
        assert result.errored and not calls and not state


def test_page_tag_moves_between_pages_without_version_or_time_change(tmp_path):
    source = archive(tmp_path, page_tags=[{'name': 'work', 'pageId': 'a'}])
    state = {}
    selection = app.Selection(include_tags=('work',))
    assert run(tmp_path, source, state, selection)[0].synced
    source = archive(tmp_path, page_tags=[{'name': 'work', 'pageId': 'c'}])
    result, calls, _ = run(tmp_path, source, state, selection)
    assert result.synced and len(calls) == 1
    assert list(state['select-id']['pages']) == ['2']
    assert 'page 1.png' not in note(tmp_path).read_text() and 'page 3.png' in note(tmp_path).read_text()


def test_deleted_page_tags_do_not_select_other_pages(tmp_path):
    source = archive(tmp_path, content={'fileType': 'notebook', 'cPages': {'pages': [
        {'id': 'b', 'idx': {'value': 'a1'}, 'deleted': {'value': True}},
        {'id': 'c', 'idx': {'value': 'a2'}}, {'id': 'a', 'idx': {'value': 'a0'}}]},
        'pageTags': [{'name': 'work', 'pageId': 'b'}]})
    result, calls, _ = run(tmp_path, source, {}, app.Selection(include_tags=('work',)))
    assert result.skipped and not calls


def test_selected_diagram_uses_only_selected_source_and_original_number(tmp_path):
    source = archive(tmp_path)
    state = {}
    with patch.object(app, 'transcribe_page', return_value='> [Diagram(page=1, top=10, bottom=90): selected chart]'), \
         patch.object(app, '_convert_diagram_to_vector', return_value=('fallback_png', '')) as vector:
        result, _, _ = run(tmp_path, source, state, app.Selection(pages=((3, 3),)))
    assert result.synced and vector.call_count == 1
    text = note(tmp_path).read_text()
    assert 'page 3.png' in text and 'page 1.png' not in text and 'diagram 1.png' in text
    assert '## Page 3' in text and '[Diagram(' not in text


def test_preview_cli_reports_unresolved_and_forwards_repeatable_filters(tmp_path, caplog):
    source = tmp_path / 'list.json'
    source.write_text(json.dumps([notebook(), notebook(id='outside', path='/Other/Selected'),
                                  notebook(id='ignored', name='Ignore', path='/Work/Ignore')]))
    config = {'state_file': str(tmp_path / 'state.json'), 'obsidian_vault': str(tmp_path / 'vault'),
              'rmapi_bin': 'forbidden', 'watch_path': '/Work', 'model': 'fake'}
    caplog.set_level('INFO')
    with patch.object(sys, 'argv', ['sync', '--dry-run', '--notebooks-json', str(source),
                                  '--include-tag', 'work', '--include-tag', 'other',
                                  '--exclude-tag', 'private', '--exclude-tag', 'secret', '--pages', '1,3-5']), \
         patch.object(app, 'load_config', return_value=config), \
         patch.object(app, 'load_ignore_patterns', return_value=['Ignore']), \
         patch.object(app, 'build_note_index', return_value={}), \
         patch.object(app, '_load_custom_prompts', return_value=[]), \
         patch.object(app, 'sync_notebooks', wraps=app.sync_notebooks) as sync, \
         patch.object(app.subprocess, 'run', side_effect=AssertionError('preview download')):
        app.main()
    assert len(sync.call_args.args[0]) == 1
    assert sync.call_args.kwargs['selection'].include_tags == ('work', 'other')
    assert sync.call_args.kwargs['selection'].exclude_tags == ('private', 'secret')
    assert 'unresolved' in caplog.text


def test_conflict_and_empty_selection_preserve_output_state_and_assets(tmp_path):
    source = archive(tmp_path)
    state = {}
    selection = app.Selection(pages=((1, 1),))
    assert run(tmp_path, source, state, selection)[0].synced
    note(tmp_path).write_text(note(tmp_path).read_text() + '\nMANUAL EDIT')
    original = copy.deepcopy(state)
    before = {str(p): p.read_bytes() for p in (tmp_path / 'vault').rglob('*') if p.is_file()}
    result, calls, downloads = run(tmp_path, source, state, app.Selection(pages=((2, 2),)))
    assert result.errored and not calls and not downloads and state == original
    assert before == {str(p): p.read_bytes() for p in (tmp_path / 'vault').rglob('*') if p.is_file()}


@pytest.mark.parametrize('mode', [['--retag'], ['--merge-states', 'state.json'], ['--list-only', 'list.json']])
def test_selection_rejects_alternate_mutation_modes_before_io(mode):
    with patch.object(sys, 'argv', ['sync', '--include-tag', 'work', *mode]), \
         patch.object(app, 'load_config', side_effect=AssertionError('unexpected I/O')):
        with pytest.raises(SystemExit) as error:
            app.main()
        assert error.value.code == 2


@pytest.mark.parametrize('value', ['0', '-1', '5-2', '1,,2'])
def test_cli_invalid_pages_fails_before_processing(value):
    with patch.object(sys, 'argv', ['sync', '--pages=' + value]), \
         patch.object(app, 'load_config', side_effect=AssertionError('unexpected I/O')):
        with pytest.raises(SystemExit) as error:
            app.main()
        assert error.value.code == 2
