import json
import sys
from types import SimpleNamespace
from pathlib import Path

from teammem.chat.runtime import check_readiness, load_credentials
from teammem.cli import main
from tests.test_chat_config import FIXTURE


def test_dedicated_credentials_do_not_inherit_ambient_key(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','ambient-secret')
    env = tmp_path / 'chat.env'
    env.write_text('TEAMMEM_CHAT_FEISHU_APP_ID=cli_exampleapp123\nTEAMMEM_CHAT_FEISHU_APP_SECRET=bot-secret\nOPENAI_API_KEY=\n')
    env.chmod(0o600)
    assert load_credentials(env)['OPENAI_API_KEY'] == ''


def test_readiness_reports_disabled_staging_without_leaking_secrets(tmp_path, capsys):
    document = json.loads(json.dumps(FIXTURE))
    path = tmp_path / 'chat.json'
    document['paths']['credentials_env'] = str(tmp_path / 'chat.env')
    (tmp_path / 'chat.env').write_text('OPENAI_API_KEY=secret-never-print\n')
    (tmp_path / 'chat.env').chmod(0o600)
    path.write_text(json.dumps(document))
    assert main(['chat','check','--config',str(path)]) == 2
    output = capsys.readouterr().out
    assert 'disabled' in output.lower()
    assert 'secret-never-print' not in output


def test_readiness_rejects_shared_state_paths_before_creating_anything(tmp_path):
    document = json.loads(json.dumps(FIXTURE))
    document['paths'].update(chat_db=str(tmp_path/'ledger.db'), ledger_db=str(tmp_path/'ledger.db'))
    path = tmp_path / 'chat.json'
    path.write_text(json.dumps(document))
    assert not check_readiness(path).ready
    assert not (tmp_path/'ledger.db').exists()


def test_serve_does_not_start_disabled_config(tmp_path, capsys):
    path = tmp_path/'chat.json'
    path.write_text(json.dumps(FIXTURE))
    assert main(['chat','serve','--config',str(path)]) == 2


def test_codex_provider_readiness_requires_login_but_not_api_key(tmp_path, monkeypatch):
    document = json.loads(json.dumps(FIXTURE))
    document['enabled'] = True
    document['model']['provider'] = 'codex_cli'
    document['feishu'].update(tenant_key='tenant', expected_bot_open_id='ou_bot00000000')
    document['access']['users'] = {'alice': []}
    document['attachments']['enabled'] = False
    document['paths'] = {name: str(tmp_path/name) for name in document['paths']}
    env = tmp_path/'credentials_env'
    env.write_text('TEAMMEM_CHAT_FEISHU_APP_ID=cli_exampleapp123\nTEAMMEM_CHAT_FEISHU_APP_SECRET=bot-secret\n')
    env.chmod(0o600)
    source = tmp_path/'source_config_dir'; source.mkdir()
    (source/'projects.yaml').write_text('projects: {}\n')
    (source/'roster.yaml').write_text('people: {}\n')
    path = tmp_path/'chat.json'; path.write_text(json.dumps(document))
    monkeypatch.setitem(sys.modules, 'teammem.chat.codex', SimpleNamespace(check_login=lambda: True))

    checks = dict((name, ok) for name, ok, _ in check_readiness(path, verify_bot=lambda *_: True).checks)

    assert checks['credentials'] is True
    assert checks['Codex CLI login'] is True
