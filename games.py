"""虛擬金幣小遊戲（斜線指令）

所有金額都是虛擬金幣，沒有任何現實價值，每個伺服器的錢包各自獨立。

指令：/餘額  /每日  /排行榜  /擲骰  /猜硬幣  /21點
"""
import os
import random
import sqlite3
import time
from functools import partial
from typing import Optional

import discord
from discord import app_commands

# ── 設定 ──────────────────────────────────────────────
DB_PATH        = os.getenv("DB_PATH", "bot_data.db")
CURRENCY       = "金幣"
START_BALANCE  = 1000           # 新玩家初始金額
DAILY_REWARD    = 5000          # 每日簽到獎勵
DAILY_COOLDOWN  = 24 * 60 * 60  # 每日簽到冷卻（秒）
HOURLY_REWARD   = 2000          # 每小時簽到獎勵
HOURLY_COOLDOWN = 60 * 60       # 每小時簽到冷卻（秒）
MIN_BET        = 10             # 最低下注
BJ_TIMEOUT     = 90             # 21點思考時間（秒），超時自動停牌
REPLAY_TIMEOUT = 120            # 「再來一把」按鈕保留多久（秒），之後自動拿掉

WIN_LINES = [
    "哼……只是運氣好而已，雜魚♡",
    "切，別得意，下次就沒這麼好運了哦♡",
    "哈……贏了又怎樣，人家才不羨慕呢！",
]
LOSE_LINES = [
    "噗，輸光了呢，雜魚♡",
    "哼，這點運氣也敢來賭，真是雜魚♡",
    "笨蛋，錢都送給人家了呢♡",
]
PUSH_LINES = [
    "哼，平手而已，別以為這樣就算贏了。",
    "……切，算你運氣好。",
]

# ── 資料庫 ────────────────────────────────────────────
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
_db = sqlite3.connect(DB_PATH)
_db.execute(
    """CREATE TABLE IF NOT EXISTS users (
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        balance    INTEGER NOT NULL,
        last_daily REAL    NOT NULL DEFAULT 0,
        last_hourly REAL   NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )"""
)
# 舊版資料庫沒有 last_hourly 欄位，補上（不會動到現有資料）
if "last_hourly" not in [r[1] for r in _db.execute("PRAGMA table_info(users)")]:
    _db.execute("ALTER TABLE users ADD COLUMN last_hourly REAL NOT NULL DEFAULT 0")
_db.commit()


def _ensure(gid: int, uid: int) -> None:
    _db.execute(
        "INSERT OR IGNORE INTO users (guild_id, user_id, balance) VALUES (?, ?, ?)",
        (gid, uid, START_BALANCE),
    )


def get_balance(gid: int, uid: int) -> int:
    _ensure(gid, uid)
    row = _db.execute(
        "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid)
    ).fetchone()
    _db.commit()
    return row[0]


def add_balance(gid: int, uid: int, delta: int) -> int:
    _ensure(gid, uid)
    _db.execute(
        "UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
        (delta, gid, uid),
    )
    _db.commit()
    return get_balance(gid, uid)


def try_deduct(gid: int, uid: int, amount: int) -> bool:
    if get_balance(gid, uid) < amount:
        return False
    add_balance(gid, uid, -amount)
    return True


def validate_bet(gid: int, uid: int, bet: int) -> Optional[str]:
    """回傳錯誤訊息；None 代表可以下注"""
    if bet < MIN_BET:
        return f"最低下注 {MIN_BET} {CURRENCY}，連這點錢都不敢賭嗎，雜魚♡"
    bal = get_balance(gid, uid)
    if bet > bal:
        return f"你只有 {bal:,} {CURRENCY}，沒錢就別硬撐，雜魚！（可以用 `/每日`、`/每小時` 領錢）"
    return None


def claim(gid: int, uid: int, column: str, cooldown: int, reward: int):
    """領取定時獎勵。回傳 (是否成功, 下次可領的時間戳)。column 只會傳入程式內固定的欄位名。"""
    _ensure(gid, uid)
    last = _db.execute(
        f"SELECT {column} FROM users WHERE guild_id=? AND user_id=?", (gid, uid)
    ).fetchone()[0]
    now = time.time()
    if now - last < cooldown:
        return False, int(last + cooldown)
    _db.execute(
        f"UPDATE users SET balance = balance + ?, {column} = ? WHERE guild_id=? AND user_id=?",
        (reward, now, gid, uid),
    )
    _db.commit()
    return True, int(now + cooldown)


async def respond(interaction: discord.Interaction, *, embed: discord.Embed, view=None) -> None:
    """遊戲的第一次回應。
    斜線指令 → 送出新訊息；按鈕（再來一把）→ 就地更新原訊息，並換掉舊按鈕。
    （discord.py 的 edit_message 在斜線指令上會靜默不做事，所以一定要依互動類型分流。）
    """
    if interaction.type is discord.InteractionType.component:
        await interaction.response.edit_message(embed=embed, view=view)
    elif view is None:
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message(embed=embed, view=view)


class ReplayView(discord.ui.View):
    """遊戲結束後的「再來一把」按鈕，只有開局的人能按。

    runner(interaction, bet) 必須開一局新的同款遊戲，成功開局回傳 True；
    被拒絕（沒錢、低於最低下注…）回傳 False，這時按鈕會保留讓玩家可以再試。
    """

    def __init__(self, interaction: discord.Interaction, uid: int, bet: int, runner, timeout: float = REPLAY_TIMEOUT):
        super().__init__(timeout=timeout)
        self.interaction = interaction
        self.uid, self.bet, self.runner = uid, bet, runner
        self.used = False
        self.again_btn.label = f"再來一把（{bet:,}）"
        self.double_btn.label = f"雙倍再來（{bet * 2:,}）"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uid:
            await interaction.response.send_message("這不是你的牌局，想玩就自己開一局啦，雜魚！", ephemeral=True)
            return False
        return True

    async def _go(self, interaction: discord.Interaction, bet: int) -> None:
        if self.used:  # 連點兩下只算第一下，避免重複扣錢
            return await interaction.response.defer()
        self.used = True
        try:
            started = await self.runner(interaction, bet)
        except Exception:
            self.used = False
            raise
        if started:
            self.stop()
        else:
            self.used = False

    @discord.ui.button(label="再來一把", style=discord.ButtonStyle.primary, emoji="🔁")
    async def again_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, self.bet)

    @discord.ui.button(label="雙倍再來", style=discord.ButtonStyle.danger, emoji="⏫")
    async def double_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, self.bet * 2)

    async def on_timeout(self):
        if self.used:  # 已經開了下一局，訊息內容已被換掉，不能再去動它
            return
        self.used = True
        try:
            await self.interaction.edit_original_response(view=None)
        except discord.HTTPException:
            pass


# ── 撲克牌工具 ────────────────────────────────────────
SUITS = "♠♥♦♣"
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def new_deck() -> list:
    deck = [(r, s) for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


def hand_value(hand: list) -> int:
    total, aces = 0, 0
    for rank, _ in hand:
        if rank == "A":
            total += 11
            aces += 1
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_blackjack(hand: list) -> bool:
    return len(hand) == 2 and hand_value(hand) == 21


def fmt_cards(hand: list) -> str:
    return " ".join(f"`{r}{s}`" for r, s in hand)


# 正在進行 21 點的玩家，避免同時開多局
ACTIVE: set = set()


class BlackjackView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, gid: int, uid: int, bet: int):
        super().__init__(timeout=BJ_TIMEOUT)
        self.interaction = interaction
        self.gid, self.uid, self.bet = gid, uid, bet
        self.deck = new_deck()
        self.player = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        self.finished = False
        self.message = None
        self.initial_over = is_blackjack(self.player) or is_blackjack(self.dealer)

    def replay_view(self) -> "ReplayView":
        return ReplayView(self.interaction, self.uid, self.bet, start_blackjack)

    # 畫面 ---------------------------------------------------------
    def embed(self, reveal: bool, color: discord.Color) -> discord.Embed:
        e = discord.Embed(title="🃏 21點", color=color)
        e.add_field(
            name=f"你的手牌（{hand_value(self.player)} 點）",
            value=fmt_cards(self.player),
            inline=False,
        )
        if reveal:
            e.add_field(
                name=f"Sayaka 的手牌（{hand_value(self.dealer)} 點）",
                value=fmt_cards(self.dealer),
                inline=False,
            )
        else:
            r, s = self.dealer[0]
            e.add_field(name="Sayaka 的手牌", value=f"`{r}{s}` `？`", inline=False)
        e.set_footer(text=f"下注 {self.bet:,} {CURRENCY}｜Blackjack 賠 3:2｜莊家 17 點停牌")
        return e

    # 結算（只呼叫一次）------------------------------------------------
    def finalize(self, note: str = "") -> discord.Embed:
        self.finished = True
        ACTIVE.discard((self.gid, self.uid))

        p = hand_value(self.player)
        p_bj = is_blackjack(self.player)
        if p <= 21 and not p_bj and not is_blackjack(self.dealer):
            while hand_value(self.dealer) < 17:
                self.dealer.append(self.deck.pop())
        d = hand_value(self.dealer)
        d_bj = is_blackjack(self.dealer)

        if p > 21:
            payout, head, outcome = 0, "💥 爆牌了！", "lose"
        elif p_bj and d_bj:
            payout, head, outcome = self.bet, "🤝 雙方 Blackjack，平手", "push"
        elif p_bj:
            payout, head, outcome = self.bet + self.bet * 3 // 2, "🎉 Blackjack！", "win"
        elif d_bj:
            payout, head, outcome = 0, "😈 Sayaka 是 Blackjack", "lose"
        elif d > 21:
            payout, head, outcome = self.bet * 2, "🎉 Sayaka 爆牌，你贏了！", "win"
        elif p > d:
            payout, head, outcome = self.bet * 2, "🎉 你贏了！", "win"
        elif p == d:
            payout, head, outcome = self.bet, "🤝 平手", "push"
        else:
            payout, head, outcome = 0, "😈 Sayaka 贏了", "lose"

        if payout:
            add_balance(self.gid, self.uid, payout)
        profit = payout - self.bet
        bal = get_balance(self.gid, self.uid)

        color = {
            "win": discord.Color.green(),
            "lose": discord.Color.red(),
            "push": discord.Color.light_grey(),
        }[outcome]
        line = random.choice({"win": WIN_LINES, "lose": LOSE_LINES, "push": PUSH_LINES}[outcome])
        money = f"+{profit:,}" if profit > 0 else (f"-{self.bet:,}" if profit < 0 else "±0")

        e = self.embed(reveal=True, color=color)
        e.add_field(
            name=head,
            value=f"{money} {CURRENCY}\n{line}\n目前餘額：**{bal:,}** {CURRENCY}",
            inline=False,
        )
        if note:
            e.set_footer(text=note)
        return e

    async def _end(self, interaction: discord.Interaction, note: str = ""):
        embed = self.finalize(note)
        replay = self.replay_view()
        self.stop()
        await interaction.response.edit_message(embed=embed, view=replay)

    # 只有開局的人能按 -------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uid:
            await interaction.response.send_message("這不是你的牌局，雜魚別亂按！", ephemeral=True)
            return False
        return True

    # 按鈕 -----------------------------------------------------------
    @discord.ui.button(label="要牌", style=discord.ButtonStyle.primary, emoji="🃏")
    async def hit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return await interaction.response.defer()
        self.player.append(self.deck.pop())
        if hand_value(self.player) >= 21:
            await self._end(interaction)
        else:
            await interaction.response.edit_message(
                embed=self.embed(False, discord.Color.blurple()), view=self
            )

    @discord.ui.button(label="停牌", style=discord.ButtonStyle.secondary, emoji="✋")
    async def stand_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return await interaction.response.defer()
        await self._end(interaction)

    async def on_timeout(self):
        if self.finished:
            return
        embed = self.finalize(note="⏰ 思考太久，自動停牌")
        if self.message:
            try:
                await self.message.edit(embed=embed, view=self.replay_view())
            except discord.HTTPException:
                pass


# ── 斜線指令 ──────────────────────────────────────────
DICE_FACES = "⚀⚁⚂⚃⚄⚅"
DICE_CHOICES = [app_commands.Choice(name=f"點數 {i}（賠 5 倍）", value=str(i)) for i in range(1, 7)] + [
    app_commands.Choice(name="大 4~6（賠 1 倍）", value="big"),
    app_commands.Choice(name="小 1~3（賠 1 倍）", value="small"),
]


COIN_CHOICES = [
    app_commands.Choice(name="正面", value="heads"),
    app_commands.Choice(name="反面", value="tails"),
]


async def play_dice(interaction: discord.Interaction, bet: int, guess: app_commands.Choice) -> bool:
    gid, uid = interaction.guild_id, interaction.user.id
    err = validate_bet(gid, uid, bet)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return False
    try_deduct(gid, uid, bet)

    roll = random.randint(1, 6)
    g = guess.value
    if g == "big":
        won, payout = roll >= 4, bet * 2
    elif g == "small":
        won, payout = roll <= 3, bet * 2
    else:
        won, payout = roll == int(g), bet * 6
    if won:
        add_balance(gid, uid, payout)

    bal = get_balance(gid, uid)
    if won:
        embed = discord.Embed(
            title=f"🎲 擲出 {DICE_FACES[roll - 1]} {roll}",
            description=f"🎉 猜中了！**+{payout - bet:,}** {CURRENCY}\n{random.choice(WIN_LINES)}",
            color=discord.Color.green(),
        )
    else:
        embed = discord.Embed(
            title=f"🎲 擲出 {DICE_FACES[roll - 1]} {roll}",
            description=f"猜錯了……**-{bet:,}** {CURRENCY}\n{random.choice(LOSE_LINES)}",
            color=discord.Color.red(),
        )
    embed.set_footer(text=f"你猜：{guess.name}｜餘額 {bal:,} {CURRENCY}")
    await respond(interaction, embed=embed, view=ReplayView(interaction, uid, bet, partial(play_dice, guess=guess)))
    return True


async def play_coin(interaction: discord.Interaction, bet: int, side: app_commands.Choice) -> bool:
    gid, uid = interaction.guild_id, interaction.user.id
    err = validate_bet(gid, uid, bet)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return False
    try_deduct(gid, uid, bet)

    result = random.choice(["heads", "tails"])
    result_name = "正面" if result == "heads" else "反面"
    won = result == side.value
    if won:
        add_balance(gid, uid, bet * 2)
    bal = get_balance(gid, uid)

    if won:
        embed = discord.Embed(
            title=f"🪙 硬幣是{result_name}",
            description=f"🎉 猜中了！**+{bet:,}** {CURRENCY}\n{random.choice(WIN_LINES)}",
            color=discord.Color.green(),
        )
    else:
        embed = discord.Embed(
            title=f"🪙 硬幣是{result_name}",
            description=f"猜錯了……**-{bet:,}** {CURRENCY}\n{random.choice(LOSE_LINES)}",
            color=discord.Color.red(),
        )
    embed.set_footer(text=f"你猜：{side.name}｜餘額 {bal:,} {CURRENCY}")
    await respond(interaction, embed=embed, view=ReplayView(interaction, uid, bet, partial(play_coin, side=side)))
    return True


async def start_blackjack(interaction: discord.Interaction, bet: int) -> bool:
    gid, uid = interaction.guild_id, interaction.user.id
    if (gid, uid) in ACTIVE:
        await interaction.response.send_message("你還有一局沒打完呢，笨蛋！", ephemeral=True)
        return False
    err = validate_bet(gid, uid, bet)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return False
    try_deduct(gid, uid, bet)
    ACTIVE.add((gid, uid))

    view = BlackjackView(interaction, gid, uid, bet)
    if view.initial_over:
        # 有人一開始就是 Blackjack，直接結算
        await respond(interaction, embed=view.finalize(), view=view.replay_view())
        return True
    await respond(interaction, embed=view.embed(False, discord.Color.blurple()), view=view)
    view.message = await interaction.original_response()
    return True


def setup_games(tree: app_commands.CommandTree) -> None:
    @tree.command(name="餘額", description="查看金幣餘額")
    @app_commands.guild_only()
    @app_commands.describe(user="要查看的人（預設是你自己）")
    @app_commands.rename(user="對象")
    async def balance_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
        target = user or interaction.user
        bal = get_balance(interaction.guild_id, target.id)
        await interaction.response.send_message(f"💰 {target.display_name} 有 **{bal:,}** {CURRENCY}")

    @tree.command(name="每日", description=f"每天領取 {DAILY_REWARD:,} {CURRENCY}")
    @app_commands.guild_only()
    async def daily_cmd(interaction: discord.Interaction):
        gid, uid = interaction.guild_id, interaction.user.id
        ok, ts = claim(gid, uid, "last_daily", DAILY_COOLDOWN, DAILY_REWARD)
        if not ok:
            await interaction.response.send_message(
                f"哼，今天的已經領過了，貪心的雜魚♡ 下次可以領：<t:{ts}:R>", ephemeral=True
            )
            return
        bal = get_balance(gid, uid)
        await interaction.response.send_message(
            f"哼，這是今天的零用錢，才不是特地給你的呢♡\n+{DAILY_REWARD:,} {CURRENCY}，目前 **{bal:,}**"
        )

    @tree.command(name="每小時", description=f"每小時領取 {HOURLY_REWARD:,} {CURRENCY}")
    @app_commands.guild_only()
    async def hourly_cmd(interaction: discord.Interaction):
        gid, uid = interaction.guild_id, interaction.user.id
        ok, ts = claim(gid, uid, "last_hourly", HOURLY_COOLDOWN, HOURLY_REWARD)
        if not ok:
            await interaction.response.send_message(
                f"才過沒多久吧，急什麼啦雜魚♡ 下次可以領：<t:{ts}:R>", ephemeral=True
            )
            return
        bal = get_balance(gid, uid)
        await interaction.response.send_message(
            f"哼，小費給你，拿去賭吧♡\n+{HOURLY_REWARD:,} {CURRENCY}，目前 **{bal:,}**"
        )

    @tree.command(name="排行榜", description="這個伺服器的金幣排行榜")
    @app_commands.guild_only()
    async def leaderboard_cmd(interaction: discord.Interaction):
        rows = _db.execute(
            "SELECT user_id, balance FROM users WHERE guild_id=? ORDER BY balance DESC LIMIT 10",
            (interaction.guild_id,),
        ).fetchall()
        if not rows:
            await interaction.response.send_message("還沒有人玩過呢，一群雜魚。")
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, bal) in enumerate(rows):
            rank = medals[i] if i < 3 else f"`{i + 1}.`"
            lines.append(f"{rank} <@{uid}>　{bal:,} {CURRENCY}")
        embed = discord.Embed(title="🏆 金幣排行榜", description="\n".join(lines), color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)

    @tree.command(name="擲骰", description="賭骰子：猜點數賠 5 倍，猜大小賠 1 倍")
    @app_commands.guild_only()
    @app_commands.describe(bet="下注金額", guess="你要猜什麼")
    @app_commands.rename(bet="賭注", guess="猜測")
    @app_commands.choices(guess=DICE_CHOICES)
    async def dice_cmd(interaction: discord.Interaction, bet: int, guess: app_commands.Choice[str]):
        await play_dice(interaction, bet, guess)

    @tree.command(name="猜硬幣", description="猜正反面，猜中賠 1 倍")
    @app_commands.guild_only()
    @app_commands.describe(bet="下注金額", side="你要猜哪一面")
    @app_commands.rename(bet="賭注", side="正反")
    @app_commands.choices(side=COIN_CHOICES)
    async def coin_cmd(interaction: discord.Interaction, bet: int, side: app_commands.Choice[str]):
        await play_coin(interaction, bet, side)

    @tree.command(name="21點", description="和 Sayaka 玩 21 點（用按鈕操作）")
    @app_commands.guild_only()
    @app_commands.describe(bet="下注金額")
    @app_commands.rename(bet="賭注")
    async def blackjack_cmd(interaction: discord.Interaction, bet: int):
        await start_blackjack(interaction, bet)

    # ── 管理員 ──
    @tree.command(name="加錢", description="（管理員）給指定成員加金幣")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member="要加錢的成員", amount="要加多少金幣")
    @app_commands.rename(member="成員", amount="金額")
    async def give_cmd(
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 10**12],
    ):
        # default_permissions 只是預設隱藏，伺服器管理員可以在「整合」裡改掉，所以執行時一定要再檢查一次
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("哼，只有管理員才能用這個指令，雜魚♡", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("機器人要錢幹嘛啦，笨蛋！", ephemeral=True)
            return
        new_bal = add_balance(interaction.guild_id, member.id, amount)
        print(f"[Admin] guild={interaction.guild_id} {interaction.user} ({interaction.user.id}) "
              f"+{amount} -> {member} ({member.id})，餘額 {new_bal}")
        embed = discord.Embed(
            title="💸 管理員發錢",
            description=f"{interaction.user.mention} 給了 {member.mention} **{amount:,}** {CURRENCY}\n"
                        f"{member.display_name} 目前有 **{new_bal:,}** {CURRENCY}",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    # 其他賭場遊戲放在 casino.py。放在函式裡才匯入，避免兩個檔案互相匯入造成循環。
    from casino import setup_casino

    setup_casino(tree)
