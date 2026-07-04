# ILinkBridge

> 让 [OpenClaw](https://github.com/anomalyco/openclaw) 的微信桥接真正好用。

ILinkBridge 是对 [openclaw-weixin](https://github.com/Tencent/openclaw-weixin) 官方插件的替代轮询层。它使用同一套 [iLink Bot API](https://github.com/Tencent/openclaw-weixin#backend-api-protocol)、兼容同一个 token 格式，但以独立进程运行，在插件和 OpenClaw 之间增加了 session 管理、附件缓存等功能。不需要卸载官方插件，只需停掉插件的轮询即可。

## 解决的痛点

| 痛点 | 原生 clawbot | ILinkBridge |
|---|---|---|
| **不能切换 session** | 1 个固定 session | `/session` 动态切换，无限创建 |
| **每天 4 点失忆** | openclaw 自动清理 session | session 持久化，不受清理影响 |
| **自动解读图片/视频** | 收到就解析，无法暂停 | 缓存附件，等用户下指令再处理 |
| **多话题串上下文** | 所有对话混在一起 | 独立 session，互不干扰 |

## 快速开始

```bash
git clone https://github.com/ifyr/ilinkbridge.git
cd ilinkbridge
pip install -r requirements.txt
```

编辑 `ilinkbridge.json`，填写微信 Bot token：

```json
{
  "ilinkbridge": {
    "uploads": "~/ilinkbridge/uploads",
    "token": "your-bot-token"
  }
}
```

> Bot token 也可以留空，启动时自动从 `~/.openclaw/openclaw-weixin/accounts/` 目录发现最新 token。

启动：

```bash
bash start_ilinkbridge.sh
```

## 命令

| 命令 | 说明 |
|---|---|
| `/session` | 列出所有 session，`【当前】`标注 |
| `/session <name>` | 切换到已有 session，不存在则自动创建 |
| `/session <name> <comment>` | 创建/切换并写上注释 |
| `/help` | 显示帮助 + session 列表 |
| `/compact` | 压缩当前 session 上下文，释放 token |
| 其他消息 | 直接发给当前 session 的 OpenClaw |

## 配置

`ilinkbridge.json`：

```json
{
  "ilinkbridge": {
    "uploads": "~/ilinkbridge/uploads",
    "token": ""
  },
  "openclaw": {
    "command": ["openclaw"],
    "agent": "main"
  },
  "sessions": {
    "main": {"session-id": "", "comment": "OpenClaw默认"}
  }
}
```

| 字段 | 说明 |
|---|---|
| `ilinkbridge.uploads` | 附件下载目录 |
| `ilinkbridge.token` | 微信 Bot token，可留空自动发现 |
| `openclaw.command` | openclaw 命令路径 |
| `openclaw.agent` | openclaw agent 名称 |
| `sessions` | session 持久化（启动时自动补全 session-id） |

## 附件处理

发送图片/视频/文件时，ILinkBridge **不会**自动发给 AI 解读，而是缓存下来回复「已收到，请指示」。等你发送文字指令后，附件路径会附在消息中一起转发。

## 防止失忆

openclaw 默认每天凌晨 4 点清理所有 session。在 `openclaw.json` 中设置：

```json
{
  "session": {
    "dmScope": "per-channel-peer",
    "reset": {
      "mode": "idle",
      "idleMinutes": 86400
    }
  }
}
```

这样 session 上下文保留两个月，配合 ILinkBridge 的 session 持久化，再也不会「失忆」。

## 输入提示

收到消息后，每10秒发送一个“对方正在输入...”的提示，让用户感知到正在处理。

处理完消息自动关闭。

## 设计原则

- **零侵入** — 不修改 openclaw 源码，全部通过 subprocess CLI 通信
- **长文本分段** — 超过 4000 字按自然断点拆分，前缀 `(1/3)` 标注
- **输入提示** — 自动续期，处理完自动关闭
- **AES 解密** — 微信附件自动 AES-128-ECB 解密保存

## License

MIT
