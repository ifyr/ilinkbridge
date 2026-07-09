# iLinkBridge

> 让 [OpenCode](https://github.com/anomalyco/opencode) 的微信桥接真正好用。

iLinkBridge 是微信 iLink Bot 的独立异步桥接层。它通过 HTTP API 与 opencode serve 通信，将微信消息转发给 OpenCode 处理，并将回复按上下文分段发回微信。支持多 session、plan/build mode、附件解密缓存、二维码登录等功能。

## 解决的痛点

| 痛点 | 原生 clawbot | iLinkBridge |
|---|---|---|
| **不能切换 session** | 1 个固定 session | `/session` 动态切换，无限创建 |
| **每天 4 点失忆** | opencode 自动清理 session | session 持久化，不受清理影响 |
| **自动解读图片/视频** | 收到就解析，无法暂停 | 缓存附件，等用户下指令再处理 |
| **多话题串上下文** | 所有对话混在一起 | 独立 session，互不干扰 |
| **思考过程过于冗长** | 全部输出 | 仅发送最终回复 |

## 快速开始

```bash
git clone https://github.com/ifyr/iLinkBridge.git
cd iLinkBridge
pip install -r requirements.txt
```

启动 OpenCode 服务：

```bash
opencode serve --port 4096 --hostname 127.0.0.1
```

启动 iLinkBridge：

```bash
bash start_bridge.sh
```

> **首次运行**：如果 `iLinkBridge.json` 中没有 `bridge.token`，桥接会自动通过二维码绑定流程获取。

## 命令

| 命令 | 说明 |
|---|---|
| `/mode plan` | 方案模式（只读，禁用 write/edit/exec/apply_patch） |
| `/mode build` | 执行模式（全开） |
| `/session` | 列出所有 session，`【当前】`标注 |
| `/session <name>` | 切换或创建 session |
| `/session <name> <comment>` | 切换或创建，带注释 |
| `/session <name> <comment> <workspace>` | 切换或创建，注释+工作目录 |
| `/compact` | 压缩当前 session 上下文 |
| `/delete` | 删除当前 session（default 清空重置） |
| `/cancel` | 取消当前请求 |
| `/exec <cmd>` | 执行本地 shell 命令 |
| `/help` | 显示帮助 + session 列表 |
| 其他消息 | 直接转发给当前 session 的 OpenCode |

## 配置

`iLinkBridge.json`：

```json
{
  "bridge": {
    "uploads": "~/iLinkBridge/uploads",
    "token": ""
  },
  "opencode": {
    "server": "http://localhost:4096",
    "timeout": 600,
    "password": ""
  },
  "exec": {
    "cwd": "~",
    "timeout": 600
  },
  "sessions": {
    "default": {
      "session-id": "",
      "comment": "默认对话",
      "mode": "plan",
      "workspace": "~/iLinkBridge"
    }
  }
}
```

| 字段 | 说明 |
|---|---|
| `bridge.uploads` | 附件下载目录 |
| `bridge.token` | 微信 Bot token，可留空自动发现或二维码绑定 |
| `opencode.server` | opencode serve 地址 |
| `opencode.timeout` | API 请求超时（秒） |
| `opencode.password` | opencode serve 的 Basic auth 密码 |
| `sessions` | session 持久化（启动时自动补全 session-id） |
| `sessions.*.mode` | 默认模式（plan / build） |
| `sessions.*.workspace` | 该 session 对应的 OpenCode 工作目录 |

### Plan / Build mode

通过 OpenCode 内置的 `plan` / `build` agent 实现。Plan mode 在工具层面禁止 LLM 调用 `write` / `edit` / `apply_patch` / `exec` / `bash`，仅允许 `read`、`grep`、`glob`和`webfetch` 。

建议在opencode.json中设置：
```json
{
  "agent": {
    "plan": {
      "permission": {
        "edit": "deny",
        "write": "deny",
        "bash": "deny",
        "exec": "deny",
        "apply_patch": "deny"
      }
    }
  }
}
```

## 附件处理

- 发送图片/视频/文件时，iLinkBridge **不会**自动发给 AI 解读，而是缓存下来回复「已收到，请指示」。
- 等你发送文字指令后，附件路径会附在消息中一起转发。

## 二维码登录

首次启动未配置 `bridge.token` 时，iLinkBridge 会自动调用 iLink API 获取二维码链接，输出到终端。用户用手机微信访问该链接即可完成 bot 绑定，token 自动保存到配置文件中。

## 输入提示

- 收到消息后，每 10 秒发送一次"对方正在输入..."提示，让用户感知处理进度。
- 处理完消息自动关闭。

## 依赖

- Python >= 3.10
- httpx >= 0.28.0
- pycryptodome >= 3.20
- OpenCode（运行中）

## License

MIT
