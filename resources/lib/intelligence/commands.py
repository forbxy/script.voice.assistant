"""Explicit command rules before an agent; media policy lives in its resolver."""
import re
from .command_request import normalize, media_request, movie_request, search_request
from .command_result import outcome
from .media_actions import MediaActions


class Commands:
    _outcome = staticmethod(outcome)

    def __init__(self, tools):
        self.tools = tools
        self.media_actions = MediaActions(tools)

    def process(self, text, selection=None, cancelled=lambda: False, progress=lambda value: None):
        if cancelled():
            raise InterruptedError('已取消。')
        if selection is not None:
            return self.media_actions.selected(text, selection, cancelled, progress)
        from .command_rules import common_request
        common = common_request(text)
        if common is not None:
            return self._common(text, common, cancelled, progress)
        request = search_request(text) or media_request(text)
        if request is None:
            return None
        from .media_resolver import resolve
        return resolve(self.media_actions, text, request, cancelled, progress)

    def _play(self, text, item, resume, cancelled, progress):
        return self.media_actions.play(text, item, resume, cancelled, progress)

    def _common(self, text, request, cancelled, progress):
        if 'continue_kind' in request:
            from .command_resume import resume
            return resume(self, text, request['continue_kind'], cancelled, progress)
        if 'addon' in request:
            from .ui_context import plain
            names = {normalize(request['addon'])}
            query = request['addon']
            if not request.get('literal_name') and query.endswith('插件') and len(query) > 2:
                query = query[:-2].strip()
                names.add(normalize(query))
            result = self.tools.execute('search_addons', {'query': query, 'limit': 30, 'runnable_only': True}, cancelled)
            if not result.get('ok'):
                return self._outcome(text, '插件查询失败：' + result.get('error', '请稍后重试。'))
            def aliases(row):
                name = plain(row.get('name', ''))
                # Edition labels are display qualifiers, e.g. "名称 · CE".
                # Only a whole primary name may omit them; do not fuzzy-launch.
                primary = re.split(r'\s+[·|]\s+', name, maxsplit=1)[0]
                primary = re.sub(r'\s*[（(][^()（）]*[）)]$', '', primary)
                return {normalize(name), normalize(primary), normalize(row.get('addonid', ''))}
            matches = [row for row in result.get('addons', []) if names.intersection(aliases(row))]
            if not matches:
                return None
            if len(matches) != 1 or result.get('next_offset') is not None:
                choices = [{'label': plain(row['name']) + '（' + row['addonid'] + '）',
                            'value': '打开插件 ' + row['addonid'], 'kind': 'none', 'id': 0} for row in matches]
                return self._outcome(text, '找到多个同名插件，请选择。', choices)
            request = {'tool': 'open_addon', 'arguments': {'addonid': matches[0]['addonid']},
                       'message': '正在打开插件：' + plain(matches[0]['name']) + '。'}
        elif 'channel' in request:
            result = self.tools.execute('search_channels', {'query': request['channel']}, cancelled)
            if not result.get('ok'):
                return self._outcome(text, '频道查询失败：' + result.get('error', '请稍后重试。'))
            chosen = result.get('recommended')
            if chosen:
                return self._play(text, chosen, False, cancelled, progress)
            matches = result.get('channels') or []
            if not matches:
                from .media_resolver import resolve
                return resolve(self.media_actions, text, {'title': request['channel'], 'kind': 'channel',
                                           'year': None, 'resume': False,
                                           'allow_fuzzy': (media_request('播放' + request['channel']) or {}).get('allow_fuzzy', False)},
                              cancelled, progress)
            choices = [dict(row, label=row['title'], value='播放' + row['title']) for row in matches]
            return self._outcome(text, '请确认要播放的频道。', choices)
        elif 'volume_delta' in request:
            if cancelled():
                raise InterruptedError('已取消。')
            current = self.tools.rpc('Application.GetProperties', {'properties': ['volume']})['volume']
            target = max(0, min(100, round(current + request['volume_delta'])))
            request = {'tool': 'set_volume', 'arguments': {'volume': target},
                       'message': '音量已设为{}%。'.format(target)}
        progress(self.tools.label(request['tool']))
        result = self.tools.execute(request['tool'], request['arguments'], cancelled)
        trace = [{'name': request['tool'], 'arguments': request['arguments'], 'ok': result.get('ok', True)}]
        if result.get('ok', True):
            message = request['message']
            if request['tool'] == 'upload_logs':
                message += '\n' + result['url']
        else:
            message = '未能执行：' + result.get('error', '请稍后重试。')
        return self._outcome(text, message, trace=trace)
