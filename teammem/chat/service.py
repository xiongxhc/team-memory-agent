"""Serialized durable chat turn handling."""

import asyncio
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .access import AccessDenied, authorize
from .attachments import AttachmentAdmissionError
from .document_worker import DocumentError
from .feishu import FeishuError, NormalizedEvent, is_own_bot_mention, session_key
from .model import ModelError
from .state import ChatState, Turn


class ChatService:
    def __init__(self, state: ChatState, config: Mapping[str, Any], model: Callable[..., tuple[str, list]], send_reply: Callable[..., str], *, authorize_fn: Callable = authorize, invalidate_session: Callable | None = None, search_factory: Callable | None = None, transport: Any = None, prepare_attachments: Callable | None = None, config_loader: Callable[[], Any] | None = None, add_reaction: Callable | None = None, remove_reaction: Callable | None = None):
        self.state, self.config, self.model, self.send_reply, self.authorize = state, config, model, send_reply, authorize_fn
        self.invalidate_session = invalidate_session
        self.search_factory, self.transport, self.prepare_attachments = search_factory, transport, prepare_attachments
        self.config_loader = config_loader
        self.add_reaction, self.remove_reaction = add_reaction, remove_reaction
        self._sessions: dict[object, asyncio.Lock] = {}
        self._cancel: dict[object, threading.Event] = {}
        self._sending: dict[object, threading.Event] = {}
        self._outbox_lock = asyncio.Lock()
        self._limit = asyncio.Semaphore(self._model_call_limit())
        self._tasks: dict[tuple[str, str, str], asyncio.Task] = {}
        self._reaction_tasks: set[asyncio.Task] = set()
        self._reaction_deleting: set[tuple[str, str, str, str]] = set()
        # Event resources are intentionally not persisted, so a restart cannot safely replay them.
        self.recovered_interrupted = self.state.recover_interrupted()
        self.recovered_reactions = self.state.recover_reactions()

    def _current_config(self) -> Any:
        return self.config if self.config_loader is None else self.config_loader()

    def _field(self, section: str, name: str) -> Any:
        config = self._current_config()
        value = config[section] if isinstance(config, Mapping) else getattr(config, section)
        return value[name] if isinstance(value, Mapping) else getattr(value, name)

    def _optional_field(self, section: str, name: str, default: Any) -> Any:
        try:
            return self._field(section, name)
        except (AttributeError, KeyError):
            return default

    def _model_call_limit(self) -> int:
        try:
            value = self._field("session", "max_concurrent_model_calls")
        except (AttributeError, KeyError):
            return 2
        return min(2, max(1, int(value)))

    async def handle(self, event: NormalizedEvent) -> None:
        if not await self.enqueue(event):
            return
        task = self._tasks.get((event.tenant, event.app, event.message_id))
        if task is not None:
            await task

    async def enqueue(self, event: NormalizedEvent) -> bool:
        """Durably admit an event before SDK acknowledgement; never waits for a model."""
        if event.is_bot_generated or not event.message_id or not event.sender:
            return False
        # A malformed text body is ignored, while a recognized non-text message gets an
        # authorized, durable explanation rather than being silently acknowledged.
        if event.text is None and not event.resources and event.message_type in {"", "text"}:
            return False
        if event.tenant != self._field("feishu", "tenant_key") or event.app != self._field("feishu", "app_id"):
            return False
        try:
            key = session_key(event)
        except ValueError:
            return False
        if key.kind == "dm" and self._optional_field("feishu", "direct_messages", True) is not True:
            return False
        if event.chat_type == "group" and not is_own_bot_mention(event, self._field("feishu", "expected_bot_open_id")):
            return False
        try:
            projects = self.authorize(self._current_config(), key, event.sender)
        except AccessDenied:
            return False
        command = (event.text or "").strip().lower()
        if command in {"/new", "/forget"}:
            access = self._field("access", "group_admins")
            if key.kind != "dm" and event.sender not in access.get(key.owner, []):
                return False
            if not self.state.apply_command(key, event.message_id, event.sender, command,
                                            forget=command == "/forget", chat_id=event.chat_id,
                                            projects=projects):
                return False
            for pending in (self._cancel.get(key), self._sending.get(key)):
                if pending is not None:
                    pending.set()
            if self.invalidate_session is not None:
                result = self.invalidate_session(key)
                if hasattr(result, "__await__"):
                    await result
            self._schedule_pending_reaction_cleanup()
            return True
        text = event.text or "Please analyze the attached file or image."
        if not self.state.record_incoming(key, event.message_id, event.sender, text, chat_id=event.chat_id, projects=projects):
            return False
        if self.add_reaction is not None and self.remove_reaction is not None:
            generation = self.state.incoming_generation(key, event.message_id)
            if generation is not None and self.state.request_reaction(key, event.message_id, "Typing", generation=generation):
                self._spawn_reaction(self._create_reaction(event.tenant, event.app, event.message_id))
        lock = self._sessions.setdefault(key, asyncio.Lock())
        task = asyncio.create_task(self._process_admitted(event, key, lock))
        identity = (event.tenant, event.app, event.message_id)
        self._tasks[identity] = task
        task.add_done_callback(lambda done: self._admitted_done(identity, done))
        return True

    def _admitted_done(self, identity: tuple[str, str, str], task: asyncio.Task) -> None:
        if self._tasks.get(identity) is task:
            self._tasks.pop(identity, None)
        self._request_reaction_cleanup(*identity)

    async def _process_admitted(self, event, key, lock):
        async with lock:
            await self._handle_locked(event, key)

    async def _handle_locked(self, event, key):
        generation = self.state.incoming_generation(key, event.message_id)
        if generation is None:
            return
        try:
            projects = self.authorize(self._current_config(), key, event.sender)
        except AccessDenied:
            self.state.fail_incoming(key, event.message_id)
            self._request_reaction_cleanup(event.tenant, event.app, event.message_id)
            return
        if not self.state.start_incoming(key, event.message_id):
            return
        if event.text is None and not event.resources:
            await self._queue_failure(key, event.message_id, generation,
                                      "I can help with text, files, and images. Please send one of those formats.")
            return
        if key.kind == "thread" and event.parent_id:
            self.state.seed_thread(key, event.parent_id, projects)
        text = event.text or "Please analyze the attached file or image."
        self.state.append(key, Turn("user", event.sender, text, frozenset()), generation=generation)
        async with self._limit:
            if self.state.generation(key) != generation:
                return
            try:
                projects = self.authorize(self._current_config(), key, event.sender)
            except AccessDenied:
                self.state.fail_incoming(key, event.message_id)
                self._request_reaction_cleanup(event.tenant, event.app, event.message_id)
                return
            cancelled = threading.Event()
            self._cancel[key] = cancelled
            try:
                attachments = () if self.prepare_attachments is None else await self.prepare_attachments(event, key, generation, cancelled)
                if cancelled.is_set() or self.state.generation(key) != generation:
                    return
                try:
                    projects = self.authorize(self._current_config(), key, event.sender)
                except AccessDenied:
                    self.state.fail_incoming(key, event.message_id)
                    self._request_reaction_cleanup(event.tenant, event.app, event.message_id)
                    return
                search = (lambda *_: []) if self.search_factory is None else self.search_factory(key, event.sender, projects)
                history = self.state.history(key, projects)
                answer, evidence = await asyncio.to_thread(
                    self.model, self._current_config(), history, search, self.transport,
                    attachments=attachments, cancel_event=cancelled, deadline=time.monotonic() + 45,
                )
                if cancelled.is_set() or self.state.generation(key) != generation:
                    return
                try:
                    projects = self.authorize(self._current_config(), key, event.sender)
                except AccessDenied:
                    self.state.fail_incoming(key, event.message_id)
                    self._request_reaction_cleanup(event.tenant, event.app, event.message_id)
                    return
            except (ModelError, FeishuError, DocumentError, AttachmentAdmissionError) as error:
                cancelled.set()
                await self._queue_failure(key, event.message_id, generation, self._failure_text(error))
                return
            except Exception:
                cancelled.set()
                await self._queue_failure(key, event.message_id, generation, "I could not complete that request. Please try again.")
                return
            finally:
                if self._cancel.get(key) is cancelled:
                    self._cancel.pop(key, None)
        evidence_projects = frozenset(item.project for item in evidence)
        answer_projects = evidence_projects | frozenset(project for turn in history for project in turn.projects)
        reply_id = str(uuid.uuid4())
        if self.state.queue_reply(key, event.message_id, reply_id, answer, answer_projects, generation=generation):
            self.state.append(key, Turn("assistant", "bot", answer, answer_projects), generation=generation)
            self.state.record_interaction(key, event.message_id, answer, answer_projects, generation=generation)
            self.state.finish_incoming(key, event.message_id)
            await self.flush_outbox()

    @staticmethod
    def _failure_text(error) -> str:
        if isinstance(error, DocumentError):
            code = error.code if isinstance(error.code, str) and error.code.replace("_", "").isalnum() else "processing_error"
            return f"The attachment could not be processed ({code}). Please check its format and try again."
        if isinstance(error, AttachmentAdmissionError):
            return "The attachment could not be accepted. Please check its format and size, then try again."
        if isinstance(error, FeishuError):
            return "Feishu could not complete that request. Please try again."
        if isinstance(error, ModelError):
            return "The chat model could not complete that request. Please try again."
        return "I could not complete that request. Please try again."

    async def _queue_failure(self, key, message_id, generation, text):
        """A recoverable provider/parser failure is durable and uses the same outbox."""
        safe = text if text and len(text) <= 500 else "I could not complete that request. Please try again."
        if self.state.queue_reply(key, message_id, str(uuid.uuid4()), safe, frozenset(), generation=generation):
            self.state.finish_incoming(key, message_id, "failed")
            await self.flush_outbox()

    async def flush_outbox(self) -> None:
        async with self._outbox_lock:
            await self._flush_outbox_locked()

    async def _flush_outbox_locked(self) -> None:
        self.state.quarantine_expired_replies()
        for reply in self.state.pending_replies():
            try:
                current = self.authorize(self._current_config(), reply.session, reply.sender)
            except AccessDenied:
                self.state.quarantine_reply(reply.reply_id)
                self._request_reaction_cleanup(reply.session.tenant, reply.session.app, reply.message_id)
                continue
            if not reply.projects.issubset(current):
                self.state.quarantine_reply(reply.reply_id)
                self._request_reaction_cleanup(reply.session.tenant, reply.session.app, reply.message_id)
                continue
            if self.state.generation(reply.session) != reply.generation:
                self.state.quarantine_reply(reply.reply_id)
                self._request_reaction_cleanup(reply.session.tenant, reply.session.app, reply.message_id)
                continue
            cancelled = threading.Event()
            self._sending[reply.session] = cancelled
            try:
                platform_id = await asyncio.to_thread(
                    self.send_reply, reply.chat_id, reply.message_id, reply.reply_id, reply.text,
                    cancel_event=cancelled, deadline=time.monotonic() + 15,
                )
            except Exception:
                self._request_reaction_cleanup(reply.session.tenant, reply.session.app, reply.message_id)
                continue
            finally:
                if self._sending.get(reply.session) is cancelled:
                    self._sending.pop(reply.session, None)
            if platform_id:
                self.state.mark_reply_delivered(reply.reply_id, str(platform_id))
                self._request_reaction_cleanup(reply.session.tenant, reply.session.app, reply.message_id)

    def _spawn_reaction(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._reaction_tasks.add(task)
        task.add_done_callback(self._reaction_tasks.discard)

    async def _create_reaction(self, tenant: str, app: str, message_id: str) -> None:
        try:
            reaction_id = await asyncio.to_thread(
                self.add_reaction, message_id, "Typing", deadline=time.monotonic() + 2,
            )
        except Exception:
            # An ambiguous timeout can leave a reaction whose ID is unknowable without
            # read scope. Never retry the create and risk duplicate indicators.
            self.state.mark_reaction_create_failed(tenant, app, message_id)
            return
        if not reaction_id:
            self.state.mark_reaction_create_failed(tenant, app, message_id)
            return
        still_active = self.state.record_reaction_created(tenant, app, message_id, str(reaction_id))
        if not still_active:
            self._schedule_reaction_delete(tenant, app, message_id, str(reaction_id))

    def _request_reaction_cleanup(self, tenant: str, app: str, message_id: str) -> None:
        reaction_id = self.state.request_reaction_cleanup(tenant, app, message_id)
        if reaction_id:
            self._schedule_reaction_delete(tenant, app, message_id, reaction_id)

    def _schedule_reaction_delete(self, tenant: str, app: str, message_id: str, reaction_id: str) -> None:
        identity = (tenant, app, message_id, reaction_id)
        if self.remove_reaction is None or identity in self._reaction_deleting:
            return
        self._reaction_deleting.add(identity)

        async def remove():
            try:
                await asyncio.to_thread(
                    self.remove_reaction, message_id, reaction_id, deadline=time.monotonic() + 2,
                )
            except Exception:
                return
            else:
                self.state.mark_reaction_removed(tenant, app, message_id, reaction_id)
            finally:
                self._reaction_deleting.discard(identity)

        self._spawn_reaction(remove())

    def _schedule_pending_reaction_cleanup(self) -> None:
        for item in self.state.pending_reaction_cleanup():
            self._schedule_reaction_delete(item.tenant, item.app, item.message_id, item.reaction_id)

    async def cleanup_reactions(self) -> None:
        self._schedule_pending_reaction_cleanup()
        await self.wait_for_reactions()

    async def wait_for_reactions(self) -> None:
        while self._reaction_tasks:
            tasks = tuple(self._reaction_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._reaction_tasks.difference_update(tasks)

    async def reconcile_access(self) -> list[object]:
        """Forget sessions whose explicit identity or group grant was fully removed."""
        config = self._current_config()
        access = config["access"] if isinstance(config, Mapping) else getattr(config, "access")
        users = access.get("users") if isinstance(access, Mapping) else getattr(access, "users", None)
        groups = access.get("groups") if isinstance(access, Mapping) else getattr(access, "groups", None)
        feishu = config["feishu"] if isinstance(config, Mapping) else getattr(config, "feishu")
        direct_messages = feishu.get("direct_messages", True) if isinstance(feishu, Mapping) else getattr(feishu, "direct_messages", True)
        purged = []
        for key in self.state.session_keys():
            revoked = (
                key.kind == "dm" and (not isinstance(users, Mapping) or key.owner not in users)
            ) or (
                key.kind in {"group", "thread"} and (not isinstance(groups, Mapping) or key.owner not in groups)
            )
            if not revoked:
                if key.kind == "dm" and direct_messages is not True:
                    for pending in (self._cancel.get(key), self._sending.get(key)):
                        if pending is not None:
                            pending.set()
                    self.state.quarantine_session_replies(key)
                    self.state.request_session_reaction_cleanup(key)
                    self._schedule_pending_reaction_cleanup()
                continue
            for pending in (self._cancel.get(key), self._sending.get(key)):
                if pending is not None:
                    pending.set()
            first_revocation = self.state.forget_revoked(key)
            if self.invalidate_session is not None:
                result = self.invalidate_session(key)
                if hasattr(result, "__await__"):
                    await result
            if first_revocation:
                purged.append(key)
            self._schedule_pending_reaction_cleanup()
        return purged
