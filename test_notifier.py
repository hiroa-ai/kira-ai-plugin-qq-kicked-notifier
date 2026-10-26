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
        self.ctx = types.SimpleNamespace(send_message_chain=AsyncMock(return_value=types.SimpleNamespace(ok=True)))
        self.plugin = module.QQKickedNotifier(self.ctx, {})
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
        await self.plugin.initialize()
        self.assertIsNone(self.plugin._task)
        self.plugin.plugin_cfg["notify_enabled"] = True
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


if __name__ == "__main__":
    unittest.main()
