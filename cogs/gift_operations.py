import discord
from discord.ext import commands
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import hashlib
import json
from datetime import datetime
import sqlite3
from discord.ext import tasks
import asyncio
import re
from .alliance_member_operations import AllianceSelectView
from .alliance import PaginatedChannelView
import os

class GiftOperations(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if hasattr(bot, 'conn'):
            self.conn = bot.conn
            self.cursor = self.conn.cursor()
        else:
            self.conn = sqlite3.connect('db/giftcode.sqlite')
            self.cursor = self.conn.cursor()
            
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS giftcodecontrol (
                alliance_id INTEGER PRIMARY KEY,
                status INTEGER DEFAULT 0
            )
        """)
        self.conn.commit()
        
        self.settings_conn = sqlite3.connect('db/settings.sqlite')
        self.settings_cursor = self.settings_conn.cursor()
        
        self.alliance_conn = sqlite3.connect('db/alliance.sqlite')
        self.alliance_cursor = self.alliance_conn.cursor()
        
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS giftcode_channel (
                alliance_id INTEGER,
                channel_id INTEGER,
                PRIMARY KEY (alliance_id)
            )
        """)
        self.conn.commit()
        
        self.wos_player_info_url = "https://wos-giftcode-api.centurygame.com/api/player"
        self.wos_giftcode_url = "https://wos-giftcode-api.centurygame.com/api/gift_code"
        self.wos_giftcode_redemption_url = "https://wos-giftcode.centurygame.com"
        self.wos_encrypt_key = "tB87#kPtkxqOS2"
        
        self.retry_config = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429],
            allowed_methods=["POST"]
        )

        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS giftcodecontrol (
                alliance_id INTEGER PRIMARY KEY,
                status INTEGER DEFAULT 0
            )
        """)
        self.conn.commit()

        self.log_directory = 'log'
        if not os.path.exists(self.log_directory):
            os.makedirs(self.log_directory)

    @commands.Cog.listener()
    async def on_ready(self):
        try:
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS giftcodecontrol (
                    alliance_id INTEGER PRIMARY KEY,
                    status INTEGER DEFAULT 0
                )
            """)
            self.conn.commit()
            
            if not self.check_channels_loop.is_running():
                self.check_channels_loop.start()
            
            
        except Exception as e:
            print(f"[ERROR] Failed to create gift code control table: {str(e)}")

    def encode_data(self, data):
        secret = self.wos_encrypt_key
        sorted_keys = sorted(data.keys())
        encoded_data = "&".join(
            [
                f"{key}={json.dumps(data[key]) if isinstance(data[key], dict) else data[key]}"
                for key in sorted_keys
            ]
        )
        sign = hashlib.md5(f"{encoded_data}{secret}".encode()).hexdigest()
        return {"sign": sign, **data}

    def get_stove_info_wos(self, player_id):
        session = requests.Session()
        session.mount("https://", HTTPAdapter(max_retries=self.retry_config))

        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/x-www-form-urlencoded",
            "origin": self.wos_giftcode_redemption_url,
        }

        data_to_encode = {
            "fid": f"{player_id}",
            "time": f"{int(datetime.now().timestamp())}",
        }
        data = self.encode_data(data_to_encode)

        response_stove_info = session.post(
            self.wos_player_info_url,
            headers=headers,
            data=data,
        )
        return session, response_stove_info

    async def claim_giftcode_rewards_wos(self, player_id, giftcode):
        try:
            if player_id != "244886619":
                self.cursor.execute("""
                    SELECT status FROM user_giftcodes 
                    WHERE fid = ? AND giftcode = ?
                """, (player_id, giftcode))
                
                existing_record = self.cursor.fetchone()
                if existing_record:
                    return existing_record[0]

            session, response_stove_info = self.get_stove_info_wos(player_id=player_id)
            
            if response_stove_info.json().get("msg") == "success":
                data_to_encode = {
                    "fid": f"{player_id}",
                    "cdk": giftcode,
                    "time": f"{int(datetime.now().timestamp())}",
                }
                data = self.encode_data(data_to_encode)

                response_giftcode = session.post(
                    self.wos_giftcode_url,
                    data=data,
                )
                
                response_json = response_giftcode.json()
                
                if response_json.get("msg") == "TIME ERROR." and response_json.get("err_code") == 40007:
                    status = "TIME_ERROR"
                elif response_json.get("msg") == "SUCCESS":
                    status = "SUCCESS"
                elif response_json.get("msg") == "RECEIVED." and response_json.get("err_code") == 40008:
                    status = "RECEIVED"
                elif response_json.get("msg") == "CDK NOT FOUND." and response_json.get("err_code") == 40014:
                    status = "CDK_NOT_FOUND"
                elif response_json.get("msg") == "SAME TYPE EXCHANGE." and response_json.get("err_code") == 40011:
                    status = "SAME TYPE EXCHANGE"
                elif response_json.get("msg") == "TIMEOUT RETRY." and response_json.get("err_code") == 40004:
                    status = "TIMEOUT_RETRY"
                else:
                    status = "ERROR"

                if player_id != "244886619" and status in ["SUCCESS", "RECEIVED", "SAME TYPE EXCHANGE"]:
                    try:
                        self.cursor.execute("""
                            INSERT INTO user_giftcodes (fid, giftcode, status)
                            VALUES (?, ?, ?)
                        """, (player_id, giftcode, status))
                        self.conn.commit()
                        print(f"Saved to database: User {player_id}, Code {giftcode}, Status {status}")
                    except Exception as e:
                        print(f"Database error: {str(e)}")

                return status

            return "ERROR"

        except Exception as e:
            print(f"Error in claim_giftcode_rewards_wos: {str(e)}")
            return "ERROR"

    @tasks.loop(seconds=300)
    async def check_channels_loop(self):
        try:
            self.cursor.execute("SELECT channel_id FROM giftcode_channel")
            channel_ids = [row[0] for row in self.cursor.fetchall()]

            for channel_id in channel_ids:
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    continue

                last_reaction_time = None
                async for message in channel.history(limit=100):
                    if message.reactions:
                        for reaction in message.reactions:
                            async for user in reaction.users():
                                if user == self.bot.user:
                                    last_reaction_time = message.created_at
                                    break
                            if last_reaction_time:
                                break
                    if last_reaction_time:
                        break

                if not last_reaction_time:
                    messages_to_check = [msg async for msg in channel.history(limit=10, oldest_first=True)]
                else:
                    messages_to_check = [msg async for msg in channel.history(limit=50, after=last_reaction_time, oldest_first=True)]

                for message in messages_to_check:
                    if message.author == self.bot.user:
                        continue

                    content = message.content.strip()
                    if not content:
                        continue

                    has_bot_reaction = False
                    for reaction in message.reactions:
                        async for user in reaction.users():
                            if user == self.bot.user:
                                has_bot_reaction = True
                                break
                        if has_bot_reaction:
                            break
                    
                    if has_bot_reaction:
                        continue


                    giftcode = None
                    
                    if len(content.split()) == 1:
                        giftcode = content
                    else:
                        code_match = re.search(r'Code:\s*(\S+)', content)
                        if code_match:
                            giftcode = code_match.group(1)

                    if not giftcode:
                        await message.add_reaction("❌")
                        continue

                    try:
                        session, response_stove_info = self.get_stove_info_wos(player_id="244886619")
                        
                        if response_stove_info.json().get("msg") == "success":
                            response_status = await self.claim_giftcode_rewards_wos("244886619", giftcode)

                            if response_status == "TIME_ERROR":
                                description = (
                                    f"**Gift Code Details**\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                    f"👤 **Sender:** {message.author.mention}\n"
                                    f"🎁 **Gift Code:** `{giftcode}`\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                )
                                expired_embed = discord.Embed(
                                    title="❌ Gift Code Expired",
                                    description=description,
                                    color=discord.Color.red()
                                )
                                await message.add_reaction("❌")
                                await message.reply(embed=expired_embed, mention_author=False)
                                continue

                            if response_status in ["SUCCESS", "RECEIVED", "SAME TYPE EXCHANGE"]:
                                self.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (giftcode,))
                                if not self.cursor.fetchone():
                                    self.cursor.execute(
                                        "INSERT INTO gift_codes (giftcode, date) VALUES (?, ?)",
                                        (giftcode, datetime.now().strftime("%Y-%m-%d"))
                                    )
                                    self.conn.commit()

                                    self.cursor.execute("SELECT alliance_id FROM giftcodecontrol WHERE status = 1")
                                    auto_alliances = self.cursor.fetchall()
                                    
                                    for alliance in auto_alliances:
                                        await self.use_giftcode_for_alliance(alliance[0], giftcode)

                                    description = f"**Gift Code Details**\n━━━━━━━━━━━━━━━━━━━━━━\n"
                                    if isinstance(message.author, discord.Member) or isinstance(message.author, discord.User):
                                        description += f"👤 **Sender:** {message.author.mention}\n"
                                    description += f"🎁 **Gift Code:** `{giftcode}`\n━━━━━━━━━━━━━━━━━━━━━━\n"

                                    success_embed = discord.Embed(
                                        title="✅ Gift Code Successfully Added",
                                        description=description,
                                        color=discord.Color.green()
                                    )
                                    await message.add_reaction("✅")
                                    await message.reply(embed=success_embed, mention_author=False)

                                else:
                                    description = f"**Gift Code Details**\n━━━━━━━━━━━━━━━━━━━━━━\n"
                                    if isinstance(message.author, discord.Member) or isinstance(message.author, discord.User):
                                        description += f"👤 **Sender:** {message.author.mention}\n"
                                    description += f"🎁 **Gift Code:** `{giftcode}`\n"
                                    description += f"📝 **Status:** `Already in database`\n━━━━━━━━━━━━━━━━━━━━━━\n"

                                    already_exists_embed = discord.Embed(
                                        title="ℹ️ Gift Code Status",
                                        description=description,
                                        color=discord.Color.blue()
                                    )
                                    await message.add_reaction("✅")
                                    await message.reply(embed=already_exists_embed, mention_author=False)
                            
                            elif response_status == "CDK_NOT_FOUND":
                                description = f"**Gift Code Details**\n━━━━━━━━━━━━━━━━━━━━━━\n"
                                if isinstance(message.author, discord.Member) or isinstance(message.author, discord.User):
                                    description += f"👤 **Sender:** {message.author.mention}\n"
                                description += f"🎁 **Gift Code:** `{giftcode}`\n━━━━━━━━━━━━━━━━━━━━━━\n"

                                error_embed = discord.Embed(
                                    title="❌ Invalid Gift Code",
                                    description=description,
                                    color=discord.Color.red()
                                )
                                await message.add_reaction("❌")
                                await message.reply(embed=error_embed, mention_author=False)
                            
                            elif response_status == "TIMEOUT_RETRY":
                                await message.add_reaction("⏳")
                                await asyncio.sleep(60)
                                _, retry_response = self.claim_giftcode_rewards_wos("244886619", giftcode)
                                await message.remove_reaction("⏳", self.bot.user)
                                if retry_response in ["SUCCESS", "ALREADY_RECEIVED", "SAME TYPE EXCHANGE"]:
                                    self.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (giftcode,))
                                    if not self.cursor.fetchone():
                                        self.cursor.execute(
                                            "INSERT INTO gift_codes (giftcode, date) VALUES (?, ?)",
                                            (giftcode, datetime.now().strftime("%Y-%m-%d"))
                                        )
                                        self.conn.commit()

                                        success_embed = discord.Embed(
                                            title="✅ Gift Code Successfully Added",
                                            description=(
                                                f"**Gift Code Details**\n"
                                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                                f"👤 **Sender:** {message.author.mention}\n"
                                                f"🎁 **Gift Code:** `{giftcode}`\n"
                                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                            ),
                                            color=discord.Color.green()
                                        )
                                        await message.add_reaction("✅")
                                        await message.reply(embed=success_embed, mention_author=False)
                                
                    except Exception as e:
                        print(f"Error processing gift code {giftcode}: {str(e)}")
                        continue

            await self.validate_gift_codes()
            
            await asyncio.sleep(300)
            
        except Exception as e:
            print(f"Error in check_channels_loop: {str(e)}")

    async def validate_gift_codes(self):
        try:
            self.cursor.execute("SELECT giftcode FROM gift_codes")
            all_codes = self.cursor.fetchall()
            
            self.settings_cursor.execute("SELECT id FROM admin WHERE is_initial = 1")
            admin_ids = [row[0] for row in self.settings_cursor.fetchall()]
            
            for code in all_codes:
                giftcode = code[0]
                status = await self.claim_giftcode_rewards_wos("244886619", giftcode)
                
                if status in ["TIME_ERROR", "CDK_NOT_FOUND"]:
                    self.cursor.execute("DELETE FROM user_giftcodes WHERE giftcode = ?", (giftcode,))
                    self.cursor.execute("DELETE FROM gift_codes WHERE giftcode = ?", (giftcode,))
                    self.conn.commit()
                    
                    reason = "expired" if status == "TIME_ERROR" else "invalid"
                    admin_embed = discord.Embed(
                        title="🎁 Gift Code Removed",
                        description=(
                            f"**Gift Code Details**\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎁 **Gift Code:** `{giftcode}`\n"
                            f"❌ **Reason:** `Code was {reason}`\n"
                            f"⏰ **Time:** <t:{int(datetime.now().timestamp())}:R>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        ),
                        color=discord.Color.red()
                    )
                    
                    for admin_id in admin_ids:
                        try:
                            admin_user = await self.bot.fetch_user(admin_id)
                            if admin_user:
                                await admin_user.send(embed=admin_embed)
                        except Exception as e:
                            print(f"Error sending message to admin {admin_id}: {str(e)}")
                
                await asyncio.sleep(60)
                
        except Exception as e:
            print(f"Error in validate_gift_codes: {str(e)}")

    async def handle_success(self, message, giftcode):
        self.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (giftcode,))
        if not self.cursor.fetchone():
            self.cursor.execute("INSERT INTO gift_codes (giftcode, date) VALUES (?, ?)", (giftcode, datetime.now()))
            self.conn.commit()
            await message.add_reaction("✅")
            await message.reply("Gift code successfully added.", mention_author=False)

    async def handle_already_received(self, message, giftcode):
        self.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (giftcode,))
        if not self.cursor.fetchone():
            self.cursor.execute("INSERT INTO gift_codes (giftcode, date) VALUES (?, ?)", (giftcode, datetime.now()))
            self.conn.commit()
            await message.add_reaction("✅")
            await message.reply("Gift code successfully added.", mention_author=False)

    async def handle_cdk_not_found(self, message):
        await message.add_reaction("❌")
        await message.reply("The gift code is incorrect.", mention_author=False)

    async def handle_time_error(self, message):
        await message.add_reaction("❌")
        await message.reply("Gift code expired.", mention_author=False)

    async def handle_timeout_retry(self, message, giftcode):
        self.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (giftcode,))
        if not self.cursor.fetchone():
            await message.add_reaction("⏳")

    async def get_admin_info(self, user_id):
        self.settings_cursor.execute("""
            SELECT id, is_initial FROM admin WHERE id = ?
        """, (user_id,))
        return self.settings_cursor.fetchone()

    async def get_alliance_names(self, user_id, is_global=False):
        if is_global:
            self.alliance_cursor.execute("SELECT name FROM alliance_list")
            return [row[0] for row in self.alliance_cursor.fetchall()]
        else:
            self.settings_cursor.execute("""
                SELECT alliances_id FROM adminserver WHERE admin = ?
            """, (user_id,))
            alliance_ids = [row[0] for row in self.settings_cursor.fetchall()]
            
            if alliance_ids:
                placeholders = ','.join('?' * len(alliance_ids))
                self.alliance_cursor.execute(f"""
                    SELECT name FROM alliance_list 
                    WHERE alliance_id IN ({placeholders})
                """, alliance_ids)
                return [row[0] for row in self.alliance_cursor.fetchall()]
            return []

    async def get_available_alliances(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        guild_id = interaction.guild_id if interaction.guild else None

        admin_info = await self.get_admin_info(user_id)
        if not admin_info:
            return []

        is_global = admin_info[1] == 1

        if is_global:
            self.alliance_cursor.execute("SELECT alliance_id, name FROM alliance_list")
            return self.alliance_cursor.fetchall()

        if guild_id:
            self.alliance_cursor.execute("""
                SELECT DISTINCT alliance_id, name 
                FROM alliance_list 
                WHERE discord_server_id = ?
            """, (guild_id,))
            guild_alliances = self.alliance_cursor.fetchall()

            self.settings_cursor.execute("""
                SELECT alliances_id FROM adminserver WHERE admin = ?
            """, (user_id,))
            special_alliance_ids = [row[0] for row in self.settings_cursor.fetchall()]

            if special_alliance_ids:
                placeholders = ','.join('?' * len(special_alliance_ids))
                self.alliance_cursor.execute(f"""
                    SELECT alliance_id, name FROM alliance_list 
                    WHERE alliance_id IN ({placeholders})
                """, special_alliance_ids)
                special_alliances = self.alliance_cursor.fetchall()
            else:
                special_alliances = []

            all_alliances = list(set(guild_alliances + special_alliances))
            return all_alliances

        return []

    async def setup_gift_channel(self, interaction: discord.Interaction):
        admin_info = await self.get_admin_info(interaction.user.id)
        if not admin_info:
            await interaction.response.send_message(
                "❌ You are not authorized to perform this action.",
                ephemeral=True
            )
            return

        available_alliances = await self.get_available_alliances(interaction)
        if not available_alliances:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ No Available Alliances",
                    description="You don't have access to any alliances.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        alliances_with_counts = []
        for alliance_id, name in available_alliances:
            with sqlite3.connect('db/users.sqlite') as users_db:
                cursor = users_db.cursor()
                cursor.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
                member_count = cursor.fetchone()[0]
                alliances_with_counts.append((alliance_id, name, member_count))

        self.cursor.execute("SELECT alliance_id, channel_id FROM giftcode_channel")
        current_channels = dict(self.cursor.fetchall())

        alliance_embed = discord.Embed(
            title="📢 Gift Code Channel Setup",
            description=(
                "Please select an alliance to set up gift code channel:\n\n"
                "**Alliance List**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Select an alliance from the list below:\n"
            ),
            color=discord.Color.blue()
        )

        view = AllianceSelectView(alliances_with_counts, self)

        async def alliance_callback(select_interaction: discord.Interaction):
            try:
                alliance_id = int(view.current_select.values[0])
                
                channel_embed = discord.Embed(
                    title="📢 Gift Code Channel Setup",
                    description=(
                        "**Instructions:**\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        "Please select a channel for gift codes\n\n"
                        "**Page:** 1/1\n"
                        f"**Total Channels:** {len(select_interaction.guild.text_channels)}"
                    ),
                    color=discord.Color.blue()
                )

                async def channel_select_callback(channel_interaction: discord.Interaction):
                    try:
                        channel_id = int(channel_interaction.data["values"][0])
                        
                        self.cursor.execute("""
                            INSERT OR REPLACE INTO giftcode_channel (alliance_id, channel_id)
                            VALUES (?, ?)
                        """, (alliance_id, channel_id))
                        self.conn.commit()

                        alliance_name = next((name for aid, name in available_alliances if aid == alliance_id), "Unknown Alliance")

                        success_embed = discord.Embed(
                            title="✅ Gift Code Channel Set",
                            description=(
                                f"Successfully set gift code channel:\n\n"
                                f"🏰 **Alliance:** {alliance_name}\n"
                                f"📝 **Channel:** <#{channel_id}>\n"
                            ),
                            color=discord.Color.green()
                        )

                        await channel_interaction.response.edit_message(
                            embed=success_embed,
                            view=None
                        )

                    except Exception as e:
                        print(f"Error setting gift code channel: {e}")
                        await channel_interaction.response.send_message(
                            "❌ An error occurred while setting the gift code channel.",
                            ephemeral=True
                        )

                channels = select_interaction.guild.text_channels
                channel_view = PaginatedChannelView(channels, channel_select_callback)

                if not select_interaction.response.is_done():
                    await select_interaction.response.edit_message(
                        embed=channel_embed,
                        view=channel_view
                    )
                else:
                    await select_interaction.message.edit(
                        embed=channel_embed,
                        view=channel_view
                    )

            except Exception as e:
                print(f"Error in alliance selection: {e}")
                if not select_interaction.response.is_done():
                    await select_interaction.response.send_message(
                        "❌ An error occurred while processing your selection.",
                        ephemeral=True
                    )
                else:
                    await select_interaction.followup.send(
                        "❌ An error occurred while processing your selection.",
                        ephemeral=True
                    )

        view.callback = alliance_callback

        await interaction.response.send_message(
            embed=alliance_embed,
            view=view,
            ephemeral=True
        )

    async def show_gift_menu(self, interaction: discord.Interaction):
        gift_menu_embed = discord.Embed(
            title="🎁 Gift Code Operations",
            description=(
                "Please select an operation:\n\n"
                "**Available Operations**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🎫 **Create Gift Code**\n"
                "└ Generate new gift codes\n\n"
                "📋 **List Gift Codes**\n"
                "└ View all active codes\n\n"
                "⚙️ **Auto Gift Settings**\n"
                "└ Configure automatic gift code usage\n\n"
                "❌ **Delete Gift Code**\n"
                "└ Remove existing codes\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.gold()
        )

        view = GiftView(self)
        try:
            await interaction.response.edit_message(embed=gift_menu_embed, view=view)
        except discord.InteractionResponded:
            pass
        except Exception:
            pass

    async def create_gift_code(self, interaction: discord.Interaction):
        self.settings_cursor.execute("SELECT 1 FROM admin WHERE id = ?", (interaction.user.id,))
        if not self.settings_cursor.fetchone():
            await interaction.response.send_message(
                "❌ You are not authorized to create gift codes.",
                ephemeral=True
            )
            return

        modal = CreateGiftCodeModal(self)
        try:
            await interaction.response.send_modal(modal)
        except Exception as e:
            print(f"Error showing modal: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An error occurred while showing the gift code creation form.",
                    ephemeral=True
                )

    async def list_gift_codes(self, interaction: discord.Interaction):
        self.cursor.execute("""
            SELECT 
                gc.giftcode,
                gc.date,
                COUNT(DISTINCT ugc.fid) as used_count
            FROM gift_codes gc
            LEFT JOIN user_giftcodes ugc ON gc.giftcode = ugc.giftcode
            GROUP BY gc.giftcode
            ORDER BY gc.date DESC
        """)
        
        codes = self.cursor.fetchall()
        
        if not codes:
            await interaction.response.send_message(
                "No gift codes found in the database.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🎁 Active Gift Codes",
            color=discord.Color.blue()
        )

        for code, date, used_count in codes:
            embed.add_field(
                name=f"Code: {code}",
                value=f"Created: {date}\nUsed by: {used_count} users",
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def delete_gift_code(self, interaction: discord.Interaction):
        try:
            settings_conn = sqlite3.connect('db/settings.sqlite')
            settings_cursor = settings_conn.cursor()
            
            settings_cursor.execute("""
                SELECT 1 FROM admin 
                WHERE id = ? AND is_initial = 1
            """, (interaction.user.id,))
            
            is_admin = settings_cursor.fetchone()
            settings_cursor.close()
            settings_conn.close()

            if not is_admin:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ Unauthorized Access",
                        description="This action requires Global Admin privileges.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            self.cursor.execute("""
                SELECT 
                    gc.giftcode,
                    gc.date,
                    COUNT(DISTINCT ugc.fid) as used_count
                FROM gift_codes gc
                LEFT JOIN user_giftcodes ugc ON gc.giftcode = ugc.giftcode
                GROUP BY gc.giftcode
                ORDER BY gc.date DESC
            """)
            
            codes = self.cursor.fetchall()
            
            if not codes:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ No Gift Codes",
                        description="There are no gift codes in the database to delete.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            select = discord.ui.Select(
                placeholder="Select a gift code to delete",
                options=[
                    discord.SelectOption(
                        label=f"Code: {code}",
                        description=f"Created: {date} | Used by: {used_count} users",
                        value=code
                    ) for code, date, used_count in codes
                ]
            )

            async def select_callback(select_interaction):
                selected_code = select_interaction.data["values"][0]
                
                confirm = discord.ui.Button(
                    style=discord.ButtonStyle.danger,
                    label="Confirm Delete",
                    custom_id="confirm"
                )
                cancel = discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label="Cancel",
                    custom_id="cancel"
                )

                async def button_callback(button_interaction):
                    try:
                        if button_interaction.data.get('custom_id') == "confirm":
                            try:
                                self.cursor.execute("DELETE FROM gift_codes WHERE giftcode = ?", (selected_code,))
                                self.cursor.execute("DELETE FROM user_giftcodes WHERE giftcode = ?", (selected_code,))
                                self.conn.commit()
                                
                                success_embed = discord.Embed(
                                    title="✅ Gift Code Deleted",
                                    description=(
                                        f"**Deletion Details**\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"🎁 **Gift Code:** `{selected_code}`\n"
                                        f"👤 **Deleted by:** {button_interaction.user.mention}\n"
                                        f"⏰ **Time:** <t:{int(datetime.now().timestamp())}:R>\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                    ),
                                    color=discord.Color.green()
                                )
                                
                                await button_interaction.response.edit_message(
                                    embed=success_embed,
                                    view=None
                                )
                                
                            except Exception as e:
                                await button_interaction.response.send_message(
                                    "❌ An error occurred while deleting the gift code.",
                                    ephemeral=True
                                )

                        else:
                            cancel_embed = discord.Embed(
                                title="❌ Deletion Cancelled",
                                description="The gift code deletion was cancelled.",
                                color=discord.Color.red()
                            )
                            await button_interaction.response.edit_message(
                                embed=cancel_embed,
                                view=None
                            )

                    except Exception as e:
                        print(f"Button callback error: {str(e)}")
                        try:
                            await button_interaction.response.send_message(
                                "❌ An error occurred while processing the request.",
                                ephemeral=True
                            )
                        except:
                            await button_interaction.followup.send(
                                "❌ An error occurred while processing the request.",
                                ephemeral=True
                            )

                confirm.callback = button_callback
                cancel.callback = button_callback

                confirm_view = discord.ui.View()
                confirm_view.add_item(confirm)
                confirm_view.add_item(cancel)

                confirmation_embed = discord.Embed(
                    title="⚠️ Confirm Deletion",
                    description=(
                        f"**Gift Code Details**\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎁 **Selected Code:** `{selected_code}`\n"
                        f"⚠️ **Warning:** This action cannot be undone!\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    ),
                    color=discord.Color.yellow()
                )

                await select_interaction.response.edit_message(
                    embed=confirmation_embed,
                    view=confirm_view
                )

            select.callback = select_callback
            view = discord.ui.View()
            view.add_item(select)

            initial_embed = discord.Embed(
                title="🗑️ Delete Gift Code",
                description=(
                    f"**Instructions**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"1️⃣ Select a gift code from the menu below\n"
                    f"2️⃣ Confirm your selection\n"
                    f"3️⃣ The code will be permanently deleted\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                ),
                color=discord.Color.blue()
            )

            await interaction.response.send_message(
                embed=initial_embed,
                view=view,
                ephemeral=True
            )

        except Exception as e:
            print(f"Delete gift code error: {str(e)}")
            await interaction.response.send_message(
                "❌ An error occurred while processing the request.",
                ephemeral=True
            )

    async def delete_gift_channel(self, interaction: discord.Interaction):
        admin_info = await self.get_admin_info(interaction.user.id)
        if not admin_info:
            await interaction.response.send_message(
                "❌ You are not authorized to perform this action.",
                ephemeral=True
            )
            return

        available_alliances = await self.get_available_alliances(interaction)
        if not available_alliances:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ No Available Alliances",
                    description="You don't have access to any alliances.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        self.cursor.execute("SELECT alliance_id, channel_id FROM giftcode_channel")
        current_channels = dict(self.cursor.fetchall())

        alliances_with_counts = []
        for alliance_id, name in available_alliances:
            if alliance_id in current_channels:
                with sqlite3.connect('db/users.sqlite') as users_db:
                    cursor = users_db.cursor()
                    cursor.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
                    member_count = cursor.fetchone()[0]
                    alliances_with_counts.append((alliance_id, name, member_count))

        if not alliances_with_counts:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ No Channels Set",
                    description="There are no gift code channels set for your alliances.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        remove_embed = discord.Embed(
            title="🗑️ Remove Gift Code Channel",
            description=(
                "Select an alliance to remove its gift code channel:\n\n"
                "**Current Log Channels**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Select an alliance from the list below:\n"
            ),
            color=discord.Color.red()
        )

        view = AllianceSelectView(alliances_with_counts, self)

        async def alliance_callback(select_interaction: discord.Interaction):
            try:
                alliance_id = int(view.current_select.values[0])
                
                self.cursor.execute("SELECT channel_id FROM giftcode_channel WHERE alliance_id = ?", (alliance_id,))
                channel_id = self.cursor.fetchone()[0]
                
                alliance_name = next((name for aid, name in available_alliances if aid == alliance_id), "Unknown Alliance")
                
                confirm_embed = discord.Embed(
                    title="⚠️ Confirm Removal",
                    description=(
                        f"Are you sure you want to remove the gift code channel for:\n\n"
                        f"🏰 **Alliance:** {alliance_name}\n"
                        f"📝 **Channel:** <#{channel_id}>\n\n"
                        "This action cannot be undone!"
                    ),
                    color=discord.Color.yellow()
                )

                confirm_view = discord.ui.View()
                
                async def confirm_callback(button_interaction: discord.Interaction):
                    try:
                        self.cursor.execute("DELETE FROM giftcode_channel WHERE alliance_id = ?", (alliance_id,))
                        self.conn.commit()

                        success_embed = discord.Embed(
                            title="✅ Gift Code Channel Removed",
                            description=(
                                f"Successfully removed gift code channel for:\n\n"
                                f"🏰 **Alliance:** {alliance_name}\n"
                                f"📝 **Channel:** <#{channel_id}>"
                            ),
                            color=discord.Color.green()
                        )

                        await button_interaction.response.edit_message(
                            embed=success_embed,
                            view=None
                        )

                    except Exception as e:
                        print(f"Error removing gift code channel: {e}")
                        await button_interaction.response.send_message(
                            "❌ An error occurred while removing the gift code channel.",
                            ephemeral=True
                        )

                async def cancel_callback(button_interaction: discord.Interaction):
                    cancel_embed = discord.Embed(
                        title="❌ Removal Cancelled",
                        description="The gift code channel removal has been cancelled.",
                        color=discord.Color.red()
                    )
                    await button_interaction.response.edit_message(
                        embed=cancel_embed,
                        view=None
                    )

                confirm_button = discord.ui.Button(
                    label="Confirm",
                    emoji="✅",
                    style=discord.ButtonStyle.danger,
                    custom_id="confirm_remove"
                )
                confirm_button.callback = confirm_callback

                cancel_button = discord.ui.Button(
                    label="Cancel",
                    emoji="❌",
                    style=discord.ButtonStyle.secondary,
                    custom_id="cancel_remove"
                )
                cancel_button.callback = cancel_callback

                confirm_view.add_item(confirm_button)
                confirm_view.add_item(cancel_button)

                if not select_interaction.response.is_done():
                    await select_interaction.response.edit_message(
                        embed=confirm_embed,
                        view=confirm_view
                    )
                else:
                    await select_interaction.message.edit(
                        embed=confirm_embed,
                        view=confirm_view
                    )

            except Exception as e:
                print(f"Error in alliance selection: {e}")
                if not select_interaction.response.is_done():
                    await select_interaction.response.send_message(
                        "❌ An error occurred while processing your selection.",
                        ephemeral=True
                    )
                else:
                    await select_interaction.followup.send(
                        "❌ An error occurred while processing your selection.",
                        ephemeral=True
                    )

        view.callback = alliance_callback

        await interaction.response.send_message(
            embed=remove_embed,
            view=view,
            ephemeral=True
        )

    async def setup_giftcode_auto(self, interaction: discord.Interaction):
        admin_info = await self.get_admin_info(interaction.user.id)
        if not admin_info:
            await interaction.response.send_message(
                "❌ You are not authorized to perform this action.",
                ephemeral=True
            )
            return

        available_alliances = await self.get_available_alliances(interaction)
        if not available_alliances:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ No Available Alliances",
                    description="You don't have access to any alliances.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        self.cursor.execute("SELECT alliance_id, status FROM giftcodecontrol")
        current_status = dict(self.cursor.fetchall())

        alliances_with_counts = []
        for alliance_id, name in available_alliances:
            with sqlite3.connect('db/users.sqlite') as users_db:
                cursor = users_db.cursor()
                cursor.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
                member_count = cursor.fetchone()[0]
                alliances_with_counts.append((alliance_id, name, member_count))

        auto_gift_embed = discord.Embed(
            title="⚙️ Auto Gift Code Settings",
            description=(
                "Select an alliance to configure auto gift code:\n\n"
                "**Alliance List**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Select an alliance from the list below:\n"
            ),
            color=discord.Color.blue()
        )

        view = AllianceSelectView(alliances_with_counts, self)

        async def alliance_callback(select_interaction: discord.Interaction):
            try:
                alliance_id = int(view.current_select.values[0])
                alliance_name = next((name for aid, name in available_alliances if aid == alliance_id), "Unknown")

                current_setting = "enabled" if current_status.get(alliance_id, 0) == 1 else "disabled"
                
                confirm_embed = discord.Embed(
                    title="⚙️ Auto Gift Code Configuration",
                    description=(
                        f"**Alliance Details**\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🏰 **Alliance:** {alliance_name}\n"
                        f"📊 **Current Status:** Auto gift code is {current_setting}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"Do you want to enable or disable auto gift code for this alliance?"
                    ),
                    color=discord.Color.yellow()
                )

                confirm_view = discord.ui.View()
                
                async def button_callback(button_interaction: discord.Interaction):
                    try:
                        status = 1 if button_interaction.data['custom_id'] == "confirm" else 0
                        
                        self.cursor.execute(
                            """
                            INSERT INTO giftcodecontrol (alliance_id, status) 
                            VALUES (?, ?) 
                            ON CONFLICT(alliance_id) 
                            DO UPDATE SET status = excluded.status
                            """,
                            (alliance_id, status)
                        )
                        self.conn.commit()

                        status_text = "enabled" if status == 1 else "disabled"
                        success_embed = discord.Embed(
                            title="✅ Auto Gift Code Setting Updated",
                            description=(
                                f"**Configuration Details**\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"🏰 **Alliance:** {alliance_name}\n"
                                f"📊 **Status:** Auto gift code {status_text}\n"
                                f"👤 **Updated by:** {button_interaction.user.mention}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            ),
                            color=discord.Color.green()
                        )
                        
                        await button_interaction.response.edit_message(
                            embed=success_embed,
                            view=None
                        )

                    except Exception as e:
                        print(f"Button callback error: {str(e)}")
                        if not button_interaction.response.is_done():
                            await button_interaction.response.send_message(
                                "❌ An error occurred while updating the settings.",
                                ephemeral=True
                            )
                        else:
                            await button_interaction.followup.send(
                                "❌ An error occurred while updating the settings.",
                                ephemeral=True
                            )

                confirm_button = discord.ui.Button(
                    label="Enable",
                    emoji="✅",
                    style=discord.ButtonStyle.success,
                    custom_id="confirm"
                )
                confirm_button.callback = button_callback

                deny_button = discord.ui.Button(
                    label="Disable",
                    emoji="❌",
                    style=discord.ButtonStyle.danger,
                    custom_id="deny"
                )
                deny_button.callback = button_callback

                confirm_view.add_item(confirm_button)
                confirm_view.add_item(deny_button)

                if not select_interaction.response.is_done():
                    await select_interaction.response.edit_message(
                        embed=confirm_embed,
                        view=confirm_view
                    )
                else:
                    await select_interaction.message.edit(
                        embed=confirm_embed,
                        view=confirm_view
                    )

            except Exception as e:
                print(f"Error in alliance selection: {e}")
                if not select_interaction.response.is_done():
                    await select_interaction.response.send_message(
                        "❌ An error occurred while processing your selection.",
                        ephemeral=True
                    )
                else:
                    await select_interaction.followup.send(
                        "❌ An error occurred while processing your selection.",
                        ephemeral=True
                    )

        view.callback = alliance_callback

        await interaction.response.send_message(
            embed=auto_gift_embed,
            view=view,
            ephemeral=True
        )

    async def use_giftcode_for_alliance(self, alliance_id, giftcode):
        try:
            self.alliance_cursor.execute(
                "SELECT channel_id FROM alliancesettings WHERE alliance_id = ?",
                (alliance_id,)
            )
            channel_id = self.alliance_cursor.fetchone()
            if not channel_id:
                return
            
            channel_id = channel_id[0]
            channel = self.bot.get_channel(channel_id)
            if not channel:
                return

            users_conn = sqlite3.connect('db/users.sqlite')
            users_cursor = users_conn.cursor()

            users_cursor.execute(
                "SELECT fid FROM users WHERE alliance = ?",
                (str(alliance_id),)
            )
            members = users_cursor.fetchall()
            users_conn.close()

            total_members = len(members)
            processed = 0
            success = 0
            received = 0
            failed = 0

            embed = discord.Embed(
                title="🎁 Auto Gift Code Progress",
                description=(
                    f"**Gift Code Distribution Started**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎁 **Gift Code:** `{giftcode}`\n"
                    f"👥 **Total Members:** `{total_members}`\n"
                    f"✅ **Success:** `{success}`\n"
                    f"⚠️ **Already Used:** `{received}`\n"
                    f"❌ **Failed:** `{failed}`\n"
                    f"⏳ **Progress:** `{processed}/{total_members}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                ),
                color=discord.Color.blue()
            )
            status_message = await channel.send(embed=embed)

            log_file_path = os.path.join(self.log_directory, 'giftlog.txt')
            with open(log_file_path, 'a', encoding='utf-8') as log_file:
                log_file.write(f"\nGIFT CODE: {giftcode}\n")
                log_file.write(f"USAGE TIME: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                log_file.write("-----------------------------\n")
                log_file.write("Alliance Member List\n\n")

            for member in members:
                player_id = member[0]
                try:
                    with sqlite3.connect('db/users.sqlite') as users_db:
                        cursor = users_db.cursor()
                        cursor.execute("SELECT nickname FROM users WHERE fid = ?", (player_id,))
                        nickname = cursor.fetchone()[0]

                    response_status = await self.claim_giftcode_rewards_wos(player_id, giftcode)
                    
                    with open(log_file_path, 'a', encoding='utf-8') as log_file:
                        log_file.write(f"{nickname} - {response_status}\n")

                    if response_status == 429:
                        embed.description = (
                            f"**API Rate Limit Detected**\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎁 **Gift Code:** `{giftcode}`\n"
                            f"👥 **Total Members:** `{total_members}`\n"
                            f"✅ **Success:** `{success}`\n"
                            f"⚠️ **Already Used:** `{received}`\n"
                            f"❌ **Failed:** `{failed}`\n"
                            f"⏳ **Progress:** `{processed}/{total_members}`\n"
                            f"⚠️ **Waiting 60 seconds for API cooldown...**\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        )
                        embed.color = discord.Color.orange()
                        await status_message.edit(embed=embed)
                        
                        await asyncio.sleep(60)
                        
                        embed.color = discord.Color.blue()
                        continue

                    if response_status == "SUCCESS":
                        success += 1
                    elif response_status in ["RECEIVED", "SAME TYPE EXCHANGE"]:
                        received += 1
                    else:
                        failed += 1

                    processed += 1

                    embed.description = (
                        f"**Processing Gift Code**\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎁 **Gift Code:** `{giftcode}`\n"
                        f"👥 **Total Members:** `{total_members}`\n"
                        f"✅ **Success:** `{success}`\n"
                        f"⚠️ **Already Used:** `{received}`\n"
                        f"❌ **Failed:** `{failed}`\n"
                        f"⏳ **Progress:** `{processed}/{total_members}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    )
                    await status_message.edit(embed=embed)

                except Exception as e:
                    print(f"Error processing member {player_id}: {str(e)}")
                    with open(log_file_path, 'a', encoding='utf-8') as log_file:
                        log_file.write(f"{nickname} - ERROR: {str(e)}\n")
                    failed += 1
                    processed += 1
                    await status_message.edit(embed=embed)

            with open(log_file_path, 'a', encoding='utf-8') as log_file:
                log_file.write("-----------------------------\n\n")

            embed.title = "🎁 Auto Gift Code Complete"
            embed.color = discord.Color.green()
            await status_message.edit(embed=embed)

        except Exception as e:
            print(f"Error in use_giftcode_for_alliance: {str(e)}")

class CreateGiftCodeModal(discord.ui.Modal):
    def __init__(self, cog):
        super().__init__(title="Create Gift Code")
        self.cog = cog
        
        self.giftcode = discord.ui.TextInput(
            label="Gift Code",
            placeholder="Enter the gift code",
            required=True,
            min_length=4,
            max_length=20
        )
        self.add_item(self.giftcode)
    
    async def on_submit(self, interaction: discord.Interaction):
        code = self.giftcode.value
        date = datetime.now().strftime("%Y-%m-%d")
        
        try:
            self.cog.cursor.execute(
                "INSERT INTO gift_codes (giftcode, date) VALUES (?, ?)",
                (code, date)
            )
            self.cog.conn.commit()
            
            embed = discord.Embed(
                title="✅ Gift Code Created",
                description=f"Gift code `{code}` has been created successfully.",
                color=discord.Color.green()
            )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except sqlite3.IntegrityError:
            await interaction.response.send_message(
                "❌ This gift code already exists!",
                ephemeral=True
            )
        except Exception as e:
            print(f"Error creating gift code: {e}")
            await interaction.response.send_message(
                "❌ An error occurred while creating the gift code.",
                ephemeral=True
            )

class DeleteGiftCodeModal(discord.ui.Modal, title="Delete Gift Code"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        
    giftcode = discord.ui.TextInput(
        label="Gift Code",
        placeholder="Enter the gift code to delete",
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        code = self.giftcode.value
        
        self.cog.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (code,))
        if not self.cog.cursor.fetchone():
            await interaction.response.send_message(
                "❌ Gift code not found!",
                ephemeral=True
            )
            return
            
        self.cog.cursor.execute("DELETE FROM gift_codes WHERE giftcode = ?", (code,))
        self.cog.cursor.execute("DELETE FROM user_giftcodes WHERE giftcode = ?", (code,))
        self.cog.conn.commit()
        
        embed = discord.Embed(
            title="✅ Gift Code Deleted",
            description=f"Gift code `{code}` has been deleted successfully.",
            color=discord.Color.green()
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

class GiftView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Create Gift Code",
        style=discord.ButtonStyle.green,
        custom_id="create_gift",
        emoji="🎫",
        row=0
    )
    async def create_gift(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.create_gift_code(interaction)

    @discord.ui.button(
        label="List Gift Codes",
        style=discord.ButtonStyle.blurple,
        custom_id="list_gift",
        emoji="📋",
        row=0
    )
    async def list_gift(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.list_gift_codes(interaction)

    @discord.ui.button(
        label="Auto Gift Settings",
        style=discord.ButtonStyle.grey,
        custom_id="auto_gift_settings",
        emoji="⚙️",
        row=1
    )
    async def auto_gift_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.setup_giftcode_auto(interaction)

    @discord.ui.button(
        label="Delete Gift Code",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="delete_gift"
    )
    async def delete_gift_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.cog.delete_gift_code(interaction)
        except Exception as e:
            print(f"Delete gift button error: {e}")
            await interaction.response.send_message(
                "❌ An error occurred while processing delete request.",
                ephemeral=True
            )

    @discord.ui.button(
        label="Gift Code Channel",
        emoji="📢",
        style=discord.ButtonStyle.primary,
        custom_id="gift_channel"
    )
    async def gift_channel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.cog.setup_gift_channel(interaction)
        except Exception as e:
            print(f"Gift channel button error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An error occurred while setting up gift channel.",
                    ephemeral=True
                )

    @discord.ui.button(
        label="Delete Gift Channel",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        custom_id="delete_gift_channel"
    )
    async def delete_gift_channel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.cog.delete_gift_channel(interaction)
        except Exception as e:
            print(f"Delete gift channel button error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An error occurred while deleting gift channel.",
                    ephemeral=True
                )

    @discord.ui.button(
        label="Use Gift Code for Alliance",
        emoji="🎯",
        style=discord.ButtonStyle.primary,
        custom_id="use_gift_alliance"
    )
    async def use_gift_alliance_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            admin_info = await self.cog.get_admin_info(interaction.user.id)
            if not admin_info:
                await interaction.response.send_message(
                    "❌ You are not authorized to perform this action.",
                    ephemeral=True
                )
                return

            available_alliances = await self.cog.get_available_alliances(interaction)
            if not available_alliances:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ No Available Alliances",
                        description="You don't have access to any alliances.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            alliances_with_counts = []
            for alliance_id, name in available_alliances:
                with sqlite3.connect('db/users.sqlite') as users_db:
                    cursor = users_db.cursor()
                    cursor.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
                    member_count = cursor.fetchone()[0]
                    alliances_with_counts.append((alliance_id, name, member_count))

            alliance_embed = discord.Embed(
                title="🎯 Use Gift Code for Alliance",
                description=(
                    "Select an alliance to use gift code:\n\n"
                    "**Alliance List**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "Select an alliance from the list below:\n"
                ),
                color=discord.Color.blue()
            )

            view = AllianceSelectView(alliances_with_counts, self.cog)

            async def alliance_callback(select_interaction: discord.Interaction):
                try:
                    alliance_id = int(view.current_select.values[0])
                    
                    self.cog.cursor.execute("""
                        SELECT giftcode, date FROM gift_codes
                        ORDER BY date DESC
                    """)
                    gift_codes = self.cog.cursor.fetchall()

                    if not gift_codes:
                        await select_interaction.response.edit_message(
                            content="No gift codes available.",
                            view=None
                        )
                        return

                    giftcode_embed = discord.Embed(
                        title="🎁 Select Gift Code",
                        description=(
                            "Select a gift code to use:\n\n"
                            "**Gift Code List**\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "Select a gift code from the list below:\n"
                        ),
                        color=discord.Color.blue()
                    )

                    select_giftcode = discord.ui.Select(
                        placeholder="Select a gift code",
                        options=[
                            discord.SelectOption(
                                label=f"Code: {code}",
                                value=code,
                                description=f"Created: {date}",
                                emoji="🎁"
                            ) for code, date in gift_codes
                        ]
                    )

                    async def giftcode_callback(giftcode_interaction: discord.Interaction):
                        try:
                            selected_code = giftcode_interaction.data["values"][0]
                            alliance_name = next((name for aid, name in available_alliances if aid == alliance_id), "Unknown")
                            
                            confirm_embed = discord.Embed(
                                title="⚠️ Confirm Gift Code Usage",
                                description=(
                                    f"Are you sure you want to use this gift code?\n\n"
                                    f"**Details**\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                    f"🏰 **Alliance:** {alliance_name}\n"
                                    f"🎁 **Gift Code:** `{selected_code}`\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                ),
                                color=discord.Color.yellow()
                            )

                            confirm_view = discord.ui.View()
                            
                            async def confirm_callback(button_interaction: discord.Interaction):
                                try:
                                    await button_interaction.response.edit_message(
                                        content="Process started! Check alliance channel for progress.",
                                        embed=None,
                                        view=None
                                    )
                                    
                                    await self.cog.use_giftcode_for_alliance(alliance_id, selected_code)

                                except Exception as e:
                                    print(f"Error using gift code: {e}")
                                    await button_interaction.followup.send(
                                        "❌ An error occurred while using the gift code.",
                                        ephemeral=True
                                    )

                            async def cancel_callback(button_interaction: discord.Interaction):
                                cancel_embed = discord.Embed(
                                    title="❌ Operation Cancelled",
                                    description="The gift code usage has been cancelled.",
                                    color=discord.Color.red()
                                )
                                await button_interaction.response.edit_message(
                                    embed=cancel_embed,
                                    view=None
                                )

                            confirm_button = discord.ui.Button(
                                label="Confirm",
                                emoji="✅",
                                style=discord.ButtonStyle.success,
                                custom_id="confirm"
                            )
                            confirm_button.callback = confirm_callback

                            cancel_button = discord.ui.Button(
                                label="Cancel",
                                emoji="❌",
                                style=discord.ButtonStyle.danger,
                                custom_id="cancel"
                            )
                            cancel_button.callback = cancel_callback

                            confirm_view.add_item(confirm_button)
                            confirm_view.add_item(cancel_button)

                            await giftcode_interaction.response.edit_message(
                                embed=confirm_embed,
                                view=confirm_view
                            )

                        except Exception as e:
                            print(f"Error in gift code selection: {e}")
                            if not giftcode_interaction.response.is_done():
                                await giftcode_interaction.response.send_message(
                                    "❌ An error occurred while processing your selection.",
                                    ephemeral=True
                                )
                            else:
                                await giftcode_interaction.followup.send(
                                    "❌ An error occurred while processing your selection.",
                                    ephemeral=True
                                )

                    select_giftcode.callback = giftcode_callback
                    giftcode_view = discord.ui.View()
                    giftcode_view.add_item(select_giftcode)

                    if not select_interaction.response.is_done():
                        await select_interaction.response.edit_message(
                            embed=giftcode_embed,
                            view=giftcode_view
                        )
                    else:
                        await select_interaction.message.edit(
                            embed=giftcode_embed,
                            view=giftcode_view
                        )

                except Exception as e:
                    print(f"Error in alliance selection: {e}")
                    if not select_interaction.response.is_done():
                        await select_interaction.response.send_message(
                            "❌ An error occurred while processing your selection.",
                            ephemeral=True
                        )
                    else:
                        await select_interaction.followup.send(
                            "❌ An error occurred while processing your selection.",
                            ephemeral=True
                        )

            view.callback = alliance_callback

            await interaction.response.send_message(
                embed=alliance_embed,
                view=view,
                ephemeral=True
            )

        except Exception as e:
            print(f"Error in use_gift_alliance_button: {str(e)}")
            await interaction.response.send_message(
                "❌ An error occurred while processing the request.",
                ephemeral=True
            )

    @discord.ui.button(
        label="Main Menu",
        emoji="🏠",
        style=discord.ButtonStyle.secondary,
        custom_id="main_menu"
    )
    async def main_menu_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            alliance_cog = self.cog.bot.get_cog("Alliance")
            if alliance_cog:
                try:
                    await interaction.message.edit(content=None, embed=None, view=None)
                except:
                    pass
                await alliance_cog.show_main_menu(interaction)
        except:
            pass

async def setup(bot):
    await bot.add_cog(GiftOperations(bot)) 
