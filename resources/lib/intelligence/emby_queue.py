"""Build one verified Emby season before changing Kodi's video queue."""
from . import emby
from .emby_playback import episodes, verify
from .emby_versions import compatible, queued


def prepare(client, item, actual, rpc):
    season, number = actual.get('ParentIndexNumber'), actual.get('IndexNumber')
    if type(season) is not int or season < 0 or type(number) is not int or number <= 0:
        raise emby.Unavailable('所选 Emby 剧集缺少有效季集编号，未替换播放列表。')
    series_id = emby.identifier(actual.get('SeriesId'))
    series = client.item(series_id)
    if series.get('Type') != 'Series' or str(series.get('Id')) != series_id:
        raise emby.Unavailable('无法确认 Emby 剧集所属电视剧，未替换播放列表。')
    show = emby.candidate(series, client.account)
    # SeasonId bounds the metadata response on large multi-season libraries.
    # Older servers may omit it; client-side filtering is authoritative either way.
    parent = emby.identifier(actual['SeasonId']) if actual.get('SeasonId') else series_id
    values = client.pages({'IncludeItemTypes': 'Episode', 'ParentId': parent, 'Recursive': 'true',
        'EnableImages': 'true', 'EnableUserData': 'false', 'Fields': 'ProductionYear',
        'SortBy': 'ParentIndexNumber,IndexNumber', 'SortOrder': 'Ascending'}, maximum=10000)
    values = [value for value in values if value.get('ParentIndexNumber') == season]
    rows = episodes(client, show, series, values)
    by_id = {str(value['Id']): value for value in values}
    selected_id = str(item['_emby_id'])
    selected = next((row for row in rows if row['_emby_id'] == selected_id), None)
    if selected is None or selected['episode'] != number:
        raise emby.Unavailable('所选 Emby 剧集已不在该季，请重新查询。')
    # The individually revalidated item includes the freshest source data.
    by_id[selected_id] = actual
    groups = {}
    for row in rows:
        if row['episode'] <= 0:
            raise emby.Unavailable('Emby 该季有剧集缺少集号，未替换播放列表。')
        groups.setdefault(row['episode'], []).append(row)
    chosen = []
    for index in sorted(groups):
        group = groups[index]
        if index == number:
            chosen.append(selected)
            continue
        same_series = [row for row in group if str(by_id[row['_emby_id']].get('SeriesId')) == series_id]
        candidates = same_series or group
        if len(candidates) != 1:
            raise emby.Unavailable('Emby 第{}季第{}集有多个媒体库条目，无法确定整季队列。'.format(season, index))
        chosen.append(candidates[0])
    select_version = compatible(rpc)
    entries = []
    for row in chosen:
        client.check()
        # The list endpoint can omit secondary sources without a source-count
        # hint. Hydrate this season only, so every queued episode keeps all of
        # its versions, stream metadata and the existing selection rules.
        detail = actual if row['_emby_id'] == selected_id else verify(client, row)
        if (detail.get('ParentIndexNumber') != season or detail.get('IndexNumber') != row['episode']
                or str(detail.get('SeriesId')) != str(by_id[row['_emby_id']].get('SeriesId'))):
            raise emby.Unavailable('Emby 剧集季集或归属已变化，请重新查询。')
        entries.append({'item': row, 'source': queued(row, detail, select_version)})
    position = next(index for index, row in enumerate(chosen) if row['_emby_id'] == selected_id)
    return {'entries': entries, 'position': position, 'season': season}


def open_queue(queue, rpc, cancelled):
    import xbmc
    from .emby_local import list_item
    prepared = []
    for row in queue['entries']:
        if cancelled():
            raise InterruptedError('已取消，未替换 Emby 播放列表。')
        prepared.append((row['source']['url'], list_item(row['source'], row['item'])))
    playlist = xbmc.PlayList(1)
    previous = [(playlist[index].getPath(), playlist[index]) for index in range(playlist.size())]
    if cancelled():
        raise InterruptedError('已取消，未替换 Emby 播放列表。')
    try:
        playlist.clear()
        for path, entry in prepared:
            if cancelled():
                raise InterruptedError('已取消建立 Emby 播放列表。')
            playlist.add(path, entry)
        if playlist.size() != len(prepared):
            raise emby.Unavailable('未能建立完整的 Emby 整季播放列表，请重试。')
        if cancelled():
            raise InterruptedError('已取消建立 Emby 播放列表。')
    except Exception:
        playlist.clear()
        for path, entry in previous:
            playlist.add(path, entry)
        raise
    rpc('Player.Open', {'item': {'playlistid': 1, 'position': queue['position']},
                        'options': {'resume': False, 'shuffled': False, 'repeat': 'off'}})
