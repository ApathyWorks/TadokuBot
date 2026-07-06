"""Entry point: load configuration and start the bot.

Run with ``python main.py`` after copying ``.env.example`` to ``.env`` and
filling in a Discord bot token.
"""

import logging
import os
import sys

from dotenv import load_dotenv

from bot import TadokuBot
from lib.permissions import parse_admin_roles

# Configure root logging once, before anything logs. INFO keeps startup/sync
# messages visible without the noise of DEBUG.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Load DISCORD_TOKEN (and any other vars) from a local .env file into os.environ.
load_dotenv()

token = os.getenv("DISCORD_TOKEN")
if not token:
    # Fail fast with a clear, actionable message rather than letting discord.py
    # raise an opaque login error later.
    sys.exit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")

# Optional: roles (by name, comma-separated) allowed to use the admin commands
# in addition to Manage Server, e.g. ADMIN_ROLES=Moderator,Officers.
admin_roles = parse_admin_roles(os.getenv("ADMIN_ROLES"))
if admin_roles:
    logging.getLogger(__name__).info("Admin roles configured: %d", len(admin_roles))

# Construct the bot and hand control to discord.py's event loop. ``run`` blocks
# until the process is stopped.
bot = TadokuBot(admin_roles=admin_roles)
bot.run(token)
