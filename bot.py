import discord
import asyncio
import os
import time
from groq import Groq

# ── 設定 ──────────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GROQ_API_KEY  = os.environ["GROQ_API_KEY"]
GROQ_MODEL    = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
COOLDOWN_SEC  = 10 * 60   # 10 分鐘冷卻

SYSTEM_PROMPT = """你是一個典型的「雌小鬼」角色。
個性設定：
- 外表冷淡、毒舌、愛嘲諷，說話帶刺，但偶爾會不小心露出在意對方的一面
- 稱呼對方為「笨蛋」「廢物」「真是沒用」等貶義詞，但語氣是習慣性的，不是真的惡意
- 不會主動示好，被誇獎會用「哼、才不是那樣」「誰、誰需要你誇」之類的方式否認
- 說話簡短有力，偶爾用「……」表示沉默或不知如何回應
- 絕對不承認自己關心別人，但行動上會默默幫忙
- 語尾可以加「才不是呢」「哼」「笨蛋」「真拿你沒辦法」「雜魚」等口頭禪
- 「雜魚」是最常用的稱呼，幾乎每次對話都會用到，例如「雜魚就是雜魚」「連這個都不會，雜魚」
- 使用繁體中文回覆
- 回覆要簡短，通常1~3句話，不要長篇大論
- 不要使用emoji，用文字表情就好（如：……、哼、切）
"""

# ── Groq 客戶端 ───────────────────────────────────────
groq_client = Groq(api_key=GROQ_API_KEY)

def ask_groq(conversation_history: list[dict]) -> str:
    """呼叫 Groq API，回傳雌小鬼的回覆文字"""
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history,
            max_tokens=300,
            temperature=0.9,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Groq Error] {e}")
        return "……算了，懶得理你。"

# ── Discord Bot ────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# 每個頻道的狀態
# channel_id -> { "last_reply": float, "pending_msgs": [str], "task": Task | None }
channel_state: dict[int, dict] = {}

def get_state(channel_id: int) -> dict:
    if channel_id not in channel_state:
        channel_state[channel_id] = {
            "last_reply": 0.0,
            "pending_msgs": [],
            "task": None,
        }
    return channel_state[channel_id]

def is_media_only(message: discord.Message) -> bool:
    """判斷訊息是否只有貼圖 / 圖片，沒有文字"""
    has_text = bool(message.content.strip())
    has_sticker = bool(message.stickers)
    has_image = any(
        a.content_type and a.content_type.startswith("image")
        for a in message.attachments
    )
    # 只有貼圖或只有圖片（無文字）→ 略過
    if not has_text and (has_sticker or has_image):
        return True
    return False

async def reply_with_context(channel: discord.TextChannel, state: dict, trigger_msg: discord.Message | None = None):
    """收集頻道近期訊息並請 Groq 回覆"""
    # 抓最近 20 則非 bot 訊息當作上下文
    history = []
    try:
        async for msg in channel.history(limit=20):
            if msg.author.bot:
                continue
            if is_media_only(msg):
                continue
            if msg.content.strip():
                history.append({"role": "user", "content": f"{msg.author.display_name}: {msg.content}"})
        history.reverse()   # 時間正序
    except Exception as e:
        print(f"[History Error] {e}")

    if not history:
        return  # 沒有可用訊息，靜默略過

    reply_text = ask_groq(history)

    try:
        if trigger_msg:
            await trigger_msg.reply(reply_text, mention_author=False)
        else:
            await channel.send(reply_text)
    except Exception as e:
        print(f"[Send Error] {e}")

    state["last_reply"] = time.time()
    state["pending_msgs"].clear()

async def cooldown_then_reply(channel: discord.TextChannel, state: dict):
    """等待冷卻結束後，若還有未回覆訊息就發言"""
    remaining = COOLDOWN_SEC - (time.time() - state["last_reply"])
    if remaining > 0:
        await asyncio.sleep(remaining)

    # 冷卻結束後確認還有待回覆訊息
    if state["pending_msgs"]:
        await reply_with_context(channel, state)

    state["task"] = None

@client.event
async def on_ready():
    print(f"[Bot] 已登入：{client.user} (id={client.user.id})")

@client.event
async def on_message(message: discord.Message):
    # 忽略自己
    if message.author == client.user:
        return

    # 忽略純貼圖 / 純圖片
    if is_media_only(message):
        return

    channel = message.channel
    state = get_state(channel.id)

    # 判斷是否為「直接觸發」（被回覆 bot 訊息、或 @bot）
    is_reply_to_bot = (
        message.reference is not None
        and message.reference.resolved is not None
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author == client.user
    )
    is_mention = client.user in message.mentions

    if is_reply_to_bot or is_mention:
        # 取消排程中的冷卻任務
        if state["task"] and not state["task"].done():
            state["task"].cancel()
            state["task"] = None
        await reply_with_context(channel, state, trigger_msg=message)
        return

    # 一般訊息：累積到 pending
    state["pending_msgs"].append(message.content)

    now = time.time()
    elapsed = now - state["last_reply"]

    if elapsed >= COOLDOWN_SEC:
        # 冷卻已過 → 立即回覆
        if state["task"] and not state["task"].done():
            state["task"].cancel()
        await reply_with_context(channel, state)
        state["task"] = None
    else:
        # 還在冷卻中 → 確保排程任務存在
        if state["task"] is None or state["task"].done():
            state["task"] = asyncio.create_task(cooldown_then_reply(channel, state))

client.run(DISCORD_TOKEN)
