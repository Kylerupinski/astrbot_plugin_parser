import re
from asyncio import wait_for
from typing import ClassVar

import msgspec
from aiohttp import ClientError
from msgspec import Struct

from astrbot.api import logger

from ..config import PluginConfig
from ..cookie import CookieJar
from ..download import Downloader
from .base import BaseParser, Platform, handle


class YouTubeParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="youtube", display_name="油管")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.youtube
        self._author_cache: dict[str, tuple[str, str | None, str | None]] = {}
        if not self.mycfg.cookies:
            logger.warning("油管Cookie未配置，将无法解析相关媒体")
        self.headers.update({"Referer": "https://www.youtube.com/"})
        self.cookiejar = CookieJar(config, self.mycfg, domain="youtube.com")

    @handle("youtu", r"youtu\.be/[A-Za-z\d\._\?%&\+\-=/#]+")
    @handle(
        "youtube",
        r"youtube\.com/(?:watch|shorts)(?:/[A-Za-z\d_\-]+|\?v=[A-Za-z\d_\-]+)",
    )
    async def _parse_video(self, searched: re.Match[str]):
        return await self.parse_video(searched)

    async def parse_video(self, searched: re.Match[str]):
        # 从匹配对象中获取原始URL
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        video_info = await self.downloader.ytdlp_extract_info(
            url,
            cookiefile=self.cookiejar.cookie_file,
            headers=self.headers,
            proxy=self.proxy,
        )
        author = await self._resolve_author(
            video_info.channel_id,
            fallback_name=video_info.channel or video_info.uploader or "YouTube",
        )

        contents = []
        if video_info.duration <= self.cfg.max_duration:
            video = self.downloader.ytdlp_download_video(
                url,
                cookiefile=self.cookiejar.cookie_file,
                headers=self.headers,
                proxy=self.proxy,
                format="bv[height<=1080][fps<=60]+ba/bv[height<=1080]+ba/b[height<=1080]/b",
                node=True,
            )
            contents.append(
                self.create_video_content(
                    video,
                    video_info.thumbnail,
                    video_info.duration,
                )
            )
        else:
            contents.extend(self.create_image_contents([video_info.thumbnail]))

        return self.result(
            title=video_info.title,
            author=author,
            contents=contents,
            timestamp=video_info.timestamp,
        )

    @handle(
        "ym",
        r"^ym(?P<url>https?://(?:www\.)?(youtu\.be/[A-Za-z\d_-]+|youtube\.com/(?:watch|shorts)(?:\?v=[A-Za-z\d_-]+|/[A-Za-z\d_-]+)))",
    )
    async def ym(self, searched: re.Match[str]):
        """获取油管的音频(需加ym前缀)"""
        url = searched.group("url")
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        video_info = await self.downloader.ytdlp_extract_info(
            url,
            cookiefile=self.cookiejar.cookie_file,
            headers=self.headers,
            proxy=self.proxy,
        )
        author = await self._resolve_author(
            video_info.channel_id,
            fallback_name=video_info.channel or video_info.uploader or "YouTube",
        )

        contents = []
        contents.extend(self.create_image_contents([video_info.thumbnail]))

        if video_info.duration <= self.cfg.max_duration:
            audio_task = self.downloader.ytdlp_download_audio(
                url,
                cookiefile=self.cookiejar.cookie_file,
                headers=self.headers,
                proxy=self.proxy,
            )
            contents.append(
                self.create_audio_content(audio_task, duration=video_info.duration)
            )

        return self.result(
            title=video_info.title,
            author=author,
            contents=contents,
            timestamp=video_info.timestamp,
        )

    async def _resolve_author(self, channel_id: str, fallback_name: str):
        if not channel_id:
            return self.create_author(fallback_name)

        cached = self._author_cache.get(channel_id)
        if cached is not None:
            name, avatar_url, description = cached
            return self.create_author(name, avatar_url, description)

        try:
            profile = await wait_for(self._fetch_author_profile(channel_id), timeout=2.0)
            self._author_cache[channel_id] = profile
            name, avatar_url, description = profile
            return self.create_author(name, avatar_url, description)
        except Exception as exc:
            logger.debug("YouTube 作者详情获取超时或失败，降级使用视频元数据: %s", exc)
            return self.create_author(fallback_name)

    async def _fetch_author_profile(self, channel_id: str) -> tuple[str, str | None, str | None]:
        url = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
        payload = {
            "context": {
                "client": {
                    "hl": "zh-HK",
                    "gl": "US",
                    "deviceMake": "Apple",
                    "deviceModel": "",
                    "clientName": "WEB",
                    "clientVersion": "2.20251002.00.00",
                    "osName": "Macintosh",
                    "osVersion": "10_15_7",
                },
                "user": {"lockedSafetyMode": False},
                "request": {
                    "useSsl": True,
                    "internalExperimentFlags": [],
                    "consistencyTokenJars": [],
                },
            },
            "browseId": channel_id,
        }
        async with self.session.post(
            url,
            json=payload,
            headers=self.headers,
            proxy=self.proxy,
        ) as resp:
            if resp.status >= 400:
                raise ClientError(f"YouTube browse API {resp.status} {resp.reason}")
            browse = msgspec.json.decode(await resp.read(), type=BrowseResponse)

        return (browse.name, browse.avatar_url, browse.description)


class Thumbnail(Struct):
    url: str


class AvatarInfo(Struct):
    thumbnails: list[Thumbnail]


class ChannelMetadataRenderer(Struct):
    title: str
    description: str
    avatar: AvatarInfo


class Metadata(Struct):
    channelMetadataRenderer: ChannelMetadataRenderer


class Avatar(Struct):
    thumbnails: list[Thumbnail]


class BrowseResponse(Struct):
    metadata: Metadata

    @property
    def name(self) -> str:
        return self.metadata.channelMetadataRenderer.title

    @property
    def avatar_url(self) -> str | None:
        thumbnails = self.metadata.channelMetadataRenderer.avatar.thumbnails
        return thumbnails[0].url if thumbnails else None

    @property
    def description(self) -> str:
        return self.metadata.channelMetadataRenderer.description
