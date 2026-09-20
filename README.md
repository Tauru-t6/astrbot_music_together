# Music Together Companion

一个用于 AstrBot 的 Music Together 陪听插件。

插件会加入 Music Together 房间，读取当前歌曲和播放进度，下载可用的音频片段交给 Gemini-compatible 接口分析，再在房间聊天中发送简短的实时听歌反应。它也可以选择调用同一台机器上的 AstrBot Chat API，让 AstrBot 负责聊天回复和歌曲结束时的收尾。

这是一个个人项目，适合已经运行 Music Together 服务、AstrBot 和模型接口的用户。插件本身不提供音乐搜索、音乐播放或 Music Together 服务端。

## 功能

- 使用 HMAC 身份令牌加入 Music Together 房间。
- 指定房间 ID，或在没有指定房间时自动加入第一个房间。
- 没有可加入的房间时，可自动创建一个房间。
- 下载 MP3 后按时间切成音频片段，按播放进度提前分析。
- 播放、暂停、恢复、拖动和切歌时同步调整陪听任务。
- 音频无法下载或不是可切片的 MP3 时，退化为根据歌曲标题和歌手生成反应。
- 对房间聊天进行节流，避免机器人快速重复回复。
- 可选使用 AstrBot Chat API 处理聊天回复和歌曲结束语。
- 插件停止时取消分析任务并断开 Socket.IO 连接。
- 保留 `bot.py` 独立入口，兼容已有的 systemd 部署。

## 工作流程

```text
Music Together Socket.IO -> 读取歌曲状态 -> 下载音频 -> MP3 切片 -> Gemini 分析 -> 定时发送反应
                                      \-> 房间聊天 -> AstrBot Chat API / Gemini 文本接口
```

每个片段会在预计播放时间之前提交分析请求。模型输出必须是 JSON，插件会再次检查时间、文本长度和歌曲结束位置，避免发送过期或过长的反应。

## 前置条件

### Music Together 服务

你需要一个可访问的 Music Together 服务端，并准备好 Socket.IO 地址、服务端 HMAC secret、音乐 URL 接口和可加入的房间。

如果服务端和 AstrBot 不在同一台机器上，`server_url` 必须填写 AstrBot 所在机器可以访问的地址。生产环境建议使用 HTTPS 或内网隧道访问，不要把带身份认证的明文 HTTP 暴露到公网。

### AstrBot

- AstrBot 4.22 或更高版本。
- AstrBot 插件依赖安装功能可用。
- 插件运行环境可以安装 `requirements.txt` 中的依赖。

### 模型接口

插件使用 Gemini `generateContent` 格式，请求头使用 `x-goog-api-key`，请求体包含文本 prompt 和可选的 `inline_data` 音频片段。默认 endpoint 是 Google Gemini 官方格式，也可以填写兼容该格式的中转服务地址。地址中的 `{model}` 会替换为 `gemini_model` 的值。

## 安装

### 从 AstrBot 插件市场安装

1. 在 AstrBot WebUI 打开插件市场。
2. 搜索 `Music Together Companion` 或 `astrbot_plugin_music_bot`。
3. 安装插件并等待依赖安装完成。
4. 重启 AstrBot。
5. 打开插件配置页填写必要字段。

### 手动安装

将 `astrbot_music_together` 仓库目录复制到 AstrBot 的插件目录，目录结构应直接包含 `main.py` 和 `metadata.yaml`：

```text
<astrbot-data>/plugins/astrbot_plugin_music_bot/
├── main.py
├── bot.py
├── audio_split.py
├── metadata.yaml
├── _conf_schema.json
└── requirements.txt
```

不要多套一层目录，例如 `plugins/astrbot_plugin_music_bot/music-bot-main/main.py` 会导致 AstrBot 找不到入口。复制后重启 AstrBot，在插件配置页填写参数。

## 配置

插件配置文件由 AstrBot 管理。不要把真实密钥写入仓库，也不要把生产环境的 `config.json`、`state.json` 或 `.astrbot_key` 提交到 Git。

### 必填字段

| 字段 | 说明 | 示例 |
| --- | --- | --- |
| `server_url` | Music Together 服务地址 | `http://127.0.0.1:3011` |
| `identity_secret` | 服务端 HMAC secret | 在服务端配置中获取 |
| `gemini_endpoint` | Gemini-compatible `generateContent` 地址 | `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` |
| `gemini_key` | Gemini 或中转服务 API key | 只在 AstrBot 配置中填写 |
| `gemini_model` | 使用的模型名 | `gemini-2.5-flash` |

### 房间字段

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `room_id` | 空 | 指定房间 ID。留空时加入第一个房间，或按 `create_if_missing` 创建房间。 |
| `nickname` | `小听` | 机器人在房间中的昵称。 |
| `create_if_missing` | `true` | 找不到房间时是否创建房间。 |

### 分析字段

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `segment_seconds` | `45` | 每个音频片段的长度，插件限制在 10 到 180 秒。 |
| `analyze_lead_seconds` | `20` | 在片段开始前多少秒提交分析。 |
| `max_reactions` | `4` | 音频不可用时最多发送多少条反应。 |
| `persona` | 小听默认人设 | 发送给模型的反应风格提示。 |
| `proxy` | 空 | 可选 HTTP 代理，用于模型请求和音频下载。 |

只有 MP3 音频能被切片分析。音源是 m4a、flac 等格式，或下载失败时，插件退化为"知识模式"：仅根据歌名、歌手和已有笔记生成反应，且会提示模型不要编造听不到的内容。

### AstrBot 对话字段

这些字段用于可选的 AstrBot Chat API。`astrbot_api_key` 留空时，插件仍能发送基于 Gemini 的听歌反应，但不会调用 AstrBot 处理聊天和收尾。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `astrbot_chat_url` | `http://127.0.0.1:6185/api/v1/chat` | AstrBot Chat API 地址。 |
| `astrbot_api_key` | 空 | AstrBot Chat API 的 Bearer token。 |
| `astrbot_session` | `music-bot` | AstrBot 会话 ID。 |
| `astrbot_username` | `music-bot` | 请求中使用的用户名。 |
| `astrbot_provider` | 空 | 可选的 provider 名称。 |
| `reply_all_chat` | `true` | 是否回应房间里的所有非系统聊天。关闭后，仅在消息包含机器人昵称或 `@昵称` 时回应。 |

仓库中的 [config.example.json](config.example.json) 只包含占位值，可以作为字段参考。AstrBot 安装时直接在 WebUI 配置，不需要创建这个文件。

## 独立运行

适合已经使用 systemd 部署的用户：

```bash
python -m pip install -r requirements.txt
cp config.example.json config.json
python bot.py
```

独立运行时，`config.json` 必须填写 `server_url`、`identity_secret`、`gemini_endpoint` 和 `gemini_key`。状态会写入同目录的 `state.json`，其中可能包含房间重连 token；请限制文件权限并避免提交到 Git。作为 AstrBot 插件运行时，状态文件保存在 AstrBot 的插件数据目录，不会写入插件目录。

## 隐私、费用和安全

启用音频分析后，插件会把当前歌曲的音频片段发送到 `gemini_endpoint`。启用房间聊天回复后，聊天内容会发送到 Gemini 或 AstrBot Chat API。第三方 endpoint 可能记录请求内容并产生模型费用，请使用自己信任的服务。

插件不会把 API key 写入日志，但日志会记录歌曲标题、歌手、房间事件和生成的反应。多人房间使用时，应提前告知参与者音频和聊天可能会被发送到外部模型服务。

建议使用 HTTPS 或受信任的内网地址，为每个服务使用独立且可撤销的 key；如果密钥曾被提交或公开，立即撤销并重新生成。不需要聊天回复时，保持 `astrbot_api_key` 为空。

## 故障排查

### 插件没有加载

检查插件目录中直接存在 `main.py` 和 `metadata.yaml`，AstrBot 版本满足要求，`requirements.txt` 已安装，尤其是 `python-socketio` 和 `curl-cffi`，然后重启 AstrBot 查看加载日志。

### 日志提示缺少配置

确认 `server_url`、`identity_secret`、`gemini_endpoint` 和 `gemini_key` 已填写。插件不会接受空字符串作为运行配置，也不会使用仓库里的示例值连接生产服务。

### 能加入房间但没有反应

检查当前歌曲是否正在播放，Music Together 是否提供 `streamUrl` 或可用的 `/api/music/url`，模型 endpoint 是否支持 `generateContent` 和音频 `inline_data`，以及代理是否能访问模型服务。重点查看日志中的 `gemini http`、`audio download` 和 `analysis timed out`。

### 只能听歌，不能回复聊天

`astrbot_api_key` 留空时不会走 AstrBot Chat API。填写有效 key 后重启插件，再检查 `astrbot_chat_url`、session ID 和 provider 名称。

### 模型返回格式错误

实时反应要求模型输出 JSON：

```json
{"reactions":[{"at":12,"text":"鼓点进来了"}]}
```

插件会忽略无法解析、过长、已经播放过去或太靠近歌曲结尾的反应。

## 开发和验证

```bash
python -m py_compile bot.py audio_split.py main.py
python -c "import json; json.load(open('_conf_schema.json')); json.load(open('config.example.json'))"
```

修改配置字段时，需要同步更新 `_conf_schema.json`、`config.example.json`、本 README 的配置表和 `CHANGELOG.md`。发布包不包含真实配置和运行状态。

## 目录说明

| 文件 | 作用 |
| --- | --- |
| `main.py` | AstrBot 插件入口和生命周期适配层。 |
| `bot.py` | Socket.IO、音频下载、模型请求、播放同步和聊天逻辑。 |
| `audio_split.py` | 内存 MP3 帧解析和时间切片。 |
| `metadata.yaml` | AstrBot 插件元数据。 |
| `_conf_schema.json` | AstrBot 配置面板字段定义。 |
| `requirements.txt` | 运行时依赖。 |
| `config.example.json` | 独立运行配置示例。 |

## 许可证

本项目使用 MIT License，详见 [LICENSE](LICENSE)。
