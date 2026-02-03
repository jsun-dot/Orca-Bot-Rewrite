import discord
from discord.ext import commands
from datetime import datetime

class Ping(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot: commands.Bot = bot

    @commands.hybrid_command(name = "ping", description = "Checks your latency to Orca's server.")
    async def ping(self, ctx: commands.Context) -> None:
        ping = (f'{round(self.bot.latency * 1000)}ms')
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        server_name = ctx.guild.name
        server_id = ctx.guild.id
        user_hash = ctx.author.discriminator
        print(f'{timestamp} - {ctx.author.display_name}#{user_hash} used the ping command in server "{server_name}" ({server_id})')
        await ctx.send(ping)

async def setup(bot):
    await bot.add_cog(Ping(bot))

