"""Editable hotwords and paged Kodi titles, compiled against the model vocabulary."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import time
import tempfile
import unicodedata

DEFAULT_TEXT = '''# 每行一个词语；可写“说法 => 显示名称”。修改后下次 F5 生效。
# 支持中文和当前模型字典中的英文。以 # 开头的行是注释。
CE => CE
C E => CE
Kodi => Kodi
K O D I => Kodi
CoreELEC => CoreELEC
Core ELEC => CoreELEC
科迪 => Kodi
扣迪 => Kodi
'''
CACHE_SECONDS = 3600
COMPILE_VERSION = 1
PAGE_SIZE = 10000
CUSTOM_WORDS_VERSION = 2


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(text)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def custom_file(profile):
    path = Path(profile) / 'hotwords_custom.txt'
    version_path = Path(profile) / 'hotwords_custom.version'
    if not path.exists():
        atomic_write(path, DEFAULT_TEXT)
        atomic_write(version_path, str(CUSTOM_WORDS_VERSION))
    else:
        try:
            version = int(version_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            version = 0
        if version < 2:
            # Upgrade existing profiles once; keep custom spellings and do not
            # restore an alias that the user subsequently deletes in the editor.
            text = path.read_text(encoding='utf-8')
            if not any(word == '扣迪' for word, unused in custom_entries(text)):
                atomic_write(path, text + ('' if text.endswith('\n') else '\n') + '扣迪 => Kodi\n')
            atomic_write(version_path, str(CUSTOM_WORDS_VERSION))
    return path


def custom_entries(text):
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        word, separator, canonical = line.partition('=>')
        word = ' '.join(unicodedata.normalize('NFKC', word).split())
        canonical = canonical.strip() if separator else ''
        if word and len(word) <= 160 and len(canonical) <= 160:
            entries.append((word, canonical))
    return list(dict.fromkeys(entries))


def refresh_library(profile, rpc, cancelled=lambda: False):
    """Replace the cache only after both library queries completed successfully."""
    titles, counts = set(), {}
    for method, key in (('VideoLibrary.GetMovies', 'movies'), ('VideoLibrary.GetTVShows', 'tvshows')):
        start = 0
        while True:
            if cancelled():
                raise InterruptedError('已取消影视热词更新。')
            result = rpc(method, {'properties': ['title'], 'limits': {'start': start, 'end': start + PAGE_SIZE},
                                  'sort': {'method': 'title', 'order': 'ascending'}})
            if not isinstance(result, dict) or key not in result:
                raise RuntimeError('Kodi 未返回完整的影视名称。')
            rows = result[key]
            for row in rows:
                title = row.get('title') or row.get('label')
                if isinstance(title, str) and title.strip():
                    titles.add(' '.join(title.split()))
            total = int(result.get('limits', {}).get('total', len(rows)))
            counts[key] = total
            start += len(rows)
            if start >= total:
                break
            if not rows:
                raise RuntimeError('Kodi 影视库分页提前结束，请稍后重试。')
    cache = dict(counts, titles=sorted(titles), updated_at=time.time())
    atomic_write(Path(profile) / 'hotwords_library.json', json.dumps(cache, ensure_ascii=False))
    return cache


def load_library(profile, rpc=None, force=False, cancelled=lambda: False):
    try:
        cache = json.loads((Path(profile) / 'hotwords_library.json').read_text(encoding='utf-8'))
        if not isinstance(cache.get('titles'), list):
            raise ValueError('Invalid library cache')
    except (OSError, ValueError, TypeError, AttributeError):
        cache = {'titles': [], 'movies': 0, 'tvshows': 0, 'updated_at': 0}
    if rpc and (force or time.time() - cache.get('updated_at', 0) >= CACHE_SECONDS):
        try:
            return refresh_library(profile, rpc, cancelled)
        except InterruptedError:
            raise
        except Exception:
            if force:
                raise
            # A temporary Kodi library error must not prevent voice input or
            # erase a previously imported title list.
            cache['refresh_failed'] = True
    return cache


def vocabulary(tokens_path):
    return {line.rsplit(maxsplit=1)[0] for line in Path(tokens_path).read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.startswith('<')}


def chinese_number(value):
    digits = '零一二三四五六七八九'
    number = int(value)
    if number == 0:
        return digits[0]
    if number > 9999 or (len(value) > 1 and value.startswith('0')):
        return ''.join(digits[int(char)] for char in value)
    result, zero = '', False
    for divisor, unit in ((1000, '千'), (100, '百'), (10, '十'), (1, '')):
        digit, number = divmod(number, divisor)
        if digit:
            if zero:
                result += '零'
            result += ('' if divisor == 10 and digit == 1 and not result else digits[digit]) + unit
            zero = False
        elif result and number:
            zero = True
    return result


def spoken_forms(title):
    title = unicodedata.normalize('NFKC', title).strip()
    title = re.sub(r'\s*\((?:19|20)\d{2}\)\s*$', '', title)
    values = [title]
    prefix = re.split(r'[:：]', title, maxsplit=1)[0].strip()
    if len(prefix) >= 2 and prefix != title:
        values.append(prefix)
    # Chinese titles containing a sequel number are usually spoken with a
    # Chinese numeral; keep both cardinal and digit-by-digit readings.
    if re.search(r'[\u3400-\u9fff]', title):
        for value in list(values):
            values.append(re.sub(r'\d+', lambda match: chinese_number(match.group()), value))
            values.append(re.sub(r'\d+', lambda match: ''.join('零一二三四五六七八九'[int(c)]
                                                              for c in match.group()), value))
    return list(dict.fromkeys(values))


class Tokenizer:
    """CJK characters plus up to two valid vocabulary segmentations per word.

    No external tokenizer dependency: ncnn contextual biasing matches token
    paths. Bilingual models use a word-boundary marker on Latin pieces.
    """
    def __init__(self, symbols):
        self.symbols = symbols

    @lru_cache(maxsize=4096)
    def latin(self, word):
        text = '▁' + word.upper()
        best = {len(text): [()]}
        for start in range(len(text) - 1, -1, -1):
            choices = []
            for end in range(start + 1, len(text) + 1):
                piece = text[start:end]
                if piece in self.symbols:
                    choices.extend((piece,) + suffix for suffix in best.get(end, []))
            best[start] = sorted(set(choices), key=lambda path: (len(path), path))[:2]
        return best.get(0, [])

    def encode(self, text):
        paths = [()]
        for part in re.findall(r'[\u3400-\u9fff]|[A-Za-z]+|\d+|[^\s\w]', text):
            if re.fullmatch(r'[A-Za-z]+', part):
                choices = self.latin(part)
            elif re.fullmatch(r'[\u3400-\u9fff]|\d+', part):
                choices = [(part,)] if part in self.symbols else []
            elif unicodedata.category(part[0]).startswith(('P', 'S')):
                continue
            else:
                choices = []
            if not choices:
                return []
            paths = [prefix + suffix for prefix in paths for suffix in choices][:4]
        # Unknown scripts must not silently disappear during regex matching.
        if re.search(r'[^\x00-\x7f\u3400-\u9fff]', re.sub(r'[^\w]', '', text)):
            return []
        return [path for path in paths if path]


def validate_hotwords(path, symbols):
    """Upstream calls exit() for an unknown token: reject it safely in Python."""
    path = Path(path)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('热词文件过大。')
    rows = path.read_text(encoding='utf-8').splitlines()
    for row in rows:
        pieces = row.split()
        if not pieces or any(piece not in symbols for piece in pieces):
            raise ValueError('热词文件含有当前模型不支持的词元，请重新更新词库。')
    return len(rows)


def prepare_hotwords(profile, tokens_path, rpc=None, force=False, cancelled=lambda: False):
    profile = Path(profile)
    custom = custom_file(profile).read_text(encoding='utf-8')
    entries = custom_entries(custom)
    library = load_library(profile, rpc, force, cancelled)
    symbols = vocabulary(tokens_path)
    token_id = hashlib.sha256(Path(tokens_path).read_bytes()).hexdigest()[:16]
    source_id = hashlib.sha256(json.dumps([COMPILE_VERSION, custom, library['titles']], ensure_ascii=False).encode()).hexdigest()
    compiled = profile / ('hotwords_' + token_id + '.txt')
    metadata = compiled.with_suffix('.json')
    try:
        report = json.loads(metadata.read_text())
        if report.get('source_id') == source_id and compiled.is_file():
            report.update(movies=library.get('movies', 0), tvshows=library.get('tvshows', 0),
                          refresh_failed=library.get('refresh_failed', False))
            return compiled if report['token_paths'] else '', report, entries
    except (OSError, ValueError, TypeError):
        pass
    tokenizer = Tokenizer(symbols)
    paths, accepted, skipped = set(), 0, []
    words = list(dict.fromkeys([word for word, unused in entries] + library['titles']))
    for word in words:
        if cancelled():
            raise InterruptedError('已取消热词准备。')
        found = set(path for form in spoken_forms(word) for path in tokenizer.encode(form))
        if found:
            paths.update(found)
            accepted += 1
        else:
            skipped.append(word)
    atomic_write(compiled, ''.join(' '.join(path) + '\n' for path in sorted(paths)))
    report = {'source_id': source_id, 'movies': library.get('movies', 0), 'tvshows': library.get('tvshows', 0),
              'library_titles': len(library['titles']), 'custom_words': len(entries),
              'accepted_words': accepted, 'skipped_words': len(skipped), 'token_paths': len(paths),
              'refresh_failed': library.get('refresh_failed', False)}
    atomic_write(metadata, json.dumps(report))
    atomic_write(profile / 'hotwords_skipped.txt', '\n'.join(skipped))
    return compiled if paths else '', report, entries


def text_formatter(entries):
    aliases = {word.casefold(): canonical for word, canonical in entries if canonical}
    if not aliases:
        return lambda text: text
    patterns = []
    for word in sorted(aliases, key=len, reverse=True):
        pattern = re.escape(word).replace(r'\ ', r'\s+')
        if word[0].isascii() and word[0].isalnum():
            pattern = r'(?<![A-Za-z0-9])' + pattern
        if word[-1].isascii() and word[-1].isalnum():
            pattern += r'(?![A-Za-z0-9])'
        patterns.append(pattern)
    expression = re.compile('|'.join(patterns), re.IGNORECASE)
    return lambda text: expression.sub(lambda match: aliases[' '.join(match.group().split()).casefold()], text)
