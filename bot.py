import os
import asyncio
from collections import deque

import discord
from discord.ext import commands
import yt_dlp
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Put your bot token in .env")

intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True  # required to read "+" prefix commands

bot = commands.Bot(command_prefix="+", intents=intents, help_command=None)
queues = {}  # guild_id -> deque of (title, webpage_url)


YTDL_OPTIONS = {
    "format": "bestaudio[abr>128]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "scsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


def get_queue(guild_id: int):
    return queues.setdefault(guild_id, deque())


JIOSAAVN_SEARCH_URL = "https://www.jiosaavn.com/api.php"


async def search_jiosaavn(query: str, count: int = 5):
    """Search JioSaavn's (unofficial) search endpoint and return candidate matches."""
    loop = asyncio.get_running_loop()

    def fetch():
        try:
            resp = requests.get(
                JIOSAAVN_SEARCH_URL,
                params={
                    "__call": "search.getResults",
                    "q": query,
                    "p": 1,
                    "n": count,
                    "_format": "json",
                    "_marker": 0,
                },
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"JioSaavn search failed: {e}")
            return []

        results = []
        for item in data.get("results", []) if isinstance(data, dict) else []:
            webpage_url = item.get("perma_url")
            if not webpage_url:
                continue
            title = item.get("song") or item.get("title") or "Unknown title"
            artists = item.get("primary_artists") or item.get("singers")
            if artists:
                title = f"{title} - {artists}"
            results.append({"title": title, "webpage_url": webpage_url})
        return results

    return await loop.run_in_executor(None, fetch)


async def search_soundcloud(query: str, count: int = 5):
    """Return up to `count` candidate matches from SoundCloud, best first."""
    loop = asyncio.get_running_loop()

    def extract():
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(f"scsearch{count}:{query}", download=False)
            entries = info.get("entries") if info else None
            if not entries:
                return []
            results = []
            for entry in entries:
                if not entry:
                    continue
                webpage_url = entry.get("webpage_url") or entry.get("original_url")
                if not webpage_url:
                    continue
                results.append({
                    "title": entry.get("title", "Unknown title"),
                    "webpage_url": webpage_url,
                })
            return results

    return await loop.run_in_executor(None, extract)


async def search_song(query: str, count: int = 5):
    """Try JioSaavn first (better Indian/Bollywood catalog), then SoundCloud as fallback."""
    jiosaavn_results = await search_jiosaavn(query, count)
    soundcloud_results = await search_soundcloud(query, count)
    return jiosaavn_results + soundcloud_results


async def get_stream_url(webpage_url: str):
    loop = asyncio.get_running_loop()

    def extract():
        opts = {
            "format": "bestaudio[abr>128]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
            return info.get("url")

    return await loop.run_in_executor(None, extract)


async def play_next(guild: discord.Guild):
    voice = guild.voice_client
    if not voice or not voice.is_connected():
        return

    queue = get_queue(guild.id)
    if not queue:
        return

    candidates = queue.popleft()  # list of {"title", "webpage_url"}, best match first

    for candidate in candidates:
        title, webpage_url = candidate["title"], candidate["webpage_url"]
        try:
            stream_url = await get_stream_url(webpage_url)
            if not stream_url:
                raise RuntimeError("no stream url returned")

            source = discord.FFmpegOpusAudio(stream_url, bitrate=192, **FFMPEG_OPTIONS)

            def after(error):
                if error:
                    print(f"Playback error in {guild.name}: {error}")
                asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)

            voice.play(source, after=after)
            print(f"Playing: {title}")
            return
        except Exception as e:
            print(f"Skipping unplayable match '{title}': {e}")
            continue

    print(f"No playable match found for this queue entry in {guild.name}.")
    await play_next(guild)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("Cleared old slash commands.")
    except Exception as e:
        print(f"Could not clear old slash commands: {e}")


@bot.command(name="join", help="Join your current voice channel.")
async def join(ctx: commands.Context):
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("Join a voice channel first.")

    channel = ctx.author.voice.channel
    voice = ctx.guild.voice_client

    if voice:
        if voice.channel != channel:
            await voice.move_to(channel)
    else:
        await channel.connect()

    await ctx.send(f"Joined **{channel.name}**.")


@bot.command(name="play", help="Play or queue a song. Usage: +play <song> - <artist>")
async def play(ctx: commands.Context, *, song: str):
    if "http://" in song or "https://" in song:
        return await ctx.send(
            "Links aren't supported here — just type the song name and artist, "
            "e.g. `+play Blinding Lights - The Weeknd`."
        )

    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("Join a voice channel first.")

    channel = ctx.author.voice.channel
    voice = ctx.guild.voice_client

    if not voice:
        voice = await channel.connect()
    elif voice.channel != channel:
        await voice.move_to(channel)

    # Build a search query from "song - artist" (or "song artist").
    query = song.replace(" - ", " ").strip()

    async with ctx.typing():
        try:
            candidates = await search_song(query)
        except Exception as e:
            return await ctx.send(f"Search failed: `{e}`")

    if not candidates:
        return await ctx.send("I couldn't find that song.")

    queue = get_queue(ctx.guild.id)
    was_playing = voice.is_playing() or voice.is_paused()

    queue.append(candidates)
    top_title = candidates[0]["title"]

    if was_playing:
        await ctx.send(f"Queued **{top_title}**.")
    else:
        await ctx.send(f"Now playing **{top_title}**.")
        await play_next(ctx.guild)


@bot.command(name="pause", help="Pause the current song.")
async def pause(ctx: commands.Context):
    voice = ctx.guild.voice_client
    if voice and voice.is_playing():
        voice.pause()
        return await ctx.send("Paused.")
    await ctx.send("Nothing is playing.")


@bot.command(name="resume", help="Resume the current song.")
async def resume(ctx: commands.Context):
    voice = ctx.guild.voice_client
    if voice and voice.is_paused():
        voice.resume()
        return await ctx.send("Resumed.")
    await ctx.send("Nothing is paused.")


@bot.command(name="skip", help="Skip the current song.")
async def skip(ctx: commands.Context):
    voice = ctx.guild.voice_client
    if voice and voice.is_playing():
        voice.stop()
        return await ctx.send("Skipped.")
    await ctx.send("Nothing is playing.")


@bot.command(name="stop", help="Stop playback and clear the queue.")
async def stop(ctx: commands.Context):
    queue = get_queue(ctx.guild.id)
    queue.clear()
    voice = ctx.guild.voice_client

    if voice:
        voice.stop()

    await ctx.send("Stopped and cleared the queue.")


@bot.command(name="queue", help="Show the current music queue.")
async def show_queue(ctx: commands.Context):
    queue = get_queue(ctx.guild.id)
    voice = ctx.guild.voice_client

    if not queue and not (voice and (voice.is_playing() or voice.is_paused())):
        return await ctx.send("The queue is empty.")

    lines = []
    for i, candidates in enumerate(list(queue)[:10], 1):
        lines.append(f"{i}. {candidates[0]['title']}")

    text = "\n".join(lines) if lines else "Nothing is queued after the current song."
    await ctx.send(f"**Queue**\n{text}")


@bot.command(name="leave", help="Leave the voice channel.")
async def leave(ctx: commands.Context):
    voice = ctx.guild.voice_client
    get_queue(ctx.guild.id).clear()

    if voice:
        await voice.disconnect()
        return await ctx.send("Left the voice channel.")

    await ctx.send("I'm not in a voice channel.")


@bot.command(name="ping", help="Check the bot latency.")
async def ping(ctx: commands.Context):
    await ctx.send(f"Pong! `{round(bot.latency * 1000)}ms`")


@bot.command(name="help", help="Show all music bot commands.")
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="🎵 Music Bot Commands",
        description="Prefix: `+`",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="+play <song> - <artist>", value="Play or queue a song.", inline=False)
    embed.add_field(name="+join", value="Join your voice channel.", inline=False)
    embed.add_field(name="+pause", value="Pause the current song.", inline=False)
    embed.add_field(name="+resume", value="Resume the current song.", inline=False)
    embed.add_field(name="+skip", value="Skip the current song.", inline=False)
    embed.add_field(name="+stop", value="Stop playback and clear the queue.", inline=False)
    embed.add_field(name="+queue", value="Show the current queue.", inline=False)
    embed.add_field(name="+leave", value="Leave the voice channel.", inline=False)
    embed.add_field(name="+ping", value="Check bot latency.", inline=False)
    await ctx.send(embed=embed)


bot.run(TOKEN)
