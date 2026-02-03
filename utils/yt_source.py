import yt_dlp as youtube_dl
import discord
from discord.ext import commands
from collections import defaultdict
import asyncio
import functools
from typing import Any, Dict, Optional, List


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


def _build_header_arg(headers: Dict[str, str]) -> str:
    """Build an ffmpeg -headers argument value.

    FFmpeg expects a single string containing CRLF-separated header lines.
    """
    lines: List[str] = []
    for k, v in headers.items():
        if v is None:
            continue
        k = str(k).strip()
        if not k:
            continue
        lines.append(f"{k}: {str(v).strip()}")

    # CRLF per ffmpeg docs
    return "\\r\\n".join(lines) + "\\r\\n"


def _ffmpeg_before_options(base_before: str, info: Dict[str, Any], webpage_url: str) -> str:
    """Build FFmpeg before_options with yt-dlp's http_headers.

    On some networks (especially VPS/datacenter IPs), googlevideo can return 403
    unless the request carries the same headers yt-dlp used to obtain the URL.

    We pass headers via FFmpeg's -headers, plus -user_agent/-referer when present.
    """
    http_headers: Dict[str, str] = dict(info.get("http_headers") or {})

    # Ensure referer/origin are present (helps for some edge cases)
    if webpage_url:
        http_headers.setdefault("Referer", webpage_url)
        http_headers.setdefault("Origin", "https://www.youtube.com")

    # Prefer explicit UA passed via -user_agent, and remove from -headers to avoid duplicates
    user_agent = http_headers.pop("User-Agent", None) or http_headers.pop("User-agent", None)

    # FFmpeg's -referer is redundant if Referer is in headers, but keep it anyway
    referer = http_headers.get("Referer")

    before_parts: List[str] = []
    if user_agent:
        ua_escaped = str(user_agent).replace('"', '\\"')
        before_parts.append(f'-user_agent "{ua_escaped}"')

    if referer:
        ref_escaped = str(referer).replace('"', '\\"')
        before_parts.append(f'-referer "{ref_escaped}"')

    if http_headers:
        hdr_val = _build_header_arg(http_headers)
        hdr_escaped = hdr_val.replace('"', '\\"')
        before_parts.append(f'-headers "{hdr_escaped}"')

    base_before = (base_before or "").strip()
    if base_before:
        before_parts.append(base_before)

    return " ".join(before_parts).strip()


class YTDLSource(discord.PCMVolumeTransformer):
    """YouTube audio source wrapper.

    Notes:
    - The direct stream URL returned by yt-dlp is temporary.
    - Some hosts return 403 unless FFmpeg sends appropriate headers.
    - We refresh the stream URL + headers right before playback with regather_stream().
    """

    YTDL_OPTIONS: Dict[str, Any] = {
        "format": "bestaudio/best",
        "extractaudio": True,
        "audioformat": "mp3",
        "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
        "restrictfilenames": True,
        "noplaylist": False,
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "logtostderr": False,
        "quiet": True,
        "no_warnings": True,
        "default_search": "auto",
        "source_address": "0.0.0.0",
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }

    FFMPEG_OPTIONS: Dict[str, str] = {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 15000000",
        "options": "-vn -af equalizer=f=40:width_type=h:width=30:g=6,equalizer=f=80:width_type=h:width=30:g=4",
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    # Cache only the *webpage_url(s)* for search queries.
    _search_cache: Dict[str, List[str]] = defaultdict(list)

    def __init__(
        self,
        ctx: commands.Context,
        source: discord.FFmpegPCMAudio,
        *,
        data: Dict[str, Any],
        volume: float = 0.5,
    ):
        super().__init__(source, volume)
        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get("uploader")
        self.uploader_url = data.get("uploader_url")
        self.title = data.get("title")
        self.thumbnail = data.get("thumbnail")
        self.description = data.get("description")
        self.duration = self.parse_duration(int(data.get("duration") or 0))
        self.tags = data.get("tags")

        # Stable URL for re-gathering.
        self.url = data.get("webpage_url")
        # Temporary direct stream URL used by FFmpeg.
        self.stream_url = data.get("url")
        # Headers yt-dlp used when generating the stream URL
        self.http_headers = dict(data.get("http_headers") or {})

    def __str__(self):
        return "**{0.title}** by **{0.uploader}**".format(self)

    @classmethod
    async def create_source(
        cls,
        ctx: commands.Context,
        search: str,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        loop = loop or asyncio.get_event_loop()

        # 1) Resolve query -> webpage URLs (cacheable)
        if search in cls._search_cache and cls._search_cache[search]:
            webpage_urls = cls._search_cache[search]
        else:
            partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
            data = await loop.run_in_executor(None, partial)
            if data is None:
                raise YTDLError(f"Couldn't find anything that matches `{search}`")

            if "entries" in data and data["entries"]:
                webpage_urls = [e["webpage_url"] for e in data["entries"] if e and e.get("webpage_url")]
            else:
                webpage_urls = [data.get("webpage_url")] if data.get("webpage_url") else []

            if not webpage_urls:
                raise YTDLError(f"Couldn't find anything that matches `{search}`")

            cls._search_cache[search] = webpage_urls

        # 2) Process each webpage URL into a playable source
        sources: List[YTDLSource] = []
        for url in webpage_urls:
            partial = functools.partial(cls.ytdl.extract_info, url, download=False)
            info = await loop.run_in_executor(None, partial)
            if info is None:
                raise YTDLError(f"Couldn't fetch `{url}`")

            if "entries" in info and info["entries"]:
                for entry in info["entries"]:
                    if not entry:
                        continue
                    before = _ffmpeg_before_options(
                        cls.FFMPEG_OPTIONS.get("before_options", ""),
                        entry,
                        entry.get("webpage_url") or url,
                    )
                    ffmpeg_opts = dict(cls.FFMPEG_OPTIONS)
                    ffmpeg_opts["before_options"] = before
                    sources.append(cls(ctx, discord.FFmpegPCMAudio(entry["url"], **ffmpeg_opts), data=entry))
            else:
                before = _ffmpeg_before_options(
                    cls.FFMPEG_OPTIONS.get("before_options", ""),
                    info,
                    info.get("webpage_url") or url,
                )
                ffmpeg_opts = dict(cls.FFMPEG_OPTIONS)
                ffmpeg_opts["before_options"] = before
                sources.append(cls(ctx, discord.FFmpegPCMAudio(info["url"], **ffmpeg_opts), data=info))

        return sources

    @classmethod
    async def regather_stream(
        cls,
        ctx: commands.Context,
        source: "YTDLSource",
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> "YTDLSource":
        """Refresh the expiring stream URL + headers right before playback."""
        loop = loop or asyncio.get_event_loop()

        if not getattr(source, "url", None):
            return source

        partial = functools.partial(cls.ytdl.extract_info, source.url, download=False)
        info = await loop.run_in_executor(None, partial)
        if info is None:
            raise YTDLError(f"Couldn't regather `{source.url}`")

        if "entries" in info and info["entries"]:
            info = next((e for e in info["entries"] if e), None) or info

        before = _ffmpeg_before_options(cls.FFMPEG_OPTIONS.get("before_options", ""), info, source.url)
        ffmpeg_opts = dict(cls.FFMPEG_OPTIONS)
        ffmpeg_opts["before_options"] = before

        refreshed = cls(
            ctx,
            discord.FFmpegPCMAudio(info["url"], **ffmpeg_opts),
            data=info,
            volume=source.volume,
        )
        return refreshed

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration_parts = []
        if days > 0:
            duration_parts.append(f"{days} days")
        if hours > 0:
            duration_parts.append(f"{hours} hours")
        if minutes > 0:
            duration_parts.append(f"{minutes} minutes")
        if seconds > 0:
            duration_parts.append(f"{seconds} seconds")
        return ", ".join(duration_parts)


class Song:
    __slots__ = ("source", "requester")

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (
            discord.Embed(
                title="Now Playing",
                description="```css\n{0.source.title}\n```".format(self),
                color=discord.Color.blue(),
            )
            .add_field(name="Duration", value=self.source.duration)
            .add_field(name="Played by", value=self.requester.mention)
            .set_thumbnail(url=self.source.thumbnail)
        )
        return embed
