# language: Python 3.12, file: main.py, target: Railway
# Single-account raw Discord gateway selfbot. aiohttp only.
# GET /status to inspect connection state. TEST_CHANNEL_ID bypasses the
# classifier for isolation testing.

import asyncio
import json
import os
import random
import re
import time
from collections import deque

import aiohttp
from aiohttp import WSMsgType, web

# ================================================================ config
TOKEN = os.environ.get("DISCORD_TOKEN", "MTUzOTEyNDExNzk1MjAwNDA5OA.GWaLnd.gHX0Cyyfj2JMiX1P5hhl5-PZCumbc08v9wM4QM")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6LHRN9tiow-4cGyIBNTV4oWt2iSqLEYdH4E9-X143zUog")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

TEST_CHANNEL_ID = os.environ.get("TEST_CHANNEL_ID", "1562221992885755921").strip()
VERBOSE = os.environ.get("VERBOSE", "1") == "1"

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

STATUS = {
    "connected_as": None,
    "self_id": None,
    "guilds": [],
    "gateway_ready": False,
    "messages_seen": 0,
    "guilds_missing": 0,
    "ai_calls": 0,
    "ai_yes": 0,
    "replies_sent": 0,
    "last_error": None,
    "last_message": None,
}

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
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
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
    if not GEMINI_API_KEY:
        log("[!] no GEMINI_API_KEY — cannot classify")
        return False
    STATUS["ai_calls"] += 1
    payload = {
        "model": GEMINI_MODEL,
        "temperature": 0,
        "max_completion_tokens": 128,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text[:600]},
        ],
    }
    headers = {"Authorization": f"Bearer {GEMINI_API_KEY}"}
    for attempt in range(3):
        try:
            async with http.post(GEMINI_URL, json=payload, headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                body = await r.text()
                if r.status == 429:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                if r.status != 200:
                    STATUS["last_error"] = f"gemini {r.status}: {body[:160]}"
                    log(f"[!] gemini {r.status}: {body[:200]}")
                    return False
                data = json.loads(body)
                raw = (data["choices"][0]["message"]["content"] or "").strip()
                verdict = raw.upper().startswith("YES")
                if verdict:
                    STATUS["ai_yes"] += 1
                log(f"[ai] {raw[:40]!r} -> {'YES' if verdict else 'NO'}")
                return verdict
        except Exception as exc:
            STATUS["last_error"] = f"gemini error: {exc}"
            log(f"[!] gemini error: {exc}")
            await asyncio.sleep(1.0 * (attempt + 1))
    return False


class Account:
    def __init__(self, token: str, http: aiohttp.ClientSession) -> None:
        self.token = token
        self.http = http
        self.self_id: str | None = None
        self.seq: int | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.guilds: dict[str, dict] = {}
        self.cooldowns: dict[str, float] = {}
        self.recent: deque[float] = deque()

    # ------------------------------------------------------------ rest
    def headers(self) -> dict:
        return {"Authorization": self.token, "Content-Type": "application/json",
                "User-Agent": UA}

    def rate_ok(self, now: float) -> bool:
        while self.recent and now - self.recent[0] > 60:
            self.recent.popleft()
        return len(self.recent) < PER_ACCOUNT_PER_MINUTE

    async def fetch_guild(self, guild_id: str) -> None:
        try:
            async with self.http.get(f"{API}/guilds/{guild_id}",
                                     headers=self.headers()) as r:
                if r.status != 200:
                    log(f"[!] fetch guild {guild_id} -> {r.status}")
                    return
                self._register_guild(await r.json())
        except Exception as exc:
            log(f"[!] fetch guild {guild_id} error: {exc}")

    async def send_reply(self, channel_id: str, guild_id: str,
                         message_id: str) -> str | None:
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
                    text = await r.text()
                    if r.status == 429:
                        wait = float((json.loads(text)).get("retry_after", 2))
                        await asyncio.sleep(min(wait, 30))
                        continue
                    if r.status not in (200, 201):
                        STATUS["last_error"] = f"send {r.status}: {text[:160]}"
                        log(f"[!] send {r.status}: {text[:200]}")
                        return None
                    return json.loads(text)["id"]
            except Exception as exc:
                STATUS["last_error"] = f"send error: {exc}"
                log(f"[!] send error: {exc}")
                await asyncio.sleep(1.5 * (attempt + 1))
        return None

    async def delete_later(self, channel_id: str, message_id: str) -> None:
        await asyncio.sleep(DELETE_AFTER)
        try:
            async with self.http.delete(
                f"{API}/channels/{channel_id}/messages/{message_id}",
                headers=self.headers()) as r:
                log(f"[x] deleted {message_id} -> {r.status}")
        except Exception as exc:
            log(f"[!] delete error: {exc}")

    # ------------------------------------------------------------ gateway
    async def run(self) -> None:
        backoff = 5
        while True:
            try:
                await self._connect()
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                STATUS["last_error"] = f"gateway: {exc}"
                log(f"[!] gateway dropped: {exc}; retry in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _connect(self) -> None:
        log("[..] connecting to gateway")
        async with self.http.ws_connect(GATEWAY, max_msg_size=0) as ws:
            self.ws = ws
            hello = await ws.receive_json()
            interval = hello["d"]["heartbeat_interval"] / 1000.0
            log(f"[..] hello, heartbeat every {interval:.1f}s")
            beat = asyncio.create_task(self._heartbeat(interval))
            await self._identify()
            try:
                async for msg in ws:
                    if msg.type is WSMsgType.TEXT:
                        await self._dispatch(json.loads(msg.data))
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        log(f"[!] socket closed: {ws.exception()}")
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
        log("[..] identify sent")

    async def _dispatch(self, payload: dict) -> None:
        if payload.get("s") is not None:
            self.seq = payload["s"]
        op = payload.get("op")
        if op == 1:
            await self.ws.send_json({"op": 1, "d": self.seq})
            return
        if op in (7, 9):
            log(f"[!] gateway op {op} — reconnecting")
            await self.ws.close()
            return
        if op == 0 and payload.get("t") == "READY":
            data = payload["d"]
            self.self_id = data["user"]["id"]
            STATUS["self_id"] = self.self_id
            STATUS["connected_as"] = f"{data['user'].get('username')}#{data['user'].get('discriminator')}"
            STATUS["gateway_ready"] = True
            guilds = data.get("guilds", [])
            log(f"[+] READY as {STATUS['connected_as']} — {len(guilds)} guilds")
            await asyncio.gather(*(self.fetch_guild(g["id"]) for g in guilds),
                                 return_exceptions=True)
            STATUS["guilds"] = sorted(g["name"] for g in self.guilds.values())
            log(f"[+] guilds loaded: {STATUS['guilds']}")
            return
        if op != 0:
            return

        event = payload.get("t")
        data = payload.get("d") or {}
        if event == "GUILD_CREATE":
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
        STATUS["messages_seen"] += 1
        guild_id = data.get("guild_id")
        if guild_id is None:
            return
        author = data.get("author") or {}
        if author.get("id") == self.self_id or author.get("bot"):
            return

        content = data.get("content") or ""
        STATUS["last_message"] = f"{author.get('username')}: {content[:60]}"
        if VERBOSE:
            log(f"[.] {content[:80]!r} <{author.get('username')}>")

        guild = self.guilds.get(guild_id)
        if guild is None:
            STATUS["guilds_missing"] += 1
            log(f"[!] guild {guild_id} unknown — fetching now")
            await self.fetch_guild(guild_id)
            guild = self.guilds.get(guild_id)
            if guild is None:
                return

        if is_staff(guild_id, guild, data.get("member") or {}):
            if VERBOSE:
                log("[.] skip: staff member")
            return

        testing = bool(TEST_CHANNEL_ID) and data["channel_id"] == TEST_CHANNEL_ID

        if not testing:
            if not worth_asking(content):
                if VERBOSE:
                    log("[.] skip: prefilter")
                return
            uid = author.get("id")
            now = time.monotonic()
            last = self.cooldowns.get(uid)
            if last is not None and now - last < COOLDOWN_SECONDS:
                if VERBOSE:
                    log("[.] skip: cooldown")
                return
            if not self.rate_ok(now):
                return
            if not claim(int(data["id"])):
                return
            if not await classify(self.http, content):
                return
            self.cooldowns[uid] = now
            self.recent.append(now)
        else:
            log("[t] test channel — bypassing classifier")

        await asyncio.sleep(random.uniform(1.5, 4.0))

        channel_id = data["channel_id"]
        sent_id = await self.send_reply(channel_id, guild_id, data["id"])
        if not sent_id:
            return
        STATUS["replies_sent"] += 1
        log(f"[>] replied to {author.get('username')} in "
            f"#{guild['channels'].get(channel_id, '?')}")
        asyncio.create_task(self.delete_later(channel_id, sent_id))


# ================================================================ web
ACCOUNT: Account | None = None


async def _ping(request: web.Request) -> web.Response:
    return web.Response(text="alive")


async def _status(request: web.Request) -> web.Response:
    return web.json_response(STATUS)


async def start_web() -> None:
    app = web.Application()
    app.router.add_get("/", _ping)
    app.router.add_get("/status", _status)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log(f"[+] http on 0.0.0.0:{PORT}  (/ and /status)")


async def main() -> None:
    global ACCOUNT
    if not TOKEN or TOKEN.startswith("PASTE_"):
        print("[!] DISCORD_TOKEN missing")
        return
    if not GEMINI_API_KEY:
        print("[!] GEMINI_API_KEY missing — only test-channel replies will work")

    async with aiohttp.ClientSession() as http:
        await start_web()
        ACCOUNT = Account(TOKEN, http)
        await ACCOUNT.run()


if __name__ == "__main__":
    asyncio.run(main())
