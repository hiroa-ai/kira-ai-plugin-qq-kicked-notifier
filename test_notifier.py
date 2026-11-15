"""Isolated contract tests; no live adapters, credentials or network required."""
import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


def load_plugin():
    class BasePlugin:
        def __init__(self, ctx, cfg):
            self.ctx, self.plugin_cfg = ctx, cfg

    class MessageChain:
        def text(self, text):
            self.content = text
            return self

    chat = types.ModuleType("core.chat")
    chat.MessageChain = MessageChain
    plugin = types.ModuleType("core.plugin")
    plugin.BasePlugin = BasePlugin
    plugin.logger = types.SimpleNamespace(info=lambda *_: None, warning=lambda *_: None)
    spec = importlib.util.spec_from_file_location("qq_notifier_test", Path(__file__).with_name("main.py"))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"core.chat": chat, "core.plugin": plugin}):
        spec.loader.exec_module(module)
    return module


module = load_plugin()


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = types.SimpleNamespace(
            self_id="123", websocket=types.SimpleNamespace(closed=False),
            shutdown_event=asyncio.Event(), event_callbacks={"meta": [], "notice": [], "napcat": []},
            send_action=AsyncMock(return_value={"status": "ok", "data": {"online": True}}),
        )
        for kind in self.client.event_callbacks:
            setattr(self.client, kind + "_event", lambda kind=kind: lambda fn: self.client.event_callbacks[kind].append(fn))
        self.adapter = types.SimpleNamespace(
            info=types.SimpleNamespace(name="qq", platform="qq", enabled=True),
            config={"bot_pid": "123"}, get_client=lambda: self.client,
            permanently_disconnected=False, _stopping=False,
        )
        self.ctx = types.SimpleNamespace(
            send_message_chain=AsyncMock(return_value=types.SimpleNamespace(ok=True)),
            publish_notice=AsyncMock(),
        )
        self.plugin = module.QQKickedNotifier(self.ctx, {"notify_enabled": True})
        self.plugin._send = AsyncMock()
        self.state = self.plugin._attach(self.adapter)

    async def emit_offline(self, **extra):
        payload = {"post_type": "notice", "notice_type": "bot_offline",
                   "tag": "KickedOffLine", "message": "你的账号当前登录已失效，请重新登录",
                   "time": 1, **extra}
        await self.client.event_callbacks["notice"][0](payload)
        return payload

    async def test_bot_offline_raw_reason_and_dedup(self):
        payload = await self.emit_offline()
        await self.plugin._check(self.adapter, self.state)
        rendered = self.plugin._send.call_args.args[1]
        self.assertIn(payload["tag"], rendered)
        self.assertIn(payload["message"], rendered)
        self.assertIn('"notice_type": "bot_offline"', rendered)
        self.assertIn("123", rendered)
        await self.emit_offline()
        self.state["next_retry"] = 0
        await self.plugin._check(self.adapter, self.state)
        self.assertEqual(self.plugin._send.await_count, 1)
        await self.emit_offline(time=2)
        await self.plugin._check(self.adapter, self.state)
        self.assertEqual(self.plugin._send.await_count, 2)

    async def test_only_exact_notice_triggers(self):
        receive = self.client.event_callbacks["notice"][0]
        for payload in (None, [], {},
                        {"post_type": "message", "notice_type": "bot_offline"},
                        {"notice_type": "bot_offline"},
                        {"post_type": "notice", "notice_type": "BOT_OFFLINE"},
                        {"post_type": "notice", "notice_type": "offline",
                         "message": "[KickedOffLine] 登录已失效 下线通知"},
                        {"post_type": "meta_event", "meta_event_type": "heartbeat",
                         "status": {"online": False}}):
            with self.subTest(payload=payload):
                await receive(payload)
                await self.plugin._check(self.adapter, self.state)
                self.assertIsNone(self.state["pending"])
        self.plugin._send.assert_not_awaited()
        self.assertFalse(self.client.event_callbacks["meta"])
        self.assertFalse(self.client.event_callbacks["napcat"])

    async def test_disconnect_never_triggers_or_probes(self):
        self.client.websocket = None
        self.adapter.permanently_disconnected = True
        await self.plugin._check(self.adapter, self.state)
        self.plugin._send.assert_not_awaited()
        self.client.send_action.assert_not_awaited()
        await self.emit_offline()
        await self.plugin._check(self.adapter, self.state)
        self.plugin._send.assert_awaited_once()

    async def test_enabled_switch(self):
        disabled = module.QQKickedNotifier(self.ctx, {"notify_enabled": False, "record_enabled": False})
        await disabled.initialize()
        self.assertIsNone(disabled._task)
        self.plugin._monitor = AsyncMock()
        await self.plugin.initialize()
        await asyncio.sleep(0)
        self.plugin._monitor.assert_awaited_once()
        await self.plugin.terminate()

    async def test_retry_and_cleanup(self):
        await self.emit_offline()
        self.plugin._send.side_effect = RuntimeError("failure")
        await self.plugin._check(self.adapter, self.state)
        self.assertIsNotNone(self.state["pending"])
        await self.plugin._check(self.adapter, self.state)
        self.assertEqual(self.plugin._send.await_count, 1)
        self.state["next_retry"] = 0
        self.plugin._send.side_effect = None
        await self.plugin._check(self.adapter, self.state)
        self.assertIsNone(self.state["pending"])
        self.plugin._states["qq"] = self.state
        await self.plugin.terminate()
        self.assertTrue(all(not callbacks for callbacks in self.client.event_callbacks.values()))

    async def test_ntfy_bearer_utf8_and_http_failure(self):
        plugin = module.QQKickedNotifier(self.ctx, {"ntfy": {"topic_url": "https://example.test/topic", "authorization": "masked"}})
        client = AsyncMock()
        response = types.SimpleNamespace(raise_for_status=lambda: None)
        client.post.return_value = response
        with patch.object(module.httpx, "AsyncClient") as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=client)
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            await plugin._send("qq", "下线")
        self.assertEqual(client.post.call_args.kwargs["headers"]["Authorization"], "Bearer masked")
        self.assertEqual(client.post.call_args.kwargs["content"], "下线".encode())

    async def test_session_requires_other_adapter(self):
        plugin = module.QQKickedNotifier(self.ctx, {"notify_method": "session", "session": {"target_session_id": "qq:dm:123"}})
        with self.assertRaises(ValueError):
            await plugin._send("qq", "alert")
        plugin.plugin_cfg["session"]["target_session_id"] = "telegram:dm:456"
        await plugin._send("qq", "alert")
        self.ctx.send_message_chain.assert_awaited_once()

    async def test_invalid_method_and_topic_url(self):
        for method in ("original", "invalid", None, 123, [], {}):
            with self.subTest(method=method):
                plugin = module.QQKickedNotifier(self.ctx, {"notify_method": method})
                with self.assertRaises(ValueError):
                    await plugin._send("qq", "alert")
        for url in ("", None, 123, "ftp://example.test/topic"):
            with self.subTest(url=url):
                plugin = module.QQKickedNotifier(self.ctx, {"ntfy": {"topic_url": url}})
                with self.assertRaises(ValueError):
                    await plugin._send("qq", "alert")

    async def test_template_defaults_and_invalid_placeholders(self):
        for template in (None, "", "plain text", "{unknown}", "{", 123, "{time.missing}"):
            with self.subTest(template=template):
                self.plugin.plugin_cfg["notify_template"] = template
                self.state["pending"] = ("login expired", "KickedOffLine", "login expired")
                self.state["next_retry"] = 0
                await self.plugin._check(self.adapter, self.state)
                self.assertIsNone(self.state["pending"])
                rendered = self.plugin._send.call_args.args[1]
                if template == "plain text":
                    self.assertEqual(rendered, "plain text")
                else:
                    self.assertIn("login expired", rendered)

    async def test_email_plugin_contract(self):
        email = types.SimpleNamespace(send_email=AsyncMock(return_value="✅ 邮件已发送！"))
        self.ctx.get_plugin_inst = lambda _: email
        plugin = module.QQKickedNotifier(self.ctx, {"notify_method": "email", "email": {"address": "admin@example.test"}})
        await plugin._send("qq", "alert")
        email.send_email.assert_awaited_once_with(None, to="admin@example.test", subject="KiraAI QQ 离线告警", body="alert")

    async def test_llm_publish_notice(self):
        plugin = module.QQKickedNotifier(self.ctx, {"notify_method": "llm", "session": {"target_session_id": "qq:dm:123"}})
        with self.assertRaises(ValueError):
            await plugin._send("qq", "alert")
        plugin.plugin_cfg["session"]["target_session_id"] = "telegram:dm:456"
        await plugin._send("qq", "alert")
        self.ctx.publish_notice.assert_awaited_once()
        target, chain = self.ctx.publish_notice.call_args.args
        self.assertEqual(target, "telegram:dm:456")
        self.assertEqual(chain.content, "alert")

    def make_session_mgr(self, sessions):
        return types.SimpleNamespace(
            get_sessions=AsyncMock(return_value=sessions),
            update_memory=AsyncMock(return_value=True),
        )

    async def test_record_writes_adapter_sessions(self):
        sessions = [
            types.SimpleNamespace(sid="qq:dm:1", adapter_name="qq", session_type="dm"),
            types.SimpleNamespace(sid="qq:gm:2", adapter_name="qq", session_type="gm"),
            types.SimpleNamespace(sid="qq:gm:3", adapter_name="qq", session_type="thread"),
            types.SimpleNamespace(sid="tg:dm:4", adapter_name="telegram", session_type="dm"),
        ]
        session_mgr = self.make_session_mgr(sessions)
        self.ctx.session_mgr = session_mgr
        self.plugin.plugin_cfg["record_enabled"] = True
        self.plugin.plugin_cfg["record_template"] = "{bad}"
        payload = await self.emit_offline()
        await self.plugin._check(self.adapter, self.state)
        self.assertEqual([c.args[0] for c in session_mgr.update_memory.await_args_list], ["qq:dm:1", "qq:gm:2"])
        for call in session_mgr.update_memory.await_args_list:
            self.assertTrue(call.kwargs["require_existing"])
        chunk = session_mgr.update_memory.await_args_list[0].args[1]
        self.assertEqual(chunk[0]["role"], "user")
        self.assertIn("[系统通知]", chunk[0]["content"])
        self.assertIn(payload["tag"], chunk[0]["content"])
        self.assertIsNotNone(self.state["recorded"])
        # the same event fingerprint is recorded once
        self.state["next_retry"] = 0
        await self.plugin._check(self.adapter, self.state)
        self.assertEqual(session_mgr.update_memory.await_count, 2)
        # guards: disabled record and missing session manager never enumerate
        guard_mgr = self.make_session_mgr([])
        self.ctx.session_mgr = guard_mgr
        self.plugin.plugin_cfg["record_enabled"] = False
        await self.plugin._record_offline(self.adapter, {"event": "raw-2"}, dict(recorded=None))
        guard_mgr.get_sessions.assert_not_awaited()
        bare = module.QQKickedNotifier(types.SimpleNamespace(publish_notice=AsyncMock()), {"record_enabled": True})
        await bare._record_offline(self.adapter, {"event": "raw-3"}, dict(recorded=None))

    async def test_record_only_mode_skips_send(self):
        session_mgr = self.make_session_mgr([types.SimpleNamespace(sid="qq:dm:1", adapter_name="qq", session_type="dm")])
        self.ctx.session_mgr = session_mgr
        self.plugin.plugin_cfg["notify_enabled"] = False
        self.plugin.plugin_cfg["record_enabled"] = True
        await self.emit_offline()
        await self.plugin._check(self.adapter, self.state)
        session_mgr.update_memory.assert_awaited_once()
        self.plugin._send.assert_not_awaited()
        self.assertIsNone(self.state["pending"])

    async def test_record_independent_of_notify_failure(self):
        session_mgr = self.make_session_mgr([types.SimpleNamespace(sid="qq:dm:1", adapter_name="qq", session_type="dm")])
        self.ctx.session_mgr = session_mgr
        self.plugin.plugin_cfg["record_enabled"] = True
        self.plugin._send = AsyncMock(side_effect=RuntimeError("failure"))
        await self.emit_offline()
        await self.plugin._check(self.adapter, self.state)
        session_mgr.update_memory.assert_awaited_once()
        self.assertIsNotNone(self.state["pending"])

    async def test_monitor_starts_for_record_only(self):
        plugin = module.QQKickedNotifier(self.ctx, {"record_enabled": True})
        plugin._monitor = AsyncMock()
        await plugin.initialize()
        await asyncio.sleep(0)
        plugin._monitor.assert_awaited_once()
        await plugin.terminate()
        disabled = module.QQKickedNotifier(self.ctx, {})
        await disabled.initialize()
        self.assertIsNone(disabled._task)


if __name__ == "__main__":
    unittest.main()
