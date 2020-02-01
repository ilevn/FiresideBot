import asyncio

import click

from bot import FiresideBot

try:
    # Try to load our blazing fast event handler.
    import uvloop
except ImportError:
    pass
else:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


def run_bot():
    # TODO: Add postgresql support.
    bot = FiresideBot(command_prefix=".")
    bot.run()


@click.group(invoke_without_command=True, options_metavar="[options]")
@click.pass_context
def main(ctx):
    """Launches the bot."""
    if ctx.invoked_subcommand is None:
        run_bot()


if __name__ == "__main__":
    main()
