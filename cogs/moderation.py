import discord
from discord.ext import commands
from datetime import datetime


class Moderation(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot: commands.Bot = bot

    @commands.hybrid_command(
        name="kick",
        description="Kick a member from your server. You must have permissions to use this command.",
        options=[
            {
                "name": "user",
                "description": "User to kick from the server.",
                "type": 6,
                "required": True,
            }
        ],
    )
    async def kick(self, ctx: commands.Context, user: discord.Member) -> None:
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        server_name = ctx.guild.name
        server_id = ctx.guild.id
        user_hash = ctx.author.discriminator

        # check if the user has permission to kick members
        if not ctx.author.guild_permissions.kick_members:
            print(
                f'{timestamp} - {ctx.author.display_name}#{user_hash} tried to kick {user.display_name}#{user.discriminator} '
                f'in server "{server_name}" ({server_id}), but does not have permission to do so'
            )
            await ctx.send("You do not have permission to kick members.")
            return

        # kick the user from the server
        try:
            await user.kick(reason="Kicked by moderator.")
            print(
                f'{timestamp} - {ctx.author.display_name}#{user_hash} kicked {user.display_name}#{user.discriminator} '
                f'in server "{server_name}" ({server_id})'
            )
            await ctx.send(f"{user.mention} has been kicked from the server.")
        except discord.Forbidden:
            await ctx.send(
                "I'm sorry, I couldn't kick that user. Make sure my role is higher than the user you want to kick."
            )

    @commands.hybrid_command(
        name="changerole",
        description="Change the role of a specified user. You must have permissions to use this command.",
        options=[
            {
                "name": "user",
                "description": "Type a user and the role to give them.",
                "type": 6,
                "required": True,
            }
        ],
    )
    async def changerole(self, ctx: commands.Context, user: discord.Member, role: discord.Role) -> None:
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        server_name = ctx.guild.name
        server_id = ctx.guild.id
        user_hash = ctx.author.discriminator

        # Check if the user has permissions to modify roles
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.send("You do not have permission to modify roles.")
            print(
                f"{timestamp} - {ctx.author.display_name}#{user_hash} tried to change {user.display_name}#{user.discriminator}'s role "
                f"({role.name}) in server \"{server_name}\" ({server_id}), but does not have permission to do so."
            )
            return

        # Check if the bot has permissions to modify roles
        if not ctx.guild.me.guild_permissions.manage_roles:
            await ctx.send("I do not have permission to modify roles.")
            return

        try:
            await user.add_roles(role)
            await ctx.send(f"{user.display_name}'s role has been changed to {role.name}.")
            print(
                f"{timestamp} - {ctx.author.display_name}#{user_hash} changed {user.display_name}#{user.discriminator}'s role "
                f"to ({role.name}) in server \"{server_name}\" ({server_id})."
            )
        except discord.Forbidden:
            await ctx.send("I'm sorry, I couldn't change that user's role due to insufficient permissions.")


async def setup(bot):
    await bot.add_cog(Moderation(bot))