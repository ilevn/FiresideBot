import discord
from discord.ext import commands

class Admin(commands.Cog):

    def __init__(self, bot):

        self.client = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print('Admin is ready')

    @commands.command()
    async def ping(self, ctx):
        await ctx.send('Pong!')

def setup(bot):
    bot.add_cog(Admin(bot))
