# language: Python 3.11+, file: main.py, target: Replit, discord.py-self + Gemini
# Two independent selfbot workers, one per account, sharing this code path and
# one Gemini key. Each reads every message in its own servers, asks Gemini
# whether the author needs support, replies as a reference to that exact
# message, and deletes the reply after 40 seconds.

import asyncio
import os
import random
import re
import time
from collections import deque

try:
    import aiohttp
    import discord
    from aiohttp import web
except ImportError as exc:
    raise SystemExit(
        f"missing dependency: {exc.name}\n"
        "  fix: Packages pane -> install aiohttp and discord.py-self\n"
        "  or:  uv pip install -r requirements.txt"
    )

# ================================================================ tokens
# Paste each account's raw user token between the quotes.
TOKENS = [
    os.environ.get("DISCORD_TOKEN_1", "MTUzOTEyMjc4NDk5MzU0MjIwNQ.GNAi4X.Q5oLJdJpMoTBM9Q1HkIYFcykuDFvpj4KlZbGC0"),
    os.environ.get("DISCORD_TOKEN_2", "MTUzOTEyNDExNzk1MjAwNDA5OA.GiQoKK.znY4Azturay9LA2jK24UP3K7WS3LmuW0Rp51z0"),
]

# ================================================================ ai
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6LHRN9tiow-4cGyIBNTV4oWt2iSqLEYdH4E9-X143zUog")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODEL = "gemini-2.5-flash-lite"

# ================================================================ behaviour
REPLY_TEXT = "i will be back soon"
DELETE_AFTER = 40             # seconds
COOLDOWN_SECONDS = 180        # per user, per account
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

# ================================================================ filters
STAFF_ROLE_NAMES = {
    "admin", "administrator", "mod", "moderator", "staff", "owner", "manager",
    "supervisor", "helper", "hmod", "smod", "head mod", "trial mod",
}

MOD_PERMS = (
    "administrator", "manage_guild", "manage_messages", "kick_members",
    "ban_members", "manage_roles", "manage_channels", "moderate_members",
)

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
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {line}\n")
    except OSError:
        pass


def is_staff(member: discord.Member) -> bool:
    gp = member.guild_permissions
    if any(getattr(gp, p, False) for p in MOD_PERMS):
        return True
    return bool({r.name.lower() for r in member.roles} & STAFF_ROLE_NAMES)


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
                    log(f"[!] gemini http {r.status}")
                    return False
                data = await r.json()
                out = (data["choices"][0]["message"]["content"] or "").strip().upper()
                return out.startswith("YES")
        except Exception as exc:
            log(f"[!] gemini error: {exc}")
            await asyncio.sleep(1.0 * (attempt + 1))
    return False


class Worker(discord.Client):
    """One account. Own cooldowns, own rate budget, own servers."""

    def __init__(self, index: int, http: aiohttp.ClientSession) -> None:
        super().__init__()
        self.index = index + 1
        self.http = http
        self.cooldowns: dict[int, float] = {}
        self.recent: deque[float] = deque()

    def rate_ok(self, now: float) -> bool:
        while self.recent and now - self.recent[0] > 60:
            self.recent.popleft()
        return len(self.recent) < PER_ACCOUNT_PER_MINUTE

    def on_cooldown(self, user_id: int, now: float) -> bool:
        last = self.cooldowns.get(user_id)
        return last is not None and (now - last) < COOLDOWN_SECONDS

    async def on_ready(self) -> None:
        log(f"[+] account {self.index} online as {self.user} "
            f"guilds={len(self.guilds)}")

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if message.author.id == self.user.id or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return
        if is_staff(message.author):
            return
        if not worth_asking(message.content):
            return

        now = time.monotonic()
        if self.on_cooldown(message.author.id, now):
            return
        if not self.rate_ok(now):
            return
        if not claim(message.id):
            return

        if not await classify(self.http, message.content):
            return

        me = message.guild.me
        if me is None or not message.channel.permissions_for(me).send_messages:
            return

        self.cooldowns[message.author.id] = now
        self.recent.append(now)
        await asyncio.sleep(random.uniform(1.5, 4.0))

        try:
            sent = await message.reply(REPLY_TEXT, mention_author=False)
        except discord.HTTPException:
            try:
                sent = await message.channel.send(REPLY_TEXT)
            except discord.HTTPException as exc:
                log(f"[!] acct{self.index} send failed: {exc}")
                return

        log(f"[>] acct{self.index} {message.author} "
            f"#{getattr(message.channel, 'name', '?')}: {message.content[:70]!r}")
        asyncio.create_task(self._expire(sent))

    @staticmethod
    async def _expire(msg: discord.Message) -> None:
        await asyncio.sleep(DELETE_AFTER)
        try:
            await msg.delete()
        except discord.HTTPException:
            pass


async def run_worker(worker: Worker, token: str) -> None:
    delay = 5
    while True:
        try:
            await worker.start(token)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"[!] account {worker.index} dropped: {exc}; retry in {delay}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


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
        print("[!] no tokens configured — fill the TOKENS list")
        return
    if GEMINI_API_KEY.startswith("PASTE_"):
        print("[!] no Gemini key — every message will classify as NO")

    async with aiohttp.ClientSession() as http:
        await start_web()
        workers = [Worker(i, http) for i in range(len(tokens))]
        log(f"[+] starting {len(workers)} account(s)")
        await asyncio.gather(*(run_worker(w, t)
                               for w, t in zip(workers, tokens)))


if __name__ == "__main__":
    asyncio.run(main())
