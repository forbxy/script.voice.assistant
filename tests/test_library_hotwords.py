from resources.lib.data_paths import data_path
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from resources.lib.asr import factory, library_hotwords
from resources.lib.asr.text import validate_hotwords, vocabulary


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('quality', ['fast', 'balanced', 'accurate'])
def test_library_titles_are_readable_by_original_parser_without_english(quality, tmp_path, asr_tokens):
    symbols = vocabulary(asr_tokens[quality])
    paths, report = library_hotwords.compile_titles(
        ['老无所依', '十八岁的天空', '流浪地球2', '流浪地球２ (2023)', '2046', 'X战警', '阿Q正传', 'Kodi', 'CE', '未知𠮷'], symbols)
    rows = [' '.join(p) for p in paths]
    assert '老 无 所 依' in rows and '十 八 岁 的 天 空' in rows
    assert '流 浪 地 球 二' in rows and rows.count('流 浪 地 球 二') == 1
    assert '二 零 四 六' in rows
    assert '战 警' in rows and '正 传' in rows and '阿 正 传' not in rows
    assert report['partial_titles'] == 2 and set(report['skipped']) == {'Kodi', 'CE', '未知𠮷'}
    internal = {' ' + p[1:] if p.startswith('▁') else p for p in symbols}
    assert all(piece in internal and '▁' not in piece and not piece.isascii()
               or piece.isdigit() and piece in internal for path in paths for piece in path)
    p = tmp_path / 'hotwords.txt';p.write_text('\n'.join(rows) + '\n')
    assert validate_hotwords(p, symbols) == len(paths)


def library_rpc(calls):
    movies, shows = ['老无所依', '流浪地球2', '老无所依'], ['十八岁的天空', 'English']
    def rpc(method, params):
        calls.append((method, params))
        key, titles = ('movies', movies) if method.endswith('GetMovies') else ('tvshows', shows)
        start = params['limits']['start']
        # Simulate a server returning fewer rows than the requested page size.
        return {key: [{'title': t} for t in titles[start:start + 2]],
                'limits': {'start': start, 'total': len(titles)}}
    return rpc


def test_all_pages_are_read_and_cache_avoids_repeated_queries(tmp_path, asr_tokens):
    tokens = asr_tokens['balanced']
    calls = []
    path, report = library_hotwords.prepare(tmp_path, tokens, library_rpc(calls))
    assert report['movies'] == 3 and report['tvshows'] == 2 and report['unique_titles'] == 4
    assert report['included_titles'] == 3 and report['skipped_titles'] == 1
    assert [p['limits']['start'] for m, p in calls if m.endswith('GetMovies')] == [0, 2]
    assert Path(path).stat().st_mode & 0o777 == 0o600
    assert library_hotwords.prepare(tmp_path, tokens, lambda *a: pytest.fail('Cache should be reused')) == (path, report)


def test_incomplete_refresh_preserves_complete_cache_and_cancellation_stops(tmp_path, asr_tokens):
    tokens = asr_tokens['balanced']
    library_hotwords.prepare(tmp_path, tokens, library_rpc([]))
    cache = data_path(tmp_path, 'asr-library-hotwords/library.json')
    before = cache.read_bytes()
    def broken(method, params):
        return {'movies': [], 'limits': {'start': 0, 'total': 3}}
    with pytest.raises(ValueError, match='提前结束'):
        library_hotwords.prepare(tmp_path, tokens, broken, force=True)
    assert cache.read_bytes() == before
    with pytest.raises(InterruptedError):
        library_hotwords.prepare(tmp_path, tokens, lambda *a: pytest.fail('Must cancel before RPC'), cancelled=lambda: True)
    with pytest.raises(InterruptedError):
        library_hotwords.compile_titles(['老无所依'], set(), cancelled=lambda: True)


def test_boundary_tokens_are_rejected_before_original_library_can_exit(tmp_path):
    path = tmp_path / 'bad.txt';path.write_text('▁K O DI\n')
    with pytest.raises(ValueError):
        validate_hotwords(path, {'▁K', 'O', 'DI'})


def test_new_runtime_does_not_query_library_or_generate_hotwords(tmp_path, monkeypatch):
    from resources.lib.asr import recognizer as onnx
    seen = []
    monkeypatch.setattr(onnx, 'Recognizer', lambda *args: seen.append(args) or 'engine')
    values = {'recognition_engine': 'onnx', 'onnx_model': 'sensevoice'}
    assert factory.recognizer(ROOT, tmp_path, values, SimpleNamespace(recognizer_endpoint=False),
                              lambda: False, rpc=lambda *a: pytest.fail('No library query')) == 'engine'
    assert seen[0][0] == tmp_path and seen[0][1] == 'sensevoice'


def resident_owner(tmp_path, monkeypatch):
    from resources.lib.asr import service
    data = service.catalog(str(ROOT))
    monkeypatch.setattr(service, 'catalog', lambda root: data)
    class FakeNative:
        def __init__(self, arguments, cancelled, *, decoding=None):
            self.arguments, self.cancelled = arguments, cancelled
            self.process = SimpleNamespace(poll=lambda: None, pid=123)
        def close(self): pass
    monkeypatch.setattr(service, 'Native', FakeNative)
    monkeypatch.setattr(service, 'command', lambda root, profile, model:
        ['worker', data['asr_models'][model]['family'],
         'encoder', 'decoder', 'joiner', 'tokens', '2', 'modified_beam_search', ''])
    owner = service.Service.__new__(service.Service)
    owner.root = owner.profile = tmp_path
    owner.native = owner.loaded_model = None
    owner.loaded_decoding = None
    owner.values = {}
    owner.hotwords = {}
    owner.rpc = lambda *a: None
    return owner


@pytest.mark.parametrize('model', ['zipformer-2025', 'zipformer-2025-fp16', 'zipformer-2025-fp32'])
def test_resident_refreshes_library_before_each_model_load_but_not_each_stream(tmp_path, monkeypatch, model):
    owner = resident_owner(tmp_path, monkeypatch)
    calls = []
    def prepare(profile, tokens, rpc, cancelled, force):
        assert owner.native is None and force
        calls.append(tokens)
        return '/hotwords.txt', {'token_paths': 7, 'skipped': ['private-title']}
    monkeypatch.setattr(library_hotwords, 'prepare', prepare)
    owner.load(model, lambda: False)
    assert calls == ['tokens'] and owner.native.arguments[-1] == '/hotwords.txt'
    assert owner.status['hotwords'] == {'token_paths': 7}
    owner.load(model, lambda: False)
    assert len(calls) == 1  # Residency, including subsequent client streams.
    owner.unload()
    owner.load(model, lambda: False)
    assert len(calls) == 2
    owner.load('sensevoice', lambda: False)
    assert len(calls) == 2 and owner.native.arguments[-1] == ''
    assert 'hotwords' not in owner.status


def test_library_failure_is_visible_and_cancellation_never_starts_native(tmp_path, monkeypatch):
    owner = resident_owner(tmp_path, monkeypatch)
    def fail(*a, **k): raise ValueError('Unavailable library')
    monkeypatch.setattr(library_hotwords, 'prepare', fail)
    owner.load('zipformer-2025', lambda: False)
    assert owner.native.arguments[-1] == ''
    assert 'warning' in owner.status['hotwords']
    owner.unload()
    def cancelled(*a, **k): raise InterruptedError()
    monkeypatch.setattr(library_hotwords, 'prepare', cancelled)
    with pytest.raises(InterruptedError): owner.load('zipformer-2025', lambda: False)
    assert owner.native is None
