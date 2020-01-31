import discord
import os
from discord.ext import commands

bot = commands.Bot(command_prefix = '.')

@bot.command()
async def load(ctx, extension):
    bot.load_extension(f'cogs.{extension}')
    await ctx.send(f'{extension} loaded')

@bot.command()
async def unload(ctx, extension):
    bot.unload_extension(f'cogs.{extension}')
    await ctx.send(f'{extension} unloaded')

@bot.command()
async def reload(ctx, extension):
    await unload(ctx, extension)
    await load(ctx, extension)
    await ctx.send(f'{extension} reloaded')


for filename in os.listdir('./cogs'):
    if filename.endswith('.py'):
        bot.load_extension(f'cogs.{filename[:-3]}')

bot.run('NjcyODY5MjQyOTkwODg2OTMy.XjSIEA.hsLLfKuThXEpaVQQpLkqhQAU8Q0')
