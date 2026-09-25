import os
import asyncio
import discord
from discord.ext import commands
import yt_dlp

# Retrieve Discord token from environment variables
TOKEN = os.getenv('DISCORD_TOKEN')

if not TOKEN:
    raise ValueError("Error: DISCORD_TOKEN is missing from environment variables.")

# Setup bot intents
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# yt-dlp & FFmpeg configurations
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'default_search': 'ytsearch',
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    print('Bot is online and ready!')

@bot.command(name='join', help='Joins your current voice channel')
async def join(ctx):
    if not ctx.author.voice:
        await ctx.send("You need to be in a voice channel first!")
        return
    channel = ctx.author.voice.channel
    if ctx.voice_client is not None:
        return await ctx.voice_client.move_to(channel)
    await channel.connect()
    await ctx.send(f"Connected to **{channel.name}**")

@bot.command(name='play', help='Plays audio from YouTube (URL or search term)')
async def play(ctx, *, search: str):
    if ctx.voice_client is None:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            await ctx.send("You must be in a voice channel!")
            return

    async with ctx.typing():
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(search, download=False))

        if 'entries' in data:
            data = data['entries'][0]

        stream_url = data['url']
        title = data.get('title', 'Audio Stream')

        source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
        
        if ctx.voice_client.is_playing():
            ctx.voice_client.stop()

        ctx.voice_client.play(source)
        await ctx.send(f"🎶 Now playing: **{title}**")

@bot.command(name='pause', help='Pauses current audio playback')
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("Playback paused ⏸️")

@bot.command(name='resume', help='Resumes paused audio playback')
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("Playback resumed ▶️")

@bot.command(name='stop', help='Stops audio playback')
async def stop(ctx):
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.send("Playback stopped ⏹️")

@bot.command(name='leave', help='Disconnects the bot from the voice channel')
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Disconnected from voice channel 👋")

# Run the bot
bot.run(TOKEN)
