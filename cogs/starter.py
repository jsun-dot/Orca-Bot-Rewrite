import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime
import pytz

# This class is responsible for starting and syncing the bot and commands
class Starter(commands.Cog):

    def __init__(self, client):
        self.client = client

    @commands.Cog.listener()
    async def on_ready(self):
        # Get current UTC time and convert to Eastern Time
        utc_now = datetime.utcnow()
        eastern = pytz.timezone('US/Eastern')
        eastern_now = utc_now.astimezone(eastern)
        current_time = eastern_now.strftime('%Y-%m-%d %H:%M:%S')

        await self.client.tree.sync()

        # Enhanced console output design
        print("""
        ╔══════════════════════════════════════════════════════════════════╗
        ║                                                                  ║
        ║                      🤖  BOT IS NOW ONLINE! 🤖                     ║ 
        ║                                                                  ║
        ║      ╭──────────────────────────────────────────────────────╮    ║
        ║      │  Time (US/Eastern): {time}              │    ║
        ║      ╰──────────────────────────────────────────────────────╯    ║
        ║                                                                  ║
        ╚══════════════════════════════════════════════════════════════════╝
        """.format(time=current_time))

        print('''
        ────────────────────────────────────────────────────────────
        📋 Logged in as:
        Username: {0.user.name}
        User ID : {0.user.id}
        ────────────────────────────────────────────────────────────
        '''.format(self.client))

        await self.client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name="music works again."))

async def setup(client):
    await client.add_cog(Starter(client))
