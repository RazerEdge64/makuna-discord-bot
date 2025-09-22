# onbot.py
# Shared Travian account helper.
# Commands:
#   /on activity:<choice> note:<text?> for:<minutes?>
#   /status
#   /off
#   /updates mode:<on|off> interval:<minutes?> ping:<here|off>
#   /clear_on (admin)

import os
from aiohttp import web
import sys
import json
import time
import datetime
import asyncio
import random
import discord
from discord import app_commands
from discord.ext import commands   # <-- use commands.Bot

# ---- optional .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN env var.")

STATE_FILE = os.environ.get("STATE_FILE", "onbot_state.json")

# Travian server timezone: UTC−1
SERVER_TZ = datetime.timezone(datetime.timedelta(hours=-1), name="UTC−1")

# ---- tiny HTTP server so Render Web Service stays healthy
_http_runner: web.AppRunner | None = None
_http_site: web.TCPSite | None = None

async def health(_request):
    return web.Response(text="ok")

async def start_http_server():
    global _http_runner, _http_site
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    port = int(os.environ.get("PORT", "10000"))  # Render sets PORT
    _http_runner = web.AppRunner(app)
    await _http_runner.setup()
    _http_site = web.TCPSite(_http_runner, "0.0.0.0", port)
    await _http_site.start()
    print(f"[http] listening on 0.0.0.0:{port}", flush=True)

# ---------- helpers ----------
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def to_server_dt(epoch: int | float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(int(epoch), tz=datetime.timezone.utc).astimezone(SERVER_TZ)

def fmt_hhmm(epoch: int | float) -> str:
    return to_server_dt(epoch).strftime("%H:%M")

def fmt_date_hhmm(epoch: int | float) -> str:
    return to_server_dt(epoch).strftime("%Y-%m-%d %H:%M")

def human_dur(seconds: int) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h: return f"{h}h{m:02d}m"
    if m: return f"{m}m"
    return f"{s}s"

# persistent memory by guild:
state = load_state()

# runtime tasks (not persisted)
update_tasks: dict[str, asyncio.Task] = {}

# ---------- bot ----------
ACTIVITY_CHOICES = [
    app_commands.Choice(name="farming",   value="farming"),
    app_commands.Choice(name="building",  value="building"),
    app_commands.Choice(name="raiding",   value="raiding"),
    app_commands.Choice(name="defending", value="defending"),
    app_commands.Choice(name="scouting",  value="scouting"),
    app_commands.Choice(name="market",    value="market"),
    app_commands.Choice(name="other",     value="other"),
]

class OnBot(commands.Bot):  # <-- commands.Bot
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),  # not used, just required
            intents=intents,
            allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=False)
        )

    async def setup_hook(self):
        ext_path = os.environ.get("ASK_EXT", "ask")  # set ASK_EXT if ask.py is in a subfolder
        try:
            await self.load_extension(ext_path)
            print(f"[ask] extension loaded: {ext_path}", flush=True)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"[ask] extension FAILED: {ext_path} -> {e}", flush=True)

        # Global sync (slow), but we'll also guild-sync below.
        await self.tree.sync()
        print("[sync] global sync requested", flush=True)

        # Debug context
        print(f"[env] CWD={os.getcwd()}", flush=True)
        print(f"[env] sys.path[0]={sys.path[0]}", flush=True)

client = OnBot()

# ---------- compose + send helpers ----------
def compose_update_text(cfg: dict, now: int | None = None) -> str:
    now = now or int(time.time())
    since = int(cfg["since"])
    end = int(since + cfg["for_min"] * 60) if cfg.get("for_min") else None
    show_date = to_server_dt(since).date() != to_server_dt(end if end else now).date()
    fmter = fmt_date_hhmm if show_date else fmt_hhmm
    user_mention = f"<@{cfg['user_id']}>"
    note = f" — {cfg['note']}" if cfg.get("note") else ""
    line2 = f"Server time: **{fmter(since)}** → **{fmter(end) if end else fmter(now)}** UTC−1"
    if end and end < now:
        line2 += " (planned end passed)"
    elapsed = human_dur(now - since)
    return f"{user_mention} **ON** — **{cfg['activity']}**{note}\n{line2} — **{elapsed}** elapsed."

async def send_update_once(channel: discord.abc.Messageable, cfg: dict):
    prefix = "@here " if cfg.get("ping_here") else ""
    await channel.send(
        prefix + "⏱️ Auto-update: " + compose_update_text(cfg),
        allowed_mentions=discord.AllowedMentions(everyone=True, users=True)
    )

# ---------- update loop ----------
async def update_loop(guild_id: str):
    try:
        while True:
            cfg = state.get(guild_id)
            if not cfg or not cfg.get("updates_enabled") or not cfg.get("user_id"):
                break
            chan_id = cfg.get("channel_id")
            channel = client.get_channel(chan_id) if chan_id else None
            if not channel:
                cfg["updates_enabled"] = False
                save_state(state)
                break
            try:
                await send_update_once(channel, cfg)
            except Exception:
                pass
            interval = max(5, int(cfg.get("interval_min", 60))) * 60
            await asyncio.sleep(interval)
    finally:
        t = update_tasks.get(guild_id)
        if t and t.done():
            update_tasks.pop(guild_id, None)

def start_update_task(guild_id: str):
    t = update_tasks.get(guild_id)
    if t and not t.done():
        t.cancel()
    if state.get(guild_id, {}).get("updates_enabled"):
        update_tasks[guild_id] = client.loop.create_task(update_loop(guild_id))

def stop_update_task(guild_id: str):
    t = update_tasks.get(guild_id)
    if t and not t.done():
        t.cancel()
    update_tasks.pop(guild_id, None)

# ---------- commands ----------
@client.tree.command(description="Mark yourself ON the account and log it here")
@app_commands.describe(activity="What are you doing?",
                       note="Optional short note",
                       for_min="Planned duration in minutes (optional)")
@app_commands.choices(activity=ACTIVITY_CHOICES)
async def on(interaction: discord.Interaction,
             activity: app_commands.Choice[str],
             note: str | None = None,
             for_min: int | None = None):
    guild_id = str(interaction.guild_id)
    current = state.get(guild_id)

    if current and current.get("user_id") != interaction.user.id:
        claimed_by = interaction.guild.get_member(current["user_id"])
        who = claimed_by.mention if claimed_by else f"<@{current['user_id']}>"
        return await interaction.response.send_message(
            f"🔴 Already ON by {who}. Use `/status` or ask a lead to `/clear_on`.",
            ephemeral=True
        )

    now = int(time.time())
    until = now + int(for_min * 60) if for_min and for_min > 0 else None

    state[guild_id] = {
        "user_id": interaction.user.id,
        "activity": activity.value,
        "note": (note or ""),
        "since": now,
        "for_min": int(for_min) if for_min else None,
        "updates_enabled": current.get("updates_enabled", False) if current else False,
        "interval_min": current.get("interval_min", 60) if current else 60,
        "channel_id": interaction.channel_id,
        "ping_here": current.get("ping_here", False) if current else False,
    }
    save_state(state)

    extras = []
    if note: extras.append(note)
    if until: extras.append(f"for {for_min}m (→ {fmt_hhmm(until)} UTC−1)")
    suffix = " — " + " | ".join(extras) if extras else ""
    msg = (
        f"🟢 {interaction.user.mention} is **ON** acc — **{activity.value}**{suffix}\n"
        f"Server time: **{fmt_hhmm(now)}** UTC−1 start"
    )
    await interaction.response.send_message(msg, allowed_mentions=discord.AllowedMentions(users=True))

    if state[guild_id]["updates_enabled"]:
        start_update_task(guild_id)

@client.tree.command(description="Show who is ON right now")
async def status(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    current = state.get(guild_id)
    if not current:
        return await interaction.response.send_message("No one is ON.", ephemeral=False)

    user = interaction.guild.get_member(current["user_id"])
    who = user.mention if user else f"<@{current['user_id']}>"

    since = int(current["since"])
    now = int(time.time())
    elapsed = now - since
    end = int(since + current["for_min"] * 60) if current.get("for_min") else None

    show_date = to_server_dt(since).date() != to_server_dt(end if end else now).date()
    fmter = fmt_date_hhmm if show_date else fmt_hhmm

    note = f" — {current['note']}" if current.get("note") else ""
    line2 = f"Server time: **{fmter(since)}** → **{fmter(end) if end else fmter(now)}** UTC−1"
    if end and end < now:
        line2 += " (planned end passed)"

    updates = "ON" if current.get("updates_enabled") else "OFF"
    interval = int(current.get("interval_min", 60))
    ping = "HERE" if current.get("ping_here") else "OFF"

    await interaction.response.send_message(
        f"🟢 {who} is **ON** — **{current['activity']}**{note}\n"
        f"{line2} — **{human_dur(elapsed)}** elapsed.\n"
        f"Auto-updates: **{updates}** (every **{interval}m**, ping **{ping}**) → <#{current.get('channel_id', interaction.channel_id)}>",
        allowed_mentions=discord.AllowedMentions(users=True)
    )

@client.tree.command(description="Mark yourself OFF the account and log it here")
async def off(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    current = state.get(guild_id)
    if not current or current.get("user_id") != interaction.user.id:
        return await interaction.response.send_message("ℹ️ You are not currently ON.", ephemeral=True)

    since = int(current["since"])
    now = int(time.time())
    duration = now - since

    show_date = to_server_dt(since).date() != to_server_dt(now).date()
    fmter = fmt_date_hhmm if show_date else fmt_hhmm

    stop_update_task(guild_id)
    state.pop(guild_id, None)
    save_state(state)

    await interaction.response.send_message(
        f"⚪ {interaction.user.mention} is **OFF**.\n"
        f"Server time: **{fmter(since)}** → **{fmter(now)}** UTC−1 — **{human_dur(duration)}**",
        allowed_mentions=discord.AllowedMentions(users=True)
    )

@client.tree.command(description="Toggle periodic updates in this channel")
@app_commands.describe(
    mode="Turn periodic updates on or off",
    interval="Minutes between updates (default 60)",
    ping="Ping @here on each update?"
)
@app_commands.choices(mode=[app_commands.Choice(name="on", value="on"),
                            app_commands.Choice(name="off", value="off")])
@app_commands.choices(ping=[app_commands.Choice(name="here", value="here"),
                            app_commands.Choice(name="off", value="off")])
async def updates(interaction: discord.Interaction,
                  mode: app_commands.Choice[str],
                  interval: int | None = None,
                  ping: app_commands.Choice[str] | None = None):
    guild_id = str(interaction.guild_id)
    cfg = state.get(guild_id) or {}
    if interval is not None and interval > 0:
        cfg["interval_min"] = int(interval)
    if ping is not None:
        cfg["ping_here"] = (ping.value == "here")
    cfg["updates_enabled"] = (mode.value == "on")
    cfg["channel_id"] = interaction.channel_id
    state[guild_id] = cfg
    save_state(state)

    if cfg.get("user_id") and cfg["updates_enabled"]:
        channel = client.get_channel(cfg["channel_id"])
        if channel:
            try:
                await send_update_once(channel, cfg)
            except Exception:
                pass
        start_update_task(guild_id)

    if not cfg.get("user_id"):
        msgs = [
            "👻 No sitter on deck. The fields are quiet. Type `/on` to claim.",
            "🌾 Nobody’s in the account—lists won’t click themselves. Use `/on`.",
            "🕳️ Empty throne. Take the helm with `/on`.",
            "🕰️ Silence in the granary. Who’s up? `/on` to start the shift."
        ]
        armed = "armed" if cfg["updates_enabled"] else "off"
        return await interaction.response.send_message(
            f"✅ Auto-updates **{armed}** (every **{cfg.get('interval_min',60)}m**, ping "
            f"{'HERE' if cfg.get('ping_here') else 'OFF'}) → <#{cfg['channel_id']}>\n" + random.choice(msgs)
        )

    ping_txt = "HERE" if cfg.get("ping_here") else "OFF"
    if cfg["updates_enabled"]:
        return await interaction.response.send_message(
            f"✅ Auto-updates **ON** every **{cfg['interval_min']}m** in <#{cfg['channel_id']}> (ping **{ping_txt}**)."
        )
    else:
        stop_update_task(guild_id)
        return await interaction.response.send_message("✅ Auto-updates **OFF**.")

@client.tree.command(description="(Admin) Clear current ON sitter")
@app_commands.default_permissions(manage_guild=True)
async def clear_on(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    stop_update_task(guild_id)
    state.pop(guild_id, None)
    save_state(state)
    await interaction.response.send_message("✅ Cleared current ON sitter.", ephemeral=True)

@client.tree.command(name="sync", description="Admin: sync slash commands to this server")
@app_commands.default_permissions(manage_guild=True)
async def sync_cmd(interaction: discord.Interaction):
    await client.tree.sync(guild=interaction.guild)
    await interaction.response.send_message("✅ Commands synced to this server.", ephemeral=True)

@client.event
async def on_ready():
    # Force a per-guild sync so commands appear instantly in each server
    for g in client.guilds:
        try:
            await client.tree.sync(guild=g)
            print(f"[sync] guild sync OK: {g.name} ({g.id})", flush=True)
        except Exception as e:
            print(f"[sync] guild sync FAILED for {g.id}: {e}", flush=True)


# ---------- run (start HTTP + Discord) ----------
async def main():
    await start_http_server()     # bind to $PORT for Render
    await client.start(TOKEN)     # don't use client.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
