"""Disposable exact-title index, bound to the private canonical title snapshot."""
import json
from contextlib import closing
import os
import sqlite3
import tempfile
import time

from ..data_paths import data_path
from ..storage import read_json


def paths(profile, account):
    stem = 'cache/emby-' + account['_identity']
    return (data_path(profile, stem + '.json'), data_path(profile, stem + '.sqlite'),
            data_path(profile, 'cache/emby-state/' + account['_identity'] + '.json'))


def signature(path):
    stat = path.stat()
    return [stat.st_ino, stat.st_size, stat.st_mtime_ns]


def lookup(profile, account, request, interval=None):
    """None means rebuild/check; an empty list is a valid negative lookup."""
    from .emby import candidate
    from .media_match import normalize
    snapshot, path, state_path = paths(profile, account)
    try:
        before = signature(snapshot)
        # Read-only connections cannot create a misleading empty index.
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            meta = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
            if (meta.get('version') != 1 or meta.get('revision') != account['_revision']
                    or meta.get('signature') != before):
                return None
            if interval is not None:
                state = read_json(state_path, {})
                if (not isinstance(state, dict) or state.get('version') != 1
                        or state.get('snapshot') != meta.get('snapshot')
                        or state.get('mode') not in ('saved_since', 'full')
                        or type(state.get('checked_at')) not in (int, float)
                        or not 0 <= time.time() - state['checked_at'] < interval):
                    return None
            query = 'SELECT itemid,title,type,year FROM titles WHERE normalized=?'
            args = [normalize(request['title'])]
            if request['kind'] != 'all':
                query += ' AND type=?'
                args.append({'movie': 'Movie', 'tvshow': 'Series'}[request['kind']])
            if request.get('year') is not None:
                query += ' AND year=?'
                args.append(request['year'])
            result = [candidate(dict(Id=row[0], Name=row[1], Type=row[2], ProductionYear=row[3]), account)
                      for row in db.execute(query, args)]
        return result if signature(snapshot) == before else None
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError):
        # The canonical JSON remains authoritative if this derived file is bad.
        return None


def build(profile, account, rows, cancelled):
    from .media_match import normalize
    snapshot, path, state_path = paths(profile, account)
    temporary = None
    try:
        before = signature(snapshot)
        state = read_json(state_path, {})
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', delete=False) as output:
            temporary = output.name
            os.fchmod(output.fileno(), 0o600)
        with closing(sqlite3.connect(temporary)) as db, db:
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('CREATE TABLE metadata(value TEXT NOT NULL)')
            db.execute('CREATE TABLE titles(itemid TEXT PRIMARY KEY,title TEXT,type TEXT,year INTEGER,normalized TEXT)')
            def records():
                for row in rows:
                    if cancelled():
                        raise InterruptedError('已取消 Emby 索引建立。')
                    yield (row['_emby_id'], row['title'], {'movie': 'Movie', 'tvshow': 'Series'}[row['media_type']],
                           row['year'], normalize(row['title']))
            db.executemany('INSERT INTO titles VALUES (?,?,?,?,?)', records())
            db.execute('CREATE INDEX exact_title ON titles(normalized,type,year)')
            db.execute('INSERT INTO metadata VALUES (?)', (json.dumps(dict(version=1, signature=before,
                revision=account['_revision'], snapshot=state.get('snapshot'))),))
        if cancelled():
            raise InterruptedError('已取消 Emby 索引建立。')
        if signature(snapshot) != before:
            return
        os.replace(temporary, path)
    except (OSError, sqlite3.Error, ValueError, KeyError):
        pass  # An optional acceleration failure must not hide playable media.
    finally:
        if temporary:
            from pathlib import Path
            Path(temporary).unlink(missing_ok=True)


def search(profile, account, client, request, load, interval):
    from .media_match import normalize
    client.check()
    found = lookup(profile, account, request, interval)
    if found:
        client.check()
        return found, True
    rows = load()
    if found is None:
        # A freshness check can advance only the small cursor. In that case the
        # existing index is still valid and must not normalize all titles again.
        found = lookup(profile, account, request)
        if found is None:
            build(profile, account, rows, client.cancelled if hasattr(client, 'cancelled') else lambda: False)
            found = lookup(profile, account, request)
    if found is None:
        key = normalize(request['title'])
        found = [row for row in rows if normalize(row['title']) == key
                 and (request['kind'] == 'all' or row['media_type'] == request['kind'])
                 and (request.get('year') is None or row['year'] == request['year'])]
    client.check()
    return (found, True) if found else (rows, False)
