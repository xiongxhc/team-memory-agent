import asyncio
import json
import threading

from teammem.chat.context import policy_dependency
from teammem.chat.feishu import normalize_event, session_key
from teammem.chat.service import ChatService
from teammem.chat.state import ChatState, Evidence, SessionKey, Turn


def _event(**changes):
    raw = {"tenant": "tenant", "app": "app", "message": {"message_id": "m1", "chat_id": "dm-chat", "chat_type": "p2p", "message_type": "text", "content": '{"text":"hello"}'}, "sender": {"sender_id": {"open_id": "alice"}}}
    raw.update(changes)
    return normalize_event(raw)


def test_duplicate_dm_event_makes_one_model_call_and_replies_to_original_chat(tmp_path):
    calls, sent = [], []
    def model(*args, **kwargs):
        calls.append(args)
        return "answer", []
    def send(chat_id, message_id, reply_id, text, **kwargs):
        sent.append((chat_id, message_id, reply_id, text))
        return "platform-message"
    service = ChatService(ChatState(tmp_path / "chat.db"), {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}, model, send)

    asyncio.run(service.handle(_event()))
    asyncio.run(service.handle(_event()))

    assert len(calls) == 1
    assert [(chat, message, text) for chat, message, _, text in sent] == [("dm-chat", "m1", "answer")]


def test_context_factory_runs_off_loop_and_passes_only_public_context(tmp_path):
    loop_thread = threading.get_ident()
    seen = {}
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": ["alpha"]}, "groups": {}, "group_admins": {}}}
    token = policy_dependency("alpha", "detail")
    def context_factory(**kwargs):
        seen["thread"] = threading.get_ident()
        seen["factory"] = kwargs
        return {
            "requester": {"slug": "alex", "name": "Alex", "aliases": []},
            "people": [], "projects": [], "clock": {}, "truncated": False,
            "_project_dependencies": ["alpha", token],
        }
    def model(*args, **kwargs):
        seen["model_context"] = kwargs["team_context"]
        return "answer", []
    service = ChatService(
        ChatState(tmp_path / "chat.db"), config, model, lambda *a, **k: "sent",
        authorize_fn=lambda *_: frozenset({"alpha", token}), context_factory=context_factory,
    )

    asyncio.run(service.handle(_event()))

    assert seen["thread"] != loop_thread
    assert seen["factory"]["requester_id"] == "alice"
    assert seen["factory"]["query"] == "hello"
    assert seen["model_context"] == {
        "requester": {"slug": "alex", "name": "Alex", "aliases": []},
        "people": [], "projects": [], "clock": {}, "truncated": False,
    }


def test_legacy_model_call_does_not_receive_team_context_keyword(tmp_path):
    seen = []
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    def model(*args, **kwargs):
        seen.append(kwargs)
        return "answer", []

    asyncio.run(ChatService(
        ChatState(tmp_path / "chat.db"), config, model, lambda *a, **k: "sent",
    ).handle(_event()))

    assert "team_context" not in seen[0]


def test_context_only_answer_is_suppressed_when_grant_changes_during_model_call(tmp_path):
    token = policy_dependency("alpha", "detail")
    allowed = {"value": frozenset({"alpha", token})}
    sent = []
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": ["alpha"]}, "groups": {}, "group_admins": {}}}
    def model(*args, **kwargs):
        allowed["value"] = frozenset()
        return "directory answer", []
    context = {"requester": None, "people": [], "projects": [], "clock": {}, "truncated": False,
               "_project_dependencies": ["alpha", token]}
    service = ChatService(
        ChatState(tmp_path / "chat.db"), config, model,
        lambda *a, **k: sent.append(1) or "sent",
        authorize_fn=lambda *_: allowed["value"], context_factory=lambda **_: context,
    )
    event = _event()

    asyncio.run(service.handle(event))

    assert sent == []
    assert service.state.pending_replies() == []
    assert [turn.text for turn in service.state.history(session_key(event), frozenset())] == ["hello"]


def test_context_only_answer_is_suppressed_when_projection_tightens_during_model_call(tmp_path):
    detail = policy_dependency("alpha", "detail")
    count = policy_dependency("alpha", "count_only")
    allowed = {"value": frozenset({"alpha", detail})}
    sent = []
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": ["alpha"]}, "groups": {}, "group_admins": {}}}
    def model(*args, **kwargs):
        allowed["value"] = frozenset({"alpha", count})
        return "detailed directory answer", []
    context = {"requester": None, "people": [], "projects": [], "clock": {}, "truncated": False,
               "_project_dependencies": ["alpha", detail]}
    service = ChatService(
        ChatState(tmp_path / "chat.db"), config, model,
        lambda *a, **k: sent.append(1) or "sent",
        authorize_fn=lambda *_: allowed["value"], context_factory=lambda **_: context,
    )

    asyncio.run(service.handle(_event()))

    assert sent == []
    assert service.state.pending_replies() == []


def test_context_enabled_service_quarantines_legacy_unbound_outbox_reply(tmp_path):
    state = ChatState(tmp_path / "chat.db")
    event = _event()
    key = session_key(event)
    state.record_incoming(key, "legacy", "alice", "old question", chat_id="dm-chat", projects=frozenset({"alpha"}))
    state.start_incoming(key, "legacy")
    state.queue_reply(key, "legacy", "00000000-0000-0000-0000-000000000009", "old detailed answer", frozenset({"alpha"}), generation=0)
    sent = []
    detail = policy_dependency("alpha", "detail")
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": ["alpha"]}, "groups": {}, "group_admins": {}}}
    service = ChatService(
        state, config, lambda *a, **k: ("unused", []), lambda *a, **k: sent.append(1),
        authorize_fn=lambda *_: frozenset({"alpha", detail}),
        context_factory=lambda **_: {},
    )

    asyncio.run(service.flush_outbox())

    assert sent == []
    assert state.pending_replies() == []


def test_context_enabled_service_omits_legacy_unbound_project_history(tmp_path):
    token = policy_dependency("alpha", "detail")
    state = ChatState(tmp_path / "chat.db")
    key = session_key(_event())
    state.append(key, Turn("user", "alice", "old question", frozenset()))
    state.append(key, Turn("assistant", "bot", "old detailed answer", frozenset({"alpha"})))
    histories = []
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": ["alpha"]}, "groups": {}, "group_admins": {}}}
    def model(_config, history, *_args, **_kwargs):
        histories.append(history)
        return "new answer", []
    context = {"requester": None, "people": [], "projects": [], "clock": {}, "truncated": False,
               "_project_dependencies": ["alpha", token]}
    service = ChatService(
        state, config, model, lambda *a, **k: "sent",
        authorize_fn=lambda *_: frozenset({"alpha", token}), context_factory=lambda **_: context,
    )

    asyncio.run(service.handle(_event()))

    assert [turn.text for turn in histories[0]] == ["old question", "hello"]


def test_group_without_exact_bot_mention_never_calls_model(tmp_path):
    called = []
    service = ChatService(ChatState(tmp_path / "chat.db"), {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {"chat": []}, "group_admins": {}}}, lambda *a, **k: called.append(1), lambda *a, **k: "x")
    event = _event(message={"message_id": "m1", "chat_id": "chat", "chat_type": "group", "message_type": "text", "content": '{"text":"hello"}'})

    asyncio.run(service.handle(event))

    assert called == []


def test_wildcard_access_accepts_new_dm_group_and_thread_but_keeps_sessions_isolated(tmp_path):
    histories = []
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app",
        "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny",
        "users": {"*": ["alpha", "beta"]}, "groups": {"*": ["beta"]},
        "group_admins": {}}}
    def model(_config, history, *_args, **_kwargs):
        histories.append([turn.text for turn in history])
        return "answer", []
    service = ChatService(ChatState(tmp_path / "chat.db"), config, model,
                          lambda *args, **kwargs: "sent")
    mention = [{"id": {"open_id": "ou_bot"}}]

    asyncio.run(service.handle(_event(sender={"sender_id": {"open_id": "new-user"}},
        message={"message_id":"dm", "chat_id":"dm-new", "chat_type":"p2p",
                 "message_type":"text", "content":'{"text":"dm question"}'})))
    asyncio.run(service.handle(_event(sender={"sender_id": {"open_id": "new-user"}},
        message={"message_id":"group", "chat_id":"new-group", "chat_type":"group",
                 "message_type":"text", "content":'{"text":"group question"}'}, mentions=mention)))
    asyncio.run(service.handle(_event(sender={"sender_id": {"open_id": "new-user"}},
        message={"message_id":"thread", "chat_id":"new-group", "chat_type":"group",
                 "root_id":"root", "message_type":"text", "content":'{"text":"thread question"}'},
        mentions=mention)))

    assert histories == [["dm question"], ["group question"], ["thread question"]]


def test_wildcard_group_still_requires_own_bot_mention_and_exact_group_admin(tmp_path):
    called, invalidated = [], []
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app",
        "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny",
        "users": {"*": []}, "groups": {"*": []},
        "group_admins": {"*": ["new-user"]}}}
    service = ChatService(ChatState(tmp_path / "chat.db"), config,
        lambda *args, **kwargs: called.append(1) or ("answer", []),
        lambda *args, **kwargs: "sent", invalidate_session=lambda key: invalidated.append(key))
    group = {"message_id":"group", "chat_id":"new-group", "chat_type":"group",
             "message_type":"text", "content":'{"text":"hello"}'}
    sender = {"sender_id": {"open_id": "new-user"}}

    asyncio.run(service.handle(_event(sender=sender, message=group)))
    asyncio.run(service.handle(_event(sender=sender, message={**group, "message_id":"wrong-bot"},
        mentions=[{"id":{"open_id":"another-bot"}}])))
    asyncio.run(service.handle(_event(sender=sender,
        message={**group, "message_id":"forget", "content":'{"text":"/forget"}'},
        mentions=[{"id":{"open_id":"ou_bot"}}])))

    assert called == [] and invalidated == []


def test_wildcard_access_ignores_wrong_tenant_or_app(tmp_path):
    called = []
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app",
        "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny",
        "users": {"*": []}, "groups": {"*": []}, "group_admins": {}}}
    service = ChatService(ChatState(tmp_path / "chat.db"), config,
                          lambda *a, **k: called.append(1), lambda *a, **k: "sent")

    asyncio.run(service.handle(_event(tenant="other", sender={"sender_id": {"open_id": "new"}})))
    asyncio.run(service.handle(_event(app="other", sender={"sender_id": {"open_id": "new"}},
        message={"message_id":"other-app", "chat_id":"dm", "chat_type":"p2p",
                 "message_type":"text", "content":'{"text":"hello"}'})))

    assert called == []


def test_dm_new_resets_and_calls_invalidation_callback(tmp_path):
    invalidated = []
    service = ChatService(ChatState(tmp_path / "chat.db"), {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}, lambda *a, **k: ("x", []), lambda *a, **k: "x", invalidate_session=lambda key: invalidated.append(key))
    asyncio.run(service.handle(_event(message={"message_id": "new", "chat_id": "dm-chat", "chat_type": "p2p", "message_type": "text", "content": '{"text":"/new"}'})))
    assert invalidated and service.state.generation(invalidated[0]) == 1


def test_group_forget_requires_explicit_group_admin(tmp_path):
    invalidated = []
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {"chat": []}, "group_admins": {"chat": ["admin"]}}}
    service = ChatService(ChatState(tmp_path / "chat.db"), config, lambda *a, **k: ("x", []), lambda *a, **k: "x", invalidate_session=lambda key: invalidated.append(key))
    event = _event(message={"message_id": "forget", "chat_id": "chat", "chat_type": "group", "message_type": "text", "content": '{"text":"/forget"}'}, mentions=[{"id": {"open_id": "ou_bot"}}])
    asyncio.run(service.handle(event))
    assert invalidated == []


def test_dm_file_event_with_no_text_is_admitted_and_passes_attachment_context(tmp_path):
    seen = []
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    def model(*args, **kwargs):
        seen.append(kwargs["attachments"])
        return "file answer", []
    async def attachments(event, key, generation, cancelled):
        assert event.resources and key.owner == "alice" and generation == 0
        return [{"id":"f1", "filename":"notes.pdf", "locator":"page 1", "text":"hello"}]
    service = ChatService(ChatState(tmp_path / "chat.db"), config, model, lambda *args, **kwargs: "sent", prepare_attachments=attachments)
    event = _event(message={"message_id":"file", "chat_id":"dm-chat", "chat_type":"p2p", "message_type":"file", "content":'{"file_key":"key","file_name":"notes.pdf"}'})

    asyncio.run(service.handle(event))

    assert seen == [[{"id":"f1", "filename":"notes.pdf", "locator":"page 1", "text":"hello"}]]


def test_duplicate_reset_event_only_changes_generation_once(tmp_path):
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    service = ChatService(ChatState(tmp_path / "chat.db"), config, lambda *args, **kwargs: ("x", []), lambda *args, **kwargs: "sent")
    event = _event(message={"message_id":"reset", "chat_id":"dm-chat", "chat_type":"p2p", "message_type":"text", "content":'{"text":"/new"}'})

    asyncio.run(service.handle(event))
    asyncio.run(service.handle(event))

    assert service.state.generation(session_key(event)) == 1


def test_denied_after_admission_is_marked_failed_without_model_call(tmp_path):
    allowed = {"value": True}
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    def authorize(*args):
        if not allowed["value"]:
            from teammem.chat.access import AccessDenied
            raise AccessDenied("revoked")
        return frozenset()
    calls = []
    service = ChatService(ChatState(tmp_path / "chat.db"), config, lambda *args, **kwargs: calls.append(1), lambda *args, **kwargs: "sent", authorize_fn=authorize)
    event = _event()

    async def run():
        assert await service.enqueue(event)
        allowed["value"] = False
        await service._tasks[("tenant", "app", "m1")]
    asyncio.run(run())

    assert calls == []
    assert service.state.start_incoming(session_key(event), "m1") is False


def test_new_thread_receives_only_its_recorded_parent_interaction(tmp_path):
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": ["alpha"]}, "groups": {"chat": ["alpha"]}, "group_admins": {}}}
    histories = []
    def model(_config, history, *_args, **_kwargs):
        histories.append(history)
        return "answer", []
    service = ChatService(ChatState(tmp_path / "chat.db"), config, model, lambda *args, **kwargs: "sent")
    parent = _event(message={"message_id":"parent", "chat_id":"chat", "chat_type":"group", "message_type":"text", "content":'{"text":"parent question"}'}, mentions=[{"id":{"open_id":"ou_bot"}}])
    child = _event(message={"message_id":"child", "chat_id":"chat", "chat_type":"group", "root_id":"root", "parent_id":"parent", "message_type":"text", "content":'{"text":"follow up"}'}, mentions=[{"id":{"open_id":"ou_bot"}}])

    asyncio.run(service.handle(parent))
    asyncio.run(service.handle(child))

    assert [(turn.role, turn.text) for turn in histories[-1]] == [
        ("user", "parent question"), ("assistant", "answer"), ("user", "follow up"),
    ]


def test_startup_recovery_sends_clear_resend_request(tmp_path):
    path = tmp_path / "chat.db"
    state = ChatState(path)
    key = session_key(_event())
    state.record_incoming(key, "interrupted", "alice", "hello", chat_id="dm-chat")
    state.close()
    sent = []
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    service = ChatService(ChatState(path), config, lambda *args, **kwargs: ("unused", []), lambda *args, **kwargs: sent.append(args) or "sent")

    asyncio.run(service.flush_outbox())

    assert service.recovered_interrupted == 1
    assert len(sent) == 1 and "send it again" in sent[0][3].lower()


def test_document_failure_is_sanitized_and_flushed_immediately(tmp_path):
    from teammem.chat.document_worker import DocumentError
    sent = []
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    def model(*args, **kwargs):
        raise DocumentError("unsafe_path", "/private/secret/token")
    service = ChatService(ChatState(tmp_path / "chat.db"), config, model, lambda *args, **kwargs: sent.append(args) or "sent")

    asyncio.run(service.handle(_event()))

    assert len(sent) == 1
    assert "unsafe_path" in sent[0][3]
    assert "secret" not in sent[0][3]


def test_reset_while_waiting_for_global_model_slot_never_calls_old_generation(tmp_path):
    import threading

    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "session": {"max_concurrent_model_calls": 2}, "access": {"default": "deny", "users": {"alice": [], "bob": [], "charlie": []}, "groups": {}, "group_admins": {}}}
    calls, ready, release = [], threading.Event(), threading.Event()
    active = {"count": 0}
    lock = threading.Lock()
    def model(_config, history, *_args, **_kwargs):
        text = history[-1].text if history else "<empty>"
        calls.append(text)
        if text in {"one", "two"}:
            with lock:
                active["count"] += 1
                if active["count"] == 2:
                    ready.set()
            release.wait(2)
        return "answer", []
    service = ChatService(ChatState(tmp_path / "chat.db"), config, model, lambda *args, **kwargs: "sent")
    def event(message_id, sender, text):
        return _event(message={"message_id":message_id, "chat_id":f"{sender}-chat", "chat_type":"p2p", "message_type":"text", "content":json.dumps({"text":text})}, sender={"sender_id":{"open_id":sender}})

    async def run():
        assert await service.enqueue(event("one", "alice", "one"))
        assert await service.enqueue(event("two", "bob", "two"))
        await asyncio.to_thread(ready.wait, 2)
        assert await service.enqueue(event("three", "charlie", "three"))
        await asyncio.sleep(0)
        assert await service.enqueue(event("reset", "charlie", "/new"))
        release.set()
        await asyncio.gather(*tuple(service._tasks.values()))
    asyncio.run(run())

    assert calls == ["one", "two"]
    assert service.state.history(session_key(event("ignored", "charlie", "ignored")), frozenset()) == []


def test_uses_configured_model_concurrency_with_hard_cap(tmp_path):
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "session": {"max_concurrent_model_calls": 1}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}

    service = ChatService(ChatState(tmp_path / "chat.db"), config, lambda *args, **kwargs: ("x", []), lambda *args, **kwargs: "sent")

    assert service._limit._value == 1


def test_forget_cancels_running_model_and_blocks_late_answer_and_attachment_cleanup(tmp_path):
    import threading

    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    entered, release, cancelled, invalidated = threading.Event(), threading.Event(), threading.Event(), []
    def model(*args, **kwargs):
        entered.set()
        release.wait(2)
        cancelled.set() if kwargs["cancel_event"].is_set() else None
        return "late answer", []
    service = ChatService(ChatState(tmp_path / "chat.db"), config, model, lambda *args, **kwargs: "sent", invalidate_session=lambda key: invalidated.append(key))
    turn = _event(message={"message_id":"turn", "chat_id":"dm-chat", "chat_type":"p2p", "message_type":"text", "content":'{"text":"keep working"}'})
    forget = _event(message={"message_id":"forget", "chat_id":"dm-chat", "chat_type":"p2p", "message_type":"text", "content":'{"text":"/forget"}'})

    async def run():
        assert await service.enqueue(turn)
        await asyncio.to_thread(entered.wait, 2)
        assert await service.enqueue(forget)
        release.set()
        await asyncio.gather(*tuple(service._tasks.values()))
    asyncio.run(run())

    assert cancelled.is_set()
    assert invalidated == [session_key(turn)]
    assert service.state.history(session_key(turn), frozenset()) == []
    assert service.state.pending_replies() == []


def test_ambiguous_send_reuses_persisted_uuid_after_reopen_and_quarantines_after_hour(tmp_path):
    path = tmp_path / "chat.db"
    state = ChatState(path)
    key = session_key(_event())
    state.record_incoming(key, "message", "alice", "hello", chat_id="dm-chat")
    assert state.start_incoming(key, "message")
    assert state.queue_reply(key, "message", "00000000-0000-0000-0000-000000000004", "answer", frozenset(), generation=0)
    state.close()
    sent = []
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    def send(_chat, _message, reply_id, _text, **_kwargs):
        sent.append(reply_id)
        if len(sent) == 1:
            raise RuntimeError("ambiguous")
        return "platform-reply"
    service = ChatService(ChatState(path), config, lambda *args, **kwargs: ("unused", []), send)

    async def run():
        await service.flush_outbox()
        await service.flush_outbox()
    asyncio.run(run())

    assert sent == ["00000000-0000-0000-0000-000000000004"] * 2
    assert service.state.pending_replies() == []
    state = ChatState(tmp_path / "expired.db")
    state.record_incoming(key, "expired", "alice", "hello", chat_id="dm-chat", now_ms=0)
    state.start_incoming(key, "expired", now_ms=0)
    state.queue_reply(key, "expired", "00000000-0000-0000-0000-000000000005", "answer", frozenset(), generation=0, now_ms=0)
    state.close()
    state = ChatState(tmp_path / "expired.db")
    assert state.quarantine_expired_replies(now_ms=3_600_001) == 1
    assert state.pending_replies() == []


def test_grant_revocation_hides_old_project_history_and_quarantines_pending_answer(tmp_path):
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": ["alpha"]}, "groups": {}, "group_admins": {}}}
    def model(*args, **kwargs):
        return "private alpha answer", [Evidence("e1", "alpha", "2026-01-01", "evidence", None)]
    def unavailable(*args, **kwargs):
        raise RuntimeError("ambiguous")
    service = ChatService(ChatState(tmp_path / "chat.db"), config, model, unavailable)
    event = _event()

    asyncio.run(service.handle(event))
    config["access"]["users"]["alice"] = []
    asyncio.run(service.flush_outbox())

    assert [turn.text for turn in service.state.history(session_key(event), frozenset())] == ["hello"]
    assert service.state.pending_replies() == []


def test_reconcile_access_forgets_revoked_dm_and_removed_group_sessions(tmp_path):
    current = {"config": {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot", "direct_messages": True}, "access": {"default": "deny", "users": {"alice": [], "bob": []}, "groups": {"chat": []}, "group_admins": {"chat": ["alice"]}}}}
    state = ChatState(tmp_path / "chat.db")
    dm_alice = session_key(_event())
    dm_bob = session_key(_event(sender={"sender_id": {"open_id": "bob"}}))
    group = session_key(_event(message={"message_id":"g", "chat_id":"chat", "chat_type":"group", "message_type":"text", "content":'{"text":"group"}'}, mentions=[{"id":{"open_id":"ou_bot"}}]))
    thread = session_key(_event(message={"message_id":"t", "chat_id":"chat", "chat_type":"group", "root_id":"root", "message_type":"text", "content":'{"text":"thread"}'}, mentions=[{"id":{"open_id":"ou_bot"}}]))
    for key in (dm_alice, dm_bob, group, thread):
        state.append(key, __import__('teammem.chat.state', fromlist=['Turn']).Turn("user", "alice", key.kind, frozenset()))
    invalidated = []
    service = ChatService(state, current["config"], lambda *args, **kwargs: ("x", []), lambda *args, **kwargs: "sent", config_loader=lambda: current["config"], invalidate_session=lambda key: invalidated.append(key))

    current["config"]["access"]["users"].pop("alice")
    assert asyncio.run(service.reconcile_access()) == [dm_alice]
    assert state.history(dm_alice, frozenset()) == []
    assert state.history(dm_bob, frozenset()) != []
    assert state.history(group, frozenset()) != []
    current["config"]["access"]["groups"].pop("chat")

    purged = asyncio.run(service.reconcile_access())

    assert set(purged) == {group, thread}
    assert state.history(group, frozenset()) == []
    assert state.history(thread, frozenset()) == []
    assert set(invalidated) == {dm_alice, group, thread}
    assert invalidated.count(dm_alice) == 2  # repairs attachments after an interrupted first purge


def test_reconcile_access_retains_wildcard_sessions_then_purges_on_wildcard_removal(tmp_path):
    current = {"config": {"feishu": {"tenant_key": "tenant", "app_id": "app",
        "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny",
        "users": {"*": []}, "groups": {"*": []}, "group_admins": {}}}}
    state = ChatState(tmp_path / "chat.db")
    dm = SessionKey("tenant", "app", "dm", "new-user")
    group = SessionKey("tenant", "app", "group", "new-group")
    thread = SessionKey("tenant", "app", "thread", "new-group", "root")
    for key in (dm, group, thread):
        state.append(key, Turn("user", "new-user", key.kind, frozenset()))
    service = ChatService(state, current["config"], lambda *a, **k: ("x", []),
                          lambda *a, **k: "sent", config_loader=lambda: current["config"])

    assert asyncio.run(service.reconcile_access()) == []
    current["config"]["access"]["users"].pop("*")
    assert asyncio.run(service.reconcile_access()) == [dm]
    current["config"]["access"]["groups"].pop("*")
    assert set(asyncio.run(service.reconcile_access())) == {group, thread}


def test_fresh_config_loader_honors_direct_message_disable(tmp_path):
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot", "direct_messages": False}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    called = []
    service = ChatService(ChatState(tmp_path / "chat.db"), config, lambda *args, **kwargs: called.append(1), lambda *args, **kwargs: "sent", config_loader=lambda: config)

    assert asyncio.run(service.enqueue(_event())) is False
    assert called == []


def test_direct_message_toggle_quarantines_pending_reply_but_retains_history(tmp_path):
    current = {"config": {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot", "direct_messages": True}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}}
    sent = []
    def send(*args, **kwargs):
        sent.append(args)
        raise RuntimeError("ambiguous send")
    service = ChatService(ChatState(tmp_path / "chat.db"), current["config"], lambda *args, **kwargs: ("answer", []), send, config_loader=lambda: current["config"])
    event = _event()

    asyncio.run(service.handle(event))
    assert len(sent) == 1 and len(service.state.pending_replies()) == 1
    current["config"]["feishu"]["direct_messages"] = False
    assert asyncio.run(service.reconcile_access()) == []
    asyncio.run(service.flush_outbox())

    assert len(sent) == 1
    assert service.state.pending_replies() == []
    assert [turn.text for turn in service.state.history(session_key(event), frozenset())] == ["hello", "answer"]


def test_direct_message_toggle_cancels_inflight_model_and_discards_late_answer(tmp_path):
    import threading

    current = {"config": {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot", "direct_messages": True}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}}
    entered, release, observed_cancel = threading.Event(), threading.Event(), threading.Event()
    def model(*args, **kwargs):
        entered.set()
        release.wait(2)
        if kwargs["cancel_event"].is_set():
            observed_cancel.set()
        return "late answer", []
    service = ChatService(ChatState(tmp_path / "chat.db"), current["config"], model, lambda *args, **kwargs: "sent", config_loader=lambda: current["config"])
    event = _event()

    async def run():
        assert await service.enqueue(event)
        await asyncio.to_thread(entered.wait, 2)
        current["config"]["feishu"]["direct_messages"] = False
        assert await service.reconcile_access() == []
        release.set()
        await asyncio.gather(*tuple(service._tasks.values()))
    asyncio.run(run())

    assert observed_cancel.is_set()
    assert [turn.text for turn in service.state.history(session_key(event), frozenset())] == ["hello"]
    assert service.state.pending_replies() == []


def test_accepted_message_gets_one_typing_reaction_removed_after_delivery(tmp_path):
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    added, removed = [], []
    service = ChatService(
        ChatState(tmp_path / "chat.db"), config,
        lambda *args, **kwargs: ("answer", []), lambda *args, **kwargs: "reply",
        add_reaction=lambda message_id, emoji, **kwargs: added.append((message_id, emoji)) or "reaction-1",
        remove_reaction=lambda message_id, reaction_id, **kwargs: removed.append((message_id, reaction_id)),
    )

    async def run():
        await service.handle(_event())
        await service.wait_for_reactions()
    asyncio.run(run())

    assert added == [("m1", "Typing")]
    assert removed == [("m1", "reaction-1")]


def test_denied_and_duplicate_events_do_not_create_reactions(tmp_path):
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    added = []
    service = ChatService(ChatState(tmp_path / "chat.db"), config, lambda *a, **k: ("answer", []), lambda *a, **k: "reply",
        add_reaction=lambda *args, **kwargs: added.append(args) or "reaction-1", remove_reaction=lambda *a, **k: None)

    async def run():
        await service.handle(_event())
        await service.handle(_event())
        await service.handle(_event(sender={"sender_id": {"open_id": "mallory"}}, message={"message_id":"denied", "chat_id":"dm", "chat_type":"p2p", "message_type":"text", "content":'{"text":"hello"}'}))
        await service.wait_for_reactions()
    asyncio.run(run())

    assert len(added) == 1


def test_reset_and_revocation_remove_inflight_reaction(tmp_path):
    import threading
    current = {"config": {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot", "direct_messages": True}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}}
    entered, release, removed = threading.Event(), threading.Event(), []
    def model(*args, **kwargs):
        entered.set(); release.wait(2)
        return "late", []
    service = ChatService(ChatState(tmp_path / "chat.db"), current["config"], model, lambda *a, **k: "reply",
        config_loader=lambda: current["config"], add_reaction=lambda *a, **k: "reaction-1",
        remove_reaction=lambda message_id, reaction_id, **kwargs: removed.append((message_id, reaction_id)))

    async def run():
        assert await service.enqueue(_event())
        await asyncio.to_thread(entered.wait, 2)
        current["config"]["access"]["users"].pop("alice")
        await service.reconcile_access()
        release.set()
        await asyncio.gather(*tuple(service._tasks.values()))
        await service.wait_for_reactions()
    asyncio.run(run())

    assert removed == [("m1", "reaction-1")]


def test_provider_and_send_failures_remove_reaction_without_masking_reply_state(tmp_path):
    from teammem.chat.model import ModelError
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    removed = []
    service = ChatService(ChatState(tmp_path / "chat.db"), config,
        lambda *a, **k: (_ for _ in ()).throw(ModelError("provider failed")),
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("send failed")),
        add_reaction=lambda *a, **k: "reaction-1",
        remove_reaction=lambda message_id, reaction_id, **kwargs: removed.append((message_id, reaction_id)))

    async def run():
        await service.handle(_event())
        await service.wait_for_reactions()
    asyncio.run(run())

    assert removed == [("m1", "reaction-1")]
    assert len(service.state.pending_replies()) == 1


def test_reaction_create_failure_does_not_block_model_or_reply(tmp_path):
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    sent = []
    service = ChatService(ChatState(tmp_path / "chat.db"), config, lambda *a, **k: ("answer", []),
        lambda *a, **k: sent.append(1) or "reply",
        add_reaction=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("reaction failed")),
        remove_reaction=lambda *a, **k: None)

    async def run():
        await service.handle(_event())
        await service.wait_for_reactions()
    asyncio.run(run())

    assert sent == [1]


def test_cancel_before_processing_starts_still_removes_reaction(tmp_path):
    config = {"feishu": {"tenant_key": "tenant", "app_id": "app", "expected_bot_open_id": "ou_bot"}, "access": {"default": "deny", "users": {"alice": []}, "groups": {}, "group_admins": {}}}
    removed = []
    service = ChatService(ChatState(tmp_path / "chat.db"), config, lambda *a, **k: ("unused", []), lambda *a, **k: "reply",
        add_reaction=lambda *a, **k: "reaction-1",
        remove_reaction=lambda message_id, reaction_id, **kwargs: removed.append((message_id, reaction_id)))

    async def run():
        assert await service.enqueue(_event())
        service._tasks[("tenant", "app", "m1")].cancel()
        await asyncio.gather(*tuple(service._tasks.values()), return_exceptions=True)
        await service.wait_for_reactions()
    asyncio.run(run())

    assert removed == [("m1", "reaction-1")]
