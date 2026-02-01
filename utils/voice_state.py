import discord
from discord.ext import commands
import asyncio
import math
import itertools
import random
from async_timeout import timeout
from datetime import datetime
import gc
import logging
from utils.views import QueuePages, NowPlayingButtons
from utils.yt_source import YTDLSource, Song, YTDLError, VoiceError

# Setup logging
logging.basicConfig(level=logging.INFO)

class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]

class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx
        self.text_channel = ctx.channel  # Use channel.send instead of ctx.send to avoid expired interaction webhooks
        self.exists = True
        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()
        self._loop = False
        self._volume = 0.3  # Default volume set to 30%
        self.skip_votes = set()
        self.audio_player = bot.loop.create_task(self.audio_player_task())
        self.now_playing_message = None
        self.queue_message = None
        self.first_song_played = False
        self.action_message = ""  # To store the action message
        self.inactivity_task = bot.loop.create_task(self.inactivity_timer())  # Add inactivity timer
        self.last_added_message = None  # Track the last added message
        self.lock = asyncio.Lock()  # Initialize the lock

    async def add_song(self, song):
        await self.songs.put(song)
        await self.add_song_message(song)

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current and self.voice.is_playing()

    async def change_volume(self, delta: int, interaction: discord.Interaction):
        new_volume = self._volume + (delta / 100)
        new_volume = max(0, min(1, new_volume))  # Ensure the volume is between 0 and 1
        self._volume = new_volume
        if self.current:
            self.current.source.volume = self._volume

        # Update the Now Playing embed with the volume change notification
        self.action_message = f"**{interaction.user.display_name} changed the volume to {int(self._volume * 100)}%**"
        await self.update_now_playing_embed(interaction)

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop or not self.current:
                try:
                    async with timeout(1800):  # Wait up to 5 minutes for the next song
                        self.current = await self.songs.get()
                        logging.info(f"Playing song: {self.current.source.title}")
                except asyncio.TimeoutError:
                    logging.info("No more songs in the queue. Stopping playback.")
                    await self.stop()
                    return

            # If voice isn't connected yet, wait instead of crashing/spinning.
            if not self.voice or not self.voice.is_connected():
                await asyncio.sleep(0.5)
                continue

            # Refresh the expiring googlevideo stream URL right before playback.
            # This prevents intermittent FFmpeg 403 errors when items sit in the queue.
            try:
                self.current.source = await YTDLSource.regather_stream(self._ctx, self.current.source, loop=self.bot.loop)
            except Exception as e:
                logging.warning(f"Failed to regather stream URL (will try old URL): {e}")

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            self.first_song_played = True
            await self.update_now_playing_embed()

            await self.next.wait()  # Wait until the song is finished

            if self.loop and self.current:
                # If looping is enabled, put the current song back into the queue
                await self.songs.put(self.current)
                logging.info("Looping the current song.")
            else:
                self.current = None  # Reset the current song

            # The loop will continue and fetch the next song from the queue

    def play_next_song(self, error=None):
        # Called from the audio thread; never raise here.
        if error:
            logging.error(f"Playback error: {error}")
        self.bot.loop.call_soon_threadsafe(self.next.set)

    def skip(self):
        self.skip_votes.clear()
        if self.is_playing:
            logging.info("Skipping the current song...")
            self.voice.stop()

    async def stop(self):
        self.songs.clear()
        if self.voice:
            await self.voice.disconnect()
            self.voice = None
        self.exists = False
        if self.queue_message:
            await self.queue_message.edit(content="Bot disconnected from the voice channel. Stopping playlist processing.", embed=None, view=None)

    async def update_queue_message(self):
        if not self.first_song_played:
            return

        ctx = self._ctx
        items_per_page = 10
        pages = math.ceil(len(self.songs) / items_per_page)
        embeds = []

        if len(self.songs) == 0:
            embed = discord.Embed(description='**Empty queue.**')
            embeds.append(embed)
        else:
            for page in range(pages):
                queue = ''
                for i, song in enumerate(self.songs[page * items_per_page:(page + 1) * items_per_page], start=page * items_per_page):
                    queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)
                embed = (discord.Embed(description='**{} track(s):**\n\n{}'.format(len(self.songs), queue))
                         .set_footer(text='Viewing page {}/{}'.format(page + 1, pages)))
                embeds.append(embed)

        view = QueuePages(ctx, embeds, current_page=0)
        try:
            if self.queue_message:
                await self.queue_message.edit(embed=embeds[0], view=view)
            else:
                channel = self.text_channel or ctx.channel
                self.queue_message = await channel.send(embed=embeds[0], view=view)
        except discord.errors.HTTPException as e:
            if e.status == 401:
                logging.error("Invalid Webhook Token. Unable to edit queue message.")
                self.queue_message = None
            else:
                raise

    async def ensure_queue_message_valid(self):
        if self.queue_message:
            try:
                await self.queue_message.edit(content="Queue updated.")
            except discord.NotFound:
                self.queue_message = None
        await self.update_queue_message()

    async def update_now_playing_embed(self, interaction=None):
        ctx = self._ctx
        if self.current is None:
            return  # Exit if there is no current song
        embed = self.current.create_embed()
        if self.action_message:
            embed.add_field(name="Action:", value=self.action_message, inline=False)
        try:
            if self.now_playing_message:
                channel = self.now_playing_message.channel if self.now_playing_message else (self.text_channel or ctx.channel)
                self.now_playing_message = await channel.fetch_message(self.now_playing_message.id)  # Re-fetch the message
                await self.now_playing_message.edit(embed=embed, view=NowPlayingButtons(ctx))
            else:
                channel = self.text_channel or ctx.channel
                self.now_playing_message = await channel.send(embed=embed, view=NowPlayingButtons(ctx))
        except discord.errors.HTTPException as e:
            logging.error(f"Failed to edit message: {e}")
            channel = self.text_channel or ctx.channel
            self.now_playing_message = await channel.send(embed=embed, view=NowPlayingButtons(ctx))
        # Clear the action message after updating the embed
        self.action_message = ""

    async def inactivity_timer(self):
        logging.info("Inactivity timer started.")
        while self.exists:
            await asyncio.sleep(1800)  # 30 minutes

            # Check if there are no songs in the queue and nothing is currently playing
            if not self.is_playing and self.songs.qsize() == 0:
                # Also check if the bot is connected to a voice channel
                if self.voice is not None:
                    channel = self.text_channel or self._ctx.channel
                    if channel:
                        await channel.send("Leaving voice channel due to inactivity.")
                    await self.stop()
                    logging.info("Inactivity timer ended: Bot stopped due to inactivity.")
                else:
                    logging.info("Inactivity timer ended: Bot was not connected to a voice channel, no need to stop.")
            else:
                logging.info("Inactivity timer refreshed: Bot is active, resetting inactivity timer.")
