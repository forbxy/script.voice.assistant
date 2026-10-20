"""One bounded HTTP request in a child process so Kodi can cancel immediately.

The private key arrives through stdin, never the command line or logs.
"""
import json
import sys
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(value):
    req = urllib.request.Request(value['url'], json.dumps(value['payload']).encode('utf-8'),
        {'Authorization': 'Bearer ' + value['api_key'], 'Content-Type': 'application/json',
         'Accept': 'application/json'})
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(req, timeout=value['timeout']) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return {'error': 'AI 返回的数据过大。'}
        return {'response': json.loads(raw)}
    except urllib.error.HTTPError as error:
        messages = {401: 'API Key 无效或已失效。', 402: 'DeepSeek 余额不足。',
                    429: 'AI 服务繁忙，请稍后再试。', 400: '请检查 AI 接口地址和模型名称。'}
        return {'error': messages.get(error.code, 'AI 请求失败，请稍后再试。') + ' (HTTP {})'.format(error.code)}
    except Exception:
        # Provider responses/exceptions can contain credentials or prompt text.
        return {'error': 'AI 连接失败或返回格式无效，请检查网络、接口地址和模型。'}


if __name__ == '__main__':
    try:
        result = request(json.load(sys.stdin))
    except Exception:
        result = {'error': 'AI 请求配置无效。'}
    print(json.dumps(result, ensure_ascii=False))
