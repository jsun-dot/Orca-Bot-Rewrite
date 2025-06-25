import asyncio
import discord
from discord.ext import commands
from datetime import datetime
import gc
import spotipy
import math
from spotipy.oauth2 import SpotifyClientCredentials
from utils.voice_state import VoiceState
from utils.views import QueuePages, ClearQueueConfirmation, NowPlayingButtons
from utils.yt_source import YTDLSource, Song, YTDLError
import logging

logging.basicConfig(level=logging.INFO)

# Spotify credentials
SPOTIPY_CLIENT_ID = '1218867043e641698fcdf1293d576357'
SPOTIPY_CLIENT_SECRET = '0c9769659178485fabc11a375c272e73'

sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=SPOTIPY_CLIENT_ID,
    client_secret=SPOTIPY_CLIENT_SECRET,
    requests_session=True,  # Ensure requests_session is properly managed
))

class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state or not state.exists:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state
        ctx.voice_state = state  # Add this line to attach voice_state to ctx
        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage('This command can\'t be used in DM channels.')
        return True

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send('An error occurred: {}'.format(str(error)))

    async def ensure_voice_state(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError('You are not connected to any voice channel.')
        if ctx.voice_client:
            if not ctx.voice_client.is_connected():
                await ctx.voice_client.disconnect(force=True)
            elif ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError('Bot is already in a voice channel.')

    @commands.hybrid_command(name='join', description='Join a voice channel.', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context) -> None:
        await self.ensure_voice_state(ctx)
        destination = ctx.author.voice.channel
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        server_name = ctx.guild.name
        server_id = ctx.guild.id
        user_hash = ctx.author.discriminator

        # Attempt to force-disconnect stale connection if it exists
        try:
            if ctx.voice_state.voice:
                await ctx.voice_state.voice.disconnect(force=True)
        except Exception as e:
            logging.warning(f"Failed to force-disconnect stale voice client: {e}")

        # Try to reconnect with timeout
        try:
            ctx.voice_state.voice = await asyncio.wait_for(
                destination.connect(reconnect=True),
                timeout=15
            )
            await ctx.send("I joined your voice channel.")
            logging.info(f"Bot connected to the voice channel: {destination.name}")
        except asyncio.TimeoutError:
            logging.error("Voice connection timed out.")
            return await ctx.send("Failed to connect to the voice channel (timeout).")
        except discord.ClientException as e:
            logging.error(f"Voice connection failed: {e}")
            return await ctx.send("Failed to connect to the voice channel.")

        print(f'{timestamp} - {ctx.author.display_name}#{user_hash} used the join command in server \"{server_name}\" ({server_id})')

    @commands.hybrid_command(name='leave', description='Leave the voice channel.', aliases=['disconnect'])
    async def _leave(self, ctx: commands.Context) -> None:
        await self.ensure_voice_state(ctx)
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        server_name = ctx.guild.name
        server_id = ctx.guild.id
        user_hash = ctx.author.discriminator
        if not ctx.voice_state.voice:
            return await ctx.send('Not connected to a voice channel.')
        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]
        print(f'{timestamp} - {ctx.author.display_name}#{user_hash} used the leave command in server "{server_name}" ({server_id})')
        await ctx.send("Left the voice channel.")

    @commands.hybrid_command(name='display', description='Displays the currently playing song.', aliases=['current', 'playing'])
    async def _now(self, ctx: commands.Context) -> None:
        await self.ensure_voice_state(ctx)
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        server_name = ctx.guild.name
        server_id = ctx.guild.id
        user_hash = ctx.author.discriminator
        await ctx.send(embed=ctx.voice_state.current.create_embed())
        print(f'{timestamp} - {ctx.author.display_name}#{user_hash} used the display command in server "{server_name}" ({server_id})')

    @commands.hybrid_command(name='pause', description='Pauses the audio.')
    @commands.has_permissions(manage_guild=True)
    async def _pause(self, ctx: commands.Context) -> None:
        await self.ensure_voice_state(ctx)
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        server_name = ctx.guild.name
        server_id = ctx.guild.id
        user_hash = ctx.author.discriminator
        player = ctx.voice_client
        if player.is_playing():
            player.pause()
            print(f'{timestamp} - {ctx.author.display_name}#{user_hash} used the pause command in server "{server_name}" ({server_id})')
            await ctx.send("I paused the audio.")
        else:
            await ctx.send('No audio is playing currently.')

    @commands.hybrid_command(name='resume', description='Resume the audio')
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context) -> None:
        await self.ensure_voice_state(ctx)
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        server_name = ctx.guild.name
        server_id = ctx.guild.id
        user_hash = ctx.author.discriminator
        player = ctx.voice_client
        if player.is_paused():
            player.resume()
            print(f'{timestamp} - {ctx.author.display_name}#{user_hash} used the resume command in server "{server_name}" ({server_id})')
            await ctx.send("I resumed the audio.")
        else:
            await ctx.send('The audio is already playing.')

    @commands.hybrid_command(name='skip', description='Skips the currently playing audio.')
    async def _skip(self, ctx: commands.Context) -> None:
        await self.ensure_voice_state(ctx)
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        server_name = ctx.guild.name
        server_id = ctx.guild.id
        user_hash = ctx.author.discriminator
        if not ctx.voice_state.is_playing:
            return await ctx.send('No audio is playing.')
        else:
            await ctx.send("Skipped the audio.")
            print(f'{timestamp} - {ctx.author.display_name}#{user_hash} used the skip command in server "{server_name}" ({server_id})')
            ctx.voice_state.skip()

    @commands.hybrid_command(name='queue', description='Shows the queue. You can optionally specify the page to show. Each page contains 10 elements.')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        await self.ensure_voice_state(ctx)
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        server_name = ctx.guild.name
        server_id = ctx.guild.id
        user_hash = ctx.author.discriminator
        if len(ctx.voice_state.songs) == 0:
            print(f'{timestamp} - {ctx.author.display_name}#{user_hash} used the queue command in server "{server_name}" ({server_id})')
            return await ctx.send('Empty queue.')

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)
        start = (page - 1) * items_per_page
        end = start + items_per_page
        embeds = []

        for page in range(pages):
            queue = ''
            for i, song in enumerate(ctx.voice_state.songs[page * items_per_page:(page + 1) * items_per_page], start=page * items_per_page):
                queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)
            embed = (discord.Embed(description='**{} track(s):**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                    .set_footer(text='Viewing page {}/{}'.format(page + 1, pages)))
            embeds.append(embed)

        view = QueuePages(ctx, embeds, current_page=page - 1)

        if ctx.voice_state.queue_message:
            await ctx.voice_state.queue_message.edit(embed=embeds[page - 1], view=view)
        else:
            ctx.voice_state.queue_message = await ctx.send(embed=embeds[page - 1], view=view)

    @commands.hybrid_command(name='clear', description='Clears the queue.')
    async def _clear(self, ctx: commands.Context):
        await self.ensure_voice_state(ctx)
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        server_name = ctx.guild.name
        server_id = ctx.guild.id
        user_hash = ctx.author.discriminator
        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('The queue is already empty.')
        confirmation_message = await ctx.send("Are you sure you want to clear the queue?", view=ClearQueueConfirmation(ctx, ctx.voice_state))
        print(f'{timestamp} - {ctx.author.display_name}#{user_hash} used the clear command in server "{server_name}" ({server_id})')

    @commands.hybrid_command(name='shuffle', description='Shuffles the queue.')
    async def _shuffle(self, ctx: commands.Context):
        await self.ensure_voice_state(ctx)
        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')
        ctx.voice_state.songs.shuffle()
        await ctx.voice_state.update_queue_message()
        await ctx.send("I shuffled the queue.")
        gc.collect()

    @commands.hybrid_command(name='remove', description='Removes audio from the queue at a given index.')
    async def _remove(self, ctx: commands.Context, index: int):
        await self.ensure_voice_state(ctx)
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        server_name = ctx.guild.name
        server_id = ctx.guild.id
        user_hash = ctx.author.discriminator
        if len(ctx.voice_state.songs) == 0:
            print(f'{timestamp} - {ctx.author.display_name}#{user_hash} used the remove command in server "{server_name}" ({server_id})')
            return await ctx.send('Empty queue.')
        ctx.voice_state.songs.remove(index - 1)
        await ctx.voice_state.update_queue_message()
        await ctx.send('Successfully removed from the queue.')
        print(f'{timestamp} - {ctx.author.display_name}#{user_hash} used the remove command in server "{server_name}" ({server_id})')

    @commands.hybrid_command(name='play', description='Plays audio.')
    async def _play(self, ctx: commands.Context, *, search: str):
        if ctx.interaction:
            try:
                await ctx.interaction.response.defer()
            except discord.NotFound:
                logging.warning("Interaction expired before defer.")
                return await ctx.send("Interaction expired. Please try again.")

        await self.ensure_voice_state(ctx)
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

        # Only connect if not already connected
        if not ctx.voice_state.voice or not ctx.voice_state.voice.is_connected():
            try:
                ctx.voice_state.voice = await asyncio.wait_for(
                    ctx.author.voice.channel.connect(reconnect=True),
                    timeout=15
                )
                logging.info(f"Connected to voice channel: {ctx.author.voice.channel.name}")
            except Exception as e:
                logging.error(f"Failed to connect to voice: {e}")
                return await ctx.send("Failed to connect to the voice channel.")

        try:
            logging.info(f"{timestamp} - {ctx.author.display_name}#{ctx.author.discriminator} used play with: {search}")
            if 'spotify.com/playlist' in search:
                await self.play_spotify_playlist(ctx, search)
            else:
                search = search.replace(":", "")
                sources = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)

                if not sources:
                    return await ctx.send("No results found.")

                if len(sources) > 1:
                    for source in sources:
                        await ctx.voice_state.songs.put(Song(source))
                    added_message = f'{ctx.author.display_name} added a playlist.'
                else:
                    song = Song(sources[0])
                    await ctx.voice_state.songs.put(song)
                    added_message = f'{ctx.author.display_name} added {song.source.title} by {song.source.uploader}.'

                ctx.voice_state.action_message = added_message

                # Start audio player task
                if not hasattr(ctx.voice_state, 'audio_player') or ctx.voice_state.audio_player.done():
                    ctx.voice_state.audio_player = self.bot.loop.create_task(ctx.voice_state.audio_player_task())

                await ctx.voice_state.update_now_playing_embed()

            await ctx.send("Your request has been added to the queue.")
        except YTDLError as e:
            await ctx.send(f"Error processing the request: {str(e)}")
            logging.error(f"YTDLError: {e}")
        except Exception as e:
            await ctx.send(f"An unexpected error occurred: {str(e)}")
            logging.exception("Unexpected error in /play")

        gc.collect()
        
    async def play_spotify_playlist(self, ctx, url):
        playlist_id = url.split('/')[-1].split('?')[0]
        playlist = sp.playlist(playlist_id)
        results = sp.playlist_tracks(playlist_id)

        # Ensure clean connection
        try:
            if ctx.voice_state.voice:
                await ctx.voice_state.voice.disconnect(force=True)
        except Exception as e:
            logging.warning(f"Failed to disconnect stale voice client: {e}")

        try:
            destination = ctx.author.voice.channel
            ctx.voice_state.voice = await asyncio.wait_for(
                destination.connect(reconnect=True),
                timeout=15
            )
            logging.info(f"Bot connected to voice for Spotify playlist: {destination.name}")
        except asyncio.TimeoutError:
            logging.error("Voice connection timed out for playlist.")
            await ctx.send("Failed to connect to the voice channel (timeout).")
            return
        except discord.ClientException as e:
            logging.error(f"Voice connection failed: {e}")
            await ctx.send("Failed to connect to the voice channel.")
            return

        self.processing_playlist = True

        loading_message = await ctx.send(embed=discord.Embed(
            description=f"Adding songs from the Spotify playlist **{playlist['name']}**... :arrows_counterclockwise:",
            color=discord.Color.orange()
        ))

        total_tracks = len(results['items'])
        for index, item in enumerate(results['items']):
            if not ctx.voice_state.voice and self.processing_playlist:
                ctx.voice_state.songs.clear()
                await loading_message.edit(embed=discord.Embed(
                    description=f"Bot disconnected from the voice channel. Stopping playlist processing.",
                    color=discord.Color.red()
                ))
                self.processing_playlist = False
                return

            track = item['track']
            query = f"{track['name']} {track['artists'][0]['name']} Audio".replace(":", "")

            try:
                source = await YTDLSource.create_source(ctx, query, loop=self.bot.loop)
                if len(source) > 1:
                    for src in source:
                        song = Song(src)
                        await ctx.voice_state.songs.put(song)
                else:
                    song = Song(source[0])
                    await ctx.voice_state.songs.put(song)
                await ctx.voice_state.update_queue_message()
            except YTDLError as e:
                await ctx.send(f'Error while processing: {track["name"]} by {track["artists"][0]["name"]}: {str(e)}')

            embed = discord.Embed(
                description=f"Adding songs from **{playlist['name']}**... ({index + 1}/{total_tracks}) :arrows_counterclockwise:",
                color=discord.Color.orange()
            )
            await loading_message.edit(embed=embed)

            await asyncio.sleep(15)  # Delay between each song to avoid rate-limiting
            gc.collect()

        # Start audio player if needed
        if not hasattr(ctx.voice_state, 'audio_player') or ctx.voice_state.audio_player.done():
            ctx.voice_state.audio_player = self.bot.loop.create_task(ctx.voice_state.audio_player_task())

        final_embed = discord.Embed(
            description=f"✅ All songs from **{playlist['name']}** have been added.",
            color=discord.Color.green()
        )
        await loading_message.edit(embed=final_embed)
        self.processing_playlist = False


async def setup(bot):
    await bot.add_cog(Music(bot))