"""Select one source for Next Gen 12.4's local metadata protocol, without raw playback."""
import re

from .emby_client import ADDON, Unavailable, identifier

MIMES = {'mkv': 'video/x-matroska', 'mp4': 'video/mp4', 'm4v': 'video/mp4', 'avi': 'video/x-msvideo',
         'ts': 'video/mp2t', 'm2ts': 'video/mp2t', 'mts': 'video/mp2t', 'mpg': 'video/mpeg',
         'mpeg': 'video/mpeg', 'webm': 'video/webm', 'mov': 'video/quicktime', 'wmv': 'video/x-ms-wmv',
         'flv': 'video/x-flv', 'm3u8': 'application/vnd.apple.mpegurl', 'iso': 'video/iso'}


def number(value):
    if type(value) is str and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    return value if type(value) is int and 0 < value < 2**63 else 0


def streams(source):
    value = source.get('MediaStreams')
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def choose(sources):
    if any(not isinstance(row, dict) for row in sources):
        raise Unavailable('Emby 媒体版本数据无效，未开始播放。')
    widths = [max([number(source.get('Width'))] + [number(row.get('Width'))
                  for row in streams(source) if row.get('Type') == 'Video']) for source in sources]
    if any(widths):
        index = max(range(len(sources)), key=widths.__getitem__)
        reason = 'resolution'
    else:
        index = max(range(len(sources)), key=lambda i: number(sources[i].get('Size')))
        reason = 'size' if number(sources[index].get('Size')) else 'first'
    return index, {'index': index, 'reason': reason, 'width': widths[index],
                   'bytes': number(sources[index].get('Size'))}


def compatible(rpc):
    try:
        addon = rpc('Addons.GetAddonDetails', {'addonid': ADDON,
                    'properties': ['enabled', 'version']}).get('addon', {})
        if (addon.get('addonid') != ADDON or addon.get('enabled') is not True
                or not re.match(r'^12\.4\.\d+(?:$|[.\-+~_])', str(addon.get('version', '')))):
            return False
        vfs = rpc('Addons.GetAddonDetails', {'addonid': 'vfs.stream.fast',
                  'properties': ['enabled']}).get('addon', {})
        return vfs.get('addonid') == 'vfs.stream.fast' and vfs.get('enabled') is True
    except InterruptedError:
        raise
    except (OSError, ValueError, RuntimeError, AttributeError):
        return False


def field(value, default=''):
    value = value if isinstance(value, str) else default
    return value.replace('\0', '').replace('\1', '').replace('<', '').replace('>', '').replace(':', '<;>')


def codec(row):
    value = str(row.get('Codec') or row.get('CodecTag') or '').lower().replace('-', '')
    if value == 'dts':
        value = {'dts-hd ma': 'dtshd_ma', 'dts-hd hra': 'dtshd_hra'}.get(
            str(row.get('Profile') or '').lower(), value)
    return field(value)


def markers(actual, source):
    per_source = any(isinstance(row, dict) and 'Chapters' in row for row in actual['MediaSources'])
    chapters = source.get('Chapters') if per_source else actual.get('Chapters')
    result = [0, 0, 0]
    mapping = {'IntroStart': 0, 'IntroEnd': 1, 'CreditsStart': 2,
               'Intro Start': 0, 'Intro End': 1, 'End Credits': 2}
    for chapter in chapters if isinstance(chapters, list) else []:
        if isinstance(chapter, dict):
            index = mapping.get(chapter.get('MarkerType') or chapter.get('Name'))
            if index is not None:
                # Next Gen's fields are named *Ticks but this protocol stores seconds.
                result[index] = round(number(chapter.get('StartPositionTicks')) / 10000000)
    return result


def metadata(actual, source):
    path = source.get('Path') or ''
    if (not isinstance(path, str) or len(path) > 32768
            or any(token in path for token in ('<>', '<<', '><', '<;>', '\r', '\n', '\0'))):
        raise Unavailable('Emby 媒体版本路径格式无效，未开始播放。')
    base = [field(source.get('Name'), 'unknown'), str(number(source.get('Size'))),
            identifier(source.get('Id')), path.replace(':', '<;>'),
            *(str(value) for value in markers(actual, source)),
            # Match 12.4's serializer exactly: its wire flag is set only for
            # the string "true". A JSON boolean otherwise enables an extra
            # remote-source verification/transcode branch absent in Next Gen.
            str(int(source.get('IsRemote') == 'true'))]
    video, audio, subtitles = [], [], []
    for row in streams(source):
        index = str(number(row.get('Index')))
        bitrate = str(number(row.get('BitRate')))
        kind = row.get('Type')
        if kind == 'Video':
            video.append(':'.join([codec(row), bitrate, index, str(number(row.get('Width')))]))
        elif kind in ('Audio', 'Default'):
            audio.append(':'.join([field(row.get('DisplayTitle'), 'unknown'), codec(row), bitrate, index]))
        elif kind == 'Subtitle':
            subtitles.append(':'.join([field(row.get('Language'), 'undefined'),
                field(row.get('DisplayTitle'), 'undefined'), str(int(row.get('IsExternal') is True)),
                index, codec(row), str(int(row.get('IsForced') is True))]))
    return '<<'.join([':'.join(base), '><'.join(video), '><'.join(audio), '><'.join(subtitles)])


def prepare(item, actual, rpc):
    sources = actual.get('MediaSources')
    if not isinstance(sources, list) or len(sources) < 2 or not compatible(rpc):
        return None
    index, selection = choose(sources)
    return entry(item, actual, [sources[index]], selection)


def queued(item, actual, select_version):
    """Queue normal sources too; the local Next Gen handler still resolves them."""
    sources = actual.get('MediaSources')
    if not isinstance(sources, list) or not sources or any(not isinstance(row, dict) for row in sources):
        raise Unavailable('Emby 剧集缺少可播放的媒体源，未替换播放列表。')
    if len(sources) > 1 and select_version:
        index, selection = choose(sources)
        return entry(item, actual, [sources[index]], selection)
    return entry(item, actual, sources)


def entry(item, actual, sources, selection=None):
    source = sources[0]
    encoded = '<>'.join(metadata(actual, row) for row in sources).encode('utf-8').hex().upper()
    path = source.get('Path') or ''
    container = str(source.get('Container') or actual.get('Container') or '').lower()
    if path.split('?', 1)[0].lower().endswith('.iso'):
        container = 'iso'
    # STRM describes the library pointer, not the real video container. Never
    # open our proxy URL as another STRM/text file or trust its synthetic HLS HEAD.
    if container not in MIMES:
        container = 'mkv'
    # Keep the normal stream handler even for ISO. Next Gen's "i" handler
    # replaces a synced database ListItem; our dynamic entry has no Kodi DB ID.
    # The .iso suffix lets Kodi open the selected disc through VFS instead.
    media = 'e' if item['media_type'] == 'episode' else 'm'
    group = 'tvshows' if item['media_type'] == 'episode' else 'movies'
    http = 'http/' if path.lower().startswith(('http://', 'https://')) else ''
    parent = identifier(actual.get('SeriesId') or '0') if group == 'tvshows' else '0'
    # Next Gen still initializes its playback session, subtitles and markers,
    # and applies its live followhttp setting to these local proxy entries.
    url = ('http://127.0.0.1:57342/dynamic/' + http + group + '/' + identifier(item['_emby_server'])
           + '/0/' + parent + '/' + identifier(item['_emby_id']) + '/' + media + '-0-0-'
           + ('m' if len(sources) > 1 else '') + '-' + encoded
           + '/unknown.' + container + '|redirect-limit=1000&failonerror=false')
    return {'url': url, 'path': path if len(sources) == 1 else '',
            'mime_type': MIMES[container], 'selection': selection}
