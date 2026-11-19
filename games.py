"""虛擬金幣小遊戲（斜線指令）

所有金額都是虛擬金幣，沒有任何現實價值，每個伺服器的錢包各自獨立。

指令：/餘額  /每日  /排行榜  /擲骰  /猜硬幣  /21點
"""
import os
import random
import sqlite3
import time
from typing import Optional

import discord
from discord import app_commands

# ── 設定 ──────────────────────────────────────────────
DB_PATH        = os.getenv("DB_PATH", "bot_data.db")
CURRENCY       = "金幣"
START_BALANCE  = 1000           # 新玩家初始金額
DAILY_REWARD   = 500            # 每日簽到獎勵
DAILY_COOLDOWN = 24 * 60 * 60   # 簽到冷卻（秒）
MIN_BET        = 10             # 最低下注
BJ_TIMEOUT     = 90             # 21點思考時間（秒），超時自動停牌

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
        PRIMARY KEY (guild_id, user_id)
    )"""
)
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
        return f"你只有 {bal:,} {CURRENCY}，沒錢就別硬撐，雜魚！（可以用 `/每日` 領錢）"
    return None


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
    def __init__(self, gid: int, uid: int, bet: int):
        super().__init__(timeout=BJ_TIMEOUT)
        self.gid, self.uid, self.bet = gid, uid, bet
        self.deck = new_deck()
        self.player = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        self.finished = False
        self.message = None
        self.initial_over = is_blackjack(self.player) or is_blackjack(self.dealer)
        self.double_btn.disabled = get_balance(gid, uid) < bet

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
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(embed=embed, view=self)

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
        self.double_btn.disabled = True
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

    @discord.ui.button(label="雙倍", style=discord.ButtonStyle.danger, emoji="💰")
    async def double_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return await interaction.response.defer()
        if len(self.player) != 2 or not try_deduct(self.gid, self.uid, self.bet):
            return await interaction.response.send_message(
                "現在不能雙倍（只能在前兩張牌，而且要有足夠的錢）。", ephemeral=True
            )
        self.bet *= 2
        self.player.append(self.deck.pop())
        await self._end(interaction)

    async def on_timeout(self):
        if self.finished:
            return
        embed = self.finalize(note="⏰ 思考太久，自動停牌")
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass


# ── 斜線指令 ──────────────────────────────────────────
DICE_FACES = "⚀⚁⚂⚃⚄⚅"
DICE_CHOICES = [app_commands.Choice(name=f"點數 {i}（賠 5 倍）", value=str(i)) for i in range(1, 7)] + [
    app_commands.Choice(name="大 4~6（賠 1 倍）", value="big"),
    app_commands.Choice(name="小 1~3（賠 1 倍）", value="small"),
]


def setup_games(tree: app_commands.CommandTree) -> None:
    @tree.command(name="餘額", description="查看金幣餘額")
    @app_commands.guild_only()
    @app_commands.describe(user="要查看的人（預設是你自己）")
    @app_commands.rename(user="對象")
    async def balance_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
        target = user or interaction.user
        bal = get_balance(interaction.guild_id, target.id)
        await interaction.response.send_message(f"💰 {target.display_name} 有 **{bal:,}** {CURRENCY}")

    @tree.command(name="每日", description=f"每天領取 {DAILY_REWARD} {CURRENCY}")
    @app_commands.guild_only()
    async def daily_cmd(interaction: discord.Interaction):
        gid, uid = interaction.guild_id, interaction.user.id
        _ensure(gid, uid)
        last = _db.execute(
            "SELECT last_daily FROM users WHERE guild_id=? AND user_id=?", (gid, uid)
        ).fetchone()[0]
        now = time.time()
        if now - last < DAILY_COOLDOWN:
            ready = int(last + DAILY_COOLDOWN)
            await interaction.response.send_message(
                f"哼，今天的已經領過了，貪心的雜魚♡ 下次可以領：<t:{ready}:R>", ephemeral=True
            )
            return
        _db.execute(
            "UPDATE users SET balance = balance + ?, last_daily = ? WHERE guild_id=? AND user_id=?",
            (DAILY_REWARD, now, gid, uid),
        )
        _db.commit()
        bal = get_balance(gid, uid)
        await interaction.response.send_message(
            f"哼，這是今天的零用錢，才不是特地給你的呢♡\n+{DAILY_REWARD:,} {CURRENCY}，目前 **{bal:,}**"
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
        gid, uid = interaction.guild_id, interaction.user.id
        err = validate_bet(gid, uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
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
        await interaction.response.send_message(embed=embed)

    @tree.command(name="猜硬幣", description="猜正反面，猜中賠 1 倍")
    @app_commands.guild_only()
    @app_commands.describe(bet="下注金額", side="你要猜哪一面")
    @app_commands.rename(bet="賭注", side="正反")
    @app_commands.choices(
        side=[
            app_commands.Choice(name="正面", value="heads"),
            app_commands.Choice(name="反面", value="tails"),
        ]
    )
    async def coin_cmd(interaction: discord.Interaction, bet: int, side: app_commands.Choice[str]):
        gid, uid = interaction.guild_id, interaction.user.id
        err = validate_bet(gid, uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
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
        await interaction.response.send_message(embed=embed)

    @tree.command(name="21點", description="和 Sayaka 玩 21 點（用按鈕操作）")
    @app_commands.guild_only()
    @app_commands.describe(bet="下注金額")
    @app_commands.rename(bet="賭注")
    async def blackjack_cmd(interaction: discord.Interaction, bet: int):
        gid, uid = interaction.guild_id, interaction.user.id
        if (gid, uid) in ACTIVE:
            await interaction.response.send_message("你還有一局沒打完呢，笨蛋！", ephemeral=True)
            return
        err = validate_bet(gid, uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        try_deduct(gid, uid, bet)
        ACTIVE.add((gid, uid))

        view = BlackjackView(gid, uid, bet)
        if view.initial_over:
            # 有人一開始就是 Blackjack，直接結算
            await interaction.response.send_message(embed=view.finalize())
            return
        await interaction.response.send_message(
            embed=view.embed(False, discord.Color.blurple()), view=view
        )
        view.message = await interaction.original_response()
