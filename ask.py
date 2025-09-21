# ask.py — LLM-powered /ask for your Discord bot (OpenAI)
import os
import time
import discord
from discord import app_commands

# optional .env for local dev
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import aiohttp

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_TOKEN")
OPENAI_MODEL   = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

ASK_MAX_CHARS   = int(os.environ.get("ASK_MAX_CHARS", "400"))
ASK_MAX_TOKENS  = int(os.environ.get("ASK_MAX_TOKENS", "300"))
ASK_COOLDOWN_S  = int(os.environ.get("ASK_COOLDOWN_S", "30"))
ASK_CHANNEL_ID  = os.environ.get("ASK_CHANNEL_ID")

_last_ask_time: dict[int, float] = {}

async def _openai_complete(prompt: str) -> str:
    if not OPENAI_API_KEY:
        return "LLM not configured. Set OPENAI_API_KEY in your env."
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "max_tokens": ASK_MAX_TOKENS,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content": "Be concise (≤120 words). Be helpful for a Travian/Discord team."},
            {"role": "user", "content": prompt[:ASK_MAX_CHARS]},
        ],
    }
    async with aiohttp.ClientSession() as sess:
        async with sess.post(url, json=payload, headers=headers, timeout=60) as r:
            js = await r.json()
            try:
                return js["choices"][0]["message"]["content"].strip()
            except Exception:
                return f"LLM error: {str(js)[:300]}"

class Ask(discord.Cog):
    def __init__(self, bot: discord.Client):
        self.bot = bot

    @app_commands.command(description="Ask the LLM (short, helpful answers).")
    @app_commands.describe(q="Your question")
    async def ask(self, interaction: discord.Interaction, q: str):
        # optional channel lock
        if ASK_CHANNEL_ID and str(interaction.channel_id) != ASK_CHANNEL_ID:
            return await interaction.response.send_message(
                f"Use this in <#{ASK_CHANNEL_ID}>.", ephemeral=True
            )
        # per-user cooldown
        now = time.time()
        last = _last_ask_time.get(interaction.user.id, 0)
        if now - last < ASK_COOLDOWN_S:
            return await interaction.response.send_message(
                f"Cooldown — try again in {int(ASK_COOLDOWN_S - (now - last))}s.",
                ephemeral=True
            )
        _last_ask_time[interaction.user.id] = now

        await interaction.response.defer(thinking=True)
        answer = await _openai_complete(q)
        await interaction.followup.send(f"**Q:** {q[:ASK_MAX_CHARS]}\n**A:** {answer}")

async def setup(bot: discord.Client):
    await bot.add_cog(Ask(bot))
