"""Whole-title source priority, then one shared ranking of all near-matches."""
from . import command_media, juku, emby
from .media_match import rank, diagnostics, normalize, origin
from .media_results import SearchResult


def projected(result):
    if result.source == 'local':
        return [candidate['row'] for candidate in result.rows]
    if result.source == 'emby':
        return result.rows
    return juku.candidates(result.rows, result.source)


def exact_result(result, request, preceding_complete=True, cancelled=lambda: False):
    title = normalize(request['title'])
    rows, automatic = rank(request['title'],
        [row for row in projected(result) if normalize(row['title']) == title],
        request.get('year'), cancelled)
    complete = result.complete and preceding_complete
    automatic = automatic and complete
    return SearchResult(source=result.source, rows=rows, automatic=automatic, exact=True,
                        complete=complete, client=result.client,
                        matching=diagnostics(request['title'], rows, automatic))


def preferred_sources(rows):
    """Keep same-source versions; don't count another source as a rival title."""
    preferred, result = {}, []
    # Input is already in local/private/online preference order. Unknown years
    # or types cannot establish equivalence and must remain separate choices.
    for row in rows:
        source = origin(row)
        kind = (juku.media_type(row) if row['kind'] == 'juku' else
                row['media_type'] if row['kind'] == 'emby' else row['kind'])
        year = row.get('year')
        if kind in ('movie', 'tvshow') and type(year) is int and year > 0:
            key = (kind, normalize(row['title']), year)
            if key in preferred and preferred[key] != source:
                continue
            preferred[key] = source
        result.append(row)
    return result


def choose(local, private, online, request, cancelled=lambda: False):
    return choose_sources([local, private, online], request, cancelled)


def choose_sources(sources, request, cancelled=lambda: False):
    """A source never hides a better or similarly scored near-match elsewhere."""
    local = sources[0]
    if local.exact:
        return local
    complete = local.complete
    for result in sources[1:]:
        if result.exact:
            return exact_result(result, request, complete, cancelled)
        complete = complete and result.complete
    if not any(result.rows for result in sources[1:]):
        return local
    candidates = [row for result in sources for row in projected(result)]
    rows, automatic = rank(request['title'], preferred_sources(candidates), request.get('year'), cancelled,
                           source_order=[result.source for result in sources])
    # Source preference only orders equal scores; close scores still require a
    # choice. An unavailable Juku source must not disable Kodi's own fallback.
    if rows and rows[0]['row']['kind'] in ('juku', 'emby'):
        automatic = automatic and complete
    return SearchResult(source='mixed', rows=rows, automatic=automatic, complete=complete,
                        client=next((result.client for result in sources if result.client), None),
                        matching=diagnostics(request['title'], rows, automatic))


def resolve(actions, text, request, cancelled, progress):
    local = command_media.search(actions.tools, request, cancelled, progress)
    chosen = local
    sources = [local]
    if not local.exact and request['kind'] != 'channel' and request.get('allow_fuzzy', True):
        from .media_order import sources as source_order
        for name in source_order(actions.tools):
            source = emby.Source(actions.tools, cancelled) if name == 'emby' else juku.Source(actions.tools, cancelled)
            if not source.enabled:
                continue
            if name == 'emby':
                progress('正在查找 Emby 动态库…')
                sources.append(source.search(request))
            else:
                progress('正在查询巨库私有库…')
                sources.append(source.search('private', request))
                if not sources[-1].exact:
                    sources.append(source.search('online', request))
            if sources[-1].exact:
                break
        chosen = choose_sources(sources, request, cancelled)
        if not chosen.rows and any(not result.complete for result in sources):
            progress('部分媒体来源暂时不可用，' + ('正在汇总查询结果…' if request.get('search_only') else
                     '继续本地匹配…' if local.rows else '转交智能体处理…'))
    if cancelled():
        raise InterruptedError('已取消查找。')
    if not chosen.rows:
        if request.get('search_only'):
            actions.tools.latest_matches = []
            message = ('部分媒体来源查询不完整，暂未找到匹配项，请稍后重试。' if any(not row.complete for row in sources) else
                       '没有找到相近的影视或频道，请换个片名或补充年份。')
            return actions._outcome(text, message)
        return None
    answer = actions.offer(text, request, chosen, cancelled, progress)
    if answer is None and chosen is not local and local.rows:
        return actions.offer(text, request, local, cancelled, progress)
    return answer
