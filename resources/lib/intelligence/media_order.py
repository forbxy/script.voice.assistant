"""One configured source order for command routing and both agent prompts."""


def sources(tools):
    order = ('juku', 'emby') if getattr(tools, 'media_search_order', 'emby_juku') == 'juku_emby' else ('emby', 'juku')
    return tuple(name for name in order if getattr(tools, 'media_search_' + name, True))


def instruction(tools):
    names = {'emby': 'Emby 动态库', 'juku': '巨库（先私有库、再在线目录）'}
    return ('\n当前媒体搜索顺序：' + ' → '.join(['Kodi 本地媒体库'] + [names[name] for name in sources(tools)])
            + '。先使用顺序在前的已提供来源工具；用户明确指定来源时按用户要求。未提供的来源静默跳过。'
            '已关闭的来源不查询，也不要通过终端或其他接口绕过媒体搜索设置。'
            '近似候选仍按片名/读音和年份核对，不能因来源靠前就播放不相近的作品。')
