"""Readiness and composition root for the optional, separate chat process."""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import ChatConfigError, load_chat_config
from .retrieval import open_ledger_readonly


@dataclass(frozen=True)
class Readiness:
    checks: tuple[tuple[str, bool, str], ...]

    @property
    def ready(self):
        return all(ok for _, ok, _ in self.checks)


def load_credentials(path):
    """Read only the dedicated env file, never interpolate or inherit shell secrets."""
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o077:
        raise ChatConfigError("credential file must be a regular private mode-600 file")
    allowed = {'TEAMMEM_CHAT_FEISHU_APP_ID', 'TEAMMEM_CHAT_FEISHU_APP_SECRET', 'OPENAI_API_KEY', 'TEAMMEM_CHAT_CONFIG'}
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, value = line.partition('=')
        if not separator or name not in allowed or name in values:
            raise ChatConfigError("credential file contains an invalid or duplicate setting")
        values[name] = value.strip().strip('"').strip("'")
    return values


def create_transport(model_config, credentials):
    """Create the configured provider transport without coupling model.py to a provider."""
    provider = model_config['provider'] if isinstance(model_config, dict) else model_config.provider if hasattr(model_config, 'provider') else model_config['provider']
    if provider == 'openai_responses':
        from .model import ResponsesTransport
        return ResponsesTransport(credentials.get('OPENAI_API_KEY'))
    if provider == 'codex_cli':
        from .codex import CodexTransport
        return CodexTransport()
    raise ChatConfigError('unsupported chat model provider')


def _writable_parent(path):
    parent = Path(path)
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)


def _document_checks(config):
    if not config.attachments['enabled']:
        return [('attachments', True, 'disabled')]
    checks = []
    dependencies = ['defusedxml', 'pypdf', 'pypdfium2', 'PIL', 'olefile']
    missing = [name for name in dependencies if importlib.util.find_spec(name) is None]
    bundled = config.paths.get('document_runtime_root')
    checks.append(('document Python libraries', not missing or bool(bundled), 'bundled runtime (sandbox probe required)' if bundled else ('missing: '+', '.join(missing) if missing else 'available')))
    root = config.paths.get('document_runtime_root')
    for binary in ('bwrap', 'libreoffice', 'tesseract'):
        available = (root/'usr'/'bin'/binary).is_file() if root and binary != 'bwrap' else shutil.which(binary) is not None
        checks.append((binary, available, 'available' if available else 'missing'))
    if root:
        language_root = root/'usr'/'share'/'tesseract-ocr'/'5'/'tessdata'
        checks.append(('OCR languages', all((language_root/(name+'.traineddata')).is_file() for name in ('eng','chi_sim')), 'requires eng and chi_sim in bundled runtime'))
    elif shutil.which('tesseract'):
        try:
            result = subprocess.run(['tesseract','--list-langs'], capture_output=True, text=True, timeout=5)
            languages = set(result.stdout.splitlines())
            checks.append(('OCR languages', {'eng','chi_sim'} <= languages, 'requires eng and chi_sim'))
        except (OSError, subprocess.TimeoutExpired):
            checks.append(('OCR languages', False, 'could not verify'))
    # Exercise actual namespace isolation with a harmless synthetic text document.
    sandbox_ok = False
    if (not missing or root) and sys.platform == 'linux' and shutil.which('bwrap'):
        try:
            from .document_worker import BubblewrapSandbox, parse_attachment
            with tempfile.TemporaryDirectory(prefix='teammem-chat-check-') as directory:
                root = Path(directory)
                source = root/'probe.txt'
                source.write_text('sandbox readiness probe')
                result = parse_attachment({'path':str(source),'filename':'probe.txt','limits':{'parse_timeout_seconds':5}},
                    BubblewrapSandbox(root/'output', **document_runtime_options(config)))
                capability = BubblewrapSandbox(root/'capability', **document_runtime_options(config)).check_runtime()
                sandbox_ok = (any('readiness probe' in f['text'] for f in result['fragments']) and
                    {'eng', 'chi_sim'} <= set(capability['languages']))
        except Exception:
            sandbox_ok = False
    checks.append(('document isolation', sandbox_ok, 'verified with synthetic file' if sandbox_ok else 'Linux sandbox unavailable or probe failed'))
    return checks


def check_readiness(path, *, verify_bot=None):
    checks = []
    try:
        config = load_chat_config(path)
    except (ChatConfigError, OSError, ValueError):
        return Readiness((('configuration', False, 'invalid or unreadable; check schema and separate paths'),))
    checks.append(('configuration', True, 'valid'))
    checks.append(('service enabled', config.enabled, 'enabled' if config.enabled else 'disabled'))
    checks.append(('trusted identity', bool(config.feishu['tenant_key'] and config.feishu['expected_bot_open_id']), 'requires configured tenant and bot open ID'))
    checks.append(('user grants', bool(config.access['users']), 'requires explicit new-app user IDs'))
    required = {'im:message.p2p_msg:readonly', 'im:message.group_at_msg:readonly', 'im:message:send_as_bot', 'im:message:readonly'}
    if config.attachments['enabled']:
        required.add('im:message:readonly')
    checks.append(('declared scopes', required <= set(config.feishu['required_scopes']), 'message and attachment scopes'))
    reaction_scope = 'im:message.reactions:write_only' in set(config.feishu['required_scopes'])
    checks.append(('processing reaction', True, 'scope declared; permission not verified' if reaction_scope else 'best effort; optional scope not declared'))
    credentials = {}
    try:
        credentials = load_credentials(config.paths['credentials_env'])
        valid = (credentials.get('TEAMMEM_CHAT_FEISHU_APP_ID') == config.feishu['app_id'] and
                 bool(credentials.get('TEAMMEM_CHAT_FEISHU_APP_SECRET')) and
                 (config.model['provider'] == 'codex_cli' or bool(credentials.get('OPENAI_API_KEY'))))
        checks.append(('credentials', valid, 'dedicated credentials present' if valid else 'missing or mismatched dedicated credentials'))
    except (OSError, ChatConfigError):
        checks.append(('credentials', False, 'missing or unsafe dedicated credential file'))
    if config.model['provider'] == 'codex_cli':
        try:
            from .codex import check_login
            valid = check_login() is True
        except Exception:
            valid = False
        checks.append(('Codex CLI login', valid, 'logged in' if valid else 'Codex CLI login required'))
    checks.append(('Feishu SDK', importlib.util.find_spec('lark_oapi') is not None, 'requires chat extra'))
    paths = config.paths
    state_paths = [paths['chat_db'].resolve(), paths['attachment_dir'].resolve()]
    protected = [paths['ledger_db'].resolve(), paths['credentials_env'].resolve(), paths['source_config_dir'].resolve()]
    separate = all(a != b and a not in b.parents and b not in a.parents for a in state_paths for b in protected)
    checks.append(('isolated paths', separate, 'state must not contain or overlap ledger, credentials, or source config'))
    checks.append(('state writable', all(_writable_parent(p.parent) for p in state_paths), 'writable state parents'))
    try:
        with open_ledger_readonly(paths['ledger_db']) as db:
            db.execute('SELECT project, kind, ts FROM events LIMIT 0')
        checks.append(('ledger', True, 'read-only query verified'))
    except Exception:
        checks.append(('ledger', False, 'unavailable or incompatible ledger'))
    checks.append(('source config', all((paths['source_config_dir']/name).is_file() for name in ('projects.yaml','roster.yaml')), 'projects.yaml and roster.yaml required'))
    checks.extend(_document_checks(config))
    if credentials.get('TEAMMEM_CHAT_FEISHU_APP_SECRET') and config.feishu['expected_bot_open_id']:
        if verify_bot is None:
            try:
                from .feishu import FeishuClient
                verify_bot = lambda cfg, env: FeishuClient(env['TEAMMEM_CHAT_FEISHU_APP_ID'], env['TEAMMEM_CHAT_FEISHU_APP_SECRET']).verify_identity(cfg.feishu['expected_bot_open_id'])
            except ImportError:
                verify_bot = None
        try:
            valid = verify_bot is not None and verify_bot(config, credentials) is not False
            checks.append(('live bot identity', valid, 'matches configuration' if valid else 'not verified'))
        except Exception:
            checks.append(('live bot identity', False, 'verification failed'))
    return Readiness(tuple(checks))


def chat_command(action, config_path):
    status = check_readiness(config_path)
    for name, ok, detail in status.checks:
        print(f"{'OK' if ok else 'FAIL'} {name}: {detail}")
    if not status.ready:
        return 2
    if action == 'serve':
        serve(config_path)
    return 0


class AttachmentPreparer:
    """Owner-loop admission and bounded off-loop download/parse orchestration."""

    def __init__(self, config, state, store, client, *, authorize_current, parser=None):
        self.config, self.state, self.store, self.client = config, state, store, client
        self.authorize_current = authorize_current
        self.parser = parser

    async def __call__(self, event, key, generation, cancelled):
        import asyncio
        import time
        from .attachments import AttachmentAdmissionError, AttachmentLimits
        from .document_worker import BubblewrapSandbox, parse_attachment
        if not self.config.attachments['enabled']:
            if event.message_type in {'file','image'} or event.parent_id:
                raise AttachmentAdmissionError('File reading is disabled for this service.')
            return ()
        self.store.cleanup_expired()
        limits = AttachmentLimits.from_mapping(self.config.attachments)
        source_id = event.message_id if event.message_type in {'file','image'} else event.parent_id
        if source_id:
            # Parent lookup is explicit, scoped and specific; never scan chat history.
            item = await asyncio.to_thread(self.client.get_message, source_id, event.chat_id)
            if item.get('msg_type') in {'file','image'}:
                _, resources = await asyncio.to_thread(self.client.message_resources, source_id, event.chat_id)
            elif event.message_type in {'file','image'}:
                raise AttachmentAdmissionError('The source does not contain a readable attachment.')
            else:
                resources = ()
            if len(resources) > limits.max_files_per_request:
                raise AttachmentAdmissionError('Too many files in one request.')
            deadline = time.monotonic() + min(60, self.config.attachments['parse_timeout_seconds'])
            remaining_bytes = limits.max_request_bytes
            remaining_visual = self.config.attachments['max_visual_pages_per_request']
            remaining_expanded = self.config.attachments['max_uncompressed_bytes_per_request']
            for resource in resources:
                self.authorize_current(self.config, key, event.sender)
                if cancelled.is_set() or self.state.generation(key) != generation:
                    raise AttachmentAdmissionError('The file request was cancelled.')
                job = self.store.admit_attachment(key, generation, {'message_id':source_id}, resource, limits)
                # Completed files remain usable after raw expiry and are not downloaded again.
                if any(fragment['id'].startswith(job.id+':') for fragment in self.store.context_fragments(key,generation)):
                    continue
                def download():
                    return b''.join(self.client.download_resource(source_id, event.chat_id, resource,
                        max_bytes=min(limits.max_file_bytes,remaining_bytes),deadline=deadline,cancel_event=cancelled))
                data = await asyncio.to_thread(download)
                self.authorize_current(self.config, key, event.sender)
                self.store.write_raw(job, [data])
                remaining_bytes -= len(data)
                del data
                if resource['type'] == 'image':
                    job = self.store.normalize_downloaded_image(job)
                parser_job = {'path':str(job.raw_path),'filename':job.filename,'mime_type':job.mime_type,
                    'question':event.text or '', 'limits':dict(self.config.attachments),'deadline':deadline,
                    'visual_pages_remaining':remaining_visual,'expanded_bytes_remaining':remaining_expanded}
                kwargs = document_runtime_options(self.config)
                sandbox = BubblewrapSandbox(job.derived_dir, cancel_event=cancelled, **kwargs)
                try:
                    result = await asyncio.to_thread(self.parser or parse_attachment, parser_job, sandbox)
                    self.authorize_current(self.config, key, event.sender)
                    if not self.store.record_result(job, result):
                        raise AttachmentAdmissionError('The file request was cancelled.')
                except BaseException:
                    self.store.discard(job)
                    raise
                remaining_visual -= result['coverage']['visual_pages']
                remaining_expanded -= result['coverage']['expanded_bytes']
        self.authorize_current(self.config, key, event.sender)
        return self.store.select_context(key,generation,event.text or 'Summarize this file')


def document_runtime_options(config):
    root = config.paths.get('document_runtime_root')
    if root is None:
        return {}
    return {'runtime_root':root}


def build_service(config_path, *, client=None, transport=None, parser=None):
    """Called on the service event-loop thread: SQLite never crosses threads."""
    import yaml
    from .access import AccessDenied, authorize, project_policy_from_source_config
    from .attachments import AttachmentStore
    from .context import (
        POLICY_DEPENDENCY_PREFIX,
        bind_policy_dependencies,
        build_team_context,
        policy_dependency,
        resolve_directory_alias,
    )
    from .feishu import FeishuClient
    from .model import (
        ModelError,
        answer,
        team_context_input_budget,
        team_context_input_cost,
    )
    from .retrieval import search_evidence
    from .service import ChatService
    from .state import ChatState
    config = load_chat_config(config_path)
    credentials = load_credentials(config.paths['credentials_env'])
    for path in (config.paths['chat_db'].parent, config.paths['attachment_dir']):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = ChatState(config.paths['chat_db'], idle_retention_days=config.session['idle_retention_days'])
    store = AttachmentStore(config.paths['attachment_dir'], state)
    client = client or FeishuClient(credentials['TEAMMEM_CHAT_FEISHU_APP_ID'], credentials['TEAMMEM_CHAT_FEISHU_APP_SECRET'])
    transport = transport or create_transport(config.model, credentials)

    def source_config(current):
        try:
            source = yaml.safe_load((current.paths['source_config_dir']/'projects.yaml').read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ModelError('The team directory could not be loaded safely.') from exc
        if not isinstance(source, Mapping):
            raise ModelError('The team directory could not be loaded safely.')
        return source

    def authorize_current(_config, key, sender):
        current = load_chat_config(config_path)
        if not current.enabled or key.tenant != current.feishu['tenant_key'] or key.app != current.feishu['app_id']:
            raise AccessDenied('Chat access is disabled.')
        granted = authorize(current, key, sender)
        try:
            return bind_policy_dependencies(source_config(current), granted)
        except ModelError as exc:
            raise AccessDenied('Chat access is disabled.') from exc

    def search_factory(key, sender, admitted):
        def strict_scope(policy, values):
            result = set()
            for project in values:
                if project.startswith(POLICY_DEPENDENCY_PREFIX):
                    continue
                mode = policy.get(project)
                if mode in {'detail','count_only'}:
                    token = policy_dependency(project, mode)
                    if token in values:
                        result.update((project, token))
            return frozenset(result)

        def search(query):
            current = load_chat_config(config_path)
            allowed = authorize_current(current,key,sender) & admitted
            source = source_config(current)
            policy = project_policy_from_source_config(source)
            allowed = strict_scope(policy, allowed)
            if isinstance(query, str):
                structured = {'text':query[:400]}
            elif isinstance(query, Mapping):
                unknown = set(query) - {'text','query','person','project','start','end'}
                if unknown:
                    raise ModelError('The structured search request is invalid.')
                text = query.get('text', query.get('query', ''))
                if not isinstance(text, str):
                    raise ModelError('The structured search request is invalid.')
                structured = {'text':text[:400]}
                for name in ('start','end'):
                    value = query.get(name)
                    if value is not None:
                        if not isinstance(value, str):
                            raise ModelError('The structured search request is invalid.')
                        structured[name] = value
                directory_query = " ".join(
                    value for value in (text, query.get('person'), query.get('project'))
                    if isinstance(value, str) and value.strip()
                )
                directory = context_factory(
                    config=current, authorization=allowed,
                    requester_id=sender, query=directory_query,
                )
                for name in ('person','project'):
                    value = query.get(name)
                    if value is not None:
                        if not isinstance(value, str) or len(value) > 200:
                            raise ModelError('The structured search request is invalid.')
                        structured[name] = resolve_directory_alias(directory, name, value)
                if 'project' in structured:
                    allowed = frozenset({structured['project']})
            else:
                raise ModelError('The structured search request is invalid.')
            current = load_chat_config(config_path)
            source = source_config(current)
            policy = project_policy_from_source_config(source)
            allowed = strict_scope(policy, authorize_current(current,key,sender) & admitted)
            raw_allowed = frozenset(project for project in allowed if not project.startswith(POLICY_DEPENDENCY_PREFIX))
            if 'project' in structured:
                raw_allowed &= frozenset({structured['project']})
            return search_evidence(current.paths['ledger_db'], policy, raw_allowed, structured,
                limit=current.retrieval['max_snippets'])
        return search

    def context_factory(*, config, authorization, requester_id, query):
        budget = min(8000, team_context_input_budget(
            config, sender=requester_id, text=query,
        ))
        return build_team_context(
            config, authorization, requester_id=requester_id, query=query,
            max_bytes=budget, measure_bytes=team_context_input_cost,
            require_policy_dependencies=True,
        )

    prepare = AttachmentPreparer(config,state,store,client,authorize_current=authorize_current,parser=parser)
    service = ChatService(state,config,answer,client.send_reply,authorize_fn=authorize_current,
        invalidate_session=store.invalidate_session,search_factory=search_factory,
        context_factory=context_factory,transport=transport,
        prepare_attachments=prepare, config_loader=lambda: load_chat_config(config_path),
        add_reaction=client.create_reaction, remove_reaction=client.delete_reaction)
    return service, store, client


def serve(config_path):
    """Run SDK socket on its loop, and durable service work on a separate owner loop."""
    import asyncio
    import logging
    import threading
    from .feishu import normalize_event
    logger = logging.getLogger(__name__)
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder = {}

    def service_thread():
        asyncio.set_event_loop(loop)
        try:
            service, store, client = build_service(config_path)
            loop.run_until_complete(service.reconcile_access())
            loop.run_until_complete(service.cleanup_reactions())
            holder.update(service=service,store=store,client=client)
        except Exception as exc:
            holder['error'] = type(exc).__name__
            ready.set()
            return
        async def maintenance():
            while True:
                try:
                    await service.reconcile_access()
                    service.state.expire_idle()
                    store.cleanup_expired()
                    await service.flush_outbox()
                    await service.cleanup_reactions()
                except Exception:
                    logger.error('Chat maintenance failed; details withheld from logs')
                await asyncio.sleep(15)
        loop.create_task(maintenance())
        ready.set()
        try:
            loop.run_forever()
        finally:
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop),return_exceptions=True))
            store.close()
            service.state.close()
            loop.close()

    thread = threading.Thread(target=service_thread,name='teammem-chat-state',daemon=True)
    thread.start()
    if not ready.wait(15) or 'error' in holder:
        raise RuntimeError('Chat service initialization failed.')
    def callback(raw):
        event = normalize_event(raw)
        # Wait only for durable admission, never for parsing/model work.
        future = asyncio.run_coroutine_threadsafe(holder['service'].enqueue(event), loop)
        future.result(timeout=5)
    try:
        holder['client'].start(callback)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=20)
