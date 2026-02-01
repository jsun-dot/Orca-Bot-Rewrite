import asyncio
import discord
import sys
import math
import os
import logging
import datetime

# Load environment variables from a local .env file if python-dotenv is installed
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:
    pass

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN. Put it in your environment or in a .env file in the project root.")


from discord.ext import commands
from discord.utils import get

# basic Discord Bot Necessities
intents = discord.Intents.all()
intents.members = True
client = commands.Bot(command_prefix="`", intents=intents, help_command=None)

# create a logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# create a console handler and set its level to INFO
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# create a file handler and set its level to INFO
current_date = datetime.date.today().strftime("%Y-%m-%d")
log_file = f"log_{current_date}.txt"
file_handler = logging.FileHandler(filename=log_file, mode='a')
file_handler.setLevel(logging.INFO)

# create a formatter and add it to the handlers
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

# add the handlers to the logger
logger.addHandler(console_handler)
logger.addHandler(file_handler)

# Load All Cogs
async def load():
    loaded_cogs = 0
    failed_cogs = []
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            try:
                await client.load_extension(f'cogs.{filename[:-3]}')
                loaded_cogs += 1
            except Exception as e:
                logger.error(f"Failed to load extension {filename}: {e}")
                failed_cogs.append(filename)
    logger.info(f"Loaded {loaded_cogs} cogs successfully.")
    if failed_cogs:
        logger.warning(f"Failed to load {len(failed_cogs)} cog(s): {', '.join(failed_cogs)}")
    else:
        logger.info("All cogs loaded successfully.")

# Run the Client
async def main():
    for attempt in range(2):
        try:
            await load()
            await client.start(DISCORD_TOKEN)
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            if attempt == 0:
                logger.info("Retrying...")
            else:
                logger.error("Failed to load again. Check the server and restart.")
                break

if __name__ == '__main__':
    asyncio.run(main())
