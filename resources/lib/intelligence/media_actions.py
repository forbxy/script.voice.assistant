"""Present candidates or execute a selected, revalidated media action."""
from .command_request import normalize
from .command_result import outcome
from time import monotonic


class MediaActions:
    _outcome = staticmethod(outcome)

    def __init__(self, tools):
        self.tools = tools

    def _play(self, *args):
        return self.play(*args)

    def selected(self, text, selection, cancelled, progress):
        if not isinstance(selection, dict):
            return self._outcome(text, '所选项目无效，请重新选择。')
        kind, identifier = selection.get('kind'), selection.get('id')
        if kind == 'juku':
            return self.selected_juku(text, selection, cancelled, progress)
        if kind == 'emby':
            remembered = self.tools.media.get(('emby', selection.get('id'))) if type(selection.get('id')) is int else None
            if not remembered or normalize(selection.get('title', '')) != normalize(remembered['title']):
                return self._outcome(text, '这个项目已不在当前查询结果中，请重新选择。')
            return self.play_emby(text, remembered, cancelled, progress)
        if kind not in ('movie', 'episode', 'channel', 'tvshow') or type(identifier) is not int:
            return None
        remembered = self.tools.media.get((kind, identifier))
        if remembered is None or normalize(selection.get('title', '')) != normalize(remembered['title']):
            return self._outcome(text, '这个项目已不在当前查询结果中，请重新选择。')
        if kind == 'tvshow' and not (remembered.get('_command_resume') or remembered.get('_command_tvshow')):
            return None
        if kind in ('movie', 'episode', 'tvshow'):
            name = {'movie': 'Movie', 'episode': 'Episode', 'tvshow': 'TVShow'}[kind]
            actual = self.tools.rpc('VideoLibrary.Get' + name + 'Details', {
                kind + 'id': identifier, 'properties': ['title']}).get(kind + 'details') or {}
            if (actual.get(kind + 'id') != identifier
                    or normalize(actual.get('title', '')) != normalize(remembered['title'])):
                return self._outcome(text, '媒体库项目已变化，请重新查询后再播放。')
        elif kind == 'channel':
            actual = self.tools.rpc('PVR.GetChannelDetails', {'channelid': identifier,
                'properties': ['channel']}).get('channeldetails') or {}
            if (actual.get('channelid') != identifier
                    or normalize(actual.get('channel') or actual.get('label', '')) != normalize(remembered['title'])):
                return self._outcome(text, '直播频道已变化，请重新查询后再播放。')
        if kind == 'tvshow':
            from .command_resume import tvshow
            return tvshow(self, text, remembered, cancelled, progress,
                          resume=remembered.get('_command_resume', True), season=remembered.get('_command_season'))
        return self.play(text, remembered, remembered.get('_command_resume', True), cancelled, progress)

    def play(self, text, item, resume, cancelled, progress):
        progress('正在打开影片…' if item['kind'] != 'channel' else '正在打开直播频道…')
        name = 'play_channel' if item['kind'] == 'channel' else 'play_media'
        arguments = ({'channelid': item['id']} if name == 'play_channel' else
                     {'kind': item['kind'], 'id': item['id'], 'resume': resume})
        result = self.tools.execute(name, arguments, cancelled)
        trace = [{'name': name, 'arguments': arguments, 'ok': result.get('ok', True)}]
        if not result.get('ok', True):
            return self._outcome(text, '未能打开：' + result.get('error', '请重新查询后重试。'), trace=trace)
        # Player.Open acknowledges a request; only the UI's actual full-screen
        # playback check may close the conversation. Do not claim AV has started.
        return self._outcome(text, '正在打开《' + item['title'] + '》。', trace=trace)

    def selected_juku(self, text, selection, cancelled, progress):
        from . import juku
        remembered = self.tools.media.get(('juku', selection.get('id'))) if type(selection.get('id')) is int else None
        if not remembered or normalize(selection.get('title', '')) != normalize(remembered['title']):
            return self._outcome(text, '这个项目已不在当前查询结果中，请重新选择。')
        if not juku.available(self.tools):
            return None  # Removed/disabled Juku stays optional and silent.
        try:
            return self.play_juku(text, remembered, juku.Client(cancelled), cancelled, progress)
        except juku.Unavailable:
            return self._outcome(text, '巨库项目暂时无法读取，请重新查询后重试。')

    def play_juku(self, text, item, client, cancelled, progress):
        from .juku import Unavailable
        from .juku_playback import open_item
        try:
            result = open_item(self.tools, item, client, cancelled, progress)
        except Unavailable:
            raise
        except ValueError as error:
            return self._outcome(text, str(error))
        trace = [{'name': 'juku_play', 'arguments': {'source': item['_juku_source'], 'id': item['_juku_id']}, 'ok': True}]
        return self._outcome(text, result['note'], trace=trace)

    def play_emby(self, text, item, cancelled, progress):
        from .emby_playback import open_item
        try:
            result = open_item(self.tools, item, cancelled, progress)
        except ValueError as error:
            return self._outcome(text, str(error))
        trace = [{'name': 'play_emby', 'arguments': {'id': item['id']}, 'ok': True}]
        return self._outcome(text, result['note'], result.get('choices', []), trace=trace)

    def offer(self, text, request, result, cancelled, progress):
        from . import juku
        from .command_media import artwork
        choices = []
        art_deadline, art_clients = monotonic() + 2, {}
        for candidate in result.rows:
            item = dict(candidate['row'])
            if item['kind'] == 'emby':
                from . import emby
                from .emby_playback import label
                if not result.automatic and monotonic() < art_deadline:
                    try:
                        account = emby.account_for(self.tools.profile, item)
                        key = account['_identity']
                        if key not in art_clients:
                            client = emby.Client(account, cancelled)
                            client.deadline = min(getattr(client, 'deadline', art_deadline), art_deadline)
                            art_clients[key] = client
                        actual = art_clients[key].item(item['_emby_id'])
                        item['thumbnail'] = emby.thumbnail(actual, account)
                    except emby.Unavailable:
                        pass
                item = emby.remember(self.tools, [item], request)[0]
                choices.append(dict(item, label=label(item), value='播放《' + item['title'] + '》'))
                continue
            if item['kind'] == 'juku':
                origin = item['_juku_source']
                item = juku.remember(self.tools, [item], origin, request)[0]
                label = item['title'] + ('（{}）'.format(item['year']) if item.get('year') else '')
                label += ' · ' + ('巨库私有库' if origin == 'private' else '巨库在线目录')
                choices.append(dict(item, label=label, value='播放《' + item['title'] + '》'))
                continue
            if not result.automatic and not result.movie_exact:
                artwork(self.tools, item, cancelled)
            if not result.movie_exact or not result.automatic:
                item['_command_resume'] = request['resume']
            if item['kind'] == 'tvshow':
                item['_command_tvshow'] = True
                item['_command_season'] = request.get('season')
            self.tools.media[(item['kind'], item['id'])] = item
            label = item['title'] + ('（{}）'.format(item['year']) if item.get('year') else '')
            if not result.movie_exact:
                label += ' · ' + {'movie': '电影', 'tvshow': '电视剧', 'channel': '直播频道'}[item['kind']]
            choices.append(dict(item, label=label, value='播放《' + item['title'] + '》'))
        if cancelled():
            raise InterruptedError('已取消查找。')
        self.tools.latest_matches = [self.tools.media[(row['kind'], row['id'])] for row in choices]
        if result.automatic:
            if choices[0]['kind'] == 'emby':
                answer = self.play_emby(text, choices[0], cancelled, progress)
            elif choices[0]['kind'] == 'juku':
                try:
                    answer = self.play_juku(text, choices[0], result.client, cancelled, progress)
                except juku.Unavailable:
                    return None
            else:
                answer = (self.play(text, choices[0], request['resume'], cancelled, progress) if result.movie_exact else
                          self.selected(text, choices[0], cancelled, progress))
        else:
            message = ('找到多个同名影片或版本，请选择要播放的一项。' if result.movie_exact else
                       '找到这些相近的影视或频道，请选择要播放的一项。')
            if result.source != 'local' and not result.complete:
                message += '部分媒体来源查询不完整，可补充完整片名或年份。'
            answer = self._outcome(text, message, choices)
        if answer is not None and result.matching is not None:
            answer['matching'] = result.matching
        return answer
