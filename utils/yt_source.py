import yt_dlp as youtube_dl
import discord
from discord.ext import commands
from collections import defaultdict
import asyncio
import functools

class VoiceError(Exception):
    pass

class YTDLError(Exception):
    pass

class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': False,  # Allow playlists
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 15000000',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)
    cache = defaultdict(dict)  # Add a cache for YouTube data

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)
        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data
        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()
        if search in cls.cache:  # Check if search result is in cache
            data = cls.cache[search]
        else:
            partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
            data = await loop.run_in_executor(None, partial)
            if data is None:
                raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))
                cls.cache[search] = data  # Cache the result
        if 'entries' in data:
            entries = data['entries']
        else:
            entries = [data]

        sources = []
        for entry in entries:
            partial = functools.partial(cls.ytdl.extract_info, entry['webpage_url'], download=False)
            processed_info = await loop.run_in_executor(None, partial)
            if processed_info is None:
                raise YTDLError('Couldn\'t fetch `{}`'.format(entry['webpage_url']))

            if 'entries' in processed_info:
                for entry_info in processed_info['entries']:
                    source = cls(ctx, discord.FFmpegPCMAudio(entry_info['url'], **cls.FFMPEG_OPTIONS), data=entry_info)
                    sources.append(source)
            else:
                source = cls(ctx, discord.FFmpegPCMAudio(processed_info['url'], **cls.FFMPEG_OPTIONS), data=processed_info)
                sources.append(source)

        return sources

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)
        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))
        return ', '.join(duration)

class Song:
    __slots__ = ('source', 'requester')
    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='Now Playing',
                               description='```css\n{0.source.title}\n```'.format(self),
                               color=discord.Color.blue())
                 .add_field(name='Duration', value=self.source.duration)
                 .add_field(name='Played by', value=self.requester.mention)
                 .set_thumbnail(url=self.source.thumbnail))
        return embed