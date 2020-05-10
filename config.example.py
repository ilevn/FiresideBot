"""
Example config for the bot. DO NOT edit this one directly.
Instead, copy this template over to config.py and make changes to it.
"""

# The token used to run the bot.
token = ""

# A list of default cogs to load on start-up.
autoload = ["cogs.admin"]

# Whether the bot is running in dev-mode.
dev_mode = True

# What prefix to use while in dev-mode.
dev_prefix = "!"

# The default postgresql driver connection string.
postgresql = "postgresql://localhost:5432/firesidebot"

# The DSN used by sentry.io's error handler.
sentry_dsn = ""

# Explicitly define the owner of the bot. This is not needed by default.
owner = None
