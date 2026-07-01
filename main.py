import logging
import os
import sys

from dotenv import load_dotenv

from bot import TadokuBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

load_dotenv()

token = os.getenv("DISCORD_TOKEN")
if not token:
    sys.exit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")

bot = TadokuBot()
bot.run(token)
