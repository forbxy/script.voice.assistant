import json
from pathlib import Path

import pytest

from legacy.hotwords import (CUSTOM_WORDS_VERSION, Tokenizer, chinese_number, custom_entries, custom_file, load_library,
    prepare_hotwords, refresh_library, spoken_forms, text_formatter, validate_hotwords, vocabulary)
from legacy.ncnn_recognizer import NcnnRecognizer


def test_title_normalization_preserves_sequel_number_and_supports_spoken_readings():
    forms = spoken_forms('指环王２：双塔奇兵 (2002)')
    assert '指环王二:双塔奇兵' in forms
    assert '指环王二' in forms
    assert chinese_number('10') == '十'
    assert chinese_number('101') == '一百零一'
    assert chinese_number('2010') == '二千零一十'


def test_custom_words_comments_duplicates_and_aliases():
    assert custom_entries('# comment\n Kodi => Kodi\nKodi => Kodi\n C  E => CE\n') == [('Kodi', 'Kodi'), ('C E', 'CE')]
    formatter = text_formatter([('CE', 'CE'), ('C E', 'CE'), ('Kodi', 'Kodi'), ('科迪', 'Kodi')])
    assert formatter('打开 kodi，再打开科迪和 c  e。FACE KODIAK') == '打开 Kodi，再打开Kodi和 CE。FACE KODIAK'


def test_default_kodi_pronunciation_compiles_and_displays_canonical_name(tmp_path, asr_tokens):
    tokens = asr_tokens['balanced']
    path, report, entries = prepare_hotwords(tmp_path, tokens)
    assert '扣 迪' in path.read_text(encoding='utf-8').splitlines()
    assert text_formatter(entries)('打开扣迪，重启科迪') == '打开Kodi，重启Kodi'


def test_existing_custom_words_get_pronunciation_once_without_overwriting_edits(tmp_path):
    path = tmp_path / 'hotwords_custom.txt'
    original = '# 我的词库\n家庭影院 => 客厅电视'
    path.write_text(original, encoding='utf-8')
    assert custom_file(tmp_path).read_text(encoding='utf-8') == original + '\n扣迪 => Kodi\n'
    path.write_text(original, encoding='utf-8')
    assert custom_file(tmp_path).read_text(encoding='utf-8') == original


def test_existing_pronunciation_mapping_is_preserved_on_upgrade(tmp_path):
    path = tmp_path / 'hotwords_custom.txt'
    original = '扣迪 => 客厅的Kodi\n'
    path.write_text(original, encoding='utf-8')
    assert custom_file(tmp_path).read_text(encoding='utf-8') == original


def test_paginated_library_reads_titles_without_indexed_originaltitle(tmp_path):
    calls = []
    def rpc(method, params):
        calls.append((method, params))
        if method.endswith('GetTVShows'):
            return {'limits': {'total': 1}, 'tvshows': [{'title': '电视剧'}]}
        if params['limits']['start'] == 0:
            return {'limits': {'total': 3}, 'movies': [{'title': '电影', 'originaltitle': 'DY|电影|339464'}, {'title': '电影'}]}
        return {'limits': {'total': 3}, 'movies': [{'title': '另一部电影'}]}
    result = refresh_library(tmp_path, rpc)
    assert result['movies'] == 3 and result['tvshows'] == 1
    assert result['titles'] == ['另一部电影', '电影', '电视剧']
    assert len(calls) == 3 and calls[1][1]['limits']['start'] == 2
    assert calls[0][1]['properties'] == ['title']


def test_failed_refresh_keeps_previous_library_and_does_not_write_partial_data(tmp_path):
    old = {'titles': ['旧片名'], 'movies': 1, 'tvshows': 0, 'updated_at': 0}
    path = tmp_path / 'hotwords_library.json'
    path.write_text(json.dumps(old))
    def broken(*unused):
        raise RuntimeError('Kodi error')
    result = load_library(tmp_path, broken)
    assert result['titles'] == ['旧片名'] and result['refresh_failed']
    assert json.loads(path.read_text()) == old
    with pytest.raises(RuntimeError):
        load_library(tmp_path, broken, force=True)


def test_unknown_tokens_are_rejected_before_native_library_can_exit(tmp_path):
    path = tmp_path / 'invalid.txt'
    path.write_text('未 知\n')
    with pytest.raises(ValueError):
        validate_hotwords(path, {'未'})
    model = tmp_path / 'model'
    model.mkdir()
    (model / 'tokens.txt').write_text('<blk> 0\n未 1\n')
    with pytest.raises(ValueError):
        NcnnRecognizer(tmp_path, str(model), hotwords_file=str(path))


def test_compiler_accepts_real_bilingual_vocabulary_and_skips_unknown_characters(tmp_path, asr_tokens):
    tokens = asr_tokens['balanced']
    (tmp_path / 'hotwords_custom.txt').write_text('Kodi => Kodi\n指环王２：双塔奇兵\n\U0001f642\n未知かな\n')
    (tmp_path / 'hotwords_custom.version').write_text(str(CUSTOM_WORDS_VERSION))
    path, report, entries = prepare_hotwords(tmp_path, tokens)
    assert report['accepted_words'] == 2 and report['skipped_words'] == 2
    assert validate_hotwords(path, vocabulary(tokens)) == report['token_paths']
    assert any('二' in row for row in path.read_text().splitlines())
    assert any('▁K' in row for row in path.read_text().splitlines())
    before = path.stat().st_mtime_ns
    prepare_hotwords(tmp_path, tokens)
    assert path.stat().st_mtime_ns == before
    with (tmp_path / 'hotwords_custom.txt').open('a') as output:
        output.write('科迪 => Kodi\n')
    unused, updated, unused = prepare_hotwords(tmp_path, tokens)
    assert updated['accepted_words'] == 3
