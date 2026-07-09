"""iLinkBridge — WeChat iLink 多 Session 桥接

独立 asyncio 进程，通过 subprocess CLI 与 OpenCode 通信。session 动态创建，独立上下文。
"""
import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import shlex
import struct
import sys
import urllib.parse
import uuid

import httpx
import time
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# ─── 配置 ──────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get(
    "BRIDGE_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ilinkbridge.json"),
)

def _find_latest_account_token() -> str:
    accounts_dir = os.path.expanduser("~/.openclaw/openclaw-weixin/accounts")
    if not os.path.isdir(accounts_dir):
        return ""
    latest = ""
    latest_time = ""
    for filename in os.listdir(accounts_dir):
        if not filename.endswith("-im-bot.json"):
            continue
        filepath = os.path.join(accounts_dir, filename)
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("savedAt", "") > latest_time:
                latest_time = data["savedAt"]
                latest = data.get("token", "")
        except Exception:
            continue
    return latest


def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        cfg = {
            "bridge": {"uploads": os.path.expanduser("~/uploads"), "token": ""},
            "opencode": {"server": "http://localhost:4096", "timeout": 600, "password": "mypass"},
            "exec": {"cwd": os.path.expanduser("~"), "timeout": 600},
            "sessions": {},
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("bridge", {}).get("token"):
        token = _find_latest_account_token()
        if token:
            cfg.setdefault("bridge", {})["token"] = token
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            print("[Bridge] 已从 accounts 目录自动获取 bridge.token", flush=True)
    return cfg


def _save_config():
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "bridge": CONFIG.get("bridge", {}),
            "opencode": CONFIG.get("opencode", {}),
            "exec": CONFIG.get("exec", {}),
            "sessions": dict(CONFIG.get("sessions", {})),
        }, f, indent=2, ensure_ascii=False)


CONFIG = _load_config()
_IL_TOKEN = CONFIG["bridge"]["token"]
_UPLOAD_DIR = os.path.expanduser(CONFIG["bridge"]["uploads"])

_OP_SERVER = CONFIG["opencode"]["server"].rstrip("/")
_OP_HTTP_TIMEOUT = int(CONFIG.get("opencode", {}).get("timeout", 600))
_OP_PASSWORD = CONFIG.get("opencode", {}).get("password", "")

_EXEC_CWD = os.path.expanduser(CONFIG.get("exec", {}).get("cwd") or "~")
_EXEC_TIMEOUT = int(CONFIG.get("exec", {}).get("timeout", 600))

# Session 管理
sessions = CONFIG.setdefault("sessions", {})
if "default" not in sessions:
    sessions["default"] = {"comment": "默认对话", "mode": "plan", "workspace": ""}

for name in sessions:
    cfg = sessions[name]
    if "mode" not in cfg:
        cfg["mode"] = "plan"

_SESSIONS = sessions
_current_session = "default"

# ─── Session 管理函数 ──────────────────────────────────────────
def _display_sessions() -> list[str]:
    lines = ["Session列表："]
    for name in _SESSIONS:
        cfg = _SESSIONS[name]
        line = f"- {name}"
        parts = []
        if cfg.get("comment"):
            parts.append(cfg["comment"])
        if cfg.get("workspace"):
            parts.append(cfg["workspace"])
        if parts:
            line += " — " + " — ".join(parts)
        if name == _current_session:
            line += "【当前】"
        lines.append(line)
    return lines


def _current_session_id() -> str:
    return _SESSIONS.get(_current_session, {}).get("session-id", "")


def _get_workspace(name=None) -> str:
    """从 session 配置读 workspace，支持 ~ 路径。"""
    name = name or _current_session
    return os.path.expanduser(_SESSIONS.get(name, {}).get("workspace", ""))

# ─── 常量 ──────────────────────────────────────────────────────
BASE_URL = "https://ilinkai.weixin.qq.com/"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

ILINK_APP_ID = "bot"
ILINK_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 4)  # 2.4.4
CHANNEL_VERSION = "2.4.4"
BOT_AGENT = "OpenCode"

# MessageItemType (official)
_TYP_NONE, _TEXT, _IMAGE, _VOICE, _FILE, _VIDEO = 0, 1, 2, 3, 4, 5
# MessageType
_BOT, _FINISH = 2, 2

SEEN_MSG_IDS = set()
_PENDING_MEDIA: dict[str, list[str]] = {}
_LAST_USER_ID: str = ""

_TYPING_TICKETS: dict[str, str] = {}
_TYPING_USERS: dict[str, float] = {}
_TYPING_LAST_SENT: dict[str, float] = {}

_ACTIVE_REQUEST: dict[str, asyncio.Task] = {}

MSG_CHUNK_SIZE = 4000  # WeChat single message character limit


# ─── Token / Headers ───────────────────────────────────────────
def _random_uin() -> str:
    n = struct.unpack("!I", os.urandom(4))[0]
    return base64.b64encode(str(n).encode()).decode()


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_IL_TOKEN}",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_CLIENT_VERSION,
    }


def _base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


# ─── 消息分段 ─────────────────────────────────────────────────
def _split_text(text: str, max_chars: int = MSG_CHUNK_SIZE) -> list[str]:
    """长文本分段。按换行/句号/感叹号等自然断点，找不到则硬截断。"""
    if not text or len(text) <= max_chars:
        return [text] if text else []

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        chunk = remaining[:max_chars]
        cut = -1
        for sep in ['\n', '。', '. ', '！', '？']:
            idx = chunk.rfind(sep)
            if idx > max_chars // 2:
                cut = idx + len(sep)
                break
        if cut == -1:
            cut = max_chars
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    return chunks


# ─── Typing Indicator ─────────────────────────────────────────
async def _get_typing_ticket(client: httpx.AsyncClient, user_id: str, context_token: str = "") -> str:
    if user_id in _TYPING_TICKETS:
        return _TYPING_TICKETS[user_id]
    try:
        resp = await client.post(
            f"{BASE_URL}ilink/bot/getconfig",
            headers=_headers(),
            json={
                "ilink_user_id": user_id,
                "context_token": context_token,
                "base_info": _base_info(),
            },
            timeout=10,
        )
        data = resp.json()
        ticket = data.get("typing_ticket", "")
        if ticket:
            _TYPING_TICKETS[user_id] = ticket
        return ticket
    except Exception as e:
        print(f"[iLink] getConfig failed: {e}", flush=True)
        return ""


async def _send_typing(client: httpx.AsyncClient, user_id: str, status: int, ticket: str = ""):
    if not ticket:
        ticket = await _get_typing_ticket(client, user_id)
    if not ticket:
        return
    try:
        await client.post(
            f"{BASE_URL}ilink/bot/sendtyping",
            headers=_headers(),
            json={
                "ilink_user_id": user_id,
                "typing_ticket": ticket,
                "status": status,
                "base_info": _base_info(),
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[iLink] sendTyping({status}) failed: {e}", flush=True)


def _cancel_typing(client: httpx.AsyncClient, user_id: str):
    if user_id in _TYPING_USERS:
        _TYPING_USERS.pop(user_id, None)
        _TYPING_LAST_SENT.pop(user_id, None)
        try:
            _ = asyncio.create_task(_send_typing(client, user_id, 2))
        except Exception:
            pass


# ─── Polling ───────────────────────────────────────────────────
async def get_updates(client: httpx.AsyncClient, buf: str = "") -> tuple[list[dict], str, str]:
    resp = await client.post(
        f"{BASE_URL}ilink/bot/getupdates",
        headers=_headers(),
        json={"get_updates_buf": buf, "base_info": _base_info()},
        timeout=40,
    )
    data = resp.json()
    parsed = await _parse(client, data.get("msgs", []))
    return parsed, data.get("get_updates_buf", buf), data.get("context_token", "")


# ─── AES key parsing ──────────────────────────────────────────
def _parse_aes_key(key_b64: str, label: str) -> bytes:
    if not key_b64:
        raise ValueError(f"{label}: empty aes_key")
    decoded = base64.b64decode(key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32 and bool(re.fullmatch(r"[0-9a-fA-F]{32}", decoded.decode("ascii"))):
        return bytes.fromhex(decoded.decode("ascii"))
    raise ValueError(
        f"{label}: aes_key must decode to 16 raw bytes or 32-char hex string, "
        f"got {len(decoded)} bytes (base64={key_b64})"
    )


# ─── AES-128-ECB 解密 ─────────────────────────────────────────
def _decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_ECB)
    plain = cipher.decrypt(ciphertext)
    return unpad(plain, 16)


# ─── CDN download + decrypt ────────────────────────────────────
async def _download_and_decrypt(
    client: httpx.AsyncClient,
    encrypt_query_param: str,
    aes_key_b64: str,
    full_url: str = "",
    label: str = "",
) -> bytes:
    key = _parse_aes_key(aes_key_b64, label)
    if full_url:
        url = full_url
    else:
        q = urllib.parse.quote(encrypt_query_param, safe="")
        url = f"{CDN_BASE_URL}/download?encrypted_query_param={q}"
    resp = await client.get(url, timeout=120)
    resp.raise_for_status()
    encrypted = resp.content
    return _decrypt_aes_ecb(encrypted, key)


# ─── 保存到 workspace ──────────────────────────────────────────
def _save_media(buf: bytes, subdir: str, filename_hint: str = "media", ext: str = "") -> str:
    dest_dir = os.path.join(_UPLOAD_DIR, subdir)
    os.makedirs(dest_dir, exist_ok=True)
    unique = hashlib.md5(buf[:1024]).hexdigest()[:12]
    base = os.path.splitext(filename_hint)[0] or "media"
    if not ext:
        ext = {"images": ".jpg", "videos": ".mp4", "voices": ".amr"}.get(subdir, "")
    safe_name = f"{base}_{unique}{ext}"
    dest = os.path.join(dest_dir, safe_name)
    with open(dest, "wb") as f:
        f.write(buf)
    return dest


# ─── 消息解析 ─────────────────────────────────────────────────
async def _parse(client: httpx.AsyncClient, raw_msgs: list[dict]) -> list[dict]:
    out = []
    for raw in raw_msgs:
        if raw.get("message_type", 0) == _BOT:
            continue
        msg_id = raw.get("message_id", "")
        from_id = raw.get("from_user_id", "")
        ctx_token = raw.get("context_token", "")

        text = ""
        media_paths: list[str] = []

        for item in raw.get("item_list", []):
            typ = int(item.get("type", 0))

            if typ == _TEXT:
                text = item.get("text_item", {}).get("text", "")

            elif typ == _IMAGE:
                img = item.get("image_item", {})
                media = img.get("media", {})
                ekp = media.get("encrypt_query_param", "")
                full = media.get("full_url", "")
                if not ekp and not full:
                    continue
                aes_key_raw = img.get("aeskey", "")
                if aes_key_raw:
                    try:
                        raw_key = bytes.fromhex(aes_key_raw)
                        aes_key_input = base64.b64encode(raw_key).decode()
                    except Exception:
                        aes_key_input = aes_key_raw
                else:
                    aes_key_input = media.get("aes_key", "")
                if not aes_key_input:
                    continue
                try:
                    buf = await _download_and_decrypt(client, ekp, aes_key_input, full, "image")
                    path = _save_media(buf, "images", "image.jpg")
                    media_paths.append(path)
                    print(f"[iLink] image saved: {path} ({len(buf)} bytes)", flush=True)
                except Exception as e:
                    print(f"[iLink] image decrypt failed: {e}", flush=True)

            elif typ == _VOICE:
                voice_text = item.get("voice_item", {}).get("text", "")
                if voice_text:
                    text = f"{text}\n{voice_text}" if text else voice_text

            elif typ == _FILE:
                fi = item.get("file_item", {})
                media = fi.get("media", {})
                ekp = media.get("encrypt_query_param", "")
                full = media.get("full_url", "")
                aes_key_input = media.get("aes_key", "")
                filename = fi.get("file_name", "file.bin")
                if (ekp or full) and aes_key_input:
                    try:
                        buf = await _download_and_decrypt(client, ekp, aes_key_input, full, "file")
                        path = _save_media(buf, "files", filename, os.path.splitext(filename)[1])
                        media_paths.append(path)
                        print(f"[iLink] file saved: {path} ({len(buf)} bytes)", flush=True)
                    except Exception as e:
                        print(f"[iLink] file decrypt failed: {e}", flush=True)

            elif typ == _VIDEO:
                vi = item.get("video_item", {})
                media = vi.get("media", {})
                ekp = media.get("encrypt_query_param", "")
                full = media.get("full_url", "")
                aes_key_input = media.get("aes_key", "")
                if (ekp or full) and aes_key_input:
                    try:
                        buf = await _download_and_decrypt(client, ekp, aes_key_input, full, "video")
                        path = _save_media(buf, "videos", "video.mp4")
                        media_paths.append(path)
                        print(f"[iLink] video saved: {path} ({len(buf)} bytes)", flush=True)
                    except Exception as e:
                        print(f"[iLink] video decrypt failed: {e}", flush=True)

        if text.strip() or media_paths:
            out.append({
                "text": text.strip(),
                "media_paths": media_paths,
                "msg_id": str(msg_id),
                "from_id": from_id,
                "context_token": ctx_token,
            })
    return out


# ─── Send ──────────────────────────────────────────────────────
async def _send_single(client: httpx.AsyncClient, to_user_id: str, text: str, context_token: str = ""):
    if not text.strip():
        return
    req = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": str(uuid.uuid4()),
            "message_type": _BOT,
            "message_state": _FINISH,
            "item_list": [{"type": _TEXT, "text_item": {"text": text}}],
            "context_token": context_token or None,
        }
    }
    await client.post(
        f"{BASE_URL}ilink/bot/sendmessage",
        headers=_headers(),
        json={**req, "base_info": _base_info()},
        timeout=20,
    )


async def send_message(client: httpx.AsyncClient, to_user_id: str, text: str, context_token: str = ""):
    """发送消息。长文本自动分段，每段 ≤ MSG_CHUNK_SIZE 字符。"""
    chunks = _split_text(text)
    for i, chunk in enumerate(chunks):
        chunk = chunk.rstrip("\n").rstrip()
        if len(chunks) > 1:
            chunk = f"({i + 1}/{len(chunks)}) {chunk}"
        await _send_single(client, to_user_id, chunk, context_token)


# ─── /exec 命令执行 ──────────────────────────────────────────
async def _exec_command(command: str) -> str:
    """执行命令，返回 stdout+stderr 拼接。"""
    if not command or not command.strip():
        return "(empty command)"
    try:
        cmd = shlex.split(command)
    except ValueError as e:
        return f"命令解析错误: {e}"

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=_EXEC_CWD,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_EXEC_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return f"(timeout after {_EXEC_TIMEOUT}s)"
    except FileNotFoundError:
        return f"命令未找到: {cmd[0]}"
    except Exception as e:
        return f"执行异常: {e}"

    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    parts = []
    if proc.returncode != 0:
        parts.append(f"(退出码 {proc.returncode})")
    if out.strip():
        parts.append(out.strip())
    if err.strip():
        parts.append(f"[stderr]\n{err.strip()}")
    return "\n".join(parts) or "(no output)"


# ─── OpenCode HTTP API ──────────────────────────────────────────

def _opencode_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if _OP_PASSWORD:
        import base64
        token = base64.b64encode(f"opencode:{_OP_PASSWORD}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    return headers


async def _post_opencode_message(sid: str, text: str, user_id: str, ctx_token: str):
    """向 OpenCode server 发消息，解析 parts，分开发【思考过程】和【最终回复】到微信。"""
    cfg = _SESSIONS.get(_current_session, {})
    agent = "plan" if cfg.get("mode") == "plan" else "build"

    body = {
        "parts": [{"type": "text", "text": text}],
        "model": {"providerID": "deepseek", "modelID": "deepseek-v4-flash"},
        "agent": agent,
    }

    # 启动 typing indicator
    _TYPING_USERS[user_id] = time.time()
    async with httpx.AsyncClient() as typer:
        try:
            await _send_typing(typer, user_id, 1)
            _TYPING_LAST_SENT[user_id] = time.time()
        except Exception:
            pass

    print(f"[iLink] → OpenCode (agent={agent}): {text[:60]}", flush=True)

    try:
        async with httpx.AsyncClient() as http:
            try:
                resp = await http.post(
                    f"{_OP_SERVER}/session/{sid}/message",
                    params={"directory": _get_workspace()},
                    headers=_opencode_headers(),
                    json=body,
                    timeout=_OP_HTTP_TIMEOUT,
                )
            except httpx.TimeoutException:
                print("[iLink] OpenCode 超时", flush=True)
                await send_message(http, user_id, "OpenCode 响应超时", ctx_token)
                return
            except Exception as e:
                print(f"[iLink] OpenCode 请求异常: {e}", flush=True)
                await send_message(http, user_id, f"OpenCode 异常: {e}", ctx_token)
                return

            if resp.status_code != 200:
                err_text = f"OpenCode 错误 (HTTP {resp.status_code})"
                print(f"[iLink] {err_text}", flush=True)
                await send_message(http, user_id, err_text, ctx_token)
                return

            data = resp.json()

        print(f"[iLink] OpenCode ← parts={len(data.get('parts',[]))}", flush=True)

        async with httpx.AsyncClient() as send_client:
            has_output = False
            for part in data.get("parts", []):
                if part.get("type") == "text":
                    text = part.get("text", "")
                    if text:
                        has_output = True
                        for chunk in _split_text(text):
                            await send_message(send_client, user_id, chunk, ctx_token)
                            await asyncio.sleep(0.2)

            if not has_output:
                await send_message(send_client, user_id, "【回复】\n(空响应)", ctx_token)

    except asyncio.CancelledError:
        print(f"[iLink] OpenCode 请求被取消", flush=True)
        async with httpx.AsyncClient() as c:
            _cancel_typing(c, user_id)
            await send_message(c, user_id, "已取消", ctx_token)
        raise
    finally:
        _ACTIVE_REQUEST.pop(user_id, None)
        async with httpx.AsyncClient() as c:
            _cancel_typing(c, user_id)

# ─── Process ───────────────────────────────────────────────────
async def _renew_typing(client: httpx.AsyncClient):
    """续期 typing indicator（每 10s）。"""
    now = time.time()
    for uid in list(_TYPING_USERS.keys()):
        last = _TYPING_LAST_SENT.get(uid, 0)
        if now - last >= 10:
            try:
                await _send_typing(client, uid, 1)
                _TYPING_LAST_SENT[uid] = now
            except Exception:
                pass


async def process(client: httpx.AsyncClient, msg: dict):
    text = msg["text"]
    media_paths: list[str] = msg.get("media_paths", [])
    to_id = msg["from_id"]
    ctx = msg.get("context_token", "")

    global _LAST_USER_ID, _current_session
    if to_id:
        _LAST_USER_ID = to_id

    # ── 纯附件消息（当前有媒体、无文字）→ 合并缓存，继续缓存 ──
    if media_paths and not text.strip():
        pending = _PENDING_MEDIA.pop(to_id, [])
        _PENDING_MEDIA[to_id] = pending + media_paths
        _TYPING_USERS[to_id] = time.time()
        await _send_typing(client, to_id, 1)
        _TYPING_LAST_SENT[to_id] = time.time()
        await asyncio.sleep(0.3)
        _cancel_typing(client, to_id)
        kind = "图片"
        for p in _PENDING_MEDIA[to_id]:
            if "/videos/" in p:
                kind = "视频"
            elif "/files/" in p:
                kind = "文件"
        reply = f"{kind}已收到，请指示"
        print(f"[iLink] 附件缓存: {_PENDING_MEDIA[to_id]} → {reply}", flush=True)
        await send_message(client, to_id, reply, ctx)
        return

    # ── 命令处理（不消费 pending media）──
    if text.strip().startswith("/"):
        forwarded = text.strip()

        if forwarded.startswith("/exec "):
            cmd = forwarded[6:].strip()
            print(f"[iLink] /exec: {cmd[:60]}", flush=True)
            result = await _exec_command(cmd)
            print(f"[iLink] /exec ←: {result[:120]}", flush=True)
            await send_message(client, to_id, result, ctx)
            return

        if forwarded == "/help":
            lines = [
                "使用方式：",
                "- /mode plan — 方案模式（只读，禁用 write/edit/exec/apply_patch）",
                "- /mode build — 执行模式（全开）",
                "- /session [name] [comment] [workspace] — 切换或创建 session",
                "- /compact — 压缩当前 session 上下文",
                "- /delete — 删除当前 session（default 清空重置）",
                "- /cancel — 取消当前请求",
                "- /exec <cmd> — 执行本地命令",
                "- /help — 显示此帮助",
                "- 其他消息 — 转发给当前 session",
                "",
            ]
            lines.extend(_display_sessions())
            await send_message(client, to_id, "\n".join(lines), ctx)
            return

        if forwarded == "/compact":
            sid = _current_session_id()
            if not sid:
                await send_message(client, to_id, "当前 session 未关联 OpenCode 会话", ctx)
                return
            try:
                resp = await client.post(
                    f"{_OP_SERVER}/api/session/{sid}/compact",
                    params={"directory": _get_workspace()},
                    headers=_opencode_headers(),
                    timeout=30,
                )
                if resp.status_code == 204:
                    await send_message(client, to_id, "Session 已压缩", ctx)
                else:
                    await send_message(client, to_id, f"压缩失败 (HTTP {resp.status_code})", ctx)
            except Exception as e:
                await send_message(client, to_id, f"压缩异常: {e}", ctx)
            return

        if forwarded == "/cancel":
            task = _ACTIVE_REQUEST.pop(to_id, None)
            if task and not task.done():
                task.cancel()
                sid = _current_session_id()
                if sid:
                    try:
                        await client.post(f"{_OP_SERVER}/session/{sid}/abort", headers=_opencode_headers(), timeout=10)
                    except Exception:
                        pass
                await send_message(client, to_id, "已取消", ctx)
            else:
                await send_message(client, to_id, "当前无进行中的请求", ctx)
            return

        if forwarded == "/delete":
            cfg = _SESSIONS.get(_current_session, {})
            sid = cfg.get("session-id", "")

            if _current_session == "default":
                if sid:
                    try:
                        await client.delete(f"{_OP_SERVER}/session/{sid}", headers=_opencode_headers(), timeout=10)
                    except Exception:
                        pass
                cfg["session-id"] = ""
                _save_config()
                await send_message(client, to_id, "默认会话已清空", ctx)
            else:
                if sid:
                    try:
                        await client.delete(f"{_OP_SERVER}/session/{sid}", headers=_opencode_headers(), timeout=10)
                    except Exception as e:
                        await send_message(client, to_id, f"删除失败: {e}", ctx)
                        return
                del _SESSIONS[_current_session]
                _current_session = "default"
                _save_config()
                await send_message(client, to_id, f"Session 已删除，已切换到默认会话", ctx)
            return

        if forwarded == "/mode plan":
            _SESSIONS.setdefault(_current_session, {})["mode"] = "plan"
            _save_config()
            await send_message(client, to_id,
                "【方案模式】\n禁用：write / edit / apply_patch / exec", ctx)
            return

        if forwarded == "/mode build":
            _SESSIONS.setdefault(_current_session, {})["mode"] = "build"
            _save_config()
            await send_message(client, to_id,
                "【执行模式】\n所有工具全开", ctx)
            return

        if forwarded == "/session":
            lines = _display_sessions()
            lines.append("")
            lines.append("用法: /session <name> [comment] [workspace]")
            await send_message(client, to_id, "\n".join(lines), ctx)
            return

        if forwarded.startswith("/session "):
            parts = forwarded[9:].strip().split(maxsplit=2)
            name = parts[0]
            comment = parts[1] if len(parts) > 1 else ""
            workspace = parts[2] if len(parts) > 2 else ""

            if name not in _SESSIONS:
                cfg = {"comment": comment, "mode": "plan", "session-id": ""}
                if workspace:
                    cfg["workspace"] = workspace
                _SESSIONS[name] = cfg
                _save_config()
                _current_session = name
                print(f"[iLink] /session 创建: {name}", flush=True)
                parts_msg = [f"已创建并切换到 session {name}"]
                if comment:
                    parts_msg.append(comment)
                if workspace:
                    parts_msg.append(f"workspace={workspace}")
                reply = " — ".join(parts_msg)
            else:
                cfg = _SESSIONS[name]
                modified = False
                if comment:
                    cfg["comment"] = comment
                    modified = True
                if workspace:
                    cfg["workspace"] = workspace
                    modified = True
                if modified:
                    _save_config()
                _current_session = name
                print(f"[iLink] /session: {name}", flush=True)
                parts_msg = [f"已切换到 session {name}"]
                if cfg.get("comment"):
                    parts_msg.append(cfg["comment"])
                if cfg.get("workspace"):
                    parts_msg.append(f"workspace={cfg['workspace']}")
                reply = " — ".join(parts_msg)

            await send_message(client, to_id, reply, ctx)
            return

    # ── 合并缓存的未决附件 + 本次附件 → 合并到 text ──
    pending = _PENDING_MEDIA.pop(to_id, [])
    all_media = pending + media_paths
    if all_media:
        media_str = "\n".join(f"[附件: {p}]" for p in all_media)
        if text.strip():
            if text.strip().startswith("/"):
                text = f"{text}\n\n{media_str}"
            else:
                text = f"{media_str}\n\n用户指令: {text}"
        else:
            text = media_str
        msg["text"] = text

    # ── Session 路由 ──
    forwarded = text.strip()
    if not forwarded:
        return

    sid = _current_session_id()
    if not sid:
        await send_message(client, to_id, "当前 session 未关联 OpenCode 会话，请用 /session 切换到有效 session", ctx)
        return

    task = asyncio.create_task(_post_opencode_message(sid, forwarded, to_id, ctx))
    _ACTIVE_REQUEST[to_id] = task
    return

# ─── Main ──────────────────────────────────────────────────────

async def _do_qrcode_login(client: httpx.AsyncClient) -> str:
    """二维码绑定流程：获取二维码链接 → 输出到 stdout → 轮询结果 → 保存 token。"""
    global _IL_TOKEN

    print("\n[Bridge] ═══════════════════════════════", flush=True)
    print("[Bridge]  未检测到 bridge.token，开始二维码绑定", flush=True)
    print("[Bridge] ═══════════════════════════════\n", flush=True)

    max_refresh = 3
    bot_type = "3"
    ilink_base = "https://ilinkai.weixin.qq.com"

    for refresh in range(max_refresh + 1):
        try:
            resp = await client.post(
                f"{ilink_base}/ilink/bot/get_bot_qrcode?bot_type={bot_type}",
                headers={"Content-Type": "application/json"},
                json={"local_token_list": []},
                timeout=30,
            )
            qr_data = resp.json()
        except Exception as e:
            print(f"[Bridge] 获取二维码失败: {e}", flush=True)
            return ""

        qrcode_id = qr_data.get("qrcode", "")
        qrcode_url = qr_data.get("qrcode_img_content", "")
        if not qrcode_id or not qrcode_url:
            print(f"[Bridge] 二维码响应异常: {qr_data}", flush=True)
            return ""

        print(f"[Bridge] 请用手机微信访问以下链接并完成绑定：", flush=True)
        print(f"{qrcode_url}\n", flush=True)

        deadline = time.time() + 480
        while time.time() < deadline:
            try:
                status_resp = await client.get(
                    f"{ilink_base}/ilink/bot/get_qrcode_status?qrcode={qrcode_id}",
                    timeout=35,
                )
                sdata = status_resp.json()
            except Exception:
                await asyncio.sleep(2)
                continue

            status = sdata.get("status", "")

            if status == "confirmed":
                token = sdata.get("bot_token", "")
                if token:
                    print(f"[Bridge] ✅ 二维码扫描成功", flush=True)
                    CONFIG.setdefault("bridge", {})["token"] = token
                    _IL_TOKEN = token
                    _save_config()
                    print(f"[Bridge] bridge.token 已保存", flush=True)
                    return token
                print(f"[Bridge] ⚠️ 扫描成功但未返回 bot_token", flush=True)
                return ""

            if status == "expired":
                print(f"[Bridge] ⏳ 二维码已过期 ({refresh + 1}/{max_refresh})", flush=True)
                break

            if status == "binded_redirect":
                print(f"[Bridge] ✅ 该机器人已绑定过此 OpenClaw，继续使用", flush=True)
                return ""

            if status in ("need_verifycode", "verify_code_blocked"):
                print(f"[Bridge] ⚠️ 需要配对码，请在微信中输入配对码", flush=True)
                await asyncio.sleep(5)
                continue

            await asyncio.sleep(1)
        else:
            print(f"[Bridge] ❌ 二维码登录超时", flush=True)
            return ""

    print(f"[Bridge] ❌ 二维码多次过期，登录失败", flush=True)
    return ""


async def _ensure_opencode_sessions(client: httpx.AsyncClient):
    """启动时确保所有非 hongine session 在 OpenCode server 上存在。"""
    for name, cfg in list(_SESSIONS.items()):
        if "mode" not in cfg:
            cfg["mode"] = "plan"
        sid = cfg.get("session-id", "")
        if sid and sid.startswith("ses_"):
            continue
        try:
            resp = await client.post(
                f"{_OP_SERVER}/session",
                params={"directory": os.path.expanduser(cfg.get("workspace", ""))},
                headers=_opencode_headers(),
                json={"title": name},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                new_sid = data.get("id", "")
                if new_sid:
                    cfg["session-id"] = new_sid
                    print(f"[Bridge] session {name} → OpenCode: {new_sid}", flush=True)
                else:
                    print(f"[Bridge] 创建 session {name} 失败: 响应无 id", flush=True)
            else:
                print(f"[Bridge] 创建 session {name} 失败: HTTP {resp.status_code}", flush=True)
        except Exception as e:
            print(f"[Bridge] 创建 session {name} 异常: {e}", flush=True)
    _save_config()


async def main():
    print("[iLinkBridge] WeChat 桥接就绪", flush=True)
    buf = ""
    async with httpx.AsyncClient() as client:
        if not _IL_TOKEN:
            token = await _do_qrcode_login(client)
            if not token:
                print("[Bridge] ❌ 无法获取 bridge.token，退出", flush=True)
                sys.exit(1)

        await _ensure_opencode_sessions(client)
        while True:
            try:
                await _renew_typing(client)
                msgs, buf, _ = await get_updates(client, buf)
                for msg in msgs:
                    if msg["msg_id"] in SEEN_MSG_IDS:
                        continue
                    SEEN_MSG_IDS.add(msg["msg_id"])
                    await process(client, msg)
            except Exception as e:
                print(f"[iLink] {e}", flush=True)
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
