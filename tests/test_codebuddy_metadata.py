import base64
from io import BytesIO
from email.message import Message
import hashlib
import json
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, HTTPSHandler, Request
from urllib.response import addinfourl

import pytest

from resources.lib import codebuddy_metadata as metadata
from resources.lib.codebuddy_account import CodeBuddyAccount
from resources.lib.codebuddy_regions import home_path


@pytest.mark.parametrize('value,result', [('x0.00 credits','0'), ('x0.06','0.06'), ('×1.50','1.5'),
    ('0 倍','0'), (0,'0'), (2.5,'2.5'), ('',None), (None,None), (False,None), ('NaN',None),
    ('Infinity',None), ('x-1',None), ('about 0',None), ('x0 credits later 5',None)])
def test_model_multiplier_requires_explicit_valid_number(value, result):
    assert metadata.multiplier(value) == result


def test_credit_summary_matches_page_remaining_sum_without_double_counting_groups():
    value = {'code': 0, 'data': {'SubscriptionPackageName': 'Free', 'Packages': [
        {'CycleRemainCapacity': '245.28000001', 'CycleTotalCapacity': '250', 'CapacityUnit': 'credit'},
        {'CycleRemainCapacity': '30', 'CapacityUnit': 'credits'},
        {'CycleRemainCapacity': '100', 'CapacityUnit': 'credits'},
        {'CycleRemainCapacity': '999', 'CapacityUnit': 'token'}],
        'CreditGroups': [{'CycleRemainCapacity': '375.28000001'}]}}
    assert metadata.summary(value)['remaining'] == '375.28000001'
    value['data']['TotalCredits'] = '1994.89'
    assert metadata.summary(value)['remaining'] == '1994.89'
    value['data']['TotalCredits'] = '0'
    assert metadata.summary(value)['remaining'] == '0'
    assert metadata.summary({'code':0,'data':{'Packages':[]}})['remaining'] == '0'


@pytest.mark.parametrize('value', [{}, {'code':403,'data':{}}, {'code':0,'data':{}},
    {'code':0,'data':{'Packages':[{'CapacityUnit':'credits','CycleRemainCapacity':'NaN'}]}},
    {'code':0,'data':{'Packages':[{'CapacityUnit':'credits','CycleRemainCapacity':'-5'}]}}])
def test_invalid_credit_payload_is_not_reported_as_zero(value):
    with pytest.raises(ValueError): metadata.summary(value)


def cache(profile, region, entries):
    folder = home_path(profile, region) / 'local_storage'
    folder.mkdir(parents=True)
    filename = 'entry_' + hashlib.md5(b'cloud_product_config_cache').hexdigest() + '.info'
    (folder / filename).write_text(json.dumps(entries))


def test_model_cache_only_uses_matching_region_and_account(tmp_path):
    cache(tmp_path,'internal',[{'userId':'other','data':{'models':[{'id':'free','credits':'x999'}]}},
        {'userId':'current','data':{'models':[{'id':'free','credits':'x0.00 credits'},
        {'id':'paid','credits':'x0.06 credits'}, {'id':'unknown','credits':None}]}}])
    cache(tmp_path,'external',[{'userId':'current','data':{'models':[{'id':'paid','credits':'x3.31'}]}}])
    assert metadata.model_prices(tmp_path,'internal',{'userId':'current'}) == {'free':'0','paid':'0.06','unknown':None}
    assert metadata.model_prices(tmp_path,'external',{'userId':'current'}) == {'paid':'3.31'}
    assert metadata.model_prices(tmp_path,'internal',{'userId':'missing'}) == {}
    assert metadata.model_prices(tmp_path,'internal',{}) == {}


@pytest.mark.parametrize('region,host',[('internal','copilot.tencent.com'),('external','www.codebuddy.ai')])
def test_credit_request_has_required_auth_headers_and_keeps_credentials_out_of_result(monkeypatch,region,host):
    calls = []
    class Opener:
        def open(self, request, **kwargs):
            calls.append(request)
            return BytesIO(b'{"code":0,"data":{"TotalCredits":"0"}}')
    monkeypatch.setattr(metadata,'build_opener',lambda *args:Opener())
    result = metadata.credits({'userId':'private-uid','token':'private-token','enterpriseId':'private-enterprise'},region)
    request = calls[0]
    assert request.full_url == 'https://' + host + metadata.SUMMARY_PATH
    assert request.data == b'{}' and request.get_header('Authorization') == 'Bearer private-token'
    assert request.get_header('X-auth-method') == region
    userinfo = json.loads(base64.b64decode(request.get_header('X-userinfo')))
    assert userinfo == {'uin':'private-uid','token_source':region,'owner_uin':'private-enterprise'}
    assert result['remaining'] == '0' and 'private' not in json.dumps(result)


def test_credit_redirect_cannot_forward_credentials_to_another_origin():
    calls = []
    class Redirect(HTTPSHandler):
        def https_open(self, request):
            calls.append(request.full_url)
            headers = Message()
            headers['Location'] = 'https://other.example/'
            response = addinfourl(BytesIO(), headers, request.full_url, 302)
            response.msg = 'Found'
            return response
    opener = build_opener(Redirect(), metadata.NoRedirect())
    with pytest.raises(HTTPError):
        opener.open(Request('https://www.codebuddy.ai/test', headers={'Authorization':'Bearer private-token'}))
    assert calls == ['https://www.codebuddy.ai/test']


def test_credit_errors_do_not_leak_response_or_proxy_and_cancel_does_not_start_request(monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            raise URLError('http://proxy-user:secret@proxy.test/private-token')
    monkeypatch.setattr(metadata,'build_opener',lambda *args:Opener())
    result = metadata.credits({'userId':'user','token':'private-token'},'external')
    assert result == {'error':'积分暂时无法查询，请检查网络后刷新。'}
    with pytest.raises(InterruptedError):
        metadata.credits({},'internal',cancelled=lambda:True)


def test_account_status_projects_prices_and_balance_without_serializing_auth(tmp_path, monkeypatch):
    account = CodeBuddyAccount.__new__(CodeBuddyAccount)
    account.profile, account.region, account.proxy, account.cancelled = tmp_path,'external','',lambda:False
    account.initialize = lambda: {'account':{'userId':'user','token':'private-token'},'currentModelId':'free',
        'models':[{'id':'free','name':'Free Model'}, {'id':'paid','name':'Paid Model','credits':'x1.5'}]}
    cache(tmp_path,'external',[{'userId':'user','data':{'models':[{'id':'free','credits':'x0.00'}]}}])
    monkeypatch.setattr(metadata,'credits',lambda *args:{'remaining':'12.5','updated_at':123})
    value = account.status()
    assert value['models'] == [{'model':'free','displayName':'Free Model','multiplier':'0'},
                              {'model':'paid','displayName':'Paid Model','multiplier':'1.5'}]
    assert value['default_model'] == 'free' and value['credits']['remaining'] == '12.5'
    assert 'private-token' not in json.dumps(value) and 'userId' not in json.dumps(value)
    account.initialize = lambda:{'models':[]}
    monkeypatch.setattr(metadata,'credits',lambda *args:pytest.fail('Logged-out account must not query credits'))
    assert account.status() == {'logged_in':False,'models':[],'credits':None}
