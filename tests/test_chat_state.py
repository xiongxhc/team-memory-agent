from teammem.chat.state import ChatState, SessionKey, Turn


def test_private_session_survives_reopen_without_cross_user_history(tmp_path):
    path = tmp_path / "chat.sqlite3"
    alice = SessionKey("tenant", "app", "dm", "alice")
    bob = SessionKey("tenant", "app", "dm", "bob")
    state = ChatState(path)
    state.append(alice, Turn("user", "alice", "my private draft", frozenset()))
    state.close()

    state = ChatState(path)
    assert [turn.text for turn in state.history(alice, frozenset())] == ["my private draft"]
    assert state.history(bob, frozenset()) == []
    state.close()


def test_group_main_and_thread_sessions_are_isolated_but_group_is_shared(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    main = SessionKey("tenant", "app", "group", "chat-1")
    thread = SessionKey("tenant", "app", "thread", "chat-1", "root-1")
    state.append(main, Turn("user", "alice", "main question", frozenset()))

    assert [turn.text for turn in state.history(main, frozenset())] == ["main question"]
    assert state.history(thread, frozenset()) == []
    state.close()


def test_tenant_and_app_are_part_of_every_session_lookup(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    original = SessionKey("tenant-a", "app-a", "dm", "alice")
    state.append(original, Turn("user", "alice", "private", frozenset()))

    assert state.history(SessionKey("tenant-b", "app-a", "dm", "alice"), frozenset()) == []
    assert state.history(SessionKey("tenant-a", "app-b", "dm", "alice"), frozenset()) == []
    state.close()


def test_history_excludes_expired_turns_after_inactivity(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3", idle_retention_days=1)
    key = SessionKey("tenant", "app", "dm", "alice")
    state.append(key, Turn("user", "alice", "old", frozenset()), now_ms=0)

    assert state.history(key, frozenset(), now_ms=86_400_001) == []
    state.close()


def test_history_excludes_turns_with_revoked_project_scope(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    key = SessionKey("tenant", "app", "dm", "alice")
    state.append(key, Turn("assistant", "bot", "alpha fact", frozenset({"alpha"})))
    state.append(key, Turn("assistant", "bot", "public chat", frozenset()))

    assert [turn.text for turn in state.history(key, frozenset())] == ["public chat"]
    assert [turn.text for turn in state.history(key, frozenset({"alpha"}))] == [
        "alpha fact", "public chat"
    ]
    state.close()


def test_reset_increments_generation_and_removes_history(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    key = SessionKey("tenant", "app", "dm", "alice")
    state.append(key, Turn("user", "alice", "before new", frozenset()))

    assert state.reset(key) == 1
    assert state.generation(key) == 1
    assert state.history(key, frozenset()) == []
    state.close()


def test_reset_persists_across_reopen_and_rejects_late_generation_work(tmp_path):
    path = tmp_path / "chat.sqlite3"
    key = SessionKey("tenant", "app", "dm", "alice")
    state = ChatState(path)
    state.append(key, Turn("user", "alice", "old context", frozenset()))
    assert state.reset(key) == 1
    state.close()

    state = ChatState(path)
    assert state.generation(key) == 1
    assert state.history(key, frozenset()) == []
    assert state.append(key, Turn("assistant", "bot", "late", frozenset()), generation=0) is False
    state.close()


def test_forget_removes_content_but_keeps_content_free_dedup_tombstone(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    key = SessionKey("tenant", "app", "dm", "alice")
    assert state.record_incoming(key, "message-1", "alice", "please erase") is True
    state.append(key, Turn("user", "alice", "please erase", frozenset()))

    state.forget(key)

    assert state.history(key, frozenset()) == []
    assert state.record_incoming(key, "message-1", "alice", "new content") is False
    assert state.incoming_content(key, "message-1") is None
    state.close()


def test_duplicate_incoming_event_is_deduplicated_by_tenant_app_and_message(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    first = SessionKey("tenant", "app", "dm", "alice")
    second_app = SessionKey("tenant", "second-app", "dm", "alice")

    assert state.record_incoming(first, "message-1", "alice", "hello") is True
    assert state.record_incoming(first, "message-1", "alice", "hello again") is False
    assert state.record_incoming(second_app, "message-1", "alice", "other app") is True
    state.close()


def test_incoming_generation_is_the_generation_captured_at_admission(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    key = SessionKey("tenant", "app", "dm", "alice")
    assert state.record_incoming(key, "message-1", "alice", "hello") is True
    assert state.incoming_generation(key, "message-1") == 0
    state.reset(key)

    assert state.incoming_generation(key, "message-1") == 0
    state.close()


def test_only_one_worker_can_start_an_admitted_incoming_message(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    key = SessionKey("tenant", "app", "dm", "alice")
    state.record_incoming(key, "message-1", "alice", "hello")

    assert state.start_incoming(key, "message-1") is True
    assert state.start_incoming(key, "message-1") is False
    assert state.finish_incoming(key, "message-1", "failed") is True
    state.close()


def test_abandoned_processing_is_reclaimable_only_after_timeout(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    key = SessionKey("tenant", "app", "dm", "alice")
    state.record_incoming(key, "message-1", "alice", "hello", now_ms=100)
    assert state.start_incoming(key, "message-1", now_ms=100) is True

    assert state.reclaim_abandoned_processing(now_ms=199, stale_after_ms=100) == 0
    assert state.reclaim_abandoned_processing(now_ms=200, stale_after_ms=100) == 1
    assert state.start_incoming(key, "message-1", now_ms=200) is True
    state.close()


def test_reply_is_bound_to_admitted_generation_and_invalidated_by_reset(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    key = SessionKey("tenant", "app", "dm", "alice")
    state.record_incoming(key, "message-1", "alice", "hello")
    assert state.start_incoming(key, "message-1") is True
    assert state.queue_reply(
        key, "message-1", "00000000-0000-0000-0000-000000000001", "answer", frozenset({"alpha"}), generation=0
    ) is True
    state.reset(key)

    assert state.pending_replies() == []
    assert state.queue_reply(
        key, "message-1", "00000000-0000-0000-0000-000000000002", "late answer", frozenset(), generation=0
    ) is False
    state.close()


def test_queued_reply_survives_reopen_with_provenance_and_can_be_delivered(tmp_path):
    path = tmp_path / "chat.sqlite3"
    key = SessionKey("tenant", "app", "dm", "alice")
    state = ChatState(path)
    state.record_incoming(key, "message-1", "alice", "hello")
    state.start_incoming(key, "message-1")
    state.queue_reply(key, "message-1", "00000000-0000-0000-0000-000000000001", "answer", frozenset({"alpha"}), generation=0)
    state.close()

    state = ChatState(path)
    reply = state.pending_replies()[0]
    assert (reply.session, reply.message_id, reply.projects, reply.generation, reply.status) == (
        key, "message-1", frozenset({"alpha"}), 0, "queued"
    )
    assert state.mark_reply_delivered("00000000-0000-0000-0000-000000000001", "platform-message") is True
    assert state.pending_replies() == []
    state.close()


def test_queued_reply_retains_original_chat_sender_and_admitted_grants(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    key = SessionKey("tenant", "app", "dm", "alice")
    state.record_incoming(
        key, "message-1", "alice", "hello", chat_id="oc_dm_chat", projects=frozenset({"alpha"})
    )
    state.start_incoming(key, "message-1")
    state.queue_reply(
        key, "message-1", "00000000-0000-0000-0000-000000000002", "answer", frozenset({"alpha"}), generation=0
    )

    reply = state.pending_replies()[0]
    assert (reply.chat_id, reply.sender, reply.admitted_projects) == (
        "oc_dm_chat", "alice", frozenset({"alpha"})
    )
    state.close()


def test_command_admission_deduplicates_before_reset(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    key = SessionKey("tenant", "app", "dm", "alice")

    assert state.apply_command(key, "new-1", "alice", "/new", forget=False) is True
    assert state.generation(key) == 1
    assert state.apply_command(key, "new-1", "alice", "/new", forget=False) is False
    assert state.generation(key) == 1
    state.close()


def test_thread_seed_requires_recorded_current_parent_in_same_chat(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    main = SessionKey("tenant", "app", "group", "chat")
    thread = SessionKey("tenant", "app", "thread", "chat", "root")
    state.record_incoming(main, "question", "alice", "What is the target?", chat_id="chat")
    assert state.start_incoming(main, "question")
    assert state.queue_reply(main, "question", "00000000-0000-0000-0000-000000000003", "The target is 42.", frozenset({"alpha"}), generation=0)
    assert state.record_interaction(main, "question", "The target is 42.", frozenset({"alpha"}), generation=0)
    assert state.mark_reply_delivered("00000000-0000-0000-0000-000000000003", "bot-answer")

    assert state.seed_thread(thread, "question", frozenset({"alpha"})) is True
    assert [(turn.role, turn.text) for turn in state.history(thread, frozenset({"alpha"}))] == [
        ("user", "What is the target?"), ("assistant", "The target is 42."),
    ]
    assert state.seed_thread(SessionKey("tenant", "app", "thread", "other-chat", "root"), "question", frozenset({"alpha"})) is False
    state.reset(main)
    assert state.seed_thread(SessionKey("tenant", "app", "thread", "chat", "other-root"), "bot-answer", frozenset({"alpha"})) is False
    state.close()


def test_recovery_turns_interrupted_incoming_into_durable_resend_reply(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    key = SessionKey("tenant", "app", "dm", "alice")
    state.record_incoming(key, "queued", "alice", "first", chat_id="dm-chat")
    state.record_incoming(key, "processing", "alice", "second", chat_id="dm-chat")
    assert state.start_incoming(key, "processing")

    assert state.recover_interrupted() == 2
    replies = state.pending_replies()
    assert {(reply.message_id, reply.chat_id) for reply in replies} == {("queued", "dm-chat"), ("processing", "dm-chat")}
    assert all("restarted" in reply.text.lower() for reply in replies)
    assert state.start_incoming(key, "queued") is False
    assert state.start_incoming(key, "processing") is False
    state.close()


def test_idle_expiry_purges_content_without_attachment_maintenance(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3", idle_retention_days=1)
    key = SessionKey("tenant", "app", "dm", "alice")
    state.record_incoming(key, "message-1", "alice", "private question", chat_id="dm", now_ms=0)
    state.start_incoming(key, "message-1", now_ms=0)
    state.queue_reply(key, "message-1", "00000000-0000-0000-0000-000000000006", "private answer", frozenset(), generation=0, now_ms=0)
    state.record_interaction(key, "message-1", "private answer", frozenset(), generation=0, now_ms=0)
    state.append(key, Turn("assistant", "bot", "private answer", frozenset()), generation=0, now_ms=0)
    state.mark_reply_delivered("00000000-0000-0000-0000-000000000006", "platform-answer")

    assert state.expire_idle(now_ms=86_400_001) == 1
    assert state.history(key, frozenset(), now_ms=86_400_001) == []
    assert state.incoming_content(key, "message-1") is None
    assert state.pending_replies() == []
    assert state._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert state._conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 0
    assert state.record_incoming(key, "message-1", "alice", "new private content", now_ms=86_400_001) is False
    state.close()


def test_reaction_identity_survives_reopen_for_cleanup(tmp_path):
    path = tmp_path / "chat.sqlite3"
    key = SessionKey("tenant", "app", "dm", "alice")
    state = ChatState(path)
    assert state.generation(key) == 0
    assert state.request_reaction(key, "message-1", "Typing", generation=0)
    assert state.record_reaction_created("tenant", "app", "message-1", "reaction-1") is True
    state.close()

    state = ChatState(path)
    assert state.recover_reactions() == 1
    cleanup = state.pending_reaction_cleanup()
    assert [(item.message_id, item.reaction_id) for item in cleanup] == [("message-1", "reaction-1")]
    assert state.mark_reaction_removed("tenant", "app", "message-1", "reaction-1")
    assert state.pending_reaction_cleanup() == []
    state.close()


def test_reset_requests_cleanup_for_active_reaction(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    key = SessionKey("tenant", "app", "dm", "alice")
    assert state.generation(key) == 0
    state.request_reaction(key, "message-1", "Typing", generation=0)
    state.record_reaction_created("tenant", "app", "message-1", "reaction-1")

    state.reset(key)

    assert [item.reaction_id for item in state.pending_reaction_cleanup()] == ["reaction-1"]
    state.close()
