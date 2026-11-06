"""
Music cog: queue-enabled player commands using Wavelink (Lavalink v4).
This version:
- Switches text query searches to YouTube (ytsearch:) to avoid YT Music edge cases.
- Announces "Now Playing" on the actual track start event to avoid false positives when playback fails.
"""
from __future__ import annotations

import asyncio
from typing import Optional, List

import discord
from discord.ext import commands
import wavelink


def fmt_duration(ms: int) -> str:
    seconds = max(0, ms // 1000)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


class MusicPlayer(wavelink.Player):
    """
    Custom Player with a per-guild queue and helpers.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue: wavelink.Queue[wavelink.Playable] = wavelink.Queue()
        self._queue_lock = asyncio.Lock()
        # The text channel to announce "Now Playing" when the track actually starts
        self.announce_channel_id: Optional[int] = None

    async def add_and_maybe_play(self, track: wavelink.Playable) -> bool:
        """
        Add a track to the queue and start playing if idle.
        Returns True if started immediately, else False (queued).
        """
        # Wavelink v3 uses the boolean property `playing` (no `is_playing()`).
        if not self.playing and self.queue.is_empty:
            await self.play(track)
            return True
        self.queue.put(track)
        return False

    async def extend_queue(self, items: List[wavelink.Playable]) -> None:
        for it in items:
            self.queue.put(it)

    async def advance(self) -> Optional[wavelink.Playable]:
        """
        Advance to the next queued track if any.
        """
        async with self._queue_lock:
            if self.queue.is_empty:
                return None
            next_track = self.queue.get()
            await self.play(next_track)
            return next_track


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------------------
    # Events from Wavelink
    # ---------------------------
    @commands.Cog.listener("on_wavelink_track_start")
    async def on_track_start(self, payload: wavelink.TrackStartEventPayload):
        player: MusicPlayer = payload.player  # type: ignore[assignment]
        track = payload.track
        chan = self.bot.get_channel(getattr(player, "announce_channel_id", 0))
        if isinstance(chan, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            embed = discord.Embed(
                title="Now Playing",
                description=f"[{track.title}]({track.uri})",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Duration", value=fmt_duration(track.length))
            await chan.send(embed=embed)

    @commands.Cog.listener("on_wavelink_track_end")
    async def on_track_end(self, payload: wavelink.TrackEndEventPayload):
        player: MusicPlayer = payload.player  # type: ignore[assignment]
        # Only auto-advance when a track naturally finishes.
        if str(payload.reason).upper() == "FINISHED":
            await player.advance()

    @commands.Cog.listener("on_wavelink_track_exception")
    async def on_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        player: MusicPlayer = payload.player  # type: ignore[assignment]
        # Try next when a track errors (e.g., YT blocked/age-restricted).
        await player.advance()

    @commands.Cog.listener("on_wavelink_track_stuck")
    async def on_track_stuck(self, payload: wavelink.TrackStuckEventPayload):
        player: MusicPlayer = payload.player  # type: ignore[assignment]
        await player.advance()

    # ---------------------------
    # Utility
    # ---------------------------
    async def _ensure_connected(
        self, ctx: commands.Context, channel: Optional[discord.VoiceChannel] = None
    ) -> MusicPlayer:
        """
        Ensure the bot is connected to a voice channel in this guild and return the MusicPlayer.
        """
        if ctx.voice_client and isinstance(ctx.voice_client, MusicPlayer):
            return ctx.voice_client  # type: ignore[return-value]

        if channel is None:
            voice_state = getattr(ctx.author, "voice", None)
            if not voice_state or not voice_state.channel:
                raise commands.CommandError("You must be in a voice channel (or specify one).")
            channel = voice_state.channel

        player: MusicPlayer = await channel.connect(cls=MusicPlayer)  # type: ignore[assignment]
        return player

    # ---------------------------
    # Commands
    # ---------------------------
    @commands.hybrid_command(name="help")
    async def help_cmd(self, ctx: commands.Context):
        p = ctx.prefix
        msg = (
            f"**Music Commands**\n"
            f"`{p}join [#channel]` — Join your voice channel (or the given one)\n"
            f"`{p}leave` — Disconnect the bot\n"
            f"`{p}play <query or URL>` — Play a track or add to queue\n"
            f"`{p}skip` — Skip the current track\n"
            f"`{p}pause` / `{p}resume` — Pause or resume playback\n"
            f"`{p}stop` — Stop playback and clear the queue\n"
            f"`{p}volume <0-150>` — Set player volume\n"
            f"`{p}queue` — Show the current queue\n"
            f"`{p}nowplaying` — Show the current track\n"
        )
        await ctx.send(msg)

    @commands.hybrid_command(name="join", aliases=["connect", "summon"])
    async def join_cmd(self, ctx: commands.Context, *, channel: Optional[discord.VoiceChannel] = None):
        await ctx.defer()  # slash interactions expire after 3s; voice connect can take longer
        try:
            player = await self._ensure_connected(ctx, channel)
            await ctx.send(f"Joined **{player.channel.name}**.")
        except Exception as e:
            await ctx.send(f"Failed to join: `{e}`")

    @commands.hybrid_command(name="leave", aliases=["disconnect"])
    async def leave_cmd(self, ctx: commands.Context):
        vc = ctx.voice_client
        if not vc:
            return await ctx.send("I'm not connected to a voice channel.")
        try:
            await vc.disconnect(force=True)
            await ctx.send("Disconnected. Bye!")
        except Exception as e:
            await ctx.send(f"Failed to disconnect: `{e}`")

    @commands.hybrid_command(name="play", aliases=["p"])
    async def play_cmd(self, ctx: commands.Context, *, query: str):
        """
        Search and play/queue tracks.
        - Text queries search standard YouTube (`ytsearch:`); URLs load directly.
        - We announce "Now Playing" on the start event instead of immediately.
        """
        await ctx.defer()  # slash interactions expire after 3s; search + connect can take longer
        try:
            player = await self._ensure_connected(ctx)

            # wavelink adds the search prefix itself (and skips it for URLs)
            search: wavelink.Search = await wavelink.Playable.search(query, source=wavelink.TrackSource.YouTube)

            # Playlist handling
            if isinstance(search, wavelink.Playlist):
                playlist: wavelink.Playlist = search
                tracks = list(playlist.tracks)
                if not tracks:
                    return await ctx.send("No tracks found in the playlist.")

                started_now = False
                if not player.playing and player.queue.is_empty:
                    player.announce_channel_id = ctx.channel.id
                    await player.play(tracks[0])
                    if len(tracks) > 1:
                        await player.extend_queue(tracks[1:])
                    started_now = True
                else:
                    await player.extend_queue(tracks)

                if started_now:
                    await ctx.send(f"Loading **{tracks[0].title}** from playlist **{playlist.name}**…")
                else:
                    await ctx.send(f"Queued **{len(tracks)}** tracks from playlist **{playlist.name}**.")
                return

            # Track list handling
            tracks_list: List[wavelink.Playable] = search  # type: ignore[assignment]
            if not tracks_list:
                return await ctx.send("No results found.")

            track = tracks_list[0]
            if not player.playing and player.queue.is_empty:
                player.announce_channel_id = ctx.channel.id

            started_now = await player.add_and_maybe_play(track)

            if started_now:
                await ctx.send(f"Loading **{track.title}**…")
            else:
                await ctx.send(f"Queued **{track.title}** (#{player.queue.count}).")

        except commands.CommandError as ce:
            await ctx.send(str(ce))
        except Exception as e:
            await ctx.send(f"Something went wrong: `{e}`")

    @commands.hybrid_command(name="skip", aliases=["next"])
    async def skip_cmd(self, ctx: commands.Context):
        player: Optional[MusicPlayer] = ctx.voice_client  # type: ignore[assignment]
        if not player or not isinstance(player, MusicPlayer):
            return await ctx.send("I'm not connected to a voice channel here.")
        if not player.playing:
            return await ctx.send("Nothing is playing right now.")
        await ctx.send("⏭️ Skipping…")
        # Stopping triggers a REPLACED reason; we manually advance.
        await player.stop()
        await player.advance()

    @commands.hybrid_command(name="pause")
    async def pause_cmd(self, ctx: commands.Context):
        player: Optional[MusicPlayer] = ctx.voice_client  # type: ignore[assignment]
        if not player or not isinstance(player, MusicPlayer):
            return await ctx.send("I'm not connected to a voice channel here.")
        if not player.playing:
            return await ctx.send("Nothing is playing right now.")
        if player.paused:
            return await ctx.send("Already paused.")
        await player.pause(True)
        await ctx.send("⏸️ Paused.")

    @commands.hybrid_command(name="resume", aliases=["unpause"])
    async def resume_cmd(self, ctx: commands.Context):
        player: Optional[MusicPlayer] = ctx.voice_client  # type: ignore[assignment]
        if not player or not isinstance(player, MusicPlayer):
            return await ctx.send("I'm not connected to a voice channel here.")
        if not player.paused:
            return await ctx.send("Playback is not paused.")
        await player.pause(False)
        await ctx.send("▶️ Resumed.")

    @commands.hybrid_command(name="stop", aliases=["clear"])
    async def stop_cmd(self, ctx: commands.Context):
        player: Optional[MusicPlayer] = ctx.voice_client  # type: ignore[assignment]
        if not player or not isinstance(player, MusicPlayer):
            return await ctx.send("I'm not connected to a voice channel here.")
        player.queue.clear()
        if player.playing or player.paused:
            await player.stop()
        await ctx.send("⏹️ Stopped and cleared the queue.")

    @commands.hybrid_command(name="volume", aliases=["vol"])
    async def volume_cmd(self, ctx: commands.Context, volume: Optional[int] = None):
        """
        Get or set volume (0-150). Default is 100.
        """
        player: Optional[MusicPlayer] = ctx.voice_client  # type: ignore[assignment]
        if not player or not isinstance(player, MusicPlayer):
            return await ctx.send("I'm not connected to a voice channel here.")

        if volume is None:
            try:
                current = getattr(player, "volume", None)
                if current is None:
                    return await ctx.send("🔊 Set a volume with `!volume <0-150>`.")
                return await ctx.send(f"🔊 Current volume: **{current}%**")
            except Exception:
                return await ctx.send("🔊 Set a volume with `!volume <0-150>`.")

        if not 0 <= volume <= 150:
            return await ctx.send("Volume must be between 0 and 150.")

        await player.set_volume(volume)
        await ctx.send(f"🔊 Volume set to **{volume}%**")

    @commands.hybrid_command(name="queue", aliases=["q"])
    async def queue_cmd(self, ctx: commands.Context):
        player: Optional[MusicPlayer] = ctx.voice_client  # type: ignore[assignment]
        if not player or not isinstance(player, MusicPlayer):
            return await ctx.send("I'm not connected to a voice channel here.")

        lines: List[str] = []

        if player.current:
            current = player.current
            pos = player.position if player.position is not None else 0
            lines.append(
                f"**Now:** [{current.title}]({current.uri}) "
                f"(`{fmt_duration(pos)}` / `{fmt_duration(current.length)}`)"
            )
        else:
            lines.append("**Now:** Nothing playing.")

        if player.queue.is_empty:
            lines.append("\n**Up Next:** *(queue is empty)*")
        else:
            lines.append("\n**Up Next:**")
            for idx, t in enumerate(list(player.queue)[:10], start=1):
                lines.append(f"`{idx}.` [{t.title}]({t.uri}) — `{fmt_duration(t.length)}`")

            if player.queue.count > 10:
                lines.append(f"...and **{player.queue.count - 10}** more.")

        embed = discord.Embed(
            title=f"Queue for {ctx.guild.name}",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="nowplaying", aliases=["np"])
    async def nowplaying_cmd(self, ctx: commands.Context):
        player: Optional[MusicPlayer] = ctx.voice_client  # type: ignore[assignment]
        if not player or not isinstance(player, MusicPlayer):
            return await ctx.send("I'm not connected to a voice channel here.")
        if not player.current:
            return await ctx.send("Nothing is playing right now.")

        track = player.current
        pos = player.position if player.position is not None else 0

        embed = discord.Embed(
            title="Now Playing",
            description=f"[{track.title}]({track.uri})",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Position", value=f"`{fmt_duration(pos)}` / `{fmt_duration(track.length)}`")
        if getattr(track, "author", None):
            embed.add_field(name="Channel", value=str(track.author), inline=True)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
