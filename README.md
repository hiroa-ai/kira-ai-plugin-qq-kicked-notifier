# QQ 下线通知（qq-kicked-notifier）

监测 KiraAI 的 NapCat QQ 适配器，通过 **ntfy、邮件或另一个在线适配器的会话**通知管理员。通知包含时间、适配器名、QQ 账号及可取得的原始控制事件。不依赖故障 QQ 连接发送通知，不支持 `original` 原地通知。

## 特性与适用范围

- 多 QQ 适配器自动发现，客户端替换后重新挂接，卸载时解除回调、取消后台任务。
- 仅监听精确的 bot_offline notice 事件，不读取普通群聊/私聊消息。
- 相同事件去重、发送失败重试、可配置模板。
- ntfy 使用 UTF-8、Bearer token、10 秒 HTTP 超时及 HTTP 错误检查；日志不输出 token、topic URL 或网络异常详情。
- 只新增插件，不修改平台核心，也不替换适配器原有永久断线回调。

## 触发机制：唯一精确信号

目前只依赖 OneBot 的 `post_type="notice"` 且 `notice_type="bot_offline"` 事件。NapCat 收到 `onKickedOffLine` 后会构造该事件；本插件仅订阅 QQ 客户端的 `notice_event()`，读取 `tag` 和 `message` 作为下线原因，保留完整事件 JSON。示例：

```json
{
  "post_type": "notice",
  "notice_type": "bot_offline",
  "self_id": 123456789,
  "time": 1760000000,
  "tag": "KickedOffLine",
  "message": "你的账号当前登录已失效，请重新登录"
}
```

**若 NapCat/平台没有上报或转发此事件，就无法告警，没有任何兜底。** 心跳离线状态、API 状态、WebSocket 断开/重连耗尽均不会触发，也不进行 get_status 轮询。其他事件即使包含踢下线关键词也不会触发。

后台约每 2 秒发现启用的 `platform=qq` 适配器并处理待通知事件；这只是客户端发现与重试调度，不是连接/账号状态探测。客户端替换后重新挂接，卸载时解除回调。事件必须在插件已挂接时被客户端收到，不补查历史事件。接口依赖当前 QQ 客户端的 `get_client()`、`notice_event()` 和 `event_callbacks`，升级后需复核。

## 安装与配置

目录：`/app/data/plugins/qq-kicked-notifier/`。manifest 的 `plugin_id=qq-kicked-notifier`、`version=1.0.0`、`display_name=QQ 下线通知`；入口为 `main.py` 中的 `QQKickedNotifier(BasePlugin)`，配置字段由 `schema.json` 提供（嵌套配置使用标准 `section/fields`）。

通过 WebUI 插件管理扫描/加载本插件，并在插件设置里配置、保存、启用/重新加载。首次 `notify_enabled=false`，不会自动发送通知。配置通常保存在 `/app/data/config/plugins/qq-kicked-notifier.json`（自定义 data 路径时相应变化）。不要覆盖已有配置或将真实密钥提交到版本库。

### ntfy（推荐）

```json
{
  "notify_enabled": true,
  "notify_method": "ntfy",
  "ntfy": {
    "topic_url": "https://ntfy.example.com/qq-admin-alerts",
    "authorization": "Bearer ***MASKED***"
  },
  "email": {"address": ""},
  "session": {"target_session_id": ""},
  "notify_template": "时间：{time}\n适配器：{adapter}\nQQ：{qq}\n信号：{signal}\n事件原文：{event}"
}
```

`topic_url` 必须为非空 HTTP(S) URL；推荐 HTTPS 和私有 topic。`authorization` 可填纯 token 或 `Bearer token`，无认证可留空。管理员须在 ntfy 客户端订阅相同 topic。topic URL 本身也可能是秘密。

### email

```json
{
  "notify_enabled": true,
  "notify_method": "email",
  "email": {"address": "admin@example.com"}
}
```

需要**已加载且配置正确**的 `kira-ai-plugin-email`。该插件中配置 `smtp_host`、`smtp_port`、`smtp_use_ssl`、`email_address`、`email_password`（示例授权码 `***MASKED***`）；本插件的 `email.address` 是**收件人**，不是 SMTP 发件人。收件人还须满足邮件插件的 `allowed_recipients` 白名单。本插件复用其 `send_email(None, to=..., subject=..., body=...)`，只有返回 `✅` 开头才算成功，因此与该接口/返回格式耦合。无需 QQ 在线。

### session（跨适配器）

```json
{
  "notify_enabled": true,
  "notify_method": "session",
  "session": {"target_session_id": "telegram-admin:dm:123456789"}
}
```

格式为 `适配器名:dm:用户ID` 或 `适配器名:gm:群ID`。必须使用**另一个在线适配器**，例如 Telegram；同一个故障 QQ 适配器会被拒绝。使用 `ctx.send_message_chain()` 直接发送，不通过 LLM，也不是把通知发布为输入消息。另一个 QQ 账号虽可配置，但仍可能共享 NapCat/网络故障，推荐独立渠道。

## 模板

标准字段为 `notify_template`；兼容读取旧键 `template`，两者同时非空时 `notify_template` 优先。

| 占位符 | 内容 |
| --- | --- |
| `{time}` | 本机带时区的 ISO 时间，精确到秒 |
| `{adapter}` | 配置的适配器名称 |
| `{qq}` | 客户端 self_id，缺失时使用 bot_pid |
| `{signal}` | 固定为 `bot_offline` |
| `{tag}` | 事件 tag，缺失时为空 |
| `{message}` | 事件 message（下线原因），缺失时为空 |
| `{event}` | 完整 bot_offline 事件 JSON |

未配置/空模板使用默认模板。不含占位符的普通文本合法；不存在的占位符、坏花括号或非字符串模板会回退默认模板，并记录不含配置内容的警告。字面花括号写成 `{{` / `}}`。不支持 exception-handler 的 `{traceback}` 等异常模板字段。

## 去重与重试

- 按客户端保存已成功发送事件的完整 JSON 指纹（排序对象键）；完全相同事件仅通知一次。不同时间戳或内容的 bot_offline 是新事件，不依赖账号恢复/在线状态重置。
- 每个客户端保留一个待通知事件，新收到的未成功通知事件会更新待通知槽位。发送期间新到达的事件不会因前一次发送成功而被清空。
- 发送失败保留待通知状态，每 60 秒最多尝试一次；配置错误、HTTP 非 2xx、邮件失败、会话结果失败均进入重试。记录失败类型，不输出秘密。不同事件也共享此重试节流。
- 状态仅保存在内存；重新加载插件/替换客户端会清空去重记录，可能再次通知。长期运行时已发送指纹集合随不同事件数量增长。
- 网络超时可能发生在远端已收到之后，无法保证严格 exactly-once。
- 为兼容已有配置，manifest/schema 保持不变：schema 中旧的 `disconnect_grace_seconds` 仍显示，但已无作用；旧默认模板也可继续使用，`{event}` 包含 tag/message。可自行改模板增加 `{tag}`、`{message}`。

## 本机验证

### 1. 无外网单测

```bash
cd /app
python -m unittest discover -s /app/data/plugins/qq-kicked-notifier -p test_notifier.py -v
```

目前 10 个用例通过；覆盖精确 bot_offline 触发、原文/原因、去重、失败重试、开关、清理、三渠道接口、非法配置和模板回退；确认 heartbeat、断线、重连耗尽、关键词文本均不触发且不发起状态探测。单测替换平台依赖/网络，不代表真实投递成功。

### 2. 真实依赖导入和安全生命周期 smoke test

```bash
cd /app
python - <<'PY'
import asyncio, importlib.util, json
from pathlib import Path
from core.config.config_field import build_fields
from core.plugin import BasePlugin
p = Path('/app/data/plugins/qq-kicked-notifier')
s = importlib.util.spec_from_file_location('qq_notifier_smoke', p / 'main.py')
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
assert issubclass(m.plugin_class, BasePlugin)
assert len(build_fields(json.loads((p / 'schema.json').read_text()))) == 7
async def check():
    plugin = m.plugin_class(None, {'notify_enabled': False})
    await plugin.initialize()
    assert plugin._task is None
    await plugin.terminate()
asyncio.run(check())
print('OK')
PY
```

本机已通过真实 `core`/`httpx` 导入、schema 解析、禁用状态 initialize/terminate。未通过正在运行的平台 PluginManager 执行启用加载；需管理员在 WebUI 中手动加载/配置并查看状态、日志。此过程不要求重启服务。

### 3. 人工真实投递（无需踢真实账号）

1. 管理员准备专用 ntfy 测试 topic 和 token，或明确授权的测试收件邮箱，或另一个在线适配器的测试会话。先保留 `notify_enabled=false`。
2. WebUI 加载插件后，可在受控开发脚本中按上面的真实导入方式实例化插件（`ntfy` 可用 `ctx=None`），传入自己提供的配置，然后执行 `await plugin._send('qq-test', 'KiraAI QQ 下线通知：人工测试，无真实离线事件')`。不要将 token 放在命令行参数、日志或共享脚本中。邮件/session 需要有效的运行时 PluginContext，不能用 `None`。
3. 确认订阅客户端/邮箱/目标会话收到内容，同时确认无真实 QQ 消息发送。
4. 通道确认后，在 WebUI 设置实际模板和 `notify_enabled=true`，重新加载插件。确认插件 ready 且无 Monitor/Notification failed 日志。不要通过踢真实账号或停止生产适配器来测试。

本机检查结果：已安装 ntfy 配置但 `topic_url` 为空；邮件配置含 SMTP 发件凭据，但没有为本次任务指定/授权测试收件人。因此**未进行真实投递**，没有读取打印 token/密码，也未自行推定管理员邮箱为测试收件人。未重启服务、未改动核心代码、未操作真实账号下线。
