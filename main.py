import os
import re
import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
import discord
from discord.ext import commands
from discord import app_commands

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

GUILD_ID = 1502696059007139991
QUEUE_CHANNEL_ID = 1503007782247469187
PING_USER_ID = 1431956941315510437

FREE_ROLE = 1502752079578665151
PREMIUM_ROLE = 1502752173149388951
MEGA_ROLE = 1502752231819182080
EXTREME_ROLE = 1502752322147586138

EMBED_COLOR = 0x7B14BB

PANEL_IMAGE = "https://cdn.discordapp.com/attachments/1432016228049752135/1502927683560931408/file_00000000d3807246aa64c785bc8dd663.png"

EMOJI_SUCCESS = "<a:emoji_12:1503025818153124021>"
EMOJI_ERROR = "<a:X_:1503025721843646494>"

PLAN_DATA = {
    "Extreme": {
        "role": EXTREME_ROLE,
        "followers": 3000,
        "cooldown": 3600
    },
    "Mega": {
        "role": MEGA_ROLE,
        "followers": 1250,
        "cooldown": 2700
    },
    "Premium": {
        "role": PREMIUM_ROLE,
        "followers": 750,
        "cooldown": 1800
    },
    "Free": {
        "role": FREE_ROLE,
        "followers": 250,
        "cooldown": 7200
    }
}

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.db = None


def normalize_twitch(input_text: str):
    input_text = input_text.strip().lower()

    patterns = [
        r"https?://(?:www\.)?twitch\.tv/([a-zA-Z0-9_]+)",
        r"twitch\.tv/([a-zA-Z0-9_]+)",
        r"^([a-zA-Z0-9_]+)$"
    ]

    for pattern in patterns:
        match = re.search(pattern, input_text)
        if match:
            return match.group(1)

    return None


def format_time(seconds: int):
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours > 0:
        return f"{hours}h {minutes}m {sec}s"

    return f"{minutes}m {sec}s"


def get_user_plan(member: discord.Member):
    for plan_name, data in PLAN_DATA.items():
        role = member.get_role(data["role"])
        if role:
            return {
                "name": plan_name,
                "followers": data["followers"],
                "cooldown": data["cooldown"]
            }

    return None


async def create_tables():
    await bot.db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            twitch_username TEXT NOT NULL,
            plan TEXT NOT NULL,
            followers INTEGER NOT NULL,
            status TEXT NOT NULL,
            claimed_by BIGINT,
            created_at TIMESTAMP NOT NULL,
            completed_at TIMESTAMP
        )
    """)

    await bot.db.execute("""
        CREATE TABLE IF NOT EXISTS cooldowns (
            user_id BIGINT PRIMARY KEY,
            expires_at TIMESTAMP NOT NULL
        )
    """)


class FollowModal(discord.ui.Modal, title="Twitch Follow Request"):
    twitch_input = discord.ui.TextInput(
        label="Twitch username or URL",
        placeholder="ninja or twitch.tv/ninja",
        required=True,
        max_length=100
    )

    async def on_submit(self, interaction: discord.Interaction):
        member = interaction.user

        plan = get_user_plan(member)

        if not plan:
            embed = discord.Embed(
                description=f"{EMOJI_ERROR} You do not have a valid plan role.",
                color=EMBED_COLOR
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        twitch_username = normalize_twitch(str(self.twitch_input))

        if not twitch_username:
            embed = discord.Embed(
                description=f"{EMOJI_ERROR} Invalid Twitch username or URL.",
                color=EMBED_COLOR
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        active_order = await bot.db.fetchrow(
            "SELECT * FROM orders WHERE user_id = $1 AND status IN ('Pending', 'Claimed')",
            member.id
        )

        if active_order:
            embed = discord.Embed(
                description=f"{EMOJI_ERROR} You already have an active order.",
                color=EMBED_COLOR
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        duplicate_order = await bot.db.fetchrow(
            "SELECT * FROM orders WHERE twitch_username = $1 AND status IN ('Pending', 'Claimed')",
            twitch_username
        )

        if duplicate_order:
            embed = discord.Embed(
                description=f"{EMOJI_ERROR} This Twitch username is already in queue.",
                color=EMBED_COLOR
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        cooldown_data = await bot.db.fetchrow(
            "SELECT * FROM cooldowns WHERE user_id = $1",
            member.id
        )

        now = datetime.now(timezone.utc)

        if cooldown_data:
            expires_at = cooldown_data["expires_at"].replace(tzinfo=timezone.utc)

            if expires_at > now:
                remaining = int((expires_at - now).total_seconds())

                embed = discord.Embed(
                    description=f"{EMOJI_ERROR} Cooldown active.\n\nTry again in: `{format_time(remaining)}`",
                    color=EMBED_COLOR
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)

        expires_at = now + timedelta(seconds=plan["cooldown"])

        await bot.db.execute(
            """
            INSERT INTO cooldowns (user_id, expires_at)
            VALUES ($1, $2)
            ON CONFLICT (user_id)
            DO UPDATE SET expires_at = EXCLUDED.expires_at
            """,
            member.id,
            expires_at
        )

        order = await bot.db.fetchrow(
            """
            INSERT INTO orders (
                user_id,
                twitch_username,
                plan,
                followers,
                status,
                created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING order_id
            """,
            member.id,
            twitch_username,
            plan["name"],
            plan["followers"],
            "Pending",
            now
        )

        order_id = order["order_id"]

        queue_channel = bot.get_channel(QUEUE_CHANNEL_ID)

        queue_embed = discord.Embed(
            title=f"Order #{order_id}",
            color=EMBED_COLOR,
            timestamp=discord.utils.utcnow()
        )

        queue_embed.add_field(name="User", value=member.mention, inline=False)
        queue_embed.add_field(name="Twitch", value=f"https://twitch.tv/{twitch_username}", inline=False)
        queue_embed.add_field(name="Plan", value=plan['name'], inline=True)
        queue_embed.add_field(name="Followers", value=str(plan['followers']), inline=True)
        queue_embed.add_field(name="Status", value="Pending", inline=True)

        view = StaffView(order_id)

        await queue_channel.send(
            content=f"<@{PING_USER_ID}>",
            embed=queue_embed,
            view=view
        )

        user_embed = discord.Embed(
            description=(
                f"{EMOJI_SUCCESS} Added to Queue\n\n"
                f"Sending `{plan['followers']}` followers to `twitch.tv/{twitch_username}`\n\n"
                f"Plan: `{plan['name']}`\n"
                f"Cooldown: `{format_time(plan['cooldown'])}`\n\n"
                f"It may take from 15 minutes to 24 hours depending on your plan."
            ),
            color=EMBED_COLOR
        )

        await interaction.response.send_message(embed=user_embed, ephemeral=True)

        try:
            dm_embed = discord.Embed(
                title="Your Order Was Added",
                description=(
                    f"Order ID: `#{order_id}`\n"
                    f"Twitch: `twitch.tv/{twitch_username}`\n"
                    f"Followers: `{plan['followers']}`\n"
                    f"Plan: `{plan['name']}`\n"
                    f"Status: `Pending`"
                ),
                color=EMBED_COLOR
            )

            await member.send(embed=dm_embed)
        except:
            pass


class FollowView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🥤 Follow",
        style=discord.ButtonStyle.primary,
        custom_id="follow_button"
    )
    async def follow_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FollowModal())


class StaffView(discord.ui.View):
    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = order_id

    @discord.ui.button(label="👤 Claim", style=discord.ButtonStyle.secondary, custom_id="claim_order")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await bot.db.execute(
            "UPDATE orders SET status = 'Claimed', claimed_by = $1 WHERE order_id = $2",
            interaction.user.id,
            self.order_id
        )

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.yellow()

        for field in embed.fields:
            if field.name == "Status":
                field.value = "Claimed"

        embed.set_footer(text=f"Claimed by {interaction.user}")

        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message("Claimed.", ephemeral=True)

    @discord.ui.button(label="✅ Complete", style=discord.ButtonStyle.success, custom_id="complete_order")
    async def complete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await bot.db.execute(
            "UPDATE orders SET status = 'Completed', completed_at = $1 WHERE order_id = $2",
            datetime.now(timezone.utc),
            self.order_id
        )

        order = await bot.db.fetchrow(
            "SELECT * FROM orders WHERE order_id = $1",
            self.order_id
        )

        user = bot.get_user(order["user_id"])

        if user:
            try:
                embed = discord.Embed(
                    description=f"{EMOJI_SUCCESS} Your order `#{self.order_id}` has been completed.",
                    color=discord.Color.green()
                )
                await user.send(embed=embed)
            except:
                pass

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()

        for field in embed.fields:
            if field.name == "Status":
                field.value = "Completed"

        await interaction.message.edit(embed=embed, view=None)
        await interaction.response.send_message("Completed.", ephemeral=True)

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger, custom_id="reject_order")
    async def reject_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await bot.db.execute(
            "UPDATE orders SET status = 'Rejected' WHERE order_id = $1",
            self.order_id
        )

        order = await bot.db.fetchrow(
            "SELECT * FROM orders WHERE order_id = $1",
            self.order_id
        )

        user = bot.get_user(order["user_id"])

        if user:
            try:
                embed = discord.Embed(
                    description=f"{EMOJI_ERROR} Your order `#{self.order_id}` was rejected.",
                    color=discord.Color.red()
                )
                await user.send(embed=embed)
            except:
                pass

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()

        for field in embed.fields:
            if field.name == "Status":
                field.value = "Rejected"

        await interaction.message.edit(embed=embed, view=None)
        await interaction.response.send_message("Rejected.", ephemeral=True)

    @discord.ui.button(label="🗑 Remove", style=discord.ButtonStyle.secondary, custom_id="remove_order")
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await bot.db.execute(
            "DELETE FROM orders WHERE order_id = $1",
            self.order_id
        )

        await interaction.message.delete()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    bot.db = await asyncpg.connect(DATABASE_URL)

    await create_tables()

    bot.add_view(FollowView())

    synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Synced {len(synced)} commands")


@bot.tree.command(name="twitch", description="Send the Twitch panel", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_permissions(administrator=True)
async def twitch(interaction: discord.Interaction):
    embed = discord.Embed(
        title="<:emoji_11:1503017304022323325> Welcome to Notix Followers",
        description=(
            "## <a:emoji_8:1502943943107678289> Boost your Twitch with follows in a few clicks.\n\n"
            "## Use the buttons below to get started.\n\n"
            "## <a:emoji_5:1502943868008665199> Support/Purchase\n"
            "Need help or want to upgrade? → https://discord.com/channels/1502696059007139991/1503007782247469187"
        ),
        color=EMBED_COLOR
    )

    embed.set_image(url=PANEL_IMAGE)

    await interaction.channel.send(embed=embed, view=FollowView())

    await interaction.response.send_message(
        f"{EMOJI_SUCCESS} Panel sent.",
        ephemeral=True
    )


@twitch.error
async def twitch_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            f"{EMOJI_ERROR} You need administrator permission.",
            ephemeral=True
        )


bot.run(TOKEN)
