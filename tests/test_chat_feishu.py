from teammem.chat.feishu import normalize_event, session_key


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
