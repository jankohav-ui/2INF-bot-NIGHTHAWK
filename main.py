import discord
from discord import app_commands
from discord.ext import tasks, commands
import asyncio
import random
from datetime import datetime
import pytz
import os
import json
from typing import Optional

# ==========================================
# CONFIGURATION
# ==========================================
TOKEN = os.environ.get('DISCORD_TOKEN', '')

SETTINGS_FILE = 'settings.json'
DEFAULT_SETTINGS = {
    'CHANNEL_ID': 0,
    'MAX_SLOTS': 10,
    'START_MINUTE': 25,
    'DRAW_MINUTE': 35,
    'END_MINUTE': 40,
    'PRIORITY_ROLE_ID': None,
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                data = json.load(f)
                for key, val in DEFAULT_SETTINGS.items():
                    if key not in data:
                        data[key] = val
                return data
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings():
    data = {
        'CHANNEL_ID': CHANNEL_ID,
        'MAX_SLOTS': MAX_SLOTS,
        'START_MINUTE': START_MINUTE,
        'DRAW_MINUTE': DRAW_MINUTE,
        'END_MINUTE': END_MINUTE,
        'PRIORITY_ROLE_ID': PRIORITY_ROLE_ID,
    }
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

_s = load_settings()
CHANNEL_ID       = _s['CHANNEL_ID']
MAX_SLOTS        = _s['MAX_SLOTS']
START_MINUTE     = _s['START_MINUTE']
DRAW_MINUTE      = _s['DRAW_MINUTE']
END_MINUTE       = _s['END_MINUTE']
PRIORITY_ROLE_ID = _s['PRIORITY_ROLE_ID']

BLACKLIST_USERS = set()
BAN_USERS = set()

TIMEZONE = pytz.timezone('Europe/Zagreb')

# ==========================================
# INTERNAL STATE
# ==========================================
last_winner_id = None
winner_history = []
current_participants = []
event_active = False
join_button_locked = False
current_event_message = None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# ==========================================
# UI: BUTTON + EMBED
# ==========================================
def build_embed():
    if not current_participants:
        participant_text = "🎯 *No one has joined yet*"
    else:
        channel = bot.get_channel(CHANNEL_ID)
        guild = channel.guild if channel else None
        lines = []
        for idx, uid in enumerate(current_participants[:MAX_SLOTS], start=1):
            member = guild.get_member(uid) if guild else None
            name = member.display_name if member else "Unknown"
            has_priority = PRIORITY_ROLE_ID and member and any(r.id == PRIORITY_ROLE_ID for r in member.roles)
            star = "⭐ " if has_priority else ""
            lines.append(f"{idx}. {star}{name}")
        participant_text = "\n".join(lines)

    status = "🔓 OPEN" if not join_button_locked else "🔒 LOCKED"
    embed = discord.Embed(
        title="🚛 inf lista",
        description=(
            f"**⏰ Duration:** :{str(START_MINUTE).zfill(2)} — :{str(END_MINUTE).zfill(2)}\n"
            f"**👥 First {MAX_SLOTS} are on the list, priority roles have advantage**\n"
            f"**🏆 Prize:** Random winner drives the Ammo Car\n"
            f"**📊 Status:** {status}\n\n"
            f"**Participants ({len(current_participants)}/{MAX_SLOTS}):**\n"
            f"{participant_text}\n\n"
            f"*Izvlačenje u :{str(DRAW_MINUTE).zfill(2)}, lista se zatvara u :{str(END_MINUTE).zfill(2)}*"
        ),
        color=0xFF5500
    )
    embed.set_footer(text="Click the button below to enter!")
    return embed


class JoinButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔘 Udi na listu", style=discord.ButtonStyle.success, custom_id="ammo_join")
    async def join_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        global current_participants, join_button_locked

        try:
            if not event_active:
                await interaction.response.send_message(f"❌ Event nije aktivan! Sljedeći kreće u :{str(START_MINUTE).zfill(2)}.", ephemeral=True)
                return

            if join_button_locked:
                await interaction.response.send_message("🔒 Lista je zaključana!", ephemeral=True)
                return

            if interaction.user.id in BAN_USERS:
                await interaction.response.send_message("🚫 Baniran/a si i ne možeš ući na listu.", ephemeral=True)
                return

            if interaction.user.id in current_participants:
                await interaction.response.send_message("⚠️ Već si na listi!", ephemeral=True)
                return

            guild = interaction.guild
            member = guild.get_member(interaction.user.id) if guild else None
            nick = member.display_name if member else interaction.user.display_name
            has_priority = bool(PRIORITY_ROLE_ID and member and any(r.id == PRIORITY_ROLE_ID for r in member.roles))

            if len(current_participants) >= MAX_SLOTS:
                if has_priority:
                    bumped_uid = None
                    for uid in reversed(current_participants):
                        m = guild.get_member(uid) if guild else None
                        if not m or not any(r.id == PRIORITY_ROLE_ID for r in m.roles):
                            bumped_uid = uid
                            break

                    if bumped_uid is None:
                        await interaction.response.send_message("❌ Lista je puna i svi imaju priority rol. Nema mjesta.", ephemeral=True)
                        return

                    current_participants.remove(bumped_uid)
                    current_participants.append(interaction.user.id)
                    bumped_member = guild.get_member(bumped_uid) if guild else None
                    bumped_name = bumped_member.display_name if bumped_member else f"<@{bumped_uid}>"
                    await interaction.response.send_message(f"⭐ Ušao/la priority rolom! **{bumped_name}** je izbačen/a.", ephemeral=True)
                    await update_message()
                    ch = bot.get_channel(CHANNEL_ID)
                    if ch:
                        await ch.send(f"⭐ **{nick}** je ušao/la priority rolom i izbacio/la **{bumped_name}** s liste!")
                else:
                    await interaction.response.send_message(f"❌ Lista je puna ({MAX_SLOTS}/{MAX_SLOTS}). Pričekaj do :{str(END_MINUTE).zfill(2)}, možda neko izađe!", ephemeral=True)
                return

            current_participants.append(interaction.user.id)
            prefix = "⭐ " if has_priority else ""
            await interaction.response.send_message(f"✅ **{prefix}{nick}** na listi! ({len(current_participants)}/{MAX_SLOTS})", ephemeral=True)
            await update_message()

        except Exception as e:
            print(f"❌ Greška u join_callback: {e}")
            try:
                await interaction.response.send_message("❌ Došlo je do greške. Pokušaj ponovo.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="🚪 Izađi s liste", style=discord.ButtonStyle.danger, custom_id="ammo_leave")
    async def leave_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        global current_participants, join_button_locked

        try:
            if not event_active:
                await interaction.response.send_message("❌ Nema aktivnog eventa.", ephemeral=True)
                return

            if join_button_locked:
                await interaction.response.send_message("🔒 Lista je zaključana, ne možeš izaći.", ephemeral=True)
                return

            if interaction.user.id not in current_participants:
                await interaction.response.send_message("⚠️ Nisi na listi!", ephemeral=True)
                return

            current_participants.remove(interaction.user.id)
            guild = interaction.guild
            member = guild.get_member(interaction.user.id) if guild else None
            nick = member.display_name if member else interaction.user.display_name
            await interaction.response.send_message(f"✅ **{nick}** skinut/a s liste.", ephemeral=True)
            await update_message()

        except Exception as e:
            print(f"❌ Greška u leave_callback: {e}")
            try:
                await interaction.response.send_message("❌ Došlo je do greške. Pokušaj ponovo.", ephemeral=True)
            except Exception:
                pass


async def update_message():
    if current_event_message:
        embed = build_embed()
        view = JoinButtonView()
        await current_event_message.edit(embed=embed, view=view)


# ==========================================
# GLOBAL SLASH COMMAND ERROR HANDLER
# ==========================================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ Nemaš admin permisije za ovu komandu."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"⏳ Pričekaj {error.retry_after:.1f}s."
    else:
        msg = f"❌ Greška: {error}"
    try:
        await interaction.response.send_message(msg, ephemeral=True)
    except discord.InteractionResponded:
        await interaction.followup.send(msg, ephemeral=True)


# ==========================================
# SCHEDULER: RUNS EVERY MINUTE
# ==========================================
@tasks.loop(minutes=1)
async def event_scheduler():
    global event_active, join_button_locked, current_participants, current_event_message

    now = datetime.now(TIMEZONE)
    minute = now.minute

    if CHANNEL_ID == 0:
        return

    reminder_minute = (START_MINUTE - 5) % 60
    if minute == reminder_minute and not event_active:
        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            await channel.send("⏳ **INF - lista pocinje za 5 minuta.**")

    if minute == START_MINUTE and not event_active:
        event_active = True
        join_button_locked = False
        current_participants = []

        channel = bot.get_channel(CHANNEL_ID)
        if not channel:
            print(f"❌ Channel {CHANNEL_ID} not found!")
            return

        embed = build_embed()
        view = JoinButtonView()
        msg = await channel.send(embed=embed, view=view)
        current_event_message = msg
        await channel.send("@everyone 🚨 INF lista je pocela! Prvih 10 ulazi, bira se ko vozi AMMO CAR! 🚛")
        print(f"✅ Event started at {now.strftime('%H:%M')}")

    if minute == DRAW_MINUTE and event_active:
        channel = bot.get_channel(CHANNEL_ID)

        if len(current_participants) == 0:
            await channel.send("😢 **Nitko nije na listi. Ajmo se aktivirat malo.**")
        else:
            eligible = [uid for uid in current_participants if uid not in BLACKLIST_USERS]
            if not eligible:
                await channel.send("⚠️ **Nitko od prijavljenih nije prihvatljiv za izvlačenje.** Svi sudionici su na blacklisti.")
            else:
                winner_id = random.choice(eligible)
                global last_winner_id
                last_winner_id = winner_id
                winner_history.append({"id": winner_id, "time": datetime.now(TIMEZONE).strftime("%d.%m. %H:%M")})
                if len(winner_history) > 5:
                    winner_history.pop(0)
                winner = bot.get_user(winner_id)
                winner_mention = winner.mention if winner else f"<@{winner_id}>"
                await channel.send(f"🎲 **IZVLAČENJE!** Pobjednik je... {winner_mention} 🎉\n🚗💨 **Ammo car vozi {winner_mention}!** 🚗💨")

        print(f"🎲 Draw done at {now.strftime('%H:%M')}")

    if minute == END_MINUTE and event_active:
        join_button_locked = True
        await update_message()

        event_active = False
        join_button_locked = False
        current_participants = []

        if current_event_message:
            old_view = discord.ui.View.from_message(current_event_message)
            for child in old_view.children:
                child.disabled = True
            await current_event_message.edit(view=old_view)
            current_event_message = None

        print(f"🏁 Event finished at {now.strftime('%H:%M')}")


# ==========================================
# SLASH COMMANDS
# ==========================================

@bot.tree.command(name="force_start", description="Ručno pokreće event odmah.")
@app_commands.checks.has_permissions(administrator=True)
async def force_start(interaction: discord.Interaction):
    global event_active, join_button_locked, current_participants, current_event_message

    if event_active:
        await interaction.response.send_message("⚠️ Event već traje! Pričekaj da završi.", ephemeral=True)
        return

    event_active = True
    join_button_locked = False
    current_participants = []

    await interaction.response.send_message("✅ Event pokrenuto! Pratite kanal.", ephemeral=True)

    embed = build_embed()
    view = JoinButtonView()
    msg = await interaction.channel.send(embed=embed, view=view)
    current_event_message = msg
    await interaction.channel.send(f"@everyone 🚨 **Inf lista je pocela imate do :{str(END_MINUTE).zfill(2)} da udete i pobjednik vozi ammo!**")

    async def run_event():
        global event_active, join_button_locked, current_participants, current_event_message, last_winner_id
        await asyncio.sleep(900)
        if event_active:
            join_button_locked = True
            await update_message()
            if len(current_participants) == 0:
                await interaction.channel.send("😢 Nitko nije ušao. Event završen.")
            else:
                eligible = [uid for uid in current_participants if uid not in BLACKLIST_USERS]
                if not eligible:
                    await interaction.channel.send("⚠️ **Nema prihvatljivih sudionika** — svi su na blacklisti.")
                else:
                    winner_id = random.choice(eligible)
                    last_winner_id = winner_id
                    winner_history.append({"id": winner_id, "time": datetime.now(TIMEZONE).strftime("%d.%m. %H:%M")})
                    if len(winner_history) > 5:
                        winner_history.pop(0)
                    winner = bot.get_user(winner_id)
                    winner_mention = winner.mention if winner else f"<@{winner_id}>"
                    await interaction.channel.send(f"🎉 **POBJEDNIK:** {winner_mention} vozi Ammo Car! 🚛")
            event_active = False
            current_participants = []
            current_event_message = None

    asyncio.create_task(run_event())


@bot.tree.command(name="force_end", description="Zaustavlja trenutni event bez izvlačenja pobjednika.")
@app_commands.checks.has_permissions(administrator=True)
async def force_end(interaction: discord.Interaction):
    global event_active, current_participants, current_event_message
    if not event_active:
        await interaction.response.send_message("❌ Nema aktivnog eventa.", ephemeral=True)
        return
    event_active = False
    current_participants = []
    if current_event_message:
        old_view = discord.ui.View.from_message(current_event_message)
        for child in old_view.children:
            child.disabled = True
        await current_event_message.edit(view=old_view)
        current_event_message = None
    await interaction.response.send_message("⏹️ Event force-stopan.", ephemeral=True)


@bot.tree.command(name="ping", description="Provjeri radi li bot i kolika mu je latencija.")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! Latencija: **{latency}ms**", ephemeral=True)


@bot.tree.command(name="remind", description="Ručno šalje podsjetnik u event kanal da lista uskoro počinje.")
@app_commands.checks.has_permissions(administrator=True)
async def remind(interaction: discord.Interaction):
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("❌ Event kanal nije pronađen.", ephemeral=True)
        return
    await channel.send(f"⏳ **INF lista počinje za malo — :{str(START_MINUTE).zfill(2)}! Budite spremni! 🚛**")
    await interaction.response.send_message("✅ Podsjetnik poslan.", ephemeral=True)


@bot.tree.command(name="reroll", description="Bira novog pobjednika — lista ostaje ista.")
@app_commands.checks.has_permissions(administrator=True)
async def reroll(interaction: discord.Interaction):
    global last_winner_id
    if len(current_participants) == 0:
        await interaction.response.send_message("😢 **Lista je prazna. Nema koga birati!**", ephemeral=True)
        return

    eligible = [uid for uid in current_participants if uid not in BLACKLIST_USERS]
    if not eligible:
        await interaction.response.send_message("⚠️ **Nitko nije prihvatljiv za reroll.** Svi su na blacklisti.", ephemeral=True)
        return

    winner_id = random.choice(eligible)
    last_winner_id = winner_id
    winner_history.append({"id": winner_id, "time": datetime.now(TIMEZONE).strftime("%d.%m. %H:%M")})
    if len(winner_history) > 5:
        winner_history.pop(0)
    winner = bot.get_user(winner_id)
    winner_mention = winner.mention if winner else f"<@{winner_id}>"
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        await channel.send(f"🔁 **REROLL!** Novi vozač Ammo Cara je... {winner_mention} 🎉🚗💨")
    await interaction.response.send_message(f"✅ Reroll gotov — pobjednik: {winner_mention}", ephemeral=True)


@bot.tree.command(name="add", description="Dodaj korisnika na listu dok je event aktivan.")
async def add_to_list(interaction: discord.Interaction, member: discord.Member):
    global current_participants, join_button_locked

    if not event_active:
        await interaction.response.send_message("❌ Nema aktivnog eventa.", ephemeral=True)
        return

    if join_button_locked:
        await interaction.response.send_message("🔒 Lista je zaključana.", ephemeral=True)
        return

    if member.id in BAN_USERS:
        await interaction.response.send_message(f"🚫 **{member.display_name}** je baniran/a i ne može ući na listu.", ephemeral=True)
        return

    if member.id in current_participants:
        await interaction.response.send_message(f"⚠️ **{member.display_name}** već je na listi.", ephemeral=True)
        return

    guild = interaction.guild
    m = guild.get_member(member.id) if guild else None
    has_priority = bool(PRIORITY_ROLE_ID and m and any(r.id == PRIORITY_ROLE_ID for r in m.roles))

    if len(current_participants) >= MAX_SLOTS:
        if has_priority:
            bumped_uid = None
            for uid in reversed(current_participants):
                bm = guild.get_member(uid) if guild else None
                if not bm or not any(r.id == PRIORITY_ROLE_ID for r in bm.roles):
                    bumped_uid = uid
                    break

            if bumped_uid is None:
                await interaction.response.send_message("❌ Lista je puna i svi imaju priority rol. Nema mjesta.", ephemeral=True)
                return

            current_participants.remove(bumped_uid)
            current_participants.append(member.id)
            bumped_member = guild.get_member(bumped_uid) if guild else None
            bumped_name = bumped_member.display_name if bumped_member else f"<@{bumped_uid}>"
            await interaction.response.send_message(f"⭐ **{member.display_name}** dodan priority rolom! **{bumped_name}** je izbačen/a.", ephemeral=True)
            await update_message()
        else:
            await interaction.response.send_message(f"❌ Lista je puna ({MAX_SLOTS}/{MAX_SLOTS}).", ephemeral=True)
        return

    current_participants.append(member.id)
    prefix = "⭐ " if has_priority else ""
    await interaction.response.send_message(f"✅ **{prefix}{member.display_name}** dodan/a na listu! ({len(current_participants)}/{MAX_SLOTS})", ephemeral=True)
    await update_message()


@bot.tree.command(name="kick_from_list", description="Makni korisnika s liste dok je event aktivan.")
@app_commands.checks.has_permissions(administrator=True)
async def kick_from_list(interaction: discord.Interaction, member: discord.Member):
    if not event_active:
        await interaction.response.send_message("❌ Nema aktivnog eventa.", ephemeral=True)
        return

    if member.id not in current_participants:
        await interaction.response.send_message(f"⚠️ **{member.display_name}** nije na listi.", ephemeral=True)
        return

    current_participants.remove(member.id)
    await interaction.response.send_message(f"✅ **{member.display_name}** je maknut/a s liste.", ephemeral=True)
    await update_message()


@bot.tree.command(name="ban", description="Zabranjuje korisniku ulaz na listu.")
@app_commands.checks.has_permissions(administrator=True)
async def ban_user(interaction: discord.Interaction, member: discord.Member):
    global BAN_USERS

    if member.id in BAN_USERS:
        await interaction.response.send_message(f"⚠️ **{member.display_name}** već je baniran/a.", ephemeral=True)
        return

    BAN_USERS.add(member.id)
    if member.id in current_participants:
        current_participants.remove(member.id)
        await update_message()
        await interaction.response.send_message(f"🔨 **{member.display_name}** je baniran/a i maknut/a s liste.", ephemeral=True)
    else:
        await interaction.response.send_message(f"🔨 **{member.display_name}** je baniran/a — ne može ući na listu.", ephemeral=True)


@bot.tree.command(name="unban", description="Uklanja ban — korisnik može ponovo ući na listu.")
@app_commands.checks.has_permissions(administrator=True)
async def unban_user(interaction: discord.Interaction, member: discord.Member):
    global BAN_USERS

    if member.id not in BAN_USERS:
        await interaction.response.send_message(f"⚠️ **{member.display_name}** nije baniran/a.", ephemeral=True)
        return

    BAN_USERS.discard(member.id)
    await interaction.response.send_message(f"✅ **{member.display_name}** je unbaniran/a — može ponovo ući na listu.", ephemeral=True)


@bot.tree.command(name="banlist", description="Prikaži sve trenutno banirane korisnike.")
@app_commands.checks.has_permissions(administrator=True)
async def banlist(interaction: discord.Interaction):
    if not BAN_USERS:
        await interaction.response.send_message("✅ Nema banirani korisnika.", ephemeral=True)
        return

    guild = interaction.guild
    lines = []
    for uid in BAN_USERS:
        member = guild.get_member(uid) if guild else None
        name = member.display_name if member else f"<@{uid}> *(nije na serveru)*"
        lines.append(f"• {name} (`{uid}`)")

    embed = discord.Embed(
        title="🔨 Banirani korisnici",
        description="\n".join(lines),
        color=0x880000
    )
    embed.set_footer(text=f"{len(BAN_USERS)} korisnik(a) je baniran/a.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="set_channel", description="Postavi kanal u koji bot šalje event.")
@app_commands.checks.has_permissions(administrator=True)
async def set_channel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    global CHANNEL_ID

    if channel is None:
        current = bot.get_channel(CHANNEL_ID)
        current_mention = current.mention if current else f"`{CHANNEL_ID}` *(nije pronađen)*"
        await interaction.response.send_message(f"ℹ️ Trenutni event kanal: {current_mention}\nKorištenje: `/set_channel #kanal`", ephemeral=True)
        return

    if event_active:
        await interaction.response.send_message("⚠️ Ne možeš mijenjati kanal dok event traje. Koristi `/force_end` prvo.", ephemeral=True)
        return

    old_id = CHANNEL_ID
    CHANNEL_ID = channel.id
    save_settings()
    old_channel = bot.get_channel(old_id)
    old_mention = old_channel.mention if old_channel else "*(nije bio postavljen)*"
    await interaction.response.send_message(f"✅ Event kanal postavljen: {old_mention} → {channel.mention}\n✅ Snimljeno trajno — ostaje i nakon restarta.", ephemeral=True)


@bot.tree.command(name="set_slots", description="Mijenja max broj mjesta na listi.")
@app_commands.checks.has_permissions(administrator=True)
async def set_slots(interaction: discord.Interaction, number: Optional[int] = None):
    global MAX_SLOTS

    if number is None:
        await interaction.response.send_message(f"ℹ️ Trenutno max slotova: **{MAX_SLOTS}**\nKorištenje: `/set_slots broj` (npr. `/set_slots 20`)", ephemeral=True)
        return

    if number < 1 or number > 100:
        await interaction.response.send_message("❌ Broj mora biti između 1 i 100.", ephemeral=True)
        return

    if event_active:
        await interaction.response.send_message("⚠️ Ne možeš mijenjati slotove dok event traje. Koristi `/force_end` prvo.", ephemeral=True)
        return

    old = MAX_SLOTS
    MAX_SLOTS = number
    save_settings()
    await interaction.response.send_message(f"✅ Max slotova updateano: **{old}** → **{MAX_SLOTS}**", ephemeral=True)


@bot.tree.command(name="set_priority_role", description="Postavlja rol koji ima prednost — izbacuje zadnjeg bez njega.")
@app_commands.checks.has_permissions(administrator=True)
async def set_priority_role(interaction: discord.Interaction, role: Optional[discord.Role] = None):
    global PRIORITY_ROLE_ID

    if role is None:
        if PRIORITY_ROLE_ID:
            guild = interaction.guild
            r = guild.get_role(PRIORITY_ROLE_ID)
            mention = r.mention if r else f"`{PRIORITY_ROLE_ID}` *(nije pronađen)*"
            await interaction.response.send_message(f"ℹ️ Trenutni priority rol: {mention}\nKorištenje: `/set_priority_role @Rol`", ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ Priority rol nije postavljen.\nKorištenje: `/set_priority_role @Rol`", ephemeral=True)
        return

    PRIORITY_ROLE_ID = role.id
    save_settings()
    await interaction.response.send_message(
        f"✅ Priority rol postavljen na **{role.name}**!\n"
        f"Kad je lista puna, korisnici s ovim rolom izbacuju zadnjeg bez njega. ⭐",
        ephemeral=True
    )


@bot.tree.command(name="clear_priority_role", description="Uklanja priority rol.")
@app_commands.checks.has_permissions(administrator=True)
async def clear_priority_role(interaction: discord.Interaction):
    global PRIORITY_ROLE_ID
    if not PRIORITY_ROLE_ID:
        await interaction.response.send_message("ℹ️ Priority rol već nije postavljen.", ephemeral=True)
        return
    PRIORITY_ROLE_ID = None
    save_settings()
    await interaction.response.send_message("✅ Priority rol uklonjen. Svi su ravnopravni.", ephemeral=True)


@bot.tree.command(name="blacklist_user", description="Dodaje korisnika na blacklistu — može se prijaviti ali ne može biti izvučen.")
@app_commands.checks.has_permissions(administrator=True)
async def blacklist_user(interaction: discord.Interaction, member: discord.Member):
    global BLACKLIST_USERS

    if member.id in BLACKLIST_USERS:
        await interaction.response.send_message(f"⚠️ **{member.display_name}** već je na blacklisti.", ephemeral=True)
        return

    BLACKLIST_USERS.add(member.id)
    await interaction.response.send_message(
        f"🚫 **{member.display_name}** dodan/a na blacklistu.\n"
        f"Može se prijaviti na listu, ali neće biti biran/a za Ammo Car.",
        ephemeral=True
    )


@bot.tree.command(name="unblacklist_user", description="Uklanja korisnika s blackliste.")
@app_commands.checks.has_permissions(administrator=True)
async def unblacklist_user(interaction: discord.Interaction, member: discord.Member):
    global BLACKLIST_USERS

    if member.id not in BLACKLIST_USERS:
        await interaction.response.send_message(f"⚠️ **{member.display_name}** nije na blacklisti.", ephemeral=True)
        return

    BLACKLIST_USERS.discard(member.id)
    await interaction.response.send_message(f"✅ **{member.display_name}** uklonjen/a s blackliste. Može biti biran/a za Ammo Car.", ephemeral=True)


@bot.tree.command(name="blacklist_list", description="Prikaži sve korisnike na blacklisti.")
@app_commands.checks.has_permissions(administrator=True)
async def blacklist_list(interaction: discord.Interaction):
    if not BLACKLIST_USERS:
        await interaction.response.send_message("✅ Blacklista je prazna — svi sudionici su prihvatljivi za izvlačenje.", ephemeral=True)
        return

    guild = interaction.guild
    lines = []
    for uid in BLACKLIST_USERS:
        member = guild.get_member(uid) if guild else None
        name = member.display_name if member else f"<@{uid}> *(nije na serveru)*"
        lines.append(f"• {name} (`{uid}`)")

    embed = discord.Embed(
        title="🚫 Blacklista — Ammo Car",
        description="\n".join(lines),
        color=0xAA0000
    )
    embed.set_footer(text=f"{len(BLACKLIST_USERS)} korisnik(a) na blacklisti.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="set_time", description="Mijenja minute starta i kraja eventa svaki sat.")
@app_commands.checks.has_permissions(administrator=True)
async def set_time(interaction: discord.Interaction, start: Optional[int] = None, end: Optional[int] = None):
    global START_MINUTE, END_MINUTE

    if start is None or end is None:
        await interaction.response.send_message(
            f"ℹ️ Trenutno: start :{str(START_MINUTE).zfill(2)} → end :{str(END_MINUTE).zfill(2)}\n"
            f"Korištenje: `/set_time start end` (npr. `/set_time 25 40`)\n"
            f"Oba broja moraju biti između 0 i 59.",
            ephemeral=True
        )
        return

    if not (0 <= start <= 59) or not (0 <= end <= 59):
        await interaction.response.send_message("❌ Minuta mora biti između 0 i 59.", ephemeral=True)
        return

    if start == end:
        await interaction.response.send_message("❌ Start i end ne mogu biti isti.", ephemeral=True)
        return

    if event_active:
        await interaction.response.send_message("⚠️ Ne možeš mijenjati vrijeme dok event traje. Koristi `/force_end` prvo.", ephemeral=True)
        return

    old_start, old_end = START_MINUTE, END_MINUTE
    START_MINUTE = start
    END_MINUTE = end
    save_settings()
    await interaction.response.send_message(
        f"✅ Vrijeme updateano!\n"
        f"**Start:** :{str(old_start).zfill(2)} → :{str(START_MINUTE).zfill(2)}\n"
        f"**End:** :{str(old_end).zfill(2)} → :{str(END_MINUTE).zfill(2)}\n"
        f"Svaki sat bot šalje u :{str(START_MINUTE).zfill(2)} i zaključava u :{str(END_MINUTE).zfill(2)}.",
        ephemeral=True
    )


@bot.tree.command(name="set_draw", description="Mijenja minutu automatskog izvlačenja.")
@app_commands.checks.has_permissions(administrator=True)
async def set_draw_time(interaction: discord.Interaction, minute: Optional[int] = None):
    global DRAW_MINUTE

    if minute is None:
        await interaction.response.send_message(
            f"ℹ️ Trenutno izvlačenje je u :{str(DRAW_MINUTE).zfill(2)}.\n"
            f"Korištenje: `/set_draw minuta` (npr. `/set_draw 35`)\n"
            f"Minuta mora biti između 0 i 59 i prije kraja (:{str(END_MINUTE).zfill(2)}).",
            ephemeral=True
        )
        return

    if not (0 <= minute <= 59):
        await interaction.response.send_message("❌ Minuta mora biti između 0 i 59.", ephemeral=True)
        return

    if minute >= END_MINUTE:
        await interaction.response.send_message(f"❌ Minuta izvlačenja mora biti prije kraja (:{str(END_MINUTE).zfill(2)}). Odaberi manju minutu.", ephemeral=True)
        return

    if minute <= START_MINUTE:
        await interaction.response.send_message(f"❌ Minuta izvlačenja mora biti nakon starta (:{str(START_MINUTE).zfill(2)}). Odaberi veću minutu.", ephemeral=True)
        return

    if event_active:
        await interaction.response.send_message("⚠️ Ne možeš mijenjati vrijeme izvlačenja dok event traje. Koristi `/force_end` prvo.", ephemeral=True)
        return

    old = DRAW_MINUTE
    DRAW_MINUTE = minute
    save_settings()
    await interaction.response.send_message(
        f"✅ Minuta izvlačenja updateana: :{str(old).zfill(2)} → :{str(DRAW_MINUTE).zfill(2)}\n"
        f"Raspored: start :{str(START_MINUTE).zfill(2)} → izvlačenje :{str(DRAW_MINUTE).zfill(2)} → kraj :{str(END_MINUTE).zfill(2)}",
        ephemeral=True
    )


@bot.tree.command(name="winner", description="Ponovo objavljuje zadnjeg pobjednika u event kanalu.")
@app_commands.checks.has_permissions(administrator=True)
async def winner_cmd(interaction: discord.Interaction):
    if last_winner_id is None:
        await interaction.response.send_message("❌ Nema zabilježenog pobjednika od kad je bot pokrenut.", ephemeral=True)
        return
    winner = bot.get_user(last_winner_id)
    winner_mention = winner.mention if winner else f"<@{last_winner_id}>"
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("❌ Event kanal nije pronađen.", ephemeral=True)
        return
    await channel.send(f"🏆 **Podsjetnik — zadnji pobjednik Ammo Cara:** {winner_mention} 🚗💨")
    await interaction.response.send_message(f"✅ Pobjednik ponovo objavljen: {winner_mention}", ephemeral=True)


@bot.tree.command(name="clearwinner", description="Resetira zabilježenog pobjednika.")
@app_commands.checks.has_permissions(administrator=True)
async def clearwinner(interaction: discord.Interaction):
    global last_winner_id
    if last_winner_id is None:
        await interaction.response.send_message("ℹ️ Nema zabilježenog pobjednika — već je čisto.", ephemeral=True)
        return
    last_winner_id = None
    await interaction.response.send_message("✅ Zadnji pobjednik resetiran.", ephemeral=True)


@bot.tree.command(name="history", description="Prikazuje zadnjih 5 pobjednika.")
@app_commands.checks.has_permissions(administrator=True)
async def history(interaction: discord.Interaction):
    if not winner_history:
        await interaction.response.send_message("ℹ️ Nema zabilježenih pobjednika od kad je bot pokrenut.", ephemeral=True)
        return
    embed = discord.Embed(title="🏆 Zadnjih 5 pobjednika", color=0xFF5500)
    lines = []
    for i, entry in enumerate(reversed(winner_history), 1):
        user = bot.get_user(entry["id"])
        name = user.display_name if user else f"ID {entry['id']}"
        lines.append(f"`{i}.` **{name}** — {entry['time']}")
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="setup", description="Prikazuje trenutnu konfiguraciju bota.")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    channel = bot.get_channel(CHANNEL_ID)
    channel_val = channel.mention if channel else "❌ Nije postavljen — koristi `/set_channel #kanal`"

    if PRIORITY_ROLE_ID:
        priority_role = interaction.guild.get_role(PRIORITY_ROLE_ID)
        priority_val = priority_role.mention if priority_role else f"<@&{PRIORITY_ROLE_ID}>"
    else:
        priority_val = "*nije postavljen*"

    embed = discord.Embed(
        title="⚙️ Ammo Car Bot — Konfiguracija",
        color=0xFF5500
    )
    embed.add_field(name="📡 Event kanal", value=channel_val, inline=False)
    embed.add_field(name="⏰ Raspored", value=(
        f"Start: `:{str(START_MINUTE).zfill(2)}`\n"
        f"Izvlačenje: `:{str(DRAW_MINUTE).zfill(2)}`\n"
        f"Kraj: `:{str(END_MINUTE).zfill(2)}`"
    ), inline=True)
    embed.add_field(name="👥 Max slotova", value=f"`{MAX_SLOTS}`", inline=True)
    embed.add_field(name="⭐ Priority rol", value=priority_val, inline=True)
    embed.add_field(name="🚛 Event aktivan", value="✅ Da" if event_active else "❌ Ne", inline=True)
    embed.add_field(name="🚫 Blacklista", value=f"`{len(BLACKLIST_USERS)}` korisnika", inline=True)
    embed.add_field(name="🔨 Banirani", value=f"`{len(BAN_USERS)}` korisnika", inline=True)
    embed.set_footer(text="Koristi /helpinf za listu svih komandi.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Pokazuje stanje eventa — koliko je ljudi ušlo i kada kreće sljedeći.")
@app_commands.checks.has_permissions(administrator=True)
async def status(interaction: discord.Interaction):
    now = datetime.now(TIMEZONE)
    minute = now.minute

    if not event_active:
        mins_until = (START_MINUTE - minute) % 60
        desc = (
            f"**📭 Nema aktivnog eventa**\n"
            f"Sljedeći auto-start za **{mins_until} minutu/minuta** (u :{str(START_MINUTE).zfill(2)})\n\n"
            f"Koristi `/force_start` za pokretanje odmah."
        )
        color = 0x888888
    else:
        mins_until_lock = (END_MINUTE - minute) % 60
        mins_until_draw = (DRAW_MINUTE - minute) % 60
        lock_status = "🔒 Zaključano" if join_button_locked else f"🔓 Otvoreno — zatvara se za **{mins_until_lock} min**"

        if current_participants:
            names = []
            for i, uid in enumerate(current_participants, start=1):
                user = bot.get_user(uid)
                name = user.display_name if user else f"<@{uid}>"
                names.append(f"{i}. {name}")
            participant_list = "\n".join(names)
        else:
            participant_list = "*Nitko još*"

        draw_info = f"Izvlačenje u :{str(DRAW_MINUTE).zfill(2)} (za **{mins_until_draw} min**)" if not join_button_locked else f"Izvlačenje završeno (:{str(DRAW_MINUTE).zfill(2)})"
        desc = (
            f"**🚛 Event AKTIVAN**\n"
            f"**Prijava:** {lock_status}\n"
            f"**🎲 Izvlačenje:** {draw_info}\n"
            f"**Sudionici:** {len(current_participants)}/{MAX_SLOTS}\n\n"
            f"{participant_list}"
        )
        color = 0xFF5500 if not join_button_locked else 0xAA2200

    embed = discord.Embed(title="📊 Ammo Car Event Status", description=desc, color=color)
    embed.set_footer(text=f"Provjereno u {now.strftime('%H:%M')} ({TIMEZONE})")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="helpinf", description="Prikazuje sve admin komande.")
@app_commands.checks.has_permissions(administrator=True)
async def help_command(interaction: discord.Interaction):
    priority_status = "nije postavljen"
    if PRIORITY_ROLE_ID:
        r = interaction.guild.get_role(PRIORITY_ROLE_ID)
        priority_status = r.name if r else f"ID {PRIORITY_ROLE_ID}"

    embed = discord.Embed(
        title="📋 Inf Lista — Admin Komande (Slash /)",
        color=0xFF5500
    )
    embed.add_field(name="/setup", value="Prikazuje trenutnu konfiguraciju — kanal, vremena, slotovi.", inline=False)
    embed.add_field(name="/force_start", value="Ručno pokreće event odmah.", inline=False)
    embed.add_field(name="/force_end", value="Zaustavlja trenutni event bez izvlačenja pobjednika.", inline=False)
    embed.add_field(name="/reroll", value="Bira novog pobjednika ako prvi ne može voziti — lista ostaje ista.", inline=False)
    embed.add_field(name="/winner", value="Ponovo objavljuje zadnjeg pobjednika u event kanalu.", inline=False)
    embed.add_field(name="/clearwinner", value="Resetira zabilježenog pobjednika.", inline=False)
    embed.add_field(name="/history", value="Prikazuje zadnjih 5 pobjednika s vremenima.", inline=False)
    embed.add_field(name="/remind", value="Ručno šalje podsjetnik u event kanal da lista uskoro počinje.", inline=False)
    embed.add_field(name="/ping", value="Provjeri radi li bot i kolika mu je latencija. *(svi mogu koristiti)*", inline=False)
    embed.add_field(name="/status", value="Pokazuje stanje eventa — koliko je ljudi ušlo i kada kreće sljedeći.", inline=False)
    embed.add_field(name="/add @korisnik", value="Dodaj korisnika na listu dok je event aktivan. *(svi mogu koristiti)*", inline=False)
    embed.add_field(name="/ban @korisnik", value="Zabranjuje korisniku ulaz na listu dok ga ne unbaniraš.", inline=False)
    embed.add_field(name="/unban @korisnik", value="Uklanja ban — korisnik može ponovo ući na listu.", inline=False)
    embed.add_field(name="/banlist", value="Prikaži sve trenutno banirane korisnike.", inline=False)
    embed.add_field(name="/set_time start end", value=f"Mijenja minute starta i kraja svaki sat.\nPrimjer: `/set_time 25 40`\nTrenutno: :{str(START_MINUTE).zfill(2)} → :{str(END_MINUTE).zfill(2)}", inline=False)
    embed.add_field(name="/set_draw minuta", value=f"Mijenja minutu automatskog izvlačenja.\nPrimjer: `/set_draw 35`\nTrenutno: :{str(DRAW_MINUTE).zfill(2)}", inline=False)
    embed.add_field(name="/set_slots broj", value=f"Mijenja max broj mjesta.\nPrimjer: `/set_slots 20`\nTrenutno: {MAX_SLOTS}", inline=False)
    embed.add_field(name="/set_channel #kanal", value="Mijenja kanal u koji bot šalje event.\nBez argumenta pokazuje trenutni kanal.", inline=False)
    embed.add_field(name="/kick_from_list @korisnik", value="Makni korisnika s liste dok je event aktivan.", inline=False)
    embed.add_field(name="/set_priority_role @Rol", value=f"Postavlja rol koji ima prednost.\nTrenutno: **{priority_status}**", inline=False)
    embed.add_field(name="/clear_priority_role", value="Uklanja priority rol.", inline=False)
    embed.add_field(name="/blacklist_user @korisnik", value="Dodaje na blacklistu — može se prijaviti ali ne može biti izvučen.", inline=False)
    embed.add_field(name="/unblacklist_user @korisnik", value="Uklanja s blackliste.", inline=False)
    embed.add_field(name="/blacklist_list", value="Prikaži sve na blacklisti.", inline=False)
    embed.set_footer(text="Sve komande su admin only (osim /ping i /add). Odgovori su vidljivi samo tebi.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==========================================
# BOT EVENTS
# ==========================================
@bot.event
async def on_ready():
    print(f"✅ LOGGED IN AS {bot.user} (ID: {bot.user.id})")
    if CHANNEL_ID == 0:
        print("⚠️  Kanal nije postavljen. Admin treba upisati /set_channel #kanal na serveru.")
    else:
        print(f"📡 CHANNEL TARGET: {CHANNEL_ID}")
        print(f"🚛 BOT AKTIVAN — sljedeći event u :{str(START_MINUTE).zfill(2)}")
    bot.add_view(JoinButtonView())
    event_scheduler.start()
    try:
        synced = await bot.tree.sync()
        print(f"✅ Slash komande syncirane: {len(synced)} komanda(i)")
    except Exception as e:
        print(f"❌ Greška pri synciranju slash komandi: {e}")


@bot.event
async def on_guild_join(guild):
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            embed = discord.Embed(
                title="🚛 Ammo Car Bot — Setup",
                description=(
                    "Hvala što si me dodao/la! Koristi sljedeće admin komande za postavljanje:\n\n"
                    f"**1.** `/set_channel #kanal` — odaberi kanal za events\n"
                    f"**2.** `/set_time 25 40` — postavi minute starta i kraja\n"
                    f"**3.** `/set_draw 35` — postavi minutu izvlačenja\n\n"
                    f"Nakon toga bot automatski pokreće event svaki sat.\n"
                    f"Upiši `/helpinf` za sve komande."
                ),
                color=0xFF5500
            )
            await channel.send(embed=embed)
            break


# ==========================================
# RUN THE BOT
# ==========================================
if __name__ == "__main__":
    if not TOKEN:
        print("❌ ERROR: DISCORD_TOKEN secret is not set!")
    else:
        bot.run(TOKEN)
