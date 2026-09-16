import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from teammem.chat.context import POLICY_DEPENDENCY_PREFIX
from teammem.chat.config import load_chat_config
from teammem.chat.feishu import NormalizedEvent, session_key
from teammem.chat.model import ModelError, team_context_input_budget, team_context_input_cost
from teammem.chat.runtime import build_service, check_readiness, create_transport
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


def directory_configured(tmp_path):
    path = configured(tmp_path)
    config = json.loads(path.read_text())
    config['context'] = {'timezone':'Asia/Dubai','user_people':{'ou_requester123':'alex'}}
    config['access']['users'] = {'ou_requester123':['alpha']}
    path.write_text(json.dumps(config))
    source = tmp_path/'source_config_dir'
    (source/'roster.yaml').write_text('''members:\n  alex:\n    name: Alex Rivera\n    feishu_names: [Alex]\n  sam:\n    name: Sam Lee\n    feishu_names: [Sam]\n''')
    (source/'projects.yaml').write_text('''projects:\n  alpha:\n    name: Project Alpha\n    aliases: [Alpha]\n''')
    conn = open_db(tmp_path/'ledger_db')
    conn.execute("INSERT INTO events (person, project, ts, source, kind, summary, hash) VALUES ('sam','alpha','2026-09-16T08:00:00+04:00','test','note','release status green','context-runtime')")
    conn.commit(); conn.close()
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
    def create_reaction(self, *args, **kwargs):
        return 'reaction1'
    def delete_reaction(self, *args, **kwargs):
        return None


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


def test_build_service_wires_reaction_transport(tmp_path):
    path = configured(tmp_path)
    client = Client()
    service, store, _ = build_service(path, client=client, transport=Transport(completed('Hello')))

    assert service.add_reaction == client.create_reaction
    assert service.remove_reaction == client.delete_reaction
    store.close(); service.state.close()


def test_build_service_wires_scoped_directory_and_structured_alias_search(tmp_path):
    path = directory_configured(tmp_path)
    service, store, _ = build_service(path, client=Client(), transport=Transport())
    event = NormalizedEvent('tenant','cli_exampleapp123','m1','dmchat','ou_requester123','p2p','','',frozenset(),'status','text',())
    key = session_key(event)
    admitted = service.authorize(service.config, key, event.sender)

    context = service.context_factory(
        config=service.config, authorization=admitted,
        requester_id=event.sender, query='Sam on Alpha',
    )
    evidence = service.search_factory(key, event.sender, admitted)({
        'query':'release status', 'person':'Sam', 'project':'Alpha',
    })

    assert context['requester']['slug'] == 'alex'
    assert {person['slug'] for person in context['people']} == {'alex','sam'}
    assert any(item.startswith(POLICY_DEPENDENCY_PREFIX) for item in context['_project_dependencies'])
    assert len(evidence) == 1 and evidence[0].project == 'alpha'
    store.close(); service.state.close()


def test_structured_search_rejects_unknown_or_ambiguous_scoped_alias(tmp_path):
    path = directory_configured(tmp_path)
    source = tmp_path/'source_config_dir'
    with (source/'roster.yaml').open('a') as stream:
        stream.write('  sam-two:\n    name: Samantha Lee\n    feishu_names: [Sam]\n')
    conn = open_db(tmp_path/'ledger_db')
    conn.execute("INSERT INTO events (person, project, ts, source, kind, summary, hash) VALUES ('sam-two','alpha','2026-09-16','test','note','other','context-runtime-two')")
    conn.commit(); conn.close()
    service, store, _ = build_service(path, client=Client(), transport=Transport())
    event = NormalizedEvent('tenant','cli_exampleapp123','m1','dmchat','ou_requester123','p2p','','',frozenset(),'status','text',())
    key = session_key(event)
    admitted = service.authorize(service.config, key, event.sender)
    search = service.search_factory(key, event.sender, admitted)

    with pytest.raises(ModelError, match='ambiguous'):
        search({'query':'release', 'person':'Sam'})
    with pytest.raises(ModelError, match='unknown'):
        search({'query':'release', 'project':'Unknown Project'})
    store.close(); service.state.close()


def test_search_rechecks_grant_and_projection_after_factory_creation(tmp_path):
    path = directory_configured(tmp_path)
    service, store, _ = build_service(path, client=Client(), transport=Transport())
    event = NormalizedEvent('tenant','cli_exampleapp123','m1','dmchat','ou_requester123','p2p','','',frozenset(),'status','text',())
    key = session_key(event)
    admitted = service.authorize(service.config, key, event.sender)
    search = service.search_factory(key, event.sender, admitted)
    source = tmp_path/'source_config_dir'/'projects.yaml'
    source.write_text('projects:\n  alpha:\n    projection: count-only\n')

    assert search('release status') == []

    config = json.loads(path.read_text())
    config['access']['users']['ou_requester123'] = []
    path.write_text(json.dumps(config))
    assert search('release status') == []
    store.close(); service.state.close()


def test_search_never_upgrades_count_admission_to_detail(tmp_path):
    path = directory_configured(tmp_path)
    source = tmp_path/'source_config_dir'/'projects.yaml'
    source.write_text('projects:\n  alpha:\n    projection: count-only\n')
    service, store, _ = build_service(path, client=Client(), transport=Transport())
    event = NormalizedEvent('tenant','cli_exampleapp123','m1','dmchat','ou_requester123','p2p','','',frozenset(),'status','text',())
    key = session_key(event)
    admitted = service.authorize(service.config, key, event.sender)
    search = service.search_factory(key, event.sender, admitted)
    source.write_text('projects:\n  alpha: {}\n')

    assert search('release status') == []
    store.close(); service.state.close()


def test_structured_filter_is_prioritized_when_directory_is_truncated(tmp_path):
    path = directory_configured(tmp_path)
    roster = tmp_path/'source_config_dir'/'roster.yaml'
    with roster.open('a') as stream:
        for number in range(100):
            stream.write(f'  person-{number}:\n    name: Person {number} With A Long Name\n')
        stream.write('  zz-target:\n    name: Zz Target\n')
    conn = open_db(tmp_path/'ledger_db')
    conn.executemany(
        "INSERT INTO events (person, project, ts, source, kind, summary, hash) VALUES (?, 'alpha', '2026-09-16', 'test', 'note', 'target work', ?)",
        [(f'person-{number}', f'bulk-{number}') for number in range(100)] + [('zz-target','bulk-target')],
    )
    conn.commit(); conn.close()
    service, store, _ = build_service(path, client=Client(), transport=Transport())
    event = NormalizedEvent('tenant','cli_exampleapp123','m1','dmchat','ou_requester123','p2p','','',frozenset(),'status','text',())
    key = session_key(event)
    admitted = service.authorize(service.config, key, event.sender)

    evidence = service.search_factory(key, event.sender, admitted)({'text':'','person':'zz-target'})

    assert evidence and evidence[0].person == 'zz-target'
    store.close(); service.state.close()


def test_runtime_directory_budget_retains_full_24k_scope_and_priorities_at_12k(tmp_path):
    path = directory_configured(tmp_path)
    document = json.loads(path.read_text())
    document['model']['max_input_tokens'] = 24_000
    projects = [f'project-{number}' for number in range(34)]
    document['access']['users']['ou_requester123'] = projects
    path.write_text(json.dumps(document))
    source = tmp_path/'source_config_dir'
    roster_lines = ['members:', '  alex:', '    name: Alex Rivera']
    for number in range(43):
        roster_lines.extend((f'  person-{number}:', f'    name: Person {number}'))
    (source/'roster.yaml').write_text('\n'.join(roster_lines)+'\n')
    project_lines = ['projects:']
    for number, project in enumerate(projects):
        project_lines.extend((f'  {project}:', f'    name: Project {number}'))
    (source/'projects.yaml').write_text('\n'.join(project_lines)+'\n')
    conn = open_db(tmp_path/'ledger_db')
    conn.executemany(
        "INSERT INTO events (person, project, ts, source, kind, summary, hash) VALUES (?, 'project-0', '2026-09-16', 'test', 'note', 'work', ?)",
        [(f'person-{number}', f'budget-{number}') for number in range(43)],
    )
    conn.commit(); conn.close()
    service, store, _ = build_service(path, client=Client(), transport=Transport())
    event = NormalizedEvent('tenant','cli_exampleapp123','m1','dmchat','ou_requester123','p2p','','',frozenset(),'status','text',())
    key = session_key(event)
    admitted = service.authorize(service.config, key, event.sender)
    query = 'Person 42 on Project 33'

    full = service.context_factory(config=service.config, authorization=admitted, requester_id=event.sender, query=query)
    assert len(full['people']) == 44 and len(full['projects']) == 34
    assert team_context_input_cost(full) <= 8000

    document['model']['max_input_tokens'] = 12_000
    path.write_text(json.dumps(document))
    legacy = load_chat_config(path)
    bounded = service.context_factory(config=legacy, authorization=admitted, requester_id=event.sender, query=query)
    assert bounded['truncated'] is True
    assert team_context_input_cost(bounded) <= team_context_input_budget(legacy, sender=event.sender, text=query)
    assert {'alex','person-42'} <= {person['slug'] for person in bounded['people']}
    assert 'project-33' in {project['slug'] for project in bounded['projects']}
    store.close(); service.state.close()


def test_readiness_describes_optional_reaction_scope_as_best_effort(tmp_path):
    path = configured(tmp_path)

    status = check_readiness(path, verify_bot=lambda *_: True)

    detail = next(detail for name, _, detail in status.checks if name == 'processing reaction')
    assert detail == 'best effort; optional scope not declared'


def test_readiness_does_not_claim_declared_reaction_permission_is_verified(tmp_path):
    path = configured(tmp_path)
    document = json.loads(path.read_text())
    document['feishu']['required_scopes'].append('im:message.reactions:write_only')
    path.write_text(json.dumps(document))

    status = check_readiness(path, verify_bot=lambda *_: True)

    detail = next(detail for name, _, detail in status.checks if name == 'processing reaction')
    assert detail == 'scope declared; permission not verified'
