# ask.py — LLM-powered /ask with spicy/funny persona (OpenAI)
import os, time, discord, aiohttp
from discord import app_commands
from discord.ext import commands

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_TOKEN")

PRIMARY_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
FALLBACK_MODELS = [
    "gpt-4o-mini-2024-07-18",
    "gpt-4o-2024-11-20",
    "gpt-4o",
]

ASK_MAX_CHARS   = int(os.environ.get("ASK_MAX_CHARS", "400"))
ASK_MAX_TOKENS  = int(os.environ.get("ASK_MAX_TOKENS", "300"))
ASK_COOLDOWN_S  = int(os.environ.get("ASK_COOLDOWN_S", "30"))
ASK_CHANNEL_ID  = os.environ.get("ASK_CHANNEL_ID")  # optional channel lock

# --- Personality knobs ---
# 0 = chill; 1 = playful; 2 = spicy banter; 3 = max spicy (still SFW-ish)
SPICE_LEVEL     = max(0, min(3, int(os.environ.get("SPICE_LEVEL", "2"))))
# You can override the persona copy with ASK_PERSONA if you want.
ASK_PERSONA     = os.environ.get("ASK_PERSONA", "").strip()

_last_ask_time: dict[int, float] = {}

def _persona_text(spice: int, nsfw_ok: bool) -> str:
    """Build the system prompt based on spice level and channel NSFW."""
    if ASK_PERSONA:
        base = ASK_PERSONA
    else:
        base = (
            "You are 'Black', the cheeky foreman of a shared Travian account. "
            "You speak like a gamer-farmer: sarcastic, quick, motivational, and funny. "
            "Your job: answer questions helpfully and keep the squad farming/raiding smart."
        )

    spice_bands = {
        0: "Keep it clean and friendly. No innuendo. Zero profanity.",
        1: "Playful and witty. Light teasing. Minimal mild profanity only when it improves humor.",
        2: "Spicy banter welcomed. Use mild innuendo and PG-13 profanity sparingly.",
        3: "High spice allowed: sharp banter, innuendo, and some profanity—but stay clever, not crude.",
    }
    # If channel isn’t NSFW, cap the spice at 2 (PG-13)
    effective_spice = min(spice, 2) if not nsfw_ok else spice

    rails = (
        "Hard rules:\n"
        # "- No hate speech or slurs. No targeting protected classes.\n"
        # "- No minors, explicit sex acts, graphic body parts, or pornographic detail.\n"
        # "- No doxxing or real-world harmful advice.\n"
        "- Keep answers concise (≤120 words) and actionable when relevant.\n"
        "- If asked for something disallowed, refuse briefly and redirect with a joke.\n"
    )

    seasoning = spice_bands[effective_spice]
    signoff = "Occasionally add a short quip like “lists won’t click themselves.”"

    return f"{base}\nTone: {seasoning}\n{rails}{signoff}"

async def _try_openai(model: str, prompt: str, sysmsg: str) -> tuple[bool, str]:
    if not OPENAI_API_KEY:
        return False, "LLM not configured. Set OPENAI_API_KEY in your env."
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "max_tokens": ASK_MAX_TOKENS,
        "temperature": 0.6,
        "messages": [
            {"role": "system", "content": sysmsg},
            {"role": "user", "content": prompt[:ASK_MAX_CHARS]},
        ],
    }
    async with aiohttp.ClientSession() as sess:
        async with sess.post(url, json=payload, headers=headers, timeout=60) as r:
            js = await r.json()
            if r.status == 200 and "choices" in js:
                try:
                    return True, js["choices"][0]["message"]["content"].strip()
                except Exception:
                    return False, f"Unexpected OpenAI response: {str(js)[:300]}"
            err = js.get("error", {}) if isinstance(js, dict) else {}
            msg = err.get("message") or str(js)[:300]
            return False, msg

async def _openai_complete(prompt: str, sysmsg: str) -> str:
    tried = [PRIMARY_MODEL] + [m for m in FALLBACK_MODELS if m != PRIMARY_MODEL]
    last_err = None
    for m in tried:
        ok, out = await _try_openai(m, prompt, sysmsg)
        if ok:
            return out
        last_err = (m, out)
        if out and "model" not in out.lower() and "invalid" not in out.lower():
            break
    if last_err:
        bad_model, msg = last_err
        return (
            f"OpenAI error with model **{bad_model}**: {msg}\n"
            "Tip: set `OPENAI_MODEL` to a model your account has access to "
            "(e.g., `gpt-4o-2024-11-20` or `gpt-4o-mini-2024-07-18`)."
        )
    return "Unknown OpenAI error."

class Ask(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(description="Ask the LLM (funny + a little spicy).")
    @app_commands.describe(q="Your question")
    async def ask(self, interaction: discord.Interaction, q: str):
        # Optional: lock to a single channel
        if ASK_CHANNEL_ID and str(interaction.channel_id) != ASK_CHANNEL_ID:
            return await interaction.response.send_message(
                f"Use this in <#{ASK_CHANNEL_ID}>.", ephemeral=True
            )

        # Per-user cooldown
        now = time.time()
        last = _last_ask_time.get(interaction.user.id, 0)
        if now - last < ASK_COOLDOWN_S:
            return await interaction.response.send_message(
                f"Cooldown — try again in {int(ASK_COOLDOWN_S - (now - last))}s.",
                ephemeral=True
            )
        _last_ask_time[interaction.user.id] = now

        # Detect NSFW channel if available (tones down if not NSFW)
        nsfw_ok = True
        ch = interaction.channel
        if hasattr(ch, "is_nsfw") and callable(ch.is_nsfw):
            try:
                nsfw_ok = bool(ch.is_nsfw())
            except Exception:
                nsfw_ok = False

        sysmsg = _persona_text(SPICE_LEVEL, nsfw_ok)

        await interaction.response.defer(thinking=True)
        answer = await _openai_complete(q, sysmsg)
        await interaction.followup.send(f"**Q:** {q[:ASK_MAX_CHARS]}\n**A:** {answer}")

async def setup(bot: commands.Bot):
    await bot.add_cog(Ask(bot))
