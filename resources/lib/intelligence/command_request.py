"""Parse complete media commands without searching or changing player state."""
import re
import unicodedata


def normalize(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).casefold()


def search_request(text):
    """Only an explicit library scope makes a search a local media command."""
    if not isinstance(text, str) or len(text) > 500:
        return None
    prefix = r'(?:请|麻烦)?(?:你)?\s*(?:帮我|给我)?\s*(?:我想要|我想|我要|想)?\s*'
    library = r'(?P<library>媒体库|影视库|电影库|电视剧库|片库|库)(?:里面|里|中)?\s*'
    scoped_library = r'(?P<library>媒体库|影视库|电影库|电视剧库|片库|库(?=\s|里|中|《))(?:里面|里|中)?\s*'
    verb = r'(?:搜索|搜搜|搜|查找|查询|查查|查|找找|找)(?:一下|下)?\s*'
    patterns = (
        r'(?:(?:看看|看)(?:一下|下)?\s*|' + verb + r')?' + library + r'(?:有没有|是否有)\s*',
        r'(?:从|在)\s*' + library + r'(?:帮我|给我)?\s*' + verb,
        r'搜索\s*' + library,
        verb + scoped_library,
        library + verb,
        library + r'有\s*',
    )
    value = text.strip()
    for pattern in patterns:
        match = re.match(prefix + pattern, value)
        if match:
            break
    else:
        return None
    # Consume the complete command prefix before validating the remaining title;
    # otherwise a bare "从库里搜索" can backtrack and treat "索" as a title.
    name = value[match.end():].strip().rstrip('。!！?？').strip()
    name = re.sub(r'(?:吗|么|呢)$', '', name).strip()
    request = media_request('播放' + name)
    if request is None or not request['resume'] or not request['allow_fuzzy']:
        return None
    kind = {'电影库': 'movie', '电视剧库': 'tvshow'}.get(match['library'])
    if kind:
        if request['kind'] not in ('all', kind):
            return None
        request['kind'] = kind
    return dict(request, search_only=True)


def media_request(text):
    """Accept a whole, explicit play utterance, never a keyword within a task."""
    if not isinstance(text, str) or len(text) > 500:
        return None
    from .command_rules import RESTART_ACTION, RESTART_SUFFIX, NUM, number
    value = text.strip()
    prefix = r'(?:请|麻烦)?(?:帮我|给我)?(?:我想要|我想|我要|想)?'
    match = re.fullmatch(
        prefix + r'(?P<verb>' + RESTART_ACTION + r'|继续播放|接着播放|继续看|接着看|播放|观看|看)'
        r'(?:一下)?\s*(?P<name>.+?)\s*[。!！]?', value)
    if match:
        restart = bool(re.fullmatch(RESTART_ACTION, match['verb']))
        name = match['name'].strip()
        tail = re.fullmatch(r'(?P<name>.+?)\s*[,，]?\s*' + RESTART_SUFFIX, name)
        if tail:
            name, restart = tail['name'].strip(), True
    else:
        # "把《片名》从头播放" / "《片名》再看一遍".
        tail = re.fullmatch(prefix + r'(?:(?:把|将)(?P<name>.+?)|(?P<quoted>《[^《》\r\n]+》))\s*[,，]?\s*'
                            + RESTART_SUFFIX + r'\s*[。!！]?', value)
        if not tail:
            return None
        name, restart = (tail['name'] or tail['quoted']).strip(), True
    # A negative/compound tail must not become a near-matching title after
    # stripping the restart phrase. Exact literal titles may still end in 别.
    unclear_restart = restart and tail and re.search(
        r'(?:不要|不用|不必|不想|不能|不需要|别|然后|之后|之前|并且|并|再|接着)$', name)
    season = None
    # A season can precede or follow the title. Book-title marks keep a season
    # inside the quoted title literal; only an outside suffix is syntax.
    selected_season = re.fullmatch(r'(?:电视剧\s*)?第(?P<season>' + NUM + r')季\s*(?P<name>.+)', name)
    if selected_season is None:
        selected_season = re.fullmatch(r'(?:电视剧\s*)?(?P<name>.+?)\s*第(?P<season>' + NUM
            + r')季(?P<year>\s*[（(]\d{4}[）)])?', name)
    if selected_season:
        season = number(selected_season['season'])
        if season is None or not 0 <= season <= 1000:
            return None
        name = (selected_season['name'] + (selected_season.groupdict().get('year') or '')).strip()
    # Quoting makes a title containing punctuation explicit. Extra instructions
    # outside the closing book-title marks must still go to the agent.
    quoted = re.fullmatch(r'(?:(?P<kind>电影|电视剧|直播频道|电视频道|频道|电视)\s*)?《(?P<title>[^《》\r\n]{1,200})》'
                          r'(?:\s*[（(](?P<year>\d{4})[）)])?', name)
    if quoted:
        title, year = quoted['title'], quoted['year']
        kind = quoted['kind']
        literal_title = title
    else:
        if any(mark in name for mark in '《》，,。；;？?！!\r\n'):
            return None
        unquoted = re.fullmatch(r'(?P<title>[^()（）]{1,200}?)(?:\s*[（(](?P<year>\d{4})[）)])?', name)
        if not unquoted:
            return None
        title, year = unquoted['title'].strip(), unquoted['year']
        literal_title = title
        if title in ('电影', '影片', '电视剧', '电视', '直播', '这部', '这部电影', '这个'):
            return None
        if title.startswith(('一部', '几部', '一些', '随便', '随机')):
            return None
        typed = re.fullmatch(r'(?P<kind>电视剧|直播频道|电视频道|频道|电影)\s*(?P<title>.+)', title)
        kind = typed['kind'] if typed else None
        if typed:
            title = typed['title'].strip()
            if title.startswith(('一部', '几部', '一些', '随便', '随机')):
                return None
    if not title.strip() or year and not 1800 <= int(year) <= 2200:
        return None
    if season is not None and kind not in (None, '电视剧'):
        return None
    request = {'title': title.strip(), 'literal_title': literal_title, 'year': int(year) if year else None,
            'kind': {'电影': 'movie', '电视剧': 'tvshow', '频道': 'channel', '直播频道': 'channel',
                     '电视频道': 'channel', '电视': 'channel'}.get(kind, 'all'),
            'resume': not restart,
            'allow_fuzzy': not unclear_restart and (bool(quoted) or not re.search(
                r'(?:然后|之后|之前|并且|并|再|接着)(?:请|帮我|给我|把)?'
                r'(?:打开|关闭|播放|暂停|停止|关机|重启|睡眠|挂起|静音|音量|字幕)', title))}
    if season is not None:
        request.update(kind='tvshow', season=season)
    return request

def movie_request(text):
    request = media_request(text)
    return {key: request[key] for key in ('title', 'year', 'resume')} if request else None
