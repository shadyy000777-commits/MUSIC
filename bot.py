import os
import asyncio
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Put your bot token in .env")

intents = discord.Intents.default()
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
queues = {}  # guild_id -> deque of (title, url, webpage_url)


YTDL_OPTIONS = {
    "format": "bestaudio/best",
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


async def search_youtube(query: str):
    loop = asyncio.get_running_loop()

    def extract():
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = next((x for x in info["entries"] if x), None)
            if not info:
                return None
            return {
                "title": info.get("title", "Unknown title"),
                "url": info.get("url"),
                "webpage_url": info.get("webpage_url") or info.get("original_url"),
            }

    return await loop.run_in_executor(None, extract)


async def get_stream_url(webpage_url: str):
    loop = asyncio.get_running_loop()

    def extract():
        opts = {
            "format": "bestaudio/best",
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

    title, webpage_url = queue.popleft()

    try:
        stream_url = await get_stream_url(webpage_url)
        source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)

        def after(error):
            if error:
                print(f"Playback error in {guild.name}: {error}")
            asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)

        voice.play(source, after=after)
        print(f"Playing: {title}")
    except Exception as e:
        print(f"Could not play {title}: {e}")
        await play_next(guild)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Logged in as {bot.user}")
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Command sync failed: {e}")


@bot.tree.command(name="join", description="Join your current voice channel.")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message(
            "Join a voice channel first.", ephemeral=True
        )

    channel = interaction.user.voice.channel
    voice = interaction.guild.voice_client

    if voice:
        if voice.channel != channel:
            await voice.move_to(channel)
    else:
        await channel.connect()

    await interaction.response.send_message(f"Joined **{channel.name}**.")


@bot.tree.command(name="play", description="Play or queue a song by name and artist.")
@app_commands.describe(song="Song name and artist, e.g. 'Blinding Lights - The Weeknd'")
async def play(interaction: discord.Interaction, song: str):
    await interaction.response.defer()

    if "http://" in song or "https://" in song:
        return await interaction.followup.send(
            "Links aren't supported here — just type the song name and artist, "
            "e.g. `Blinding Lights - The Weeknd`."
        )

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("Join a voice channel first.")

    channel = interaction.user.voice.channel
    voice = interaction.guild.voice_client

    if not voice:
        voice = await channel.connect()
    elif voice.channel != channel:
        await voice.move_to(channel)

    # Build a precise search query from "song - artist" (or "song artist").
    query = song.replace(" - ", " ").strip()

    try:
        result = await search_youtube(query)
    except Exception as e:
        return await interaction.followup.send(f"Search failed: `{e}`")

    if not result or not result.get("webpage_url"):
        return await interaction.followup.send("I couldn't find that song.")

    queue = get_queue(interaction.guild.id)
    was_playing = voice.is_playing() or voice.is_paused()

    queue.append((result["title"], result["webpage_url"]))

    if was_playing:
        await interaction.followup.send(f"Queued **{result['title']}**.")
    else:
        await interaction.followup.send(f"Now playing **{result['title']}**.")
        await play_next(interaction.guild)


@bot.tree.command(name="pause", description="Pause the current song.")
async def pause(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    if voice and voice.is_playing():
        voice.pause()
        return await interaction.response.send_message("Paused.")
    await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume the current song.")
async def resume(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    if voice and voice.is_paused():
        voice.resume()
        return await interaction.response.send_message("Resumed.")
    await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@bot.tree.command(name="skip", description="Skip the current song.")
async def skip(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    if voice and voice.is_playing():
        voice.stop()
        return await interaction.response.send_message("Skipped.")
    await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    queue.clear()
    voice = interaction.guild.voice_client

    if voice:
        voice.stop()

    await interaction.response.send_message("Stopped and cleared the queue.")


@bot.tree.command(name="queue", description="Show the current music queue.")
async def show_queue(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    voice = interaction.guild.voice_client

    if not queue and not (voice and (voice.is_playing() or voice.is_paused())):
        return await interaction.response.send_message("The queue is empty.")

    lines = []
    for i, (title, _) in enumerate(list(queue)[:10], 1):
        lines.append(f"{i}. {title}")

    text = "\n".join(lines) if lines else "Nothing is queued after the current song."
    await interaction.response.send_message(f"**Queue**\n{text}")


@bot.tree.command(name="leave", description="Leave the voice channel.")
async def leave(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    get_queue(interaction.guild.id).clear()

    if voice:
        await voice.disconnect()
        return await interaction.response.send_message("Left the voice channel.")

    await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)


@bot.tree.command(name="ping", description="Check the bot latency.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! `{round(bot.latency * 1000)}ms`")


bot.run(TOKEN)
