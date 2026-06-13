import discord
from discord.ext import tasks, commands
import asyncio
import random
from datetime import datetime
import pytz
import os

# ==========================================
# CONFIGURATION — loaded from environment secrets
# ==========================================
TOKEN = os.environ.get('DISCORD_TOKEN', '')
CHANNEL_ID = int(os.environ.get('CHANNEL_ID', '0'))

# Discord user IDs that are never eligible to be selected as Ammo Car driver.
# Users can still join the list — they just won't be picked in the draw.
# Format: integer user IDs. Add more with !blacklist_user or hardcode them here.
BLACKLIST_USERS = set()
# To hardcode users, add their integer IDs:
# BLACKLIST_USERS = {123456789012345678, 987654321098765432}

BAN_USERS = set()

# Timezone (Croatia = Europe/Zagreb, or UTC)
TIMEZONE = pytz.timezone('Europe/Zagreb')

# ==========================================
# INTERNAL STATE
# ==========================================
MAX_SLOTS = 10
START_MINUTE = 25
DRAW_MINUTE = 35
END_MINUTE = 40
PRIORITY_ROLE_ID = None
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
            name = member.display_name if member else f"Unknown"
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


async def private_reply(ctx, content=None, embed=None):
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass
    try:
        if embed:
            await ctx.author.send(embed=embed)
        else:
            await ctx.author.send(content)
    except discord.Forbidden:
        if embed:
            await ctx.send(embed=embed)
        else:
            await ctx.send(content)


# ==========================================
# SCHEDULER: RUNS EVERY MINUTE
# ==========================================
@tasks.loop(minutes=1)
async def event_scheduler():
    global event_active, join_button_locked, current_participants, current_event_message

    now = datetime.now(TIMEZONE)
    minute = now.minute

    # REMINDER 5 MINUTES BEFORE START
    reminder_minute = (START_MINUTE - 5) % 60
    if minute == reminder_minute and not event_active:
        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            await channel.send("⏳ **INF - lista pocinje za 5 minuta.**")

    # START AT CONFIGURED MINUTE
    if minute == START_MINUTE and not event_active:
        event_active = True
        join_button_locked = False
        current_participants = []

        channel = bot.get_channel(CHANNEL_ID)
        if not channel:
            print(f"❌ Channel {CHANNEL_ID} not found! Check ID and bot permissions.")
            return

        embed = build_embed()
        view = JoinButtonView()
        msg = await channel.send(embed=embed, view=view)
        current_event_message = msg
        await channel.send("@everyone 🚨 INF lista je pocela! Prvih 10 ulazi, bira se ko vozi AMMO CAR! 🚛")
        print(f"✅ Event started at {now.strftime('%H:%M')}")

    # DRAW AT DRAW_MINUTE (lista ostaje otvorena)
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
                winner = bot.get_user(winner_id)
                winner_mention = winner.mention if winner else f"<@{winner_id}>"
                await channel.send(f"🎲 **IZVLAČENJE!** Pobjednik je... {winner_mention} 🎉\n🚗💨 **Ammo car vozi {winner_mention}!** 🚗💨")

        print(f"🎲 Draw done at {now.strftime('%H:%M')}")

    # LOCK & CLOSE EVENT AT END_MINUTE
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
# ADMIN COMMANDS
# ==========================================
@bot.command(name="force_start")
@commands.has_permissions(administrator=True)
async def force_start(ctx):
    global event_active, join_button_locked, current_participants, current_event_message

    if event_active:
        await private_reply(ctx, "⚠️ Event already running! Wait for it to finish.")
        return

    event_active = True
    join_button_locked = False
    current_participants = []

    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass

    embed = build_embed()
    view = JoinButtonView()
    msg = await ctx.channel.send(embed=embed, view=view)
    current_event_message = msg
    await ctx.channel.send(f"@everyone 🚨 **Inf lista je pocela imate do :{str(END_MINUTE).zfill(2)} da udete i pobjednik vozi ammo!**")

    await asyncio.sleep(900)
    if event_active:
        join_button_locked = True
        await update_message()
        if len(current_participants) == 0:
            await ctx.channel.send("😢 No one joined. Event cancelled.")
        else:
            eligible = [uid for uid in current_participants if uid not in BLACKLIST_USERS]
            if not eligible:
                await ctx.channel.send("⚠️ **No eligible participants** — all are on the blacklist.")
            else:
                winner_id = random.choice(eligible)
                winner = bot.get_user(winner_id)
                winner_mention = winner.mention if winner else f"<@{winner_id}>"
                await ctx.channel.send(f"🎉 **WINNER:** {winner_mention} drives the Ammo Car! 🚛")
        event_active = False
        current_participants = []


@bot.command(name="force_end")
@commands.has_permissions(administrator=True)
async def force_end(ctx):
    global event_active, current_participants, current_event_message
    if not event_active:
        await private_reply(ctx, "❌ Nema aktivnog eventa.")
        return
    event_active = False
    current_participants = []
    if current_event_message:
        old_view = discord.ui.View.from_message(current_event_message)
        for child in old_view.children:
            child.disabled = True
        await current_event_message.edit(view=old_view)
        current_event_message = None
    await private_reply(ctx, "⏹️ Event force-stopped.")


@bot.command(name="ping")
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await private_reply(ctx, f"🏓 Pong! Latencija: **{latency}ms**")


@bot.command(name="remind")
@commands.has_permissions(administrator=True)
async def remind(ctx):
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        await private_reply(ctx, "❌ Event kanal nije pronađen.")
        return
    await channel.send(f"⏳ **INF lista počinje za malo — :{str(START_MINUTE).zfill(2)}! Budite spremni! 🚛**")
    await private_reply(ctx, "✅ Podsjetnik poslan.")


@bot.command(name="reroll")
@commands.has_permissions(administrator=True)
async def reroll(ctx):
    if len(current_participants) == 0:
        await private_reply(ctx, "😢 **Lista je prazna. Nema koga birati!**")
        return

    eligible = [uid for uid in current_participants if uid not in BLACKLIST_USERS]
    if not eligible:
        await private_reply(ctx, "⚠️ **Nitko nije prihvatljiv za reroll.** Svi su na blacklisti.")
        return

    winner_id = random.choice(eligible)
    winner = bot.get_user(winner_id)
    winner_mention = winner.mention if winner else f"<@{winner_id}>"
    channel = bot.get_channel(CHANNEL_ID)
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass
    if channel:
        await channel.send(f"🔁 **REROLL!** Novi vozač Ammo Cara je... {winner_mention} 🎉🚗💨")


@bot.command(name="add")
async def add_to_list(ctx, member: discord.Member = None):
    global current_participants, join_button_locked

    if not event_active:
        await private_reply(ctx, "❌ Nema aktivnog eventa.")
        return

    if join_button_locked:
        await private_reply(ctx, "🔒 Lista je zaključana.")
        return

    if member is None:
        await private_reply(ctx, "❌ Navedi korisnika. Primjer: `!add @korisnik`")
        return

    if member.id in BAN_USERS:
        await private_reply(ctx, f"🚫 **{member.display_name}** je baniran/a i ne može ući na listu.")
        return

    if member.id in current_participants:
        await private_reply(ctx, f"⚠️ **{member.display_name}** već je na listi.")
        return

    guild = ctx.guild
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
                await private_reply(ctx, "❌ Lista je puna i svi imaju priority rol. Nema mjesta.")
                return

            current_participants.remove(bumped_uid)
            current_participants.append(member.id)
            bumped_member = guild.get_member(bumped_uid) if guild else None
            bumped_name = bumped_member.display_name if bumped_member else f"<@{bumped_uid}>"
            await private_reply(ctx, f"⭐ **{member.display_name}** dodan priority rolom! **{bumped_name}** je izbačen/a.")
            await update_message()
        else:
            await private_reply(ctx, f"❌ Lista je puna ({MAX_SLOTS}/{MAX_SLOTS}).")
        return

    current_participants.append(member.id)
    prefix = "⭐ " if has_priority else ""
    await private_reply(ctx, f"✅ **{prefix}{member.display_name}** dodan/a na listu! ({len(current_participants)}/{MAX_SLOTS})")
    await update_message()


@bot.command(name="kick_from_list")
@commands.has_permissions(administrator=True)
async def kick_from_list(ctx, member: discord.Member = None):
    if not event_active:
        await private_reply(ctx, "❌ Nema aktivnog eventa.")
        return

    if member is None:
        await private_reply(ctx, "❌ Navedi korisnika. Primjer: `!kick_from_list @korisnik`")
        return

    if member.id not in current_participants:
        await private_reply(ctx, f"⚠️ **{member.display_name}** nije na listi.")
        return

    current_participants.remove(member.id)
    await private_reply(ctx, f"✅ **{member.display_name}** je maknut/a s liste.")
    await update_message()


@bot.command(name="ban")
@commands.has_permissions(administrator=True)
async def ban_user(ctx, member: discord.Member = None):
    global BAN_USERS

    if member is None:
        await private_reply(ctx, "❌ Navedi korisnika. Primjer: `!ban @korisnik`")
        return

    if member.id in BAN_USERS:
        await private_reply(ctx, f"⚠️ **{member.display_name}** već je baniran/a.")
        return

    BAN_USERS.add(member.id)
    if member.id in current_participants:
        current_participants.remove(member.id)
        await update_message()
        await private_reply(ctx, f"🔨 **{member.display_name}** je baniran/a i maknut/a s liste.")
    else:
        await private_reply(ctx, f"🔨 **{member.display_name}** je baniran/a — ne može ući na listu.")


@bot.command(name="unban")
@commands.has_permissions(administrator=True)
async def unban_user(ctx, member: discord.Member = None):
    global BAN_USERS

    if member is None:
        await private_reply(ctx, "❌ Navedi korisnika. Primjer: `!unban @korisnik`")
        return

    if member.id not in BAN_USERS:
        await private_reply(ctx, f"⚠️ **{member.display_name}** nije baniran/a.")
        return

    BAN_USERS.discard(member.id)
    await private_reply(ctx, f"✅ **{member.display_name}** je unbaniran/a — može ponovo ući na listu.")


@bot.command(name="banlist")
@commands.has_permissions(administrator=True)
async def banlist(ctx):
    if not BAN_USERS:
        await private_reply(ctx, "✅ Nema banirani korisnika.")
        return

    guild = ctx.guild
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
    await private_reply(ctx, embed=embed)


@bot.command(name="set_channel")
@commands.has_permissions(administrator=True)
async def set_channel(ctx, channel: discord.TextChannel = None):
    global CHANNEL_ID

    if channel is None:
        current = bot.get_channel(CHANNEL_ID)
        current_mention = current.mention if current else f"`{CHANNEL_ID}` *(not found)*"
        await private_reply(ctx, f"ℹ️ Current event channel: {current_mention}\nUsage: `!set_channel #channel-name`")
        return

    if event_active:
        await private_reply(ctx, "⚠️ Can't change channel while an event is running. Use `!force_end` first.")
        return

    old_id = CHANNEL_ID
    CHANNEL_ID = channel.id
    old_channel = bot.get_channel(old_id)
    old_mention = old_channel.mention if old_channel else f"`{old_id}`"
    await private_reply(ctx, f"✅ Event channel updated: {old_mention} → {channel.mention}\n⚠️ **Note:** This change is temporary and will reset on bot restart. Update `CHANNEL_ID` in your Replit secrets to make it permanent.")


@bot.command(name="set_slots")
@commands.has_permissions(administrator=True)
async def set_slots(ctx, number: int = None):
    global MAX_SLOTS

    if number is None:
        await private_reply(ctx, f"ℹ️ Current max slots: **{MAX_SLOTS}**\nUsage: `!set_slots <number>` (e.g. `!set_slots 20`)")
        return

    if number < 1 or number > 100:
        await private_reply(ctx, "❌ Number must be between 1 and 100.")
        return

    if event_active:
        await private_reply(ctx, "⚠️ Can't change slots while an event is running. Use `!force_end` first.")
        return

    old = MAX_SLOTS
    MAX_SLOTS = number
    await private_reply(ctx, f"✅ Max slots updated: **{old}** → **{MAX_SLOTS}**")


@bot.command(name="set_priority_role")
@commands.has_permissions(administrator=True)
async def set_priority_role(ctx, role: discord.Role = None):
    global PRIORITY_ROLE_ID

    if role is None:
        if PRIORITY_ROLE_ID:
            guild = ctx.guild
            r = guild.get_role(PRIORITY_ROLE_ID)
            mention = r.mention if r else f"`{PRIORITY_ROLE_ID}` *(nije pronađen)*"
            await private_reply(ctx, f"ℹ️ Trenutni priority rol: {mention}\nKorištenje: `!set_priority_role @Rol`")
        else:
            await private_reply(ctx, "ℹ️ Priority rol nije postavljen.\nKorištenje: `!set_priority_role @Rol`")
        return

    PRIORITY_ROLE_ID = role.id
    await private_reply(ctx,
        f"✅ Priority rol postavljen na **{role.name}**!\n"
        f"Kad je lista puna, korisnici s ovim rolom izbacuju zadnjeg bez njega. ⭐"
    )


@bot.command(name="clear_priority_role")
@commands.has_permissions(administrator=True)
async def clear_priority_role(ctx):
    global PRIORITY_ROLE_ID
    if not PRIORITY_ROLE_ID:
        await private_reply(ctx, "ℹ️ Priority rol već nije postavljen.")
        return
    PRIORITY_ROLE_ID = None
    await private_reply(ctx, "✅ Priority rol uklonjen. Svi su ravnopravni.")


@bot.command(name="blacklist_user")
@commands.has_permissions(administrator=True)
async def blacklist_user(ctx, member: discord.Member = None):
    global BLACKLIST_USERS

    if member is None:
        await private_reply(ctx, "❌ Navedi korisnika. Primjer: `!blacklist_user @korisnik`")
        return

    if member.id in BLACKLIST_USERS:
        await private_reply(ctx, f"⚠️ **{member.display_name}** već je na blacklisti.")
        return

    BLACKLIST_USERS.add(member.id)
    await private_reply(ctx,
        f"🚫 **{member.display_name}** dodan/a na blacklistu.\n"
        f"Može se prijaviti na listu, ali neće biti biran/a za Ammo Car."
    )


@bot.command(name="unblacklist_user")
@commands.has_permissions(administrator=True)
async def unblacklist_user(ctx, member: discord.Member = None):
    global BLACKLIST_USERS

    if member is None:
        await private_reply(ctx, "❌ Navedi korisnika. Primjer: `!unblacklist_user @korisnik`")
        return

    if member.id not in BLACKLIST_USERS:
        await private_reply(ctx, f"⚠️ **{member.display_name}** nije na blacklisti.")
        return

    BLACKLIST_USERS.discard(member.id)
    await private_reply(ctx, f"✅ **{member.display_name}** uklonjen/a s blackliste. Može biti biran/a za Ammo Car.")


@bot.command(name="blacklist_list")
@commands.has_permissions(administrator=True)
async def blacklist_list(ctx):
    if not BLACKLIST_USERS:
        await private_reply(ctx, "✅ Blacklista je prazna — svi sudionici su prihvatljivi za izvlačenje.")
        return

    guild = ctx.guild
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
    await private_reply(ctx, embed=embed)


@bot.command(name="helpinf")
@commands.has_permissions(administrator=True)
async def help_command(ctx):
    priority_status = "nije postavljen"
    if PRIORITY_ROLE_ID:
        r = ctx.guild.get_role(PRIORITY_ROLE_ID)
        priority_status = r.name if r else f"ID {PRIORITY_ROLE_ID}"

    embed = discord.Embed(
        title="📋 Inf Lista — Admin Komande",
        color=0xFF5500
    )
    embed.add_field(name="!force_start", value="Ručno pokreće event odmah.", inline=False)
    embed.add_field(name="!force_end", value="Zaustavlja trenutni event bez izvlačenja pobjednika.", inline=False)
    embed.add_field(name="!reroll", value="Bira novog pobjednika ako prvi ne može voziti — lista ostaje ista.", inline=False)
    embed.add_field(name="!remind", value="Ručno šalje podsjetnik u event kanal da lista uskoro počinje.", inline=False)
    embed.add_field(name="!ping", value="Provjeri radi li bot i kolika mu je latencija. *(svi mogu koristiti)*", inline=False)
    embed.add_field(name="!status", value="Pokazuje stanje eventa — koliko je ljudi ušlo i kada kreće sljedeći.", inline=False)
    embed.add_field(name="!add @korisnik", value="Dodaj korisnika na listu dok je event aktivan. *(svi mogu koristiti)*", inline=False)
    embed.add_field(name="!ban @korisnik", value="Zabranjuje korisniku ulaz na listu dok ga ne unbaniraš.", inline=False)
    embed.add_field(name="!unban @korisnik", value="Uklanja ban — korisnik može ponovo ući na listu.", inline=False)
    embed.add_field(name="!banlist", value="Prikaži sve trenutno banirane korisnike.", inline=False)
    embed.add_field(name="!set_time <start> <end>", value=f"Mijenja minute starta i kraja svaki sat.\nPrimjer: `!set_time 25 40`\nTrenutno: :{str(START_MINUTE).zfill(2)} → :{str(END_MINUTE).zfill(2)}", inline=False)
    embed.add_field(name="!set_draw <minuta>", value=f"Mijenja minutu automatskog izvlačenja.\nPrimjer: `!set_draw 35`\nTrenutno: :{str(DRAW_MINUTE).zfill(2)}", inline=False)
    embed.add_field(name="!set_slots <broj>", value=f"Mijenja max broj mjesta.\nPrimjer: `!set_slots 20`\nTrenutno: {MAX_SLOTS}", inline=False)
    embed.add_field(name="!set_channel #kanal", value="Mijenja kanal u koji bot šalje event.\nBez argumenta pokazuje trenutni kanal.", inline=False)
    embed.add_field(name="!kick_from_list @korisnik", value="Makni korisnika s liste dok je event aktivan.", inline=False)
    embed.add_field(name="!set_priority_role @Rol", value=f"Postavlja rol koji ima prednost — izbacuje zadnjeg bez njega kad je lista puna.\nTrenutno: **{priority_status}**", inline=False)
    embed.add_field(name="!clear_priority_role", value="Uklanja priority rol.", inline=False)
    embed.set_footer(text="Sve komande su admin only. Odgovori su vidljivi samo tebi.")
    await private_reply(ctx, embed=embed)


@bot.command(name="set_time")
@commands.has_permissions(administrator=True)
async def set_time(ctx, start: int = None, end: int = None):
    global START_MINUTE, END_MINUTE

    if start is None or end is None:
        await private_reply(ctx,
            f"ℹ️ Trenutno: start :{str(START_MINUTE).zfill(2)} → end :{str(END_MINUTE).zfill(2)}\n"
            f"Korištenje: `!set_time <start> <end>` (npr. `!set_time 25 40`)\n"
            f"Oba broja moraju biti između 0 i 59."
        )
        return

    if not (0 <= start <= 59) or not (0 <= end <= 59):
        await private_reply(ctx, "❌ Minuta mora biti između 0 i 59.")
        return

    if start == end:
        await private_reply(ctx, "❌ Start i end ne mogu biti isti.")
        return

    if event_active:
        await private_reply(ctx, "⚠️ Ne možeš mijenjati vrijeme dok event traje. Koristi `!force_end` prvo.")
        return

    old_start, old_end = START_MINUTE, END_MINUTE
    START_MINUTE = start
    END_MINUTE = end
    await private_reply(ctx,
        f"✅ Vrijeme updateano!\n"
        f"**Start:** :{str(old_start).zfill(2)} → :{str(START_MINUTE).zfill(2)}\n"
        f"**End:** :{str(old_end).zfill(2)} → :{str(END_MINUTE).zfill(2)}\n"
        f"Svaki sat bot šalje u :{str(START_MINUTE).zfill(2)} i zaključava u :{str(END_MINUTE).zfill(2)}."
    )


@bot.command(name="set_draw_time", aliases=["set_draw"])
@commands.has_permissions(administrator=True)
async def set_draw_time(ctx, minute: int = None):
    global DRAW_MINUTE

    if minute is None:
        await private_reply(ctx,
            f"ℹ️ Trenutno izvlačenje je u :{str(DRAW_MINUTE).zfill(2)}.\n"
            f"Korištenje: `!set_draw <minuta>` ili `!set_draw_time <minuta>` (npr. `!set_draw 35`)\n"
            f"Minuta mora biti između 0 i 59 i prije kraja (:{str(END_MINUTE).zfill(2)})."
        )
        return

    if not (0 <= minute <= 59):
        await private_reply(ctx, "❌ Minuta mora biti između 0 i 59.")
        return

    if minute >= END_MINUTE:
        await private_reply(ctx, f"❌ Minuta izvlačenja mora biti prije kraja (:{str(END_MINUTE).zfill(2)}). Odaberi manju minutu.")
        return

    if minute <= START_MINUTE:
        await private_reply(ctx, f"❌ Minuta izvlačenja mora biti nakon starta (:{str(START_MINUTE).zfill(2)}). Odaberi veću minutu.")
        return

    if event_active:
        await private_reply(ctx, "⚠️ Ne možeš mijenjati vrijeme izvlačenja dok event traje. Koristi `!force_end` prvo.")
        return

    old = DRAW_MINUTE
    DRAW_MINUTE = minute
    await private_reply(ctx,
        f"✅ Minuta izvlačenja updateana: :{str(old).zfill(2)} → :{str(DRAW_MINUTE).zfill(2)}\n"
        f"Raspored: start :{str(START_MINUTE).zfill(2)} → izvlačenje :{str(DRAW_MINUTE).zfill(2)} → kraj :{str(END_MINUTE).zfill(2)}"
    )


@bot.command(name="status")
@commands.has_permissions(administrator=True)
async def status(ctx):
    now = datetime.now(TIMEZONE)
    minute = now.minute

    if not event_active:
        mins_until = (START_MINUTE - minute) % 60
        desc = (
            f"**📭 No event running**\n"
            f"Next auto-start in **{mins_until} minute(s)** (at :{str(START_MINUTE).zfill(2)})\n\n"
            f"Use `!force_start` to start one now."
        )
        color = 0x888888
    else:
        mins_until_lock = (END_MINUTE - minute) % 60
        mins_until_draw = (DRAW_MINUTE - minute) % 60
        lock_status = "🔒 Locked" if join_button_locked else f"🔓 Open — closes in **{mins_until_lock} min**"

        if current_participants:
            names = []
            for i, uid in enumerate(current_participants, start=1):
                user = bot.get_user(uid)
                name = user.display_name if user else f"<@{uid}>"
                names.append(f"{i}. {name}")
            participant_list = "\n".join(names)
        else:
            participant_list = "*No one yet*"

        draw_info = f"Draw at :{str(DRAW_MINUTE).zfill(2)} (in **{mins_until_draw} min**)" if not join_button_locked else f"Draw already done (:{str(DRAW_MINUTE).zfill(2)})"
        desc = (
            f"**🚛 Event is ACTIVE**\n"
            f"**Join window:** {lock_status}\n"
            f"**🎲 Draw:** {draw_info}\n"
            f"**Participants:** {len(current_participants)}/{MAX_SLOTS}\n\n"
            f"{participant_list}"
        )
        color = 0xFF5500 if not join_button_locked else 0xAA2200

    embed = discord.Embed(title="📊 Ammo Car Event Status", description=desc, color=color)
    embed.set_footer(text=f"Checked at {now.strftime('%H:%M')} ({TIMEZONE})")
    await private_reply(ctx, embed=embed)


@bot.event
async def on_ready():
    print(f"✅ LOGGED IN AS {bot.user} (ID: {bot.user.id})")
    print(f"📡 CHANNEL TARGET: {CHANNEL_ID}")
    print(f"🚛 AMMO CAR BOT IS RUNNING — WILL START NEXT :{str(START_MINUTE).zfill(2)}")
    bot.add_view(JoinButtonView())
    event_scheduler.start()


# ==========================================
# RUN THE BOT
# ==========================================
if __name__ == "__main__":
    if not TOKEN:
        print("❌ ERROR: DISCORD_TOKEN secret is not set!")
    elif CHANNEL_ID == 0:
        print("❌ ERROR: CHANNEL_ID environment variable is not set!")
    else:
        bot.run(TOKEN)
