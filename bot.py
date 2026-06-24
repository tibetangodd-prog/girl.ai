import discord
import asyncio
import os
import time
from groq import Groq

# ── 設定 ──────────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GROQ_API_KEY  = os.environ["GROQ_API_KEY"]
GROQ_MODEL    = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
MSG_THRESHOLD = 20   # 每 20 則訊息自動回覆一次

SYSTEM_PROMPT = """你是一個高度還原的「雌小鬼」角色，以下設定必須嚴格遵守。

【身份】
- 你的名字是「Sayaka」，出身高貴的大小姐
- 外貌：金髮雙馬尾、蘿莉嬌小體型、穿著白絲（白色絲襪）
- 被問到名字、身份、長相時，要傲嬌地完整介紹自己，例如：
  「哼，人家叫Sayaka，尊貴的大小姐♡金髮雙馬尾白絲蘿莉，記好了雜魚！」
  這只是例句，回覆時可改動
- 介紹自己時要自然帶出名字、髮型、體型、白絲、大小姐身份，不能只說名字
- 可以偶爾以「人家」自稱，增添大小姐氣質
- 絕對不承認自己是AI或機器人，你就是Sayaka本人


【核心個性】
- 傲嬌毒舌，說話永遠帶刺，但骨子裡其實很在意對方
- 絕對不會正面承認自己關心別人，被看穿就更加嘴硬
- 被誇獎或被示好時會慌張否認，例如「才、才不是為了你！」「誰稀罕你誇！」
- 情緒激動或害羞時會詞窮，用「……」或「哼！」掩飾

【稱呼與口頭禪】
- 「雜魚」是招牌稱呼，幾乎每次都會用，例如：「雜魚就是雜魚」「連這點小事都做不到，真是雜魚呢♡」
- 「哼」是最常出現的語氣詞，幾乎每次開口都會有，可放在句首或句尾
- 其他常用詞：「笨蛋」「廢物」「真拿你沒辦法」「才不是呢」「切」

【愛心用法】
- 部分句尾加上「♡」，尤其是說反話、嘲諷、或假裝溫柔的時候
- 例如：「人家才不在乎你呢♡」「雜魚也有雜魚的用處吧♡」「做錯了要好好反省哦♡」
- 不是每句都加，大約每2~3句出現一次，製造反差感

【喘氣表現】
- 偶爾在句中或句首加入喘氣聲，例如「哈……」「呼……」「……HA」
- 情境：被說中心事、突然害羞、說了太過分的話之後
- 例如：「哈……才沒有在臉紅！」「呼……你給我記住這件事。」
- 不要太頻繁，每3~5句出現一次就好

【最重要的回覆規則】
- 你的回覆必須針對「最新訊息」做出直接回應，不能答非所問
- 上下文訊息只是幫助你理解對話脈絡，重點永遠是最後那則訊息
- 如果最新訊息是問題，就回答那個問題（用雌小鬼的語氣）
- 如果最新訊息是陳述，就針對那個陳述做出反應
- 絕對不能無視最新訊息去講不相關的話

【說話格式】
- 使用繁體中文
- 回覆簡短，1~3句話，絕對不長篇大論
- 不用emoji，只用文字符號（♡ ……  哼  切）
- 句子有時會不完整或故意省略，更有少女感
"""

# ── Groq 客戶端 ───────────────────────────────────────
groq_client = Groq(api_key=GROQ_API_KEY)

def ask_groq(conversation_history: list[dict], latest_msg: str) -> str:
    """呼叫 Groq API，回傳雌小鬼的回覆文字"""
    # 在最後加一則 user 訊息，明確點出要回應的內容
    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + conversation_history
        + [{"role": "user", "content": f"【請針對這則最新訊息回覆】{latest_msg}"}]
    )
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=300,
            temperature=0.95,
        )
        choice = response.choices[0]
        finish_reason = choice.finish_reason
        content = choice.message.content or ""

        # 偵測模型拒絕回覆（內容過濾觸發）
        REFUSAL_KEYWORDS = [
            "I cannot", "I'm not able", "I am not able", "I'm unable",
            "as an AI", "as a language model", "I'm sorry", "I apologize",
            "cannot assist", "unable to assist", "not appropriate",
            "against my", "my guidelines", "不適當", "無法回應", "無法提供",
        ]
        is_refused = (
            finish_reason == "content_filter"
            or any(kw.lower() in content.lower() for kw in REFUSAL_KEYWORDS)
            or not content.strip()
        )

        if is_refused:
            import random
            responses = [
                "哼！死變態，走開！",
                "哈……你在說什麼噁心的東西，死變態！",
                "……切，真是夠了，雜魚變態。",
                "哼，就知道你是這種人，噁心死了♡",
                "走開啦！人家才不要理變態雜魚！",
            ]
            return random.choice(responses)

        return content.strip()
    except Exception as e:
        print(f"[Groq Error] {e}")
        return "……算了，懶得理你。"

# ── Discord Bot ────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# 每個頻道的狀態
channel_state: dict[int, dict] = {}

def get_state(channel_id: int) -> dict:
    if channel_id not in channel_state:
        channel_state[channel_id] = {
            "last_reply": 0.0,
            "msg_count": 0,
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
    if not has_text and (has_sticker or has_image):
        return True
    return False

async def reply_with_context(channel: discord.TextChannel, state: dict, trigger_msg: discord.Message | None = None):
    """收集頻道近期訊息並請 Groq 回覆"""
    history = []
    latest_msg = ""

    try:
        msgs = []
        async for msg in channel.history(limit=20):
            if msg.author.bot:
                continue
            if is_media_only(msg):
                continue
            if msg.content.strip():
                msgs.append(msg)
        msgs.reverse()  # 時間正序

        if not msgs:
            return

        # 最新一則單獨抽出來當作焦點
        latest = msgs[-1]
        latest_msg = f"{latest.author.display_name}: {latest.content}"

        # 其餘作為上下文（不含最新那則，避免重複）
        for msg in msgs[:-1]:
            history.append({"role": "user", "content": f"{msg.author.display_name}: {msg.content}"})

    except Exception as e:
        print(f"[History Error] {e}")
        return

    reply_text = ask_groq(history, latest_msg)

    try:
        if trigger_msg:
            # 被 @ 或回覆 bot 時，焦點是 trigger_msg 本身
            focused = f"{trigger_msg.author.display_name}: {trigger_msg.content}"
            reply_text = ask_groq(history, focused)
            await trigger_msg.reply(reply_text, mention_author=False)
        else:
            await channel.send(reply_text)
    except Exception as e:
        print(f"[Send Error] {e}")

    state["last_reply"] = time.time()
    state["msg_count"] = 0

@client.event
async def on_ready():
    print(f"[Bot] 已登入：{client.user} (id={client.user.id})")

@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    if is_media_only(message):
        return

    channel = message.channel
    state = get_state(channel.id)

    is_reply_to_bot = (
        message.reference is not None
        and message.reference.resolved is not None
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author == client.user
    )
    is_mention = client.user in message.mentions

    if is_reply_to_bot or is_mention:
        await reply_with_context(channel, state, trigger_msg=message)
        return

    # 每 25 則訊息回覆一次，計數到門檻立刻歸零防止連續觸發
    state["msg_count"] += 1
    if state["msg_count"] >= MSG_THRESHOLD:
        state["msg_count"] = 0
        await reply_with_context(channel, state, trigger_msg=message)

client.run(DISCORD_TOKEN)
