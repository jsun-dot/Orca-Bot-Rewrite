import discord
from discord.ext import commands
from datetime import datetime
import spotipy
import math
import asyncio
import time
from collections import defaultdict
import requests
import os
from pathlib import Path
from spotipy.oauth2 import SpotifyClientCredentials
import sys

# Ensure project root is on sys.path so absolute imports like `utils.*` work reliably.
# (This helps when running via VS Code debugger/terminal where CWD/PYTHONPATH may differ.)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from utils.voice_state import VoiceState
from utils.views import QueuePages, ClearQueueConfirmation
from utils.yt_source import YTDLSource, Song, YTDLError

import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

 # Spotify credentials (set via environment variables or a local .env file)
# Required env vars:
#   SPOTIPY_CLIENT_ID
#   SPOTIPY_CLIENT_SECRET
try:
    # Optional: load variables from a local .env file if python-dotenv is installed.
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:
    # If python-dotenv isn't installed, we just rely on the process environment.
    pass

SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")

if not SPOTIPY_CLIENT_ID or not SPOTIPY_CLIENT_SECRET:
    raise RuntimeError(
        "Missing Spotify credentials. Set SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET in your environment (or a .env file in the project root)."
    )

sp = spotipy.Spotify(
    auth_manager=SpotifyClientCredentials(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
    ),
    # Spotipy expects a requests.Session (not a boolean). Using a real session also reduces overhead.
    requests_session=requests.Session(),
)

# Delay between YouTube source resolutions when adding Spotify playlists.
SPOTIFY_PLAYLIST_RESOLVE_DELAY_SEC = 20


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states: dict[int, VoiceState] = {}
        self.processing_playlists: set[int] = set()  # guild ids currently processing a Spotify playlist
        self._playlist_locks = defaultdict(asyncio.Lock)  # per-guild lock to avoid interleaving playlist enqueues

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state or not state.exists:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state
        ctx.voice_state = state
        # IMPORTANT: keep VoiceState.voice in sync with discord.py's ctx.voice_client
        if ctx.voice_client and (state.voice is None or state.voice != ctx.voice_client):
            state.voice = ctx.voice_client
        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage("This command can't be used in DM channels.")
        return True

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send(f"An error occurred: {error}")

    async def ensure_voice_state(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError("You are not connected to any voice channel.")

        # If bot is already in a voice channel, enforce same channel.
        if ctx.voice_client and ctx.voice_client.channel != ctx.author.voice.channel:
            raise commands.CommandError("Orca is already in a voice channel.")

    async def _maybe_defer(self, ctx: commands.Context):
        # For hybrid/slash invocations: prevent "interaction failed" when doing network-heavy work.
        inter = getattr(ctx, "interaction", None)
        if inter and not inter.response.is_done():
            try:
                await ctx.defer(thinking=True)
            except Exception:
                pass

    async def _ensure_connected(self, ctx: commands.Context, destination: discord.VoiceChannel):
        # Prefer discord.py's managed voice_client when available.
        if ctx.voice_client:
            if not ctx.voice_client.is_connected():
                try:
                    await ctx.voice_client.disconnect(force=True)
                except Exception:
                    pass
                ctx.voice_state.voice = await destination.connect(reconnect=True)
            else:
                ctx.voice_state.voice = ctx.voice_client
            return ctx.voice_state.voice

        # No ctx.voice_client: use our stored one (may be stale after transient disconnects)
        if ctx.voice_state.voice and ctx.voice_state.voice.is_connected():
            return ctx.voice_state.voice

        # Connect fresh
        ctx.voice_state.voice = await destination.connect(reconnect=True)
        return ctx.voice_state.voice

    @commands.hybrid_command(name="join", description="Join a voice channel.", invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context) -> None:
        await self.ensure_voice_state(ctx)
        await self._maybe_defer(ctx)

        destination = ctx.author.voice.channel
        await self._ensure_connected(ctx, destination)

        if ctx.voice_state.voice.channel == destination:
            await ctx.send("I joined your voice channel." if not ctx.voice_client else "I am already in your voice channel.")
        else:
            await ctx.voice_state.voice.move_to(destination)
            await ctx.send("I moved to your voice channel.")

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        log.info('%s - %s used join in "%s" (%s)', timestamp, ctx.author, ctx.guild.name, ctx.guild.id)

    @commands.hybrid_command(name="leave", description="Leave the voice channel.", aliases=["disconnect"])
    async def _leave(self, ctx: commands.Context) -> None:
        await self.ensure_voice_state(ctx)
        await self._maybe_defer(ctx)

        if not (ctx.voice_state.voice or ctx.voice_client):
            return await ctx.send("Not connected to a voice channel.")

        await ctx.voice_state.stop()
        self.voice_states.pop(ctx.guild.id, None)

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        log.info('%s - %s used leave in "%s" (%s)', timestamp, ctx.author, ctx.guild.name, ctx.guild.id)
        await ctx.send("Left the voice channel.")

    @commands.hybrid_command(name="display", description="Displays the currently playing song.", aliases=["current", "playing"])
    async def _now(self, ctx: commands.Context) -> None:
        await self.ensure_voice_state(ctx)
        await self._maybe_defer(ctx)

        if not ctx.voice_state.current:
            return await ctx.send("Nothing is playing right now.")
        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.hybrid_command(name="pause", description="Pauses the audio.")
    @commands.has_permissions(manage_guild=True)
    async def _pause(self, ctx: commands.Context) -> None:
        await self.ensure_voice_state(ctx)
        await self._maybe_defer(ctx)

        player = ctx.voice_client
        if player and player.is_playing():
            player.pause()
            await ctx.send("I paused the audio.")
        else:
            await ctx.send("No audio is playing currently.")

    @commands.hybrid_command(name="resume", description="Resume the audio")
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context) -> None:
        await self.ensure_voice_state(ctx)
        await self._maybe_defer(ctx)

        player = ctx.voice_client
        if player and player.is_paused():
            player.resume()
            await ctx.send("I resumed the audio.")
        else:
            await ctx.send("The audio is already playing.")

    @commands.hybrid_command(name="skip", description="Skips the currently playing audio.")
    async def _skip(self, ctx: commands.Context) -> None:
        await self.ensure_voice_state(ctx)
        await self._maybe_defer(ctx)

        if not ctx.voice_state.is_playing:
            return await ctx.send("No audio is playing.")
        ctx.voice_state.skip()
        await ctx.send("Skipped the audio.")

    @commands.hybrid_command(
        name="queue",
        description="Shows the queue. You can optionally specify the page to show. Each page contains 10 elements.",
    )
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        await self.ensure_voice_state(ctx)
        await self._maybe_defer(ctx)

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("Empty queue.")

        items_per_page = 10
        pages = max(1, math.ceil(len(ctx.voice_state.songs) / items_per_page))

        requested_page = max(1, min(page, pages))  # clamp
        embeds = []

        for p in range(pages):
            queue = ""
            for i, song in enumerate(
                ctx.voice_state.songs[p * items_per_page : (p + 1) * items_per_page],
                start=p * items_per_page,
            ):
                queue += "`{0}.` [**{1.source.title}**]({1.source.url})\n".format(i + 1, song)
            embed = (
                discord.Embed(description="**{} track(s):**\n\n{}".format(len(ctx.voice_state.songs), queue))
                .set_footer(text="Viewing page {}/{}".format(p + 1, pages))
            )
            embeds.append(embed)

        view = QueuePages(ctx, embeds, current_page=requested_page - 1)

        if ctx.voice_state.queue_message:
            await ctx.voice_state.queue_message.edit(embed=embeds[requested_page - 1], view=view)
        else:
            ctx.voice_state.queue_message = await ctx.send(embed=embeds[requested_page - 1], view=view)

    @commands.hybrid_command(name="clear", description="Clears the queue.")
    async def _clear(self, ctx: commands.Context):
        await self.ensure_voice_state(ctx)
        await self._maybe_defer(ctx)

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("The queue is already empty.")

        await ctx.send("Are you sure you want to clear the queue?", view=ClearQueueConfirmation(ctx, ctx.voice_state))

    @commands.hybrid_command(name="shuffle", description="Shuffles the queue.")
    async def _shuffle(self, ctx: commands.Context):
        await self.ensure_voice_state(ctx)
        await self._maybe_defer(ctx)

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("Empty queue.")

        ctx.voice_state.songs.shuffle()
        await ctx.voice_state.update_queue_message()
        await ctx.send("I shuffled the queue.")

    @commands.hybrid_command(name="remove", description="Removes audio from the queue at a given index.")
    async def _remove(self, ctx: commands.Context, index: int):
        await self.ensure_voice_state(ctx)
        await self._maybe_defer(ctx)

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("Empty queue.")

        ctx.voice_state.songs.remove(index - 1)
        await ctx.voice_state.update_queue_message()
        await ctx.send("Successfully removed from the queue.")

    @commands.hybrid_command(name="play", description="Plays audio.")
    async def _play(self, ctx: commands.Context, *, search: str):
        await self.ensure_voice_state(ctx)
        await self._maybe_defer(ctx)

        destination = ctx.author.voice.channel

        # Key fix: sync voice client + only connect when truly disconnected.
        await self._ensure_connected(ctx, destination)

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        log.info('%s - %s used play in "%s" (%s): %s', timestamp, ctx.author, ctx.guild.name, ctx.guild.id, search)

        try:
            async with ctx.typing():
                # Spotify playlist
                if "spotify.com/playlist" in search:
                    await self.play_spotify_playlist(ctx, search)
                    await ctx.send("Your Spotify playlist has been added to the queue.")
                    return

                # Clean search string
                search = search.replace(":", "")

                sources = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
                if not sources:
                    await ctx.send("No results found.")
                    return

                # Enqueue
                async with ctx.voice_state.lock:
                    if len(sources) > 1:
                        for source in sources:
                            await ctx.voice_state.songs.put(Song(source))
                        ctx.voice_state.action_message = f"{ctx.author.display_name} added a playlist to the queue."
                    else:
                        song = Song(sources[0])
                        await ctx.voice_state.songs.put(song)
                        ctx.voice_state.action_message = f"{ctx.author.display_name} added {song.source.title} by {song.source.uploader}."

                # Best-effort UI refresh (won't spam if nothing is playing yet)
                await ctx.voice_state.update_now_playing_embed()

            await ctx.send("Your request has been added to the queue.")

        except YTDLError as e:
            log.error("YTDLError: %s", e)
            await ctx.send(f"An error occurred while processing this request: {e}")
        except Exception as e:
            log.exception("Unexpected error in play: %s", e)
            await ctx.send(f"An unexpected error occurred: {e}")

    async def _fetch_spotify_playlist(self, playlist_id: str):
        """Fetch Spotify playlist name + tracks in a thread (Spotipy is blocking)."""
        def _run():
            playlist = sp.playlist(playlist_id)
            name = playlist.get("name", "Spotify playlist")

            tracks = []
            results = sp.playlist_tracks(playlist_id)

            while True:
                for item in results.get("items", []):
                    t = item.get("track")
                    if not t:
                        continue
                    # Skip local/unsupported items
                    if t.get("is_local"):
                        continue
                    artists = t.get("artists") or []
                    artist_name = artists[0].get("name") if artists else ""
                    tracks.append((t.get("name") or "", artist_name))
                if results.get("next"):
                    results = sp.next(results)
                else:
                    break
            return name, tracks

        return await asyncio.to_thread(_run)

    async def play_spotify_playlist(self, ctx: commands.Context, url: str):
        guild_id = ctx.guild.id
        playlist_id = url.split("/")[-1].split("?")[0]

        # Prevent overlapping playlist processing from interleaving queue operations.
        async with self._playlist_locks[guild_id]:
            self.processing_playlists.add(guild_id)
            try:
                # Still connected?
                vc = ctx.voice_client or ctx.voice_state.voice
                if not vc or not vc.is_connected():
                    await ctx.send("I'm not connected to a voice channel.")
                    return

                playlist_name, tracks = await self._fetch_spotify_playlist(playlist_id)

                loading_message = await ctx.send(
                    embed=discord.Embed(
                        description=f"Adding songs from the Spotify playlist **{playlist_name}**... :arrows_counterclockwise:",
                        color=discord.Color.orange(),
                    )
                )

                # Resolve tracks in chunks with bounded concurrency to reduce CPU/network spikes.
                sem = asyncio.Semaphore(2)
                rate_limit_lock = asyncio.Lock()
                last_request = 0.0
                last_edit = 0.0

                async def resolve_one(idx: int, name: str, artist: str):
                    query = f"{name} {artist} Audio".replace(":", "")
                    async with sem:
                        nonlocal last_request
                        async with rate_limit_lock:
                            now = time.monotonic()
                            wait = max(0.0, SPOTIFY_PLAYLIST_RESOLVE_DELAY_SEC - (now - last_request))
                            if wait:
                                await asyncio.sleep(wait)
                            last_request = time.monotonic()
                        try:
                            sources = await YTDLSource.create_source(ctx, query, loop=self.bot.loop)
                            return idx, sources, None
                        except Exception as e:
                            return idx, None, e

                # Process in batches to keep memory bounded for large playlists.
                batch_size = 10
                total = len(tracks)
                added = 0

                for start in range(0, total, batch_size):
                    vc = ctx.voice_client or ctx.voice_state.voice
                    if not vc or not vc.is_connected():
                        try:
                            await loading_message.edit(
                                embed=discord.Embed(
                                    description="Bot disconnected from the voice channel. Stopping playlist processing.",
                                    color=discord.Color.red(),
                                )
                            )
                        except Exception:
                            pass
                        return

                    batch = tracks[start : start + batch_size]
                    tasks = [resolve_one(start + i, n, a) for i, (n, a) in enumerate(batch)]
                    results = await asyncio.gather(*tasks)

                    # Enqueue in Spotify order (stable).
                    results.sort(key=lambda x: x[0])

                    async with ctx.voice_state.lock:
                        for idx, sources, err in results:
                            if err or not sources:
                                # Don't spam a message for every error; log instead.
                                log.warning("Failed to resolve Spotify track %s/%s: %s", idx + 1, total, err)
                                continue
                            if len(sources) > 1:
                                for src in sources:
                                    await ctx.voice_state.songs.put(Song(src))
                                    added += 1
                            else:
                                await ctx.voice_state.songs.put(Song(sources[0]))
                                added += 1

                    # Throttle message edits to avoid rate limits.
                    now = time.monotonic()
                    if now - last_edit > 2.5:
                        last_edit = now
                        try:
                            await loading_message.edit(
                                embed=discord.Embed(
                                    description=f"Adding songs from the Spotify playlist **{playlist_name}**... ({min(start + batch_size, total)}/{total}) :arrows_counterclockwise:",
                                    color=discord.Color.orange(),
                                )
                            )
                        except Exception:
                            pass

                    # Update queue message occasionally, not per track.
                    if ctx.voice_state.first_song_played and (start % (batch_size * 3) == 0):
                        try:
                            await ctx.voice_state.update_queue_message()
                        except Exception:
                            pass

                # Final UI refresh
                ctx.voice_state.action_message = f"{ctx.author.display_name} added Spotify playlist **{playlist_name}** ({added} tracks)."
                try:
                    await ctx.voice_state.update_queue_message()
                except Exception:
                    pass

                try:
                    await loading_message.edit(
                        embed=discord.Embed(
                            description=f"Added **{added}** track(s) from Spotify playlist **{playlist_name}**.",
                            color=discord.Color.green(),
                        )
                    )
                except Exception:
                    pass

            finally:
                self.processing_playlists.discard(guild_id)

    async def check_voice_state(self, ctx: commands.Context):
        if not ctx.voice_state.voice:
            ctx.voice_state.songs.clear()
            ctx.voice_state.exists = False


async def setup(bot):
    await bot.add_cog(Music(bot))
