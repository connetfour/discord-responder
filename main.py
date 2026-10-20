# language: Python 3.12, file: main.py, target: Railway
# Raw Discord gateway selfbot. No discord.py — aiohttp only.
# Two independent accounts, Gemini support triage, inline reference reply,
# 40s auto-delete, 3-minute per-user cooldown, staff skip.

import asyncio
import json
import os
import random
import re
import time
from collections import deque

import aiohttp
from aiohttp import WSMsgType, web

# ================================================================ tokens
TOKENS = [
    os.environ.get("DISCORD_TOKEN_1", "MTUzOTEyMjc4NDk5MzU0MjIwNQ.GNAi4X.Q5oLJdJpMoTBM9Q1HkIYFcykuDFvpj4KlZbGC0"),
    os.environ.get("DISCORD_TOKEN_2", "MTUzOTEyNDExNzk1MjAwNDA5OA.GiQoKK.znY4Azturay9LA2jK24UP3K7WS3LmuW0Rp51z0"),
]

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6LHRN9tiow-4cGyIBNTV4oWt2iSqLEYdH4E9-X143zUog")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODEL = "gemini-2.5-flash-lite"

API = "https://discord.com/api/v9"
GATEWAY = "wss://gateway.discord.gg/?v=9&encoding=json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

REPLY_TEXT = "i will be back soon"
DELETE_AFTER = 40
COOLDOWN_SECONDS = 180
PER_ACCOUNT_PER_MINUTE = 6
PORT = int(os.environ.get("PORT", 8080))
LOG_FILE = "activity.log"

SYSTEM_PROMPT = """You triage Discord messages for a server support team.
Decide whether the author needs help and expects a reply.

YES only if the author is stuck, broken, confused, reporting a problem,
asking for guidance or information they could not find, or asking for a human.
NO for greetings, small talk, jokes, banter, rhetorical questions, opinions,
statements, reactions, promotions, or messages that answer someone else.

Examples:
"how are you?" -> NO
"good morning everyone" -> NO
"lol that clip was insane" -> NO
"i think its fixed now" -> NO
"whats everyones favourite map" -> NO
"my game crashes every time i open the map" -> YES
"can someone tell me how to install this" -> YES
"anyone know why i keep getting error 403" -> YES
"is the server down for anyone else" -> YES
"where do i find the config file" -> YES

Reply with exactly YES or NO. Nothing else."""

# ================================================================ permissions
ADMINISTRATOR = 1 << 3
KICK_MEMBERS = 1 << 1
BAN_MEMBERS = 1 << 2
MANAGE_CHANNELS = 1 << 4
MANAGE_GUILD = 1 << 5
MANAGE_MESSAGES = 1 << 13
MANAGE_ROLES = 1 << 28
MODERATE_MEMBERS = 1 << 40

STAFF_BITS = (ADMINISTRATOR | KICK_MEMBERS | BAN_MEMBERS | MANAGE_CHANNELS
              | MANAGE_GUILD | MANAGE_MESSAGES | MANAGE_ROLES | MODERATE_MEMBERS)

STAFF_ROLE_NAMES = {
    "admin", "administrator", "mod", "moderator", "staff", "owner", "manager",
    "supervisor", "helper", "hmod", "smod", "head mod", "trial mod",
}

SKIP_EXACT = {
    "hi", "hey", "hello", "yo", "sup", "gm", "gn", "good morning", "good night",
    "good evening", "good afternoon", "how are you", "how are you doing",
    "how is everyone", "hows everyone", "how r u", "hru", "wyd", "wbu",
    "lol", "lmao", "lmfao", "rofl", "ok", "okay", "k", "thanks", "thank you",
    "ty", "thx", "np", "brb", "afk", "nice", "cool", "same", "true", "fr",
    "yes", "no", "yeah", "nah", "idk", "maybe", "what", "huh",
}

URL_RE = re.compile(r"https?://\S+$", re.IGNORECASE)
WORD_RE = re.compile(r"[a-zA-Z]{3,}")

_SEEN: set[int] = set()
_SEEN_ORDER: deque[int] = deque()
_SEEN_MAX = 5000


def log(line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {line}\n")
    except OSError:
        pass


def is_staff(guild_id: str, guild: dict, member: dict) -> bool:
    roles = guild.get("roles", {})
    perms = roles.get(guild_id, {}).get("permissions", 0)   # @everyone
    for rid in member.get("roles", []):
        role = roles.get(rid)
        if not role:
            continue
        perms |= role["permissions"]
        if role["name"].lower() in STAFF_ROLE_NAMES:
            return True
    return bool(perms & STAFF_BITS)


def worth_asking(text: str) -> bool:
    t = text.strip()
    if len(t) < 10:
        return False
    if "```" in t or "`" in t:
        return False
    if URL_RE.fullmatch(t):
        return False
    if not WORD_RE.search(t):
        return False
    if t.lower().strip("!?.,'\" ") in SKIP_EXACT:
        return False
    return True


def claim(message_id: int) -> bool:
    if message_id in _SEEN:
        return False
    _SEEN.add(message_id)
    _SEEN_ORDER.append(message_id)
    if len(_SEEN_ORDER) > _SEEN_MAX:
        _SEEN.discard(_SEEN_ORDER.popleft())
    return True


async def classify(http: aiohttp.ClientSession, text: str) -> bool:
    if not GEMINI_API_KEY or GEMINI_API_KEY.startswith("PASTE_"):
        return False
    payload = {
        "model": GEMINI_MODEL,
        "temperature": 0,
        "max_tokens": 8,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text[:600]},
        ],
    }
    headers = {"Authorization": f"Bearer {GEMINI_API_KEY}"}
    for attempt in range(3):
        try:
            async with http.post(GEMINI_URL, json=payload, headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 429:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status != 200:
                    log(f"[!] gemini http {r.status}: {(await r.text())[:120]}")
                    return False
                data = await r.json()
                out = (data["choices"][0]["message"]["content"] or "").strip().upper()
                return out.startswith("YES")
        except Exception as exc:
            log(f"[!] gemini error: {exc}")
            await asyncio.sleep(1.0 * (attempt + 1))
    return False


class Account:
    """One Discord user account: gateway connection plus REST helpers."""

    def __init__(self, index: int, token: str, http: aiohttp.ClientSession) -> None:
        self.index = index + 1
        self.token = token
        self.http = http
        self.self_id: str | None = None
        self.seq: int | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.guilds: dict[str, dict] = {}
        self.cooldowns: dict[str, float] = {}
        self.recent: deque[float] = deque()

    # ---------------------------------------------------------- rest
    def headers(self) -> dict:
        return {"Authorization": self.token, "Content-Type": "application/json",
                "User-Agent": UA}

    def rate_ok(self, now: float) -> bool:
        while self.recent and now - self.recent[0] > 60:
            self.recent.popleft()
        return len(self.recent) < PER_ACCOUNT_PER_MINUTE

    async def send_reply(self, channel_id: str, guild_id: str, message_id: str) -> str | None:
        body = {
            "content": REPLY_TEXT,
            "message_reference": {
                "message_id": message_id,
                "channel_id": channel_id,
                "guild_id": guild_id,
                "fail_if_not_exists": False,
            },
            "allowed_mentions": {"parse": [], "replied_user": False},
        }
        for attempt in range(3):
            try:
                async with self.http.post(f"{API}/channels/{channel_id}/messages",
                                          json=body, headers=self.headers()) as r:
                    if r.status == 429:
                        wait = float((await r.json()).get("retry_after", 2))
                        await asyncio.sleep(min(wait, 30))
                        continue
                    if r.status not in (200, 201):
                        log(f"[!] acct{self.index} send {r.status}: {(await r.text())[:120]}")
                        return None
                    return (await r.json())["id"]
            except Exception as exc:
                log(f"[!] acct{self.index} send error: {exc}")
                await asyncio.sleep(1.5 * (attempt + 1))
        return None

    async def delete_later(self, channel_id: str, message_id: str) -> None:
        await asyncio.sleep(DELETE_AFTER)
        try:
            async with self.http.delete(
                f"{API}/channels/{channel_id}/messages/{message_id}",
                headers=self.headers()) as r:
                if r.status not in (200, 204, 404):
                    log(f"[!] acct{self.index} delete {r.status}")
        except Exception as exc:
            log(f"[!] acct{self.index} delete error: {exc}")

    # ---------------------------------------------------------- gateway
    async def run(self) -> None:
        backoff = 5
        while True:
            try:
                await self._connect()
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"[!] account {self.index} dropped: {exc}; retry in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _connect(self) -> None:
        async with self.http.ws_connect(GATEWAY, max_msg_size=0,
                                        heartbeat=None) as ws:
            self.ws = ws
            hello = await ws.receive_json()
            interval = hello["d"]["heartbeat_interval"] / 1000.0
            beat = asyncio.create_task(self._heartbeat(interval))
            await self._identify()
            try:
                async for msg in ws:
                    if msg.type is WSMsgType.TEXT:
                        await self._dispatch(json.loads(msg.data))
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
            finally:
                beat.cancel()

    async def _heartbeat(self, interval: float) -> None:
        await asyncio.sleep(interval * random.random())
        while True:
            try:
                if self.ws and not self.ws.closed:
                    await self.ws.send_json({"op": 1, "d": self.seq})
            except Exception:
                return
            await asyncio.sleep(interval)

    async def _identify(self) -> None:
        await self.ws.send_json({
            "op": 2,
            "d": {
                "token": self.token,
                "capabilities": 16381,
                "properties": {
                    "$os": "Windows", "$browser": "Chrome", "$device": "",
                    "$system_locale": "en-US",
                    "$browser_user_agent": UA,
                    "$browser_version": "120.0.0.0",
                    "$os_version": "10",
                    "$referrer": "", "$referring_domain": "",
                    "$referrer_current": "", "$referring_domain_current": "",
                    "$release_channel": "stable",
                    "$client_build_number": 300000,
                    "$client_event_source": None,
                },
                "presence": {"status": "online", "since": 0,
                             "activities": [], "afk": False},
                "compress": False,
            },
        })

    async def _dispatch(self, payload: dict) -> None:
        if payload.get("s") is not None:
            self.seq = payload["s"]
        op = payload.get("op")
        if op == 1:
            await self.ws.send_json({"op": 1, "d": self.seq})
            return
        if op in (7, 9):
            await self.ws.close()
            return
        if op != 0:
            return

        event = payload.get("t")
        data = payload.get("d") or {}

        if event == "READY":
            self.self_id = data["user"]["id"]
            guilds = data.get("guilds", [])
            log(f"[+] account {self.index} online as "
                f"{data['user']['username']} guilds={len(guilds)}")
        elif event == "GUILD_CREATE":
            self._register_guild(data)
        elif event == "MESSAGE_CREATE":
            await self._on_message(data)

    def _register_guild(self, data: dict) -> None:
        gid = data.get("id")
        roles = {r["id"]: {"name": r.get("name", ""),
                           "permissions": int(r.get("permissions", "0"))}
                 for r in data.get("roles", [])}
        channels = {c["id"]: c.get("name", "?")
                    for c in data.get("channels", []) if c.get("type") in (0, 5)}
        self.guilds[gid] = {"roles": roles, "channels": channels,
                            "name": data.get("name", "?")}

    async def _on_message(self, data: dict) -> None:
        guild_id = data.get("guild_id")
        if not self.self_id or guild_id is None:
            return
        author = data.get("author") or {}
        if author.get("id") == self.self_id or author.get("bot"):
            return

        guild = self.guilds.get(guild_id)
        if guild is None:
            return
        if is_staff(guild_id, guild, data.get("member") or {}):
            return

        content = data.get("content") or ""
        if not worth_asking(content):
            return

        user_id = author.get("id")
        now = time.monotonic()
        last = self.cooldowns.get(user_id)
        if last is not None and now - last < COOLDOWN_SECONDS:
            return
        if not self.rate_ok(now):
            return
        if not claim(int(data["id"])):
            return

        if not await classify(self.http, content):
            return

        self.cooldowns[user_id] = now
        self.recent.append(now)
        await asyncio.sleep(random.uniform(1.5, 4.0))

        channel_id = data["channel_id"]
        sent_id = await self.send_reply(channel_id, guild_id, data["id"])
        if not sent_id:
            return

        channel_name = guild["channels"].get(channel_id, "?")
        log(f"[>] acct{self.index} {author.get('username', '?')} "
            f"#{channel_name}: {content[:70]!r}")
        asyncio.create_task(self.delete_later(channel_id, sent_id))


# ================================================================ keepalive
async def _ping(request: web.Request) -> web.Response:
    return web.Response(text="alive")


async def start_web() -> None:
    app = web.Application()
    app.router.add_get("/", _ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log(f"[+] keepalive on 0.0.0.0:{PORT}")


async def main() -> None:
    tokens = [t.strip() for t in TOKENS
              if t and t.strip() and not t.strip().startswith("PASTE_TOKEN")]
    if not tokens:
        print("[!] no tokens configured")
        return
    if GEMINI_API_KEY.startswith("PASTE_"):
        print("[!] no Gemini key — everything will classify NO")

    async with aiohttp.ClientSession() as http:
        await start_web()
        accounts = [Account(i, t, http) for i, t in enumerate(tokens)]
        log(f"[+] starting {len(accounts)} account(s)")
        await asyncio.gather(*(a.run() for a in accounts))


if __name__ == "__main__":
    asyncio.run(main())
