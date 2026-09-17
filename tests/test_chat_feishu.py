from teammem.chat.feishu import normalize_event, session_key
import json
from types import SimpleNamespace


def test_person_mention_survives_normalization_in_dm():
    event = normalize_event({'message': {'message_type': 'text',
        'content': json.dumps({'text': 'What did @_user_1 do yesterday?'}),
        'mentions': [{'key': '@_user_1', 'id': {'open_id': 'ou_sam'}, 'name': 'Sam Lee'}]}})
    assert event.text == 'What did Sam Lee do yesterday?'
    assert event.mentions == frozenset({'ou_sam'})


def test_mention_can_touch_chinese_text_without_a_space():
    event = normalize_event({'message': {'message_type': 'text',
        'content': json.dumps({'text': '@_user_1昨天做了什么？'}),
        'mentions': [{'key': '@_user_1', 'name': '小林'}]}})
    assert event.text == '小林昨天做了什么？'


def test_only_configured_bot_is_removed_from_sdk_mentions():
    mentions = [SimpleNamespace(key='@_user_1', id=SimpleNamespace(open_id='ou_bot'), name='Team Bot'),
                SimpleNamespace(key='@_user_10', id=SimpleNamespace(open_id='ou_sam'), name='Sam Lee')]
    message = SimpleNamespace(message_type='text', mentions=mentions,
        content=json.dumps({'text': '@_user_1 What did @_user_10 do? @_user_10'}))
    event = normalize_event(SimpleNamespace(message=message), own_bot_open_id='ou_bot')
    assert event.text == 'What did Sam Lee do? Sam Lee'
    assert event.mentions == frozenset({'ou_bot', 'ou_sam'})


def test_unknown_mentions_are_not_erased_or_replaced_recursively():
    event = normalize_event({'message': {'message_type': 'text',
        'content': json.dumps({'text': '@_user_1 / @_user_2 / @_user_3'}),
        'mentions': [{'key': '@_user_1', 'name': '@_user_2'},
                     {'key': '@_user_2', 'name': 'Sam'}, {'key': '@_user_3'}]}})
    assert event.text == '@_user_2 / Sam / @_user_3'


def test_bot_mention_does_not_break_session_commands():
    event = normalize_event({'message': {'message_type': 'text',
        'content': json.dumps({'text': '@_user_1 /forget'}),
        'mentions': [{'key': '@_user_1', 'id': {'open_id': 'ou_bot'}, 'name': 'Team Bot'}]}},
        own_bot_open_id='ou_bot')
    assert event.text == '/forget'


def test_group_event_uses_thread_root_and_exact_open_id_mentions():
    event = normalize_event({"tenant": "t", "app": "a", "message": {"message_id": "m", "chat_id": "chat", "chat_type": "group", "root_id": "root", "message_type": "text", "content": '{"text":"hi"}'}, "sender": {"sender_id": {"open_id": "alice"}}, "mentions": [{"id": {"open_id": "ou_bot"}}]})

    assert session_key(event).root == "root"
    assert event.mentions == frozenset({"ou_bot"})
    assert event.text == "hi"


def test_malformed_content_is_not_interpreted_as_a_text_message():
    event = normalize_event({"tenant": "t", "app": "a", "message": {"message_id": "m", "chat_id": "chat", "chat_type": "p2p", "message_type": "text", "content": "not json"}, "sender": {"sender_id": {"open_id": "alice"}}})

    assert event.text is None


def test_real_sdk_envelope_reads_header_and_file_content():
    event = normalize_event({'header':{'app_id':'app','tenant_key':'tenant'}, 'event':{
        'sender':{'sender_type':'user','sender_id':{'open_id':'alice'}},
        'message':{'message_id':'m','chat_id':'c','chat_type':'p2p','message_type':'file',
                   'content':'{"file_key":"file_123","file_name":"notes.pdf"}'}}})
    assert event.app == 'app' and event.tenant == 'tenant'
    assert event.resources == ({'file_key':'file_123','filename':'notes.pdf','type':'file'},)


def test_malformed_json_array_does_not_crash_normalization():
    event = normalize_event({'message':{'message_type':'text','content':'[]'}})
    assert event.text is None


def test_resource_key_is_bound_to_exact_source_chat(monkeypatch):
    import pytest
    from teammem.chat.feishu import FeishuClient, FeishuError
    client = FeishuClient('app','secret')
    monkeypatch.setattr(client, '_json', lambda *a,**kw:{'data':{'items':[{
        'message_id':'m','chat_id':'other','msg_type':'file','body':{'content':'{"file_key":"f","file_name":"x.pdf"}'}}]}})
    with pytest.raises(FeishuError,match='conversation'):
        client.message_resources('m','authorized-chat')


def test_resource_download_rejects_wrong_key_before_network(monkeypatch):
    import pytest, time
    from teammem.chat.feishu import FeishuClient, FeishuError
    client = FeishuClient('app','secret')
    monkeypatch.setattr(client,'message_resources',lambda *a:({},({'file_key':'real','filename':'x.txt','type':'file'},)))
    with pytest.raises(FeishuError,match='belong'):
        list(client.download_resource('m','chat',{'file_key':'wrong','filename':'x.txt','type':'file'},max_bytes=100,deadline=time.monotonic()+5))


def test_replies_preserve_stable_uuid_and_original_message(monkeypatch):
    from teammem.chat.feishu import FeishuClient
    client = FeishuClient('app','secret')
    calls = []
    monkeypatch.setattr(client,'get_message',lambda m,c:calls.append(('verify',m,c)))
    def send(method,path,**kwargs):
        calls.append((method,path,kwargs))
        return {'data':{'message_id':'reply'}}
    monkeypatch.setattr(client,'_json',send)
    assert client.send_reply('chat','m','stable-uuid','answer') == 'reply'
    assert calls[0] == ('verify','m','chat')
    assert calls[1][2]['json']['uuid'] == 'stable-uuid'
    assert calls[1][1] == '/im/v1/messages/m/reply'


def test_actual_bot_info_must_match_expected_identity(monkeypatch):
    import pytest
    from teammem.chat.feishu import FeishuClient, FeishuError
    client = FeishuClient('app','secret')
    monkeypatch.setattr(client,'_json',lambda *a,**kw:{'bot':{'activate_status':2,'open_id':'correct'}})
    assert client.verify_identity('correct')
    with pytest.raises(FeishuError):
        client.verify_identity('other-bot')


def test_typing_reaction_uses_exact_message_endpoint_and_returned_identity(monkeypatch):
    from teammem.chat.feishu import FeishuClient
    client = FeishuClient('app', 'secret')
    calls = []
    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {'data': {'reaction_id': 'reaction-1'}}
    monkeypatch.setattr(client, '_json', request)

    assert client.create_reaction('om/source', 'Typing') == 'reaction-1'
    assert calls == [('POST', '/im/v1/messages/om%2Fsource/reactions', {
        'json': {'reaction_type': {'emoji_type': 'Typing'}}, 'deadline': None,
    })]


def test_reaction_delete_uses_exact_message_and_reaction_id(monkeypatch):
    from teammem.chat.feishu import FeishuClient
    client = FeishuClient('app', 'secret')
    calls = []
    monkeypatch.setattr(client, '_json', lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {'data': {}})

    client.delete_reaction('om/source', 'reaction/id')

    assert calls == [('DELETE', '/im/v1/messages/om%2Fsource/reactions/reaction%2Fid', {'deadline': None})]


def test_reply_embeds_citation_url_in_native_link_and_preserves_thread(monkeypatch):
    import json
    from teammem.chat.feishu import FeishuClient
    client = FeishuClient('app', 'secret')
    monkeypatch.setattr(client, 'get_message', lambda *_: {'root_id':'thread-root'})
    sent = []
    monkeypatch.setattr(client, '_json', lambda *a, **kw: sent.append(kw['json']) or {'data':{'message_id':'reply'}})
    text = "Released [E1].\n\n[E1] [project \\[draft\\] · 2026-09-15](https://example.com/a%28b%29)"
    client.send_reply('chat', 'message', 'stable-uuid', text)
    payload = sent[0]
    assert payload['msg_type'] == 'post' and payload['reply_in_thread'] is True
    assert payload['uuid'] == 'stable-uuid'
    rows = json.loads(payload['content'])['en_us']['content']
    assert rows[0] == [{'tag':'text', 'text':'Released [E1].'}]
    assert rows[2] == [{'tag':'text', 'text':'[E1] '},
                       {'tag':'a', 'text':'project [draft] · 2026-09-15', 'href':'https://example.com/a%28b%29'}]
    assert all('https://' not in node.get('text', '') for row in rows for node in row)


def test_conflicting_sender_tenant_cannot_inherit_event_tenant():
    event = normalize_event({'header': {'app_id': 'app', 'tenant_key': 'our-tenant'},
        'event': {'sender': {'sender_type': 'user', 'tenant_key': 'other-tenant',
                            'sender_id': {'open_id': 'external-member'}},
                  'message': {'message_id': 'm', 'chat_id': 'g', 'chat_type': 'group',
                              'message_type': 'text', 'content': '{"text":"Hi"}'}}})
    assert event.tenant == ''


def test_sender_profile_uses_exact_open_id_and_caches_verified_result(monkeypatch):
    from teammem.chat.feishu import FeishuClient
    client = FeishuClient('app', 'secret')
    calls = []
    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {'data': {'user': {'open_id': 'ou_sender', 'name': 'Sam Lee',
                                  'en_name': 'Sam', 'email': 'excluded@example.test'}}}
    monkeypatch.setattr(client, '_json', request)
    first = client.get_sender_profile('ou_sender')
    assert first == {'open_id': 'ou_sender', 'name': 'Sam Lee', 'en_name': 'Sam'}
    first['name'] = 'Changed by caller'
    assert client.get_sender_profile('ou_sender')['name'] == 'Sam Lee'
    assert len(calls) == 1
    assert calls[0][1] == '/contact/v3/users/ou_sender'
    assert calls[0][2]['params'] == {'user_id_type': 'open_id'}


def test_sender_profile_rejects_mismatched_ids_and_permission_failure(monkeypatch):
    from teammem.chat.feishu import FeishuClient, FeishuError
    client = FeishuClient('app', 'secret')
    monkeypatch.setattr(client, '_json', lambda *a, **kw: {'data': {'user': {
        'open_id': 'someone-else', 'name': 'Another Person'}}})
    assert client.get_sender_profile('ou_sender') is None
    def denied(*args, **kwargs):
        raise FeishuError('permission denied')
    monkeypatch.setattr(client, '_json', denied)
    assert client.get_sender_profile('ou_new_sender') is None


def test_sender_profile_retries_after_permission_failure_cache_expires(monkeypatch):
    from teammem.chat import feishu
    client = feishu.FeishuClient('app', 'secret')
    clock = [100.0]
    monkeypatch.setattr(feishu.time, 'monotonic', lambda: clock[0])
    def denied(*args, **kwargs):
        raise feishu.FeishuError('not enabled yet')
    monkeypatch.setattr(client, '_json', denied)
    assert client.get_sender_profile('ou_sender') is None
    monkeypatch.setattr(client, '_json', lambda *a, **kw: {'data': {'user': {
        'open_id': 'ou_sender', 'name': 'Sam Lee'}}})
    assert client.get_sender_profile('ou_sender') is None
    clock[0] += 16
    assert client.get_sender_profile('ou_sender')['name'] == 'Sam Lee'
