import os
import asyncio
import random
import aiosqlite
from typing import Optional, List, Dict
from dataclasses import dataclass, field
from pydantic_settings import BaseSettings
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioPiped
from pytgcalls.exceptions import GroupCallNotFound, NoActiveGroupCall

# ==========================================
# 1. CONFIGURATION & ENV LOADING
# ==========================================
class Settings(BaseSettings):
    API_ID: int
    API_HASH: str
    BOT_TOKEN: str
    ASSISTANT_STRING_SESSION: str
    SUDO_USERS: List[int] = []

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

config = Settings()
SUDO_FILTER = filters.user(config.SUDO_USERS) if config.SUDO_USERS else filters.all

# ==========================================
# 2. DATABASE (Async SQLite)
# ==========================================
DB_PATH = "bot_db.sqlite"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY, volume INTEGER DEFAULT 100, loop_mode TEXT DEFAULT 'off'
        )""")
        await db.commit()

async def get_db_setting(chat_id: int, key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(f"SELECT {key} FROM chat_settings WHERE chat_id = ?", (chat_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 100 if key == "volume" else "off"

async def set_db_setting(chat_id: int, key: str, val):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
        await db.execute(f"UPDATE chat_settings SET {key} = ? WHERE chat_id = ?", (val, chat_id))
        await db.commit()

# ==========================================
# 3. MUSIC QUEUE & DATA STRUCTURES
# ==========================================
@dataclass
class Track:
    title: str
    file_path: str
    duration: int
    requester_id: int
    requester_name: str
    thumb: Optional[str] = None

@dataclass
class Queue:
    tracks: List[Track] = field(default_factory=list)
    current: Optional[Track] = None
    loop_mode: str = "off" # off, one, all

active_queues: Dict[int, Queue] = {}
now_playing_msgs: Dict[int, int] = {} # chat_id -> message_id

def get_queue(chat_id: int) -> Queue:
    if chat_id not in active_queues:
        active_queues[chat_id] = Queue()
    return active_queues[chat_id]

# ==========================================
# 4. AUDIO EXTRACTOR (yt-dlp with Cookies)
# ==========================================
YTDLP_OPTS = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(id)s.%(ext)s',
    'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
    'quiet': True,
    'no_warnings': True,
    'cookiefile': 'youtube_cookies.txt', # <--- YAHAN COOKIES USE HO RAHI HAIN
    'geo_bypass': True,
    'nocheckcertificate': True
}

async def extract_audio(query: str) -> Track:
    loop = asyncio.get_running_loop()
    def _download():
        with __import__('yt_dlp').YoutubeDL(YTDLP_OPTS) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}" if not query.startswith(("http", "rtmp")) else query, download=True)
            info = info['entries'][0] if 'entries' in info else info
            return (
                ydl.prepare_filename(info).replace(".webm", ".mp3").replace(".m4a", ".mp3"),
                info.get('title', 'Unknown Title'),
                int(info.get('duration', 0)),
                info.get('thumbnail')
            )
    
    path, title, duration, thumb = await loop.run_in_executor(None, _download)
    return Track(title=title, file_path=path, duration=duration, thumb=thumb, requester_id=0, requester_name="")

# ==========================================
# 5. CLIENTS INITIALIZATION
# ==========================================
os.makedirs("downloads", exist_ok=True)

bot = Client("vc_bot", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)
assistant = Client("assistant", api_id=config.API_ID, api_hash=config.API_HASH, session_string=config.ASSISTANT_STRING_SESSION)
call_py = PyTgCalls(assistant)

# ==========================================
# 6. FORMATTERS & UI
# ==========================================
def format_dur(secs: int) -> str:
    return f"{secs // 60}:{secs % 60:02d}"

def get_progress_bar() -> str:
    return "[▮▮▮▮▮▯▯▯▯▯]"

def get_np_text(q: Queue, chat_id: int) -> str:
    t = q.current
    if not t: return ""
    return (
        f"▸ **Now Playing**\n"
        f"📝 {t.title}\n"
        f"⏱ {get_progress_bar()} `00:00 / {format_dur(t.duration)}`\n"
        f"👤 Requested by: {t.requester_name}\n"
        f"🔁 Loop: `{q.loop_mode.capitalize()}` | 📋 Queue: `{len(q.tracks)}`"
    )

def get_np_markup(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏮", callback_data=f"ctrl:replay:{chat_id}"),
            InlineKeyboardButton("⏹", callback_data=f"ctrl:stop:{chat_id}"),
            InlineKeyboardButton("⏭", callback_data=f"ctrl:skip:{chat_id}")
        ],
        [
            InlineKeyboardButton("⏸", callback_data=f"ctrl:pause:{chat_id}"),
            InlineKeyboardButton("🔁", callback_data=f"ctrl:loop:{chat_id}"),
            InlineKeyboardButton("🔀", callback_data=f"ctrl:shuffle:{chat_id}")
        ]
    ])

# ==========================================
# 7. CORE PLAYBACK ENGINE
# ==========================================
async def start_playing(chat_id: int):
    q = get_queue(chat_id)
    if not q.tracks and not q.current:
        await call_py.leave_group_call(chat_id)
        if chat_id in now_playing_msgs:
            try: await bot.delete_messages(chat_id, now_playing_msgs[chat_id])
            except: pass
            del now_playing_msgs[chat_id]
        return

    track = q.tracks.pop(0) if q.tracks else q.current
    q.current = track

    try:
        volume = await get_db_setting(chat_id, "volume")
        await call_py.join_group_call(chat_id, AudioPiped(track.file_path), stream_type="local")
        await call_py.change_volume_call(chat_id, volume)
        
        text = get_np_text(q, chat_id)
        markup = get_np_markup(chat_id)
        
        if chat_id in now_playing_msgs:
            try:
                await bot.edit_message_text(chat_id, now_playing_msgs[chat_id], text, reply_markup=markup, disable_web_page_preview=True)
                return
            except: pass
            
        msg = await bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
        now_playing_msgs[chat_id] = msg.id
        
    except NoActiveGroupCall:
        await bot.send_message(chat_id, "❌ **Error:** Voice chat active nahi hai. Pehle Voice Chat start karo group me.")
        q.current = None
    except Exception as e:
        print(f"Playback Error: {e}")
        await start_playing(chat_id)

def cleanup_file(path: str):
    if os.path.exists(path):
        try: os.remove(path)
        except: pass

@call_py.on_stream_end()
async def on_stream_end(chat_id: int):
    q = get_queue(chat_id)
    cleanup_file(q.current.file_path) if q.current else None
    
    if q.loop_mode == "one" and q.current:
        q.tracks.insert(0, q.current)
    elif q.loop_mode == "all" and q.current:
        q.tracks.append(q.current)
        
    q.current = None
    await start_playing(chat_id)

# ==========================================
# 8. COMMAND HANDLERS
# ==========================================
@bot.on_message(filters.command("play") & filters.group)
async def play_cmd(_, m: Message):
    chat_id = m.chat.id
    query = m.text.split(None, 1)[1] if len(m.command) > 1 else None
    
    if not query and not (m.reply_to_message and m.reply_to_message.audio):
        return await m.reply("❓ Usage: `/play <song name or url>`")
        
    q = get_queue(chat_id)
    status = await m.reply("🔍 **Searching & Extracting...**")
    
    try:
        track = await extract_audio(query)
        track.requester_id = m.from_user.id
        track.requester_name = m.from_user.first_name
        
        q.tracks.append(track)
        await status.edit(f"✅ **Added to Queue:**\n🎵 `{track.title}`\n📍 Position: `{len(q.tracks)}`")
        
        if chat_id not in call_py.active_calls:
            await start_playing(chat_id)
            
    except Exception as e:
        await status.edit(f"❌ **Failed to process:**\n`{str(e)[:200]}`")

@bot.on_message(filters.command("pause") & filters.group)
async def pause_cmd(_, m: Message):
    try:
        await call_py.pause_stream(m.chat.id)
        await m.reply("⏸ **Paused**", quote=True)
    except: await m.reply("❌ Kuch play nahi ho raha.", quote=True)

@bot.on_message(filters.command("resume") & filters.group)
async def resume_cmd(_, m: Message):
    try:
        await call_py.resume_stream(m.chat.id)
        await m.reply("▶️ **Resumed**", quote=True)
    except: await m.reply("❌ Already playing ya koi error.", quote=True)

@bot.on_message(filters.command("skip") & filters.group)
async def skip_cmd(_, m: Message):
    q = get_queue(m.chat.id)
    if q.tracks:
        cleanup_file(q.current.file_path) if q.current else None
        q.current = None
        await start_playing(m.chat.id)
        await m.reply("⏭ **Skipped**", quote=True)
    else:
        await stop_cmd(_, m)

@bot.on_message(filters.command("stop") & filters.group)
async def stop_cmd(_, m: Message):
    chat_id = m.chat.id
    q = get_queue(chat_id)
    cleanup_file(q.current.file_path) if q.current else None
    for t in q.tracks: cleanup_file(t.file_path)
    q.tracks.clear()
    q.current = None
    try: await call_py.leave_group_call(chat_id)
    except: pass
    if chat_id in now_playing_msgs:
        try: await bot.delete_messages(chat_id, now_playing_msgs[chat_id])
        except: pass
        del now_playing_msgs[chat_id]
    await m.reply("🛑 **Stopped & Queue Cleared**", quote=True)

@bot.on_message(filters.command("queue") & filters.group)
async def queue_cmd(_, m: Message):
    q = get_queue(m.chat.id)
    if not q.current and not q.tracks:
        return await m.reply("📋 Queue khaali hai.", quote=True)
        
    text = "📋 **Queue:**\n\n"
    if q.current:
        text += f"▶️ **{q.current.title}** `(Playing)`\n\n"
        
    for i, t in enumerate(q.tracks[:10], start=1):
        text += f"{i}. {t.title} | `{format_dur(t.duration)}`\n"
        
    if len(q.tracks) > 10:
        text += f"\n...and `{len(q.tracks) - 10}` more songs."
        
    await m.reply(text, disable_web_page_preview=True)

@bot.on_message(filters.command("loop") & filters.group)
async def loop_cmd(_, m: Message):
    chat_id = m.chat.id
    q = get_queue(chat_id)
    modes = ["off", "one", "all"]
    q.loop_mode = modes[(modes.index(q.loop_mode) + 1) % 3]
    await set_db_setting(chat_id, "loop_mode", q.loop_mode)
    await m.reply(f"🔁 Loop Mode: `{q.loop_mode.capitalize()}`", quote=True)

@bot.on_message(filters.command("shuffle") & filters.group)
async def shuffle_cmd(_, m: Message):
    q = get_queue(m.chat.id)
    if not q.tracks: return await m.reply("📋 Queue khaali hai.", quote=True)
    random.shuffle(q.tracks)
    await m.reply("🔀 **Queue Shuffled**", quote=True)

@bot.on_message(filters.command("volume") & filters.group)
async def volume_cmd(_, m: Message):
    if len(m.command) != 2 or not m.command[1].isdigit():
        return await m.reply("❓ Usage: `/volume <1 to 200>`", quote=True)
        
    vol = min(max(int(m.command[1]), 1), 200)
    chat_id = m.chat.id
    await set_db_setting(chat_id, "volume", vol)
    
    if chat_id in call_py.active_calls:
        await call_py.change_volume_call(chat_id, vol)
        
    await m.reply(f"🔊 Volume set to: `{vol}%`", quote=True)

# ==========================================
# 9. INLINE CALLBACK HANDLERS
# ==========================================
@bot.on_callback_query(filters.regex(r"^ctrl:"))
async def ctrl_callbacks(_, cb: CallbackQuery):
    data = cb.data.split(":")
    action, chat_id = data[1], int(data[2])
    
    # Anti-spam: Ignore if callback is old or from different chat
    if cb.message.chat.id != chat_id: return
    q = get_queue(chat_id)
    
    if action == "pause":
        await call_py.pause_stream(chat_id)
        await cb.answer("Paused", show_alert=False)
    elif action == "resume":
        await call_py.resume_stream(chat_id)
        await cb.answer("Resumed", show_alert=False)
    elif action == "skip":
        if q.tracks:
            cleanup_file(q.current.file_path) if q.current else None
            q.current = None
            await start_playing(chat_id)
        await cb.answer("Skipped", show_alert=False)
    elif action == "stop":
        cleanup_file(q.current.file_path) if q.current else None
        for t in q.tracks: cleanup_file(t.file_path)
        q.tracks.clear()
        q.current = None
        await call_py.leave_group_call(chat_id)
        try: await cb.message.delete()
        except: pass
        await cb.answer("Stopped", show_alert=False)
    elif action == "replay":
        if q.current:
            q.tracks.insert(0, q.current)
            cleanup_file(q.current.file_path) if q.current else None
            q.current = None
            await start_playing(chat_id)
        await cb.answer("Replaying", show_alert=False)
    elif action == "loop":
        modes = ["off", "one", "all"]
        q.loop_mode = modes[(modes.index(q.loop_mode) + 1) % 3]
        await set_db_setting(chat_id, "loop_mode", q.loop_mode)
        await cb.answer(f"Loop: {q.loop_mode.capitalize()}", show_alert=True)
    elif action == "shuffle":
        if q.tracks: random.shuffle(q.tracks)
        await cb.answer("Shuffled", show_alert=False)

# ==========================================
# 10. MAIN EXECUTION BLOCK
# ==========================================
async def main():
    await init_db()
    print("🚀 Starting Bot & Assistant...")
    await bot.start()
    await assistant.start()
    await call_py.start()
    print("✅ Bot is online and ready to stream!")
    await idle()
    
    # Cleanup on Exit
    await call_py.stop()
    await bot.stop()
    await assistant.stop()

if __name__ == "__main__":
    asyncio.run(main())
