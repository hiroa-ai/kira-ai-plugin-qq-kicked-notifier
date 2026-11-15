import asyncio
import json
import time
from datetime import datetime

import httpx

from core.chat import MessageChain
from core.plugin import BasePlugin, logger


DEFAULT_TEMPLATE = (
    "QQ 离线告警（不一定是腾讯踢下线）\n时间：{time}\n适配器：{adapter}\n"
    "QQ：{qq}\n信号：{signal}\n原因：{tag} {message}\n事件原文：{event}"
)

DEFAULT_RECORD_TEMPLATE = "[系统通知] {adapter} 适配器账号 {qq} 于 {time} 掉线（{tag}：{message}）"


class QQKickedNotifier(BasePlugin):
    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        self._task = None
        self._states = {}

    async def initialize(self):
        if self.plugin_cfg.get("notify_enabled", False) or self.plugin_cfg.get("record_enabled", False):
            self._task = asyncio.create_task(self._monitor())

    async def terminate(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        for state in self._states.values():
            self._detach(state)
        self._states.clear()

    def _detach(self, state):
        for kind, callback in state["hooks"]:
            callbacks = state["client"].event_callbacks.get(kind, [])
            if callback in callbacks:
                callbacks.remove(callback)

    def _attach(self, adapter):
        client = adapter.get_client()
        state = dict(client=client, hooks=[], pending=None, sent_events=set(), next_retry=0, recorded=None)

        async def receive(data):
            if not isinstance(data, dict):
                return
            if data.get("post_type") != "notice" or data.get("notice_type") != "bot_offline":
                return
            raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
            if raw not in state["sent_events"]:
                state["pending"] = (raw, data.get("tag", ""), data.get("message", ""))

        client.notice_event()(receive)
        state["hooks"].append(("notice", receive))
        return state

    async def _monitor(self):
        try:
            while True:
                adapters = dict(self.ctx.adapter_mgr.get_adapters())
                for name, state in list(self._states.items()):
                    adapter = adapters.get(name)
                    if adapter is None or adapter.get_client() is not state["client"]:
                        self._detach(state)
                        del self._states[name]
                for name, adapter in adapters.items():
                    if adapter.info.platform.lower() != "qq" or not adapter.info.enabled:
                        continue
                    try:
                        if name not in self._states:
                            self._states[name] = self._attach(adapter)
                        await self._check(adapter, self._states[name])
                    except Exception as exc:
                        logger.warning(f"[QQKickedNotifier] Monitor failed: {type(exc).__name__}")
                await asyncio.sleep(2)
        finally:
            for state in self._states.values():
                self._detach(state)

    async def _check(self, adapter, state):
        client = state["client"]
        now = time.monotonic()
        if state["pending"] and now >= state["next_retry"]:
            state["next_retry"] = now + 60
            pending = state["pending"]
            raw, tag, reason = pending
            values = dict(time=datetime.now().astimezone().isoformat(timespec="seconds"),
                          adapter=adapter.info.name,
                          qq=str(client.self_id or adapter.config.get("bot_pid", "unknown")),
                          event=raw, signal="bot_offline", tag=tag, message=reason)
            try:
                await self._record_offline(adapter, values, state)
            except Exception as exc:
                # Do not log exception messages: they can contain configuration secrets.
                logger.warning(f"[QQKickedNotifier] Record failed ({type(exc).__name__}); retry in 60s")
            if not self.plugin_cfg.get("notify_enabled", False):
                # Record-only mode: the event is handled once the record pass completed.
                if state["recorded"] == raw:
                    state["sent_events"].add(raw)
                    if state["pending"] == pending:
                        state["pending"] = None
                return
            template = self.plugin_cfg.get("notify_template") or self.plugin_cfg.get("template") or DEFAULT_TEMPLATE
            try:
                try:
                    message = template.format_map(values)
                except (AttributeError, KeyError, ValueError, IndexError, TypeError):
                    logger.warning("[QQKickedNotifier] Invalid template; using default")
                    message = DEFAULT_TEMPLATE.format_map(values)
                await self._send(adapter.info.name, message)
            except Exception as exc:
                # Do not log exception messages: HTTP errors can contain a secret topic URL.
                logger.warning(f"[QQKickedNotifier] Notification failed ({type(exc).__name__}); retry in 60s")
            else:
                state["sent_events"].add(raw)
                if state["pending"] == pending:
                    state["pending"] = None
                logger.info(f"[QQKickedNotifier] Notification delivered for {adapter.info.name}")

    async def _record_offline(self, adapter, values, state):
        """Write the offline fact into the adapter's own sessions, independent of notify success."""
        raw = values["event"]
        if state["recorded"] == raw:
            return
        if not self.plugin_cfg.get("record_enabled", False):
            state["recorded"] = raw
            return
        session_mgr = getattr(self.ctx, "session_mgr", None) if self.ctx is not None else None
        if session_mgr is None:
            state["recorded"] = raw
            return
        template = self.plugin_cfg.get("record_template") or DEFAULT_RECORD_TEMPLATE
        try:
            text = template.format_map(values)
        except (AttributeError, KeyError, ValueError, IndexError, TypeError):
            logger.warning("[QQKickedNotifier] Invalid record template; using default")
            text = DEFAULT_RECORD_TEMPLATE.format_map(values)
        chunk = [{"role": "user", "content": text}]
        sessions = await session_mgr.get_sessions(include_content=False, kind="im")
        written = 0
        for session in sessions:
            if session.adapter_name != adapter.info.name or session.session_type not in ("dm", "gm"):
                continue
            try:
                ok = await session_mgr.update_memory(session.sid, chunk, require_existing=True)
            except Exception as exc:
                logger.warning(f"[QQKickedNotifier] Record failed for {session.sid} ({type(exc).__name__})")
                continue
            if ok:
                written += 1
        state["recorded"] = raw
        logger.info(f"[QQKickedNotifier] Offline record written to {written} sessions of {adapter.info.name}")

    def _target_session(self, source_adapter):
        """Validate the shared cross-adapter target session and return its SID."""
        target = self.plugin_cfg.get("session", {}).get("target_session_id", "")
        parts = target.split(":", 2)
        if len(parts) != 3 or not all(parts) or parts[1] not in ("dm", "gm"):
            raise ValueError("Invalid target session")
        if parts[0] == source_adapter:
            raise ValueError("Notification must use a different adapter")
        return target

    async def _send(self, source_adapter, message):
        method = self.plugin_cfg.get("notify_method", "ntfy")
        if method == "ntfy":
            cfg = self.plugin_cfg.get("ntfy", {})
            url = cfg.get("topic_url", "")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                raise ValueError("Configure ntfy.topic_url")
            headers = {"Content-Type": "text/plain; charset=utf-8"}
            token = cfg.get("authorization", "").strip()
            if token:
                headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(url, content=message.encode("utf-8"), headers=headers)
                response.raise_for_status()
        elif method == "email":
            address = self.plugin_cfg.get("email", {}).get("address", "")
            plugin = self.ctx.get_plugin_inst("kira-ai-plugin-email")
            if not address or plugin is None:
                raise ValueError("Configure recipient and enable kira-ai-plugin-email")
            result = await plugin.send_email(None, to=address, subject="KiraAI QQ 离线告警", body=message)
            if not isinstance(result, str) or not result.startswith("✅"):
                raise RuntimeError("Email delivery failed")
        elif method == "session":
            result = await self.ctx.send_message_chain(self._target_session(source_adapter), MessageChain().text(message))
            if not result.ok:
                raise RuntimeError("Session delivery failed")
        elif method == "llm":
            # Publish as a system notice so the target session's LLM perceives and
            # words the outage itself; raises when the target adapter is missing.
            await self.ctx.publish_notice(self._target_session(source_adapter), MessageChain().text(message))
        else:
            raise ValueError("Only ntfy/email/session/llm are supported; original cannot work offline")


plugin_class = QQKickedNotifier
