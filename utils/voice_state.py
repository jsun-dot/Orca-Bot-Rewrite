import discord
from discord.ext import commands
import asyncio
import math
import itertools
import random
from async_timeout import timeout
from datetime import datetime
import logging

from utils.views import QueuePages, NowPlayingButtons
from utils.yt_source import YTDLSource, Song, YTDLError, VoiceError

# Setup logging
logging.basicConfig(level=logging.INFO)


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
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
        # Use channel.send instead of ctx.send to avoid expired interaction webhooks.
        self.text_channel = ctx.channel

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

        self.inactivity_task = bot.loop.create_task(self.inactivity_timer())
        self.last_added_message = None
        self.lock = asyncio.Lock()
        self.last_activity = datetime.utcnow()

    async def add_song(self, song):
        await self.songs.put(song)
        await self.add_song_message(song)

        # If the audio task was cancelled for some reason, restart it.
        if not self.audio_player or self.audio_player.done():
            self.audio_player = self.bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        try:
            if self.audio_player and not self.audio_player.done():
                self.audio_player.cancel()
        except Exception:
            pass

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
        self._volume = max(0, min(1, new_volume))
        if self.current:
            self.current.source.volume = self._volume

        self.action_message = f"**{interaction.user.display_name} changed the volume to {int(self._volume * 100)}%**"
        await self.update_now_playing_embed(interaction)

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop or not self.current:
                try:
                    async with timeout(1800):
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

            # Refresh expiring stream URL right before playback.
            try:
                self.current.source = await YTDLSource.regather_stream(self._ctx, self.current.source, loop=self.bot.loop)
            except Exception as e:
                logging.warning(f"Failed to regather stream URL (will try old URL): {e}")

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            self.first_song_played = True
            await self.update_now_playing_embed()

            await self.next.wait()

            if self.loop and self.current:
                # Loop by replaying the current track without re-queuing it.
                logging.info("Looping the current song (replay without re-queue).")
            else:
                self.current = None

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
            try:
                await self.voice.disconnect()
            except Exception:
                pass
            self.voice = None

        self.exists = False

        if self.queue_message:
            try:
                await self.queue_message.edit(
                    content="Bot disconnected from the voice channel. Stopping playback.",
                    embed=None,
                    view=None,
                )
            except Exception:
                pass

    async def update_queue_message(self):
        if not self.first_song_played:
            return

        ctx = self._ctx
        items_per_page = 10
        pages = max(1, math.ceil(len(self.songs) / items_per_page))
        embeds = []

        if len(self.songs) == 0:
            embeds.append(discord.Embed(description='**Empty queue.**'))
        else:
            for page in range(pages):
                queue = ''
                for i, song in enumerate(
                    self.songs[page * items_per_page : (page + 1) * items_per_page],
                    start=page * items_per_page,
                ):
                    queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

                embed = (
                    discord.Embed(description='**{} track(s):**\n\n{}'.format(len(self.songs), queue))
                    .set_footer(text='Viewing page {}/{}'.format(page + 1, pages))
                )
                embeds.append(embed)

        view = QueuePages(ctx, embeds, current_page=0)
        channel = self.text_channel or ctx.channel

        try:
            if self.queue_message:
                await self.queue_message.edit(embed=embeds[0], view=view)
            else:
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
            return

        embed = self.current.create_embed()
        if self.action_message:
            embed.add_field(name="Action:", value=self.action_message, inline=False)

        channel = (self.now_playing_message.channel if self.now_playing_message else None) or self.text_channel or ctx.channel

        try:
            if self.now_playing_message:
                self.now_playing_message = await channel.fetch_message(self.now_playing_message.id)
                await self.now_playing_message.edit(embed=embed, view=NowPlayingButtons(ctx))
            else:
                self.now_playing_message = await channel.send(embed=embed, view=NowPlayingButtons(ctx))
        except discord.errors.HTTPException as e:
            logging.error(f"Failed to edit message: {e}")
            self.now_playing_message = await channel.send(embed=embed, view=NowPlayingButtons(ctx))

        self.action_message = ""

    async def inactivity_timer(self):
        logging.info("Inactivity timer started.")
        while self.exists:
            await asyncio.sleep(1800)  # 30 minutes

            # Reset inactivity timer on any activity tracked by other views/commands.
            if (datetime.utcnow() - self.last_activity).total_seconds() < 1800:
                continue

            # If there are no songs in the queue and nothing is currently playing.
            if not self.is_playing and self.songs.qsize() == 0:
                if self.voice is not None:
                    channel = self.text_channel or self._ctx.channel
                    if channel:
                        try:
                            await channel.send("Leaving voice channel due to inactivity.")
                        except Exception:
                            pass
                    await self.stop()
                    logging.info("Inactivity timer ended: Bot stopped due to inactivity.")
                else:
                    logging.info("Inactivity timer ended: Bot was not connected to a voice channel.")
            else:
                logging.info("Inactivity timer refreshed: Bot is active, resetting inactivity timer.")

    async def add_song_message(self, song: Song):
        # Optional helper: only send if we have a stable channel.
        channel = self.text_channel or self._ctx.channel
        if not channel:
            return

        try:
            await channel.send(f"Added to queue: **{song.source.title}**")
        except Exception:
            pass
