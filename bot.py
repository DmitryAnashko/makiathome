from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
from collections import deque
from dataclasses import dataclass

import discord
import imageio_ffmpeg
from discord import app_commands

from controls import PlaybackControls, show_queue
from youtube import MusicError, Track, YouTube, parse_query

EMPTY_CHANNEL_SECONDS = 120


class VoiceAudio(discord.FFmpegOpusAudio):
    def __init__(self, url, on_start, **options):
        self.on_start = on_start
        self.started = False
        super().__init__(url, **options)

    def read(self):
        packet = super().read()
        # Discord expects Opus audio frames, without Ogg identification or comment headers.
        while packet.startswith((b"OpusHead", b"OpusTags")):
            packet = super().read()
        if not self.started:
            if not packet:
                raise OSError("The stream ended before the first audio frame.")
            self.started = True
            if self.on_start:
                self.on_start()
        return packet


def audio_source(url, headers, on_start=None):
    options = ["-nostdin", "-rw_timeout", "15000000", "-reconnect", "1",
               "-reconnect_streamed", "1", "-reconnect_delay_max", "3"]
    # Forward the media request headers supplied by YouTube's extractor.
    lines = [f"{k}: {v}\r\n" for k, v in headers.items()
             if k.lower() in {"user-agent", "referer", "origin", "accept", "accept-language"}
             and "\r" not in v and "\n" not in v]
    if lines:
        options.extend(["-headers", "".join(lines)])
    return VoiceAudio(url, on_start, executable=imageio_ffmpeg.get_ffmpeg_exe(),
                      before_options=shlex.join(options), options="-vn", stderr=subprocess.DEVNULL)


@dataclass
class Request:
    query: str | Track
    channel: discord.abc.Messageable
    requester_id: int
    id: int

    @property
    def label(self):
        return self.query.label if isinstance(self.query, Track) else self.query


class Player:
    def __init__(self, voice, youtube):
        self.voice = voice
        self.youtube = youtube
        self.queue = deque()
        self.current = None
        self.worker = None
        self.playing = None
        self.empty_timer = None

    def add(self, request):
        self.queue.append(request)
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.run())

    async def notice(self, request, message, *, view=None):
        try:
            return await request.channel.send(message, allowed_mentions=discord.AllowedMentions.none(),
                                              suppress_embeds=True, view=view)
        except discord.HTTPException:
            logging.warning("Could not send a playback update to the text channel.")

    async def play_one(self, request):
        track = request.query
        if isinstance(track, str):
            matches = await self.youtube.search(track)
            if not matches:
                raise MusicError("No YouTube matches. Try the title and artist together.")
            track = matches[0]
        title, url, headers = await self.youtube.stream(track)
        request.query = Track(track.id, title, track.artist, track.duration)
        loop = asyncio.get_running_loop()
        started = loop.create_future()
        finished = loop.create_future()
        source = audio_source(url, headers, lambda: loop.call_soon_threadsafe(started.set_result, True))

        def finish(error):
            if not finished.done():
                finished.set_result(error)

        try:
            self.voice.play(source, after=lambda error: loop.call_soon_threadsafe(finish, error))
        except Exception:
            source.cleanup()
            raise
        controls = None
        card = None
        try:
            await asyncio.wait([started, finished], return_when=asyncio.FIRST_COMPLETED)
            if started.done():
                controls = PlaybackControls(self, request)
                card = await self.notice(request, f"Playing **{discord.utils.escape_markdown(title[:200])}**.",
                                         view=controls)
            if await finished:
                raise MusicError("The audio stream stopped unexpectedly. Try another recording.")
        finally:
            self.voice.stop()
            if controls:
                controls.stop()
            if card:
                try:
                    await card.edit(view=None)
                except discord.HTTPException:
                    pass  # The message may have been deleted while the song was playing.

    async def run(self):
        while self.queue:
            self.current = self.queue.popleft()
            self.playing = asyncio.create_task(self.play_one(self.current))
            try:
                await self.playing
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except MusicError as error:
                await self.notice(self.current, str(error))
            except Exception as error:
                logging.warning("Playback failed (%s).", type(error).__name__)
                await self.notice(self.current, "Playback failed. Check the bot's voice permissions or try another song.")
            finally:
                self.playing = None
                self.current = None

    def skip(self):
        if self.playing and not self.playing.done():
            if self.playing.cancelling():
                return False
            self.playing.cancel()
            return True
        if self.queue:
            self.queue.popleft()
            return True
        return False

    def cancel_empty_timer(self):
        if self.empty_timer:
            self.empty_timer.cancel()
            self.empty_timer = None

    async def close(self):
        self.cancel_empty_timer()
        self.queue.clear()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.voice.disconnect(force=True)


class MusicBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.youtube = YouTube()
        self.players = {}
        self.locks = {}
        self.installed = set()

    def lock(self, guild_id):
        return self.locks.setdefault(guild_id, asyncio.Lock())

    async def register(self, guild):
        if guild.id in self.installed:
            return
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.installed.add(guild.id)

    async def on_ready(self):
        permissions = discord.Permissions(view_channel=True, send_messages=True, connect=True, speak=True)
        invite = discord.utils.oauth_url(self.user.id, permissions=permissions,
                                         scopes=("bot", "applications.commands"))
        print(f"Online as {self.user}. Add this bot to your server:\n{invite}", flush=True)
        for guild in self.guilds:
            await self.register(guild)

    async def on_guild_join(self, guild):
        await self.register(guild)

    async def on_guild_remove(self, guild):
        self.installed.discard(guild.id)
        async with self.lock(guild.id):
            player = self.players.pop(guild.id, None)
            if player:
                await player.close()

    @staticmethod
    def has_listener(channel):
        for user_id in channel.voice_states:
            member = channel.guild.get_member(user_id)
            # A voice state may arrive before its member is cached.
            if member is None or not member.bot:
                return True
        return False

    def watch_empty_channel(self, player, channel):
        if self.has_listener(channel):
            player.cancel_empty_timer()
        elif player.empty_timer is None:
            player.empty_timer = asyncio.create_task(self.leave_if_empty(player, channel))

    async def leave_if_empty(self, player, channel):
        try:
            await asyncio.sleep(EMPTY_CHANNEL_SECONDS)
            async with self.lock(channel.guild.id):
                state = channel.guild.me.voice
                if (self.players.get(channel.guild.id) is player and state and state.channel == channel
                        and not self.has_listener(channel)):
                    self.players.pop(channel.guild.id)
                    player.empty_timer = None
                    await player.close()
        finally:
            if player.empty_timer is asyncio.current_task():
                player.empty_timer = None

    async def on_voice_state_update(self, member, before, after):
        if before.channel == after.channel:
            return
        guild = member.guild
        async with self.lock(guild.id):
            player = self.players.get(guild.id)
            if not player:
                return
            state = guild.me.voice
            if member.id == guild.me.id:
                player.cancel_empty_timer()
                if not state or not state.channel:
                    self.players.pop(guild.id)
                    await player.close()
                    return
            if state and state.channel and state.channel in (before.channel, after.channel):
                if not member.bot and after.channel == state.channel:
                    player.cancel_empty_timer()
                self.watch_empty_channel(player, state.channel)


bot = MusicBot()


def user_voice(interaction):
    voice = getattr(interaction.user, "voice", None)
    if not interaction.guild or not voice or not isinstance(voice.channel, discord.VoiceChannel):
        raise MusicError("Join a server voice channel first, then run the command again.")
    return voice.channel


def same_channel(interaction):
    channel = user_voice(interaction)
    player = bot.players.get(interaction.guild_id)
    if player and player.voice.channel.id != channel.id:
        raise MusicError("Join the bot's voice channel to change its queue.")
    return channel, player


@bot.tree.command(description="Play or queue a YouTube song in your voice channel.")
@app_commands.describe(query="Type a song or artist, choose a match, or paste a YouTube video link.")
async def play(interaction: discord.Interaction, query: app_commands.Range[str, 1, 250]):
    parsed = parse_query(query)
    user_voice(interaction)
    await interaction.response.defer(thinking=True)
    if isinstance(parsed, Track):
        parsed = bot.youtube.known_track(parsed)
    async with bot.lock(interaction.guild_id):
        channel, player = same_channel(interaction)
        if player is None:
            perms = channel.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.connect and perms.speak):
                raise MusicError("Give the bot View Channel, Connect, and Speak in your voice channel.")
            try:
                voice = await channel.connect(timeout=25, self_deaf=True)
            except (discord.ClientException, discord.DiscordException, OSError, TimeoutError):
                raise MusicError("Could not join voice. Check channel permissions, capacity, and the PC's connection.") from None
            player = bot.players[interaction.guild_id] = Player(voice, bot.youtube)
            bot.watch_empty_channel(player, channel)
        if len(player.queue) >= 100:
            raise MusicError("The queue is full. Wait for a song to finish.")
        req = Request(parsed, interaction.channel, interaction.user.id, interaction.id)
        player.add(req)
    await interaction.edit_original_response(content=f"Queued **{discord.utils.escape_markdown(req.label)}**.")


@play.autocomplete("query")
async def match_songs(interaction: discord.Interaction, current: str):
    if len(current.strip()) < 2 or len(current) > 100:
        return []
    try:
        parsed = parse_query(current)
        if isinstance(parsed, Track):
            match = bot.youtube.known_track(parsed)
            return [app_commands.Choice(name=match.label, value=match.url)]
        matches = await asyncio.wait_for(bot.youtube.search(parsed), timeout=1.85)
    except (TimeoutError, MusicError):
        matches = []
    if matches:
        return [app_commands.Choice(name=t.label, value=t.url) for t in matches]
    return [app_commands.Choice(name=f"Search YouTube for {current}"[:100], value=current)]


@bot.tree.command(description="See the queue and remove your own upcoming songs.")
async def queue(interaction: discord.Interaction):
    _, player = same_channel(interaction)
    if not player or not (player.current or player.queue):
        raise MusicError("The queue is empty. Add a song with /play.")
    await show_queue(interaction, player)


@bot.tree.command(description="Skip the current song.")
async def skip(interaction: discord.Interaction):
    _, player = same_channel(interaction)
    if not player or not player.skip():
        raise MusicError("There is no song to skip.")
    await interaction.response.send_message("Skipped.")


@bot.tree.command(description="Clear the queue and disconnect the bot from voice.")
async def leave(interaction: discord.Interaction):
    user_voice(interaction)
    await interaction.response.defer(thinking=True)
    async with bot.lock(interaction.guild_id):
        _, player = same_channel(interaction)
        if not player:
            raise MusicError("The bot is already out of voice.")
        bot.players.pop(interaction.guild_id)
        await player.close()
    await interaction.edit_original_response(content="Left the voice channel and cleared the queue.")


@bot.tree.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    cause = getattr(error, "original", error)
    message = str(cause) if isinstance(cause, MusicError) else "The command failed. Try again."
    if not isinstance(cause, MusicError):
        logging.warning("Command failed (%s).", type(cause).__name__)
    if interaction.response.is_done():
        await interaction.edit_original_response(content=message)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    token = os.environ.pop("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Run Start Bot.cmd to enter your Discord bot token on this PC.")
    try:
        bot.run(token)
    except discord.LoginFailure:
        raise SystemExit("Discord rejected the token. Run Start Bot.cmd with the -ResetToken option.") from None
