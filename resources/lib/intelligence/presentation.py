"""Structured assistant output becomes verified, selectable TV content."""
import json

SCHEMA = {'type': 'object', 'additionalProperties': False,
    'properties': {
        'message': {'type': 'string'},
        'interaction': {'type': 'string', 'enum': ['continue', 'close']},
        'choices': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'properties': {'label': {'type': 'string'}, 'value': {'type': 'string'},
                           'kind': {'type': 'string', 'enum': ['none', 'movie', 'tvshow', 'episode', 'channel', 'juku', 'emby']},
                           'id': {'type': 'integer'}},
            'required': ['label', 'value', 'kind', 'id']}}},
    'required': ['message', 'interaction', 'choices']}


def structured_reply(reply):
    """Recover a reply envelope, including a prose prefix or Markdown fence."""
    try:
        value = json.loads(reply)
    except (ValueError, TypeError):
        pass
    else:
        return value if isinstance(value, dict) and isinstance(value.get('message'), str) else None
    if not isinstance(reply, str):
        return None
    decoder = json.JSONDecoder()
    start = reply.find('{')
    while start >= 0:
        try:
            value, end = decoder.raw_decode(reply, start)
        except ValueError:
            start = reply.find('{', start + 1)
            continue
        # Only recover a complete trailing protocol object. Ordinary JSON examples,
        # nested objects and braces inside message strings stay natural text.
        if (isinstance(value, dict) and isinstance(value.get('message'), str)
                and (isinstance(value.get('speech'), list)
                     or (value.get('interaction') in ('continue', 'close')
                         and isinstance(value.get('choices'), list)))
                and reply[end:].strip() in ('', '```')):
            return value
        start = reply.find('{', end)
    return None


def media_label(media):
    """Use verified metadata for consistent media cards from every backend."""
    kind = media['kind']
    media_type = media.get('media_type') if kind in ('emby', 'juku') else kind
    title = media['title']
    if media_type == 'episode' and media.get('showtitle'):
        title = '{} 第{}季第{}集：{}'.format(media['showtitle'], media.get('season', 0),
                                           media.get('episode', 0), title)
    elif media.get('year'):
        title += '（{}）'.format(media['year'])
    parts = [title, {'movie': '电影', 'tvshow': '电视剧', 'episode': '电视剧',
                     'channel': '直播频道'}.get(media_type, '影视（类型未知）')]
    if kind == 'emby':
        parts.extend(['Emby', str(media.get('server_name') or 'Emby').strip() or 'Emby'])
    elif kind == 'juku':
        parts.append({'private': '巨库私有库', 'online': '巨库在线目录'}.get(media.get('_juku_source'), '巨库'))
    elif kind in ('movie', 'tvshow', 'episode'):
        parts.append('本地媒体库')
    return ' · '.join(parts)


def parse(reply, tools):
    value = structured_reply(reply)
    if value is None:
        value = {'message': reply, 'interaction': 'continue', 'choices': []}
    choices = []
    rows = value.get('choices', [])
    for item in (rows if isinstance(rows, list) else [])[:12]:
        if not isinstance(item, dict) or not isinstance(item.get('label'), str) or not isinstance(item.get('value'), str):
            continue
        row = {'label': item['label'][:120], 'value': item['value'][:1000]}
        if item.get('kind') != 'none':
            media = tools.media.get((item.get('kind'), item.get('id')))
            if not media:
                continue
            row.update(media)
            row['label'] = media_label(media)
        choices.append(row)
    if not choices and not tools.acted and getattr(tools, 'latest_matches', None):
        for media in tools.latest_matches[:12]:
            prefix = '播放我选择的直播频道：' if media['kind'] == 'channel' else '播放我选择的这部影视：'
            choices.append(dict(media, label=media_label(media), value=prefix + media['title']))
    # Model text and scheduled maintenance cannot decide the window's lifetime.
    # The UI checks actual playback or host-verified control completion instead.
    return {'message': value['message'], 'interaction': 'continue', 'choices': choices}
