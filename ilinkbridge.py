"""ILinkBridge — WeChat iLink 多 Session 桥接

独立 asyncio 进程，通过 subprocess CLI 与 OpenClaw 通信。session 动态创建，独立上下文。
"""
import asyncio
import base64
import hashlib
import json
import os
import re
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
            "ilinkbridge": {"uploads": os.path.expanduser("~/uploads"), "token": ""},
            "openclaw": {"command": ["openclaw"], "agent": "main"},
            "sessions": {},
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("ilinkbridge", {}).get("token"):
        token = _find_latest_account_token()
        if token:
            cfg.setdefault("ilinkbridge", {})["token"] = token
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            print("[ILinkBridge] 已从 accounts 目录自动获取 ilinkbridge.token", flush=True)
        else:
            print("[ILinkBridge] 缺少 ilinkbridge.token，请检查 ilinkbridge.json", flush=True)
            sys.exit(1)
    return cfg


def _save_config():
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "ilinkbridge": CONFIG.get("ilinkbridge", {}),
            "openclaw": CONFIG.get("openclaw", {}),
            "sessions": CONFIG.get("sessions", {}),
        }, f, indent=2, ensure_ascii=False)


CONFIG = _load_config()
_IL_TOKEN = CONFIG["ilinkbridge"]["token"]
_MEDIA_DIR = CONFIG["ilinkbridge"]["uploads"]

_OC_CMD = CONFIG["openclaw"]["command"]
_OC_AGENT = CONFIG["openclaw"]["agent"]

# Session 管理
sessions = CONFIG.setdefault("sessions", {})
if "main" not in sessions:
    sessions["main"] = {"comment": ""}

changed = False
for name, cfg in sessions.items():
    if not cfg.get("session-id"):
        cfg["session-id"] = str(uuid.uuid4())
        changed = True
        print(f"[ILinkBridge] session {name} session-id 已生成", flush=True)

if changed:
    _save_config()

_SESSIONS = sessions
_current_session = "main"


def _current_full_session_key() -> str:
    return f"agent:{_OC_AGENT}:{_current_session}"


def _current_session_id() -> str:
    return _SESSIONS[_current_session]["session-id"]

# ─── 常量 ──────────────────────────────────────────────────────
BASE_URL = "https://ilinkai.weixin.qq.com/"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

ILINK_APP_ID = "bot"
ILINK_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 4)  # 2.4.4
CHANNEL_VERSION = "2.4.4"
BOT_AGENT = "OpenClaw"

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

_ACTIVE_OPENCLAW_TASKS: dict[str, tuple[asyncio.subprocess.Process, asyncio.Task, str]] = {}

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
    dest_dir = os.path.join(_MEDIA_DIR, subdir)
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


# ─── OpenClaw 转发 ────────────────────────────────────────────────

async def _compact_openclaw_session(user_id: str = "") -> str:
    key = _current_full_session_key()

    cmd = _OC_CMD + [
        "sessions", "compact",
        key,
        "--agent", _OC_AGENT,
        "--json",
        "--timeout", "180000",
    ]

    print(f"[iLink] 压缩 session: {key}", flush=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=190,
        )
        if proc.returncode == 0:
            result = stdout.decode(errors="replace").strip()
            print(f"[iLink] Session 压缩成功: {result[:120]}", flush=True)
            return "Session 压缩完成。上下文已释放。"
        else:
            err = stderr.decode(errors="replace")[:500]
            if "gateway" in err.lower() or "1008" in err or "pairing" in err.lower():
                print(f"[iLink] Gateway 不可达，降级为 max-lines 截断", flush=True)
                return await _truncate_openclaw_session(key)
            print(f"[iLink] Session 压缩失败: {err}", flush=True)
            return f"Session 压缩失败: {err[:200]}"
    except asyncio.TimeoutError:
        print("[iLink] Session 压缩超时", flush=True)
        return "Session 压缩超时（3分钟），请稍后重试。"
    except Exception as e:
        print(f"[iLink] Session 压缩异常: {e}", flush=True)
        return f"Session 压缩异常: {e}"


async def _truncate_openclaw_session(key: str) -> str:
    cmd = _OC_CMD + [
        "sessions", "compact",
        key,
        "--agent", _OC_AGENT,
        "--max-lines", "200",
        "--json",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            print("[iLink] Session 截断成功（max-lines=200）", flush=True)
            return "Session 已截断（保留最近 200 行），上下文已释放。"
        else:
            err = stderr.decode(errors="replace")[:300]
            print(f"[iLink] Session 截断也失败: {err}", flush=True)
            return "Session 压缩和截断均失败，请手动管理 session。"
    except Exception as e:
        return f"Session 截断异常: {e}"


async def _forward_to_openclaw(user_id: str, text: str, ctx_token: str = "") -> str:
    if not text or not text.strip():
        return ""

    print(f"[iLink] → OpenClaw: {text[:60]}", flush=True)

    cmd = _OC_CMD + [
        "agent",
        "--agent", _OC_AGENT,
        "--session-key", _current_full_session_key(),
        "--session-id", _current_session_id(),
        "--message", text,
        "--json",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    task = asyncio.create_task(_handle_openclaw_result(user_id, proc, text, ctx_token))
    _ACTIVE_OPENCLAW_TASKS[user_id] = (proc, task, ctx_token)
    return "_PENDING_"


async def _handle_openclaw_result(user_id: str, proc: asyncio.subprocess.Process, user_text: str = "", ctx_token: str = ""):
    reply = ""
    try:
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:500]
            out = stdout.decode(errors="replace")[:200]
            print(f"[iLink] OpenClaw 错误 (code={proc.returncode}): {err}", flush=True)
            reply = f"OpenClaw 退出码 {proc.returncode}"
            if err:
                reply += f"\n{err}"
            elif out:
                reply += f"\n{out}"
        else:
            raw = stdout.decode(errors="replace").strip()
            if not raw:
                reply = "OpenClaw 返回空"
            else:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    reply = raw
                else:
                    if "payloads" in data and isinstance(data["payloads"], list) and len(data["payloads"]) > 0:
                        reply = data["payloads"][0].get("text", "")
                    if not reply:
                        payloads = data.get("result", {}).get("payloads", [])
                        if isinstance(payloads, list) and len(payloads) > 0:
                            reply = payloads[0].get("text", "")
                    if not reply:
                        reply = data.get("text", "") or data.get("response", "") or data.get("reply", "")
                    if not reply:
                        reply = raw
                if reply:
                    print(f"[iLink] OpenClaw ←: {reply[:60]}", flush=True)
    except asyncio.CancelledError:
        print(f"[iLink] OpenClaw 请求被取消", flush=True)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        _TYPING_USERS.pop(user_id, None)
        _TYPING_LAST_SENT.pop(user_id, None)
        async with httpx.AsyncClient() as client:
            _cancel_typing(client, user_id)
            await send_message(client, user_id, "OpenClaw 请求已取消", ctx_token)
        _ACTIVE_OPENCLAW_TASKS.pop(user_id, None)
        return
    except asyncio.TimeoutError:
        print(f"[iLink] OpenClaw 超时", flush=True)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        reply = "OpenClaw 响应超时"
    except Exception as e:
        print(f"[iLink] OpenClaw 异常: {e}", flush=True)
        reply = f"OpenClaw 异常: {e}"

    async with httpx.AsyncClient() as client:
        _cancel_typing(client, user_id)
        if reply:
            await send_message(client, user_id, reply, ctx_token)
        else:
            await send_message(client, user_id, "OpenClaw 未响应，请稍后重试", ctx_token)

    _ACTIVE_OPENCLAW_TASKS.pop(user_id, None)


# ─── Process ───────────────────────────────────────────────────
async def _renew_typing(client: httpx.AsyncClient):
    """续期 typing indicator（每 12s）。"""
    now = time.time()
    for uid in list(_TYPING_USERS.keys()):
        last = _TYPING_LAST_SENT.get(uid, 0)
        if now - last >= 12:
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

    # ── Bridge 内部命令：不消费 pending media，直接处理 ──
    if text.strip().startswith("/"):
        forwarded = text.strip()

        if forwarded == "/compact":
            result = await _compact_openclaw_session(to_id)
            print(f"[iLink] /compact: {result}", flush=True)
            await send_message(client, to_id, result, ctx)
            return

        if forwarded == "/help":
            lines = [
                "- /session [name] [comment] — 切换或创建 session",
                "- /compact — 压缩当前 session 上下文",
                "- /help — 显示此帮助",
                "- 其他消息 — 转发给当前 session",
                "",
            ]
            for name in _SESSIONS:
                cfg = _SESSIONS[name]
                line = f"- {name}"
                if cfg.get("comment"):
                    line += f" — {cfg['comment']}"
                if name == _current_session:
                    line += "【当前】"
                lines.append(line)
            await send_message(client, to_id, "\n".join(lines), ctx)
            return

        if forwarded == "/session":
            lines = []
            for name in _SESSIONS:
                cfg = _SESSIONS[name]
                line = f"- {name}"
                if cfg.get("comment"):
                    line += f" — {cfg['comment']}"
                if name == _current_session:
                    line += "【当前】"
                lines.append(line)
            lines.append("")
            lines.append("用法: /session <name> [comment]")
            await send_message(client, to_id, "\n".join(lines), ctx)
            return

        if forwarded.startswith("/session "):
            parts = forwarded[9:].strip().split(maxsplit=1)
            name = parts[0]
            comment = parts[1] if len(parts) > 1 else ""

            if name not in _SESSIONS:
                _SESSIONS[name] = {"session-id": str(uuid.uuid4()), "comment": comment}
                _save_config()
                _current_session = name
                print(f"[iLink] /session 创建: {name}", flush=True)
                reply = f"已创建并切换到 session {name}"
                if comment:
                    reply += f" — {comment}"
            else:
                if comment:
                    _SESSIONS[name]["comment"] = comment
                    _save_config()
                _current_session = name
                print(f"[iLink] /session: {name}", flush=True)
                reply = f"已切换到 session {name}"
                if _SESSIONS[name].get("comment"):
                    reply += f" — {_SESSIONS[name]['comment']}"

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

    # ── 所有消息 → OpenClaw（当前 session）──
    forwarded = text.strip()
    if not forwarded:
        return

    _TYPING_USERS[to_id] = time.time()
    try:
        await _send_typing(client, to_id, 1)
        _TYPING_LAST_SENT[to_id] = time.time()
    except Exception:
        pass

    status = await _forward_to_openclaw(to_id, forwarded, ctx)
    if status == "_PENDING_":
        return
    elif status:
        _cancel_typing(client, to_id)
        await send_message(client, to_id, status, ctx)
    else:
        _cancel_typing(client, to_id)
        await send_message(client, to_id, "OpenClaw 未响应，请稍后重试", ctx)


# ─── Main ──────────────────────────────────────────────────────
async def main():
    print("[ILinkBridge] WeChat 桥接就绪", flush=True)
    buf = ""
    async with httpx.AsyncClient() as client:
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
