import asyncio
import json
import sys
from types import SimpleNamespace

from teammem.chat.feishu import NormalizedEvent
from teammem.chat.runtime import build_service, create_transport
from teammem.store import open_db
from tests.test_chat_config import FIXTURE
from tests.test_chat_model import Transport, completed


def configured(tmp_path):
    config = json.loads(json.dumps(FIXTURE))
    config['enabled'] = True
    config['feishu'].update(tenant_key='tenant',expected_bot_open_id='ou_bot00000000')
    config['access']['users'] = {'alice':[]}
    config['paths'] = {name:str(tmp_path/name) for name in config['paths']}
    env = tmp_path/'credentials_env'
    env.write_text('TEAMMEM_CHAT_FEISHU_APP_ID=cli_exampleapp123\nTEAMMEM_CHAT_FEISHU_APP_SECRET=synthetic-bot\nOPENAI_API_KEY=synthetic-model\n')
    env.chmod(0o600)
    (tmp_path/'source_config_dir').mkdir()
    (tmp_path/'source_config_dir'/'projects.yaml').write_text('projects: {}\n')
    (tmp_path/'source_config_dir'/'roster.yaml').write_text('people: {}\n')
    db = open_db(tmp_path/'ledger_db')
    db.close()
    path = tmp_path/'config.json'
    path.write_text(json.dumps(config))
    return path


class Client:
    def __init__(self):
        self.sent = []
    def get_message(self, message_id, chat_id):
        return {'message_id':message_id,'chat_id':chat_id,'msg_type':'file'}
    def message_resources(self, message_id, chat_id):
        return self.get_message(message_id,chat_id), ({'file_key':'f1','filename':'notes.txt','type':'file'},)
    def download_resource(self, *args, **kwargs):
        yield b'Annual target is 42.'
    def send_reply(self, *args, **kwargs):
        self.sent.append(args)
        return 'reply1'


def test_real_composition_casual_dm_and_restart_dedup(tmp_path):
    path = configured(tmp_path)
    client = Client()
    event = NormalizedEvent('tenant','cli_exampleapp123','m1','dmchat','alice','p2p','','',frozenset(),'Hi','text',())
    async def run():
        service, store, _ = build_service(path,client=client,transport=Transport(completed('Hello')))
        await service.handle(event)
        store.close();service.state.close()
        service, store, _ = build_service(path,client=client,transport=Transport())
        await service.handle(event)
        assert len(client.sent) == 1 and client.sent[0][0] == 'dmchat'
        store.close();service.state.close()
    asyncio.run(run())


def test_file_dm_download_parse_cite_pipeline(tmp_path):
    path = configured(tmp_path)
    client = Client()
    event = NormalizedEvent('tenant','cli_exampleapp123','m2','dmchat','alice','p2p','','',frozenset(),None,'file',({'file_key':'f1','filename':'notes.txt','type':'file'},))
    def parser(job, sandbox):
        from pathlib import Path
        assert Path(job['path']).read_bytes() == b'Annual target is 42.'
        return {'filename':job['filename'],'fragments':[{'text':'Annual target is 42.','locator':'line 1','kind':'text'}],
                'images':[],'coverage':{'complete':True,'visual_pages':0,'expanded_bytes':0}}
    async def run():
        transport = Transport(completed('Target is 42 [F1].'))
        service, store, _ = build_service(path,client=client,transport=transport,parser=parser)
        await service.handle(event)
        assert len(client.sent) == 1, [r.text for r in service.state.pending_replies()]
        assert 'notes.txt' in client.sent[0][-1] and 'line 1' in client.sent[0][-1]
        assert 'Annual target' in str(transport.payloads[0]['input'])
        store.close();service.state.close()
    asyncio.run(run())


def test_transport_factory_uses_logged_in_codex_cli_without_api_key(monkeypatch):
    class StubTransport:
        pass
    monkeypatch.setitem(sys.modules, 'teammem.chat.codex', SimpleNamespace(CodexTransport=StubTransport))

    transport = create_transport({'provider': 'codex_cli'}, {})

    assert isinstance(transport, StubTransport)
