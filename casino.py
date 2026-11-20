"""賭場遊戲（虛擬金幣，沒有任何現實價值）

/老虎機  /輪盤  /百家樂  /骰寶  /高低牌  /賽馬  /刮刮樂  /決鬥  /賭場

金幣系統（餘額、扣款、下注檢查）都來自 games.py。
有動畫的遊戲一律「先結算、再播動畫」，就算動畫中途出錯，錢也不會算錯。
"""
import asyncio
import random
from functools import partial
from typing import Optional

import discord
from discord import app_commands

from games import (
    CURRENCY,
    DICE_FACES,
    LOSE_LINES,
    PUSH_LINES,
    RANKS,
    ReplayView,
    SUITS,
    WIN_LINES,
    add_balance,
    fmt_cards,
    get_balance,
    respond,
    try_deduct,
    validate_bet,
)


async def pause(seconds: float) -> None:
    """動畫停頓。獨立成函式，測試時可以換成不等待的版本。"""
    await asyncio.sleep(seconds)


# ── 共用工具 ──────────────────────────────────────────
def net_text(net: int) -> str:
    if net > 0:
        return f"+{net:,}"
    if net < 0:
        return f"-{-net:,}"
    return "±0"


def outcome_color(net: int) -> discord.Color:
    if net > 0:
        return discord.Color.green()
    if net < 0:
        return discord.Color.red()
    return discord.Color.light_grey()


def settle_embed(title: str, detail: str, bet: int, payout: int, gid: int, uid: int) -> discord.Embed:
    """payout 是「拿回的總額」（含本金），已經加進餘額之後才呼叫。"""
    net = payout - bet
    line = random.choice(WIN_LINES if net > 0 else LOSE_LINES if net < 0 else PUSH_LINES)
    e = discord.Embed(
        title=title,
        description=f"{detail}\n\n**{net_text(net)} {CURRENCY}**\n{line}",
        color=outcome_color(net),
    )
    e.set_footer(text=f"下注 {bet:,}｜餘額 {get_balance(gid, uid):,} {CURRENCY}")
    return e


async def reject(interaction: discord.Interaction, msg: str) -> None:
    await interaction.response.send_message(msg, ephemeral=True)


async def safe_edit(interaction: discord.Interaction, **kw) -> None:
    try:
        await interaction.edit_original_response(**kw)
    except discord.HTTPException:
        pass


# ════════════════════════════════════════════════════════
#  老虎機
# ════════════════════════════════════════════════════════
SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "💎", "7️⃣"]
SLOT_WEIGHTS = [30, 25, 20, 12, 8, 5]
SLOT_TRIPLE = {"🍒": 4, "🍋": 6, "🍇": 10, "🔔": 20, "💎": 50, "7️⃣": 100}


def slot_payout(reels: list, bet: int):
    a, b, c = reels
    if a == b == c:
        return bet * SLOT_TRIPLE[a], f"三個 {a}！×{SLOT_TRIPLE[a]}"
    if a == b or b == c or a == c:
        return bet * 12 // 10, "兩個相同 ×1.2"
    return 0, "沒有連線"


def slot_frame(reels: list, shown: int) -> discord.Embed:
    cells = [reels[i] if i < shown else "🔄" for i in range(3)]
    return discord.Embed(
        title="🎰 老虎機", description=f"**｜ {' ｜ '.join(cells)} ｜**", color=discord.Color.blurple()
    )


# ════════════════════════════════════════════════════════
#  輪盤（歐式，單個 0）
# ════════════════════════════════════════════════════════
RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
COLOR_EMOJI = {"green": "🟩", "red": "🟥", "black": "⬛"}


def roulette_color(n: int) -> str:
    if n == 0:
        return "green"
    return "red" if n in RED_NUMBERS else "black"


# value: (名稱, 判斷函式, 拿回的倍數)
ROULETTE_BETS = {
    "red": ("紅", lambda n: n in RED_NUMBERS, 2),
    "black": ("黑", lambda n: n != 0 and n not in RED_NUMBERS, 2),
    "odd": ("單", lambda n: n != 0 and n % 2 == 1, 2),
    "even": ("雙", lambda n: n != 0 and n % 2 == 0, 2),
    "low": ("小 1-18", lambda n: 1 <= n <= 18, 2),
    "high": ("大 19-36", lambda n: 19 <= n <= 36, 2),
    "d1": ("第一打 1-12", lambda n: 1 <= n <= 12, 3),
    "d2": ("第二打 13-24", lambda n: 13 <= n <= 24, 3),
    "d3": ("第三打 25-36", lambda n: 25 <= n <= 36, 3),
    "num": ("單一號碼", None, 36),
}
ROULETTE_CHOICES = [
    app_commands.Choice(
        name=f"{name}（×{mult}" + ("，要填號碼）" if key == "num" else "）"), value=key
    )
    for key, (name, _, mult) in ROULETTE_BETS.items()
]


def roulette_payout(kind: str, spin: int, bet: int, number: Optional[int] = None) -> int:
    name, pred, mult = ROULETTE_BETS[kind]
    won = (spin == number) if kind == "num" else pred(spin)
    return bet * mult if won else 0


# ════════════════════════════════════════════════════════
#  百家樂（8 副牌，標準補牌規則）
# ════════════════════════════════════════════════════════
def new_shoe() -> list:
    shoe = [(r, s) for r in RANKS for s in SUITS] * 8
    random.shuffle(shoe)
    return shoe


def bac_val(card) -> int:
    r = card[0]
    if r == "A":
        return 1
    if r in ("10", "J", "Q", "K"):
        return 0
    return int(r)


def bac_total(hand: list) -> int:
    return sum(bac_val(c) for c in hand) % 10


def banker_draws(banker_total: int, player_third_value: int) -> bool:
    """閒家補了第三張時，莊家要不要補牌"""
    if banker_total <= 2:
        return True
    if banker_total == 3:
        return player_third_value != 8
    if banker_total == 4:
        return 2 <= player_third_value <= 7
    if banker_total == 5:
        return 4 <= player_third_value <= 7
    if banker_total == 6:
        return player_third_value in (6, 7)
    return False


def play_baccarat_round(shoe: list):
    player = [shoe.pop(), shoe.pop()]
    banker = [shoe.pop(), shoe.pop()]
    pt, bt = bac_total(player), bac_total(banker)
    if pt < 8 and bt < 8:  # 有人例牌（8、9）就不補牌
        if pt <= 5:
            third = shoe.pop()
            player.append(third)
            if banker_draws(bt, bac_val(third)):
                banker.append(shoe.pop())
        elif bt <= 5:
            banker.append(shoe.pop())
    pt, bt = bac_total(player), bac_total(banker)
    result = "player" if pt > bt else "banker" if bt > pt else "tie"
    return player, banker, result


def baccarat_payout(side: str, result: str, bet: int) -> int:
    if side == "tie":
        return bet * 9 if result == "tie" else 0  # 和局 8:1
    if result == "tie":
        return bet  # 押閒/莊遇到和局，退回本金
    if side == result:
        return bet * 2 if side == "player" else bet + bet * 95 // 100  # 莊家贏抽 5%
    return 0


BAC_NAMES = {"player": "閒", "banker": "莊", "tie": "和"}


# ════════════════════════════════════════════════════════
#  骰寶（三顆骰子）
# ════════════════════════════════════════════════════════
SICBO_NAMES = {
    "big": "大（11~17）",
    "small": "小（4~10）",
    "odd": "單",
    "even": "雙",
    "triple": "圍骰（任意三顆同點）",
    "single": "指定點數",
}
SICBO_CHOICES = [
    app_commands.Choice(name="大 11~17（×2，圍骰通殺）", value="big"),
    app_commands.Choice(name="小 4~10（×2，圍骰通殺）", value="small"),
    app_commands.Choice(name="單（×2，圍骰通殺）", value="odd"),
    app_commands.Choice(name="雙（×2，圍骰通殺）", value="even"),
    app_commands.Choice(name="圍骰：三顆同點（×31）", value="triple"),
    app_commands.Choice(name="指定點數（出現 1/2/3 顆賠 ×2/×3/×4，要填點數）", value="single"),
]


def sicbo_payout(kind: str, dice: list, bet: int, number: Optional[int] = None) -> int:
    triple = dice[0] == dice[1] == dice[2]
    total = sum(dice)
    if kind == "big":
        return bet * 2 if (not triple and total >= 11) else 0
    if kind == "small":
        return bet * 2 if (not triple and total <= 10) else 0
    if kind == "odd":
        return bet * 2 if (not triple and total % 2 == 1) else 0
    if kind == "even":
        return bet * 2 if (not triple and total % 2 == 0) else 0
    if kind == "triple":
        return bet * 31 if triple else 0
    k = dice.count(number)
    return bet * (1 + k) if k else 0


# ════════════════════════════════════════════════════════
#  高低牌（連續猜，可隨時收手）
# ════════════════════════════════════════════════════════
HILO_RTP = 0.95
ACTIVE_HILO: set = set()


def hilo_mult(rank: int, higher: bool) -> Optional[float]:
    """猜對時，目前獎金要乘上的倍率；該方向不可能贏就回傳 None。同點算輸。"""
    wins = (13 - rank) if higher else (rank - 1)
    if wins <= 0:
        return None
    return HILO_RTP * 13 / wins


class HiLoView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, gid: int, uid: int, bet: int):
        super().__init__(timeout=60)
        self.interaction = interaction
        self.gid, self.uid, self.bet = gid, uid, bet
        self.pot = bet
        self.rank = random.randint(1, 13)
        self.suit = random.choice(SUITS)
        self.streak = 0
        self.finished = False
        self.message = None
        self.refresh_buttons()

    def replay_view(self) -> ReplayView:
        return ReplayView(self.interaction, self.uid, self.bet, play_hilo)

    def card_text(self) -> str:
        return f"`{RANKS[self.rank - 1]}{self.suit}`"

    def refresh_buttons(self) -> None:
        hm, lm = hilo_mult(self.rank, True), hilo_mult(self.rank, False)
        self.higher_btn.disabled = hm is None
        self.higher_btn.label = f"更高 ×{hm:.2f}" if hm else "更高"
        self.lower_btn.disabled = lm is None
        self.lower_btn.label = f"更低 ×{lm:.2f}" if lm else "更低"
        self.cash_btn.disabled = self.streak == 0
        self.cash_btn.label = f"收手領 {self.pot:,}" if self.streak else "收手"

    def embed(self) -> discord.Embed:
        e = discord.Embed(
            title="🃏 高低牌",
            description=(
                f"目前的牌：**{self.card_text()}**\n"
                f"連勝 {self.streak}｜目前可領 **{self.pot:,}** {CURRENCY}\n\n"
                "猜下一張比它「更高」還是「更低」？（A 最小、K 最大，同點算輸）"
            ),
            color=discord.Color.blurple(),
        )
        e.set_footer(text=f"本金 {self.bet:,}｜按鈕上的倍率會乘在目前獎金上")
        return e

    def finish(self, payout: int, headline: str, note: str = "") -> discord.Embed:
        self.finished = True
        ACTIVE_HILO.discard((self.gid, self.uid))
        if payout:
            add_balance(self.gid, self.uid, payout)
        for child in self.children:
            child.disabled = True
        self.stop()
        net = payout - self.bet
        line = random.choice(WIN_LINES if net > 0 else LOSE_LINES if net < 0 else PUSH_LINES)
        e = discord.Embed(
            title="🃏 高低牌",
            description=f"{headline}\n\n**{net_text(net)} {CURRENCY}**\n{line}",
            color=outcome_color(net),
        )
        e.set_footer(text=note or f"連勝 {self.streak}｜餘額 {get_balance(self.gid, self.uid):,} {CURRENCY}")
        return e

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uid:
            await interaction.response.send_message("這不是你的牌局，雜魚別亂按！", ephemeral=True)
            return False
        return True

    async def _guess(self, interaction: discord.Interaction, higher: bool):
        if self.finished:
            return await interaction.response.defer()
        mult = hilo_mult(self.rank, higher)
        if mult is None:
            return await interaction.response.send_message("那個方向不可能贏啦，笨蛋！", ephemeral=True)
        nxt = random.randint(1, 13)
        suit = random.choice(SUITS)
        win = nxt > self.rank if higher else nxt < self.rank
        if win:
            self.pot = int(self.pot * mult)
            self.rank, self.suit = nxt, suit
            self.streak += 1
            self.refresh_buttons()
            await interaction.response.edit_message(embed=self.embed(), view=self)
        else:
            embed = self.finish(0, f"下一張是 `{RANKS[nxt - 1]}{suit}`，猜錯了……")
            await interaction.response.edit_message(embed=embed, view=self.replay_view())

    @discord.ui.button(label="更高", style=discord.ButtonStyle.success, emoji="⬆️")
    async def higher_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._guess(interaction, True)

    @discord.ui.button(label="更低", style=discord.ButtonStyle.danger, emoji="⬇️")
    async def lower_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._guess(interaction, False)

    @discord.ui.button(label="收手", style=discord.ButtonStyle.primary, emoji="💰")
    async def cash_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return await interaction.response.defer()
        embed = self.finish(self.pot, f"收手！帶走 {self.pot:,} {CURRENCY}")
        await interaction.response.edit_message(embed=embed, view=self.replay_view())

    async def on_timeout(self):
        if self.finished:
            return
        embed = self.finish(self.pot, "⏰ 思考太久，自動收手", note="⏰ 超時自動收手")
        if self.message:
            try:
                await self.message.edit(embed=embed, view=self.replay_view())
            except discord.HTTPException:
                pass


# ════════════════════════════════════════════════════════
#  賽馬
# ════════════════════════════════════════════════════════
HORSES = [("閃電", 35), ("疾風", 25), ("烈焰", 20), ("黑旋風", 12), ("小可愛", 8)]  # 名稱, 勝率 %
HORSE_RTP = 0.95
TRACK = 15


def horse_mult(i: int) -> float:
    return HORSE_RTP * 100 / HORSES[i][1]


HORSE_CHOICES = [
    app_commands.Choice(name=f"{i + 1} 號 {name}（勝率 {p}%，×{horse_mult(i):.2f}）", value=str(i))
    for i, (name, p) in enumerate(HORSES)
]


def simulate_race(winner: int) -> list:
    """回傳每一幀的位置。冠軍先決定好，這裡只負責產生「看起來像比賽」的過程，最後由 winner 唯一領先衝線。"""
    n = len(HORSES)
    for _ in range(2000):
        pos = [0] * n
        frames = []
        while max(pos) < TRACK:
            pos = [p + random.randint(2, 5) for p in pos]
            frames.append(list(pos))
        top = max(pos)
        if pos.count(top) == 1 and pos.index(top) == winner:
            return frames
    pos, frames = [0] * n, []  # 保底：直接讓冠軍跑比較快
    while max(pos) < TRACK:
        pos = [p + (5 if i == winner else 2) for i, p in enumerate(pos)]
        frames.append(list(pos))
    return frames


def race_text(pos: list, pick: int) -> str:
    lines = []
    for i, (name, _) in enumerate(HORSES):
        p = min(pos[i], TRACK)
        mark = "⭐" if i == pick else "▫️"
        lines.append(f"{mark}`{i + 1}` {'·' * p}🐎{'·' * (TRACK - p)}🏁 {name}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════
#  刮刮樂
# ════════════════════════════════════════════════════════
SCRATCH_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "💎", "7️⃣", "⭐", "🍀"]
# (拿回的倍數, 中獎圖案, 機率)；其餘機率都是沒中
SCRATCH_PRIZES = [
    (200, "7️⃣", 0.0002),
    (50, "💎", 0.0035),
    (10, "🔔", 0.018),
    (5, "🍇", 0.05),
    (2, "🍋", 0.08),
    (1, "🍒", 0.13),
]


def draw_scratch():
    r = random.random()
    acc = 0.0
    for mult, sym, p in SCRATCH_PRIZES:
        acc += p
        if r < acc:
            return mult, sym
    return 0, None


def build_grid(sym: Optional[str]) -> list:
    """3x3 九格。中獎＝剛好湊出三個 sym，其他圖案每種最多兩個；沒中＝沒有任何圖案湊到三個。"""
    if sym is None:
        return random.sample(SCRATCH_SYMBOLS * 2, 9)
    others = [s for s in SCRATCH_SYMBOLS if s != sym]
    cells = [sym] * 3 + random.sample(others * 2, 6)
    random.shuffle(cells)
    return cells


def grid_text(cells: list) -> str:
    return "\n".join(" ".join(f"||{c}||" for c in cells[i:i + 3]) for i in (0, 3, 6))


# ════════════════════════════════════════════════════════
#  決鬥（玩家對玩家）
# ════════════════════════════════════════════════════════
class DuelView(discord.ui.View):
    def __init__(self, gid: int, challenger, opponent, bet: int):
        super().__init__(timeout=60)
        self.gid, self.challenger, self.opponent, self.bet = gid, challenger, opponent, bet
        self.finished = False
        self.message = None

    def _close(self) -> None:
        self.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()

    def _refund(self) -> None:
        add_balance(self.gid, self.challenger.id, self.bet)

    @discord.ui.button(label="接受決鬥", style=discord.ButtonStyle.success, emoji="⚔️")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return await interaction.response.defer()
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message("這不是給你的決鬥，旁邊看戲就好，雜魚！", ephemeral=True)
        if not try_deduct(self.gid, self.opponent.id, self.bet):
            return await interaction.response.send_message(
                f"你的錢不夠 {self.bet:,} {CURRENCY}……", ephemeral=True
            )
        self._close()
        a = b = 0
        while a == b:
            a, b = random.randint(1, 100), random.randint(1, 100)
        winner, loser = (self.challenger, self.opponent) if a > b else (self.opponent, self.challenger)
        add_balance(self.gid, winner.id, self.bet * 2)
        embed = discord.Embed(
            title="⚔️ 決鬥結果",
            description=(
                f"🎲 {self.challenger.display_name}：**{a}**\n"
                f"🎲 {self.opponent.display_name}：**{b}**\n\n"
                f"🏆 **{winner.display_name}** 贏走了 **{self.bet * 2:,}** {CURRENCY}！\n"
                f"{loser.display_name}，哼，輸給別人了呢，雜魚♡"
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.edit_message(content=None, embed=embed, view=self)

    @discord.ui.button(label="拒絕", style=discord.ButtonStyle.danger)
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return await interaction.response.defer()
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message("這不是給你的決鬥。", ephemeral=True)
        self._refund()
        self._close()
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(
                title="⚔️ 決鬥取消",
                description=f"{self.opponent.display_name} 拒絕了決鬥，真是膽小鬼♡（賭注已退回）",
                color=discord.Color.light_grey(),
            ),
            view=self,
        )

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return await interaction.response.defer()
        if interaction.user.id != self.challenger.id:
            return await interaction.response.send_message("只有發起的人能取消。", ephemeral=True)
        self._refund()
        self._close()
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(
                title="⚔️ 決鬥取消",
                description=f"{self.challenger.display_name} 取消了決鬥（賭注已退回）",
                color=discord.Color.light_grey(),
            ),
            view=self,
        )

    async def on_timeout(self):
        if self.finished:
            return
        self._refund()
        self._close()
        if self.message:
            try:
                await self.message.edit(
                    content=None,
                    embed=discord.Embed(
                        title="⚔️ 決鬥取消",
                        description="沒有人應戰，哼，被無視了呢♡（賭注已退回）",
                        color=discord.Color.light_grey(),
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


# ════════════════════════════════════════════════════════
#  斜線指令
# ════════════════════════════════════════════════════════
# 每個 play_* 函式負責「一局」：斜線指令與「再來一把」按鈕都呼叫它。
# 成功開局回傳 True；被拒絕（沒錢、參數錯誤…）回傳 False 且不扣錢。
async def play_slots(interaction: discord.Interaction, bet: int) -> bool:
    gid, uid = interaction.guild_id, interaction.user.id
    err = validate_bet(gid, uid, bet)
    if err:
        await reject(interaction, err)
        return False
    try_deduct(gid, uid, bet)

    reels = random.choices(SLOT_SYMBOLS, weights=SLOT_WEIGHTS, k=3)
    payout, label = slot_payout(reels, bet)
    if payout:
        add_balance(gid, uid, payout)
    final = settle_embed("🎰 老虎機", f"**｜ {' ｜ '.join(reels)} ｜**\n{label}", bet, payout, gid, uid)

    await respond(interaction, embed=slot_frame(reels, 0))
    for shown in (1, 2):
        await pause(0.8)
        await safe_edit(interaction, embed=slot_frame(reels, shown))
    await pause(0.8)
    await safe_edit(interaction, embed=final, view=ReplayView(interaction, uid, bet, play_slots))
    return True


async def play_roulette(
    interaction: discord.Interaction, bet: int, kind: app_commands.Choice, number: Optional[int] = None
) -> bool:
    gid, uid = interaction.guild_id, interaction.user.id
    if kind.value == "num" and (number is None or not 0 <= number <= 36):
        await reject(interaction, "押單一號碼要填 0~36 的號碼，笨蛋！")
        return False
    err = validate_bet(gid, uid, bet)
    if err:
        await reject(interaction, err)
        return False
    try_deduct(gid, uid, bet)

    spin = random.randint(0, 36)
    payout = roulette_payout(kind.value, spin, bet, number)
    if payout:
        add_balance(gid, uid, payout)
    pick = f"單一號碼 {number}" if kind.value == "num" else ROULETTE_BETS[kind.value][0]
    final = settle_embed(
        "🎡 輪盤",
        f"球落在 **{COLOR_EMOJI[roulette_color(spin)]} {spin}**\n你押：{pick}",
        bet, payout, gid, uid,
    )

    await respond(
        interaction,
        embed=discord.Embed(title="🎡 輪盤", description="輪盤轉動中……", color=discord.Color.blurple()),
    )
    await pause(1.5)
    runner = partial(play_roulette, kind=kind, number=number)
    await safe_edit(interaction, embed=final, view=ReplayView(interaction, uid, bet, runner))
    return True


async def play_baccarat(interaction: discord.Interaction, bet: int, side: app_commands.Choice) -> bool:
    gid, uid = interaction.guild_id, interaction.user.id
    err = validate_bet(gid, uid, bet)
    if err:
        await reject(interaction, err)
        return False
    try_deduct(gid, uid, bet)

    player, banker, result = play_baccarat_round(new_shoe())
    payout = baccarat_payout(side.value, result, bet)
    if payout:
        add_balance(gid, uid, payout)
    detail = (
        f"閒家 {fmt_cards(player)}（**{bac_total(player)}** 點）\n"
        f"莊家 {fmt_cards(banker)}（**{bac_total(banker)}** 點）\n"
        f"結果：**{BAC_NAMES[result]}**　你押：{BAC_NAMES[side.value]}"
    )
    embed = settle_embed("🎴 百家樂", detail, bet, payout, gid, uid)
    await respond(interaction, embed=embed, view=ReplayView(interaction, uid, bet, partial(play_baccarat, side=side)))
    return True


async def play_sicbo(
    interaction: discord.Interaction, bet: int, kind: app_commands.Choice, number: Optional[int] = None
) -> bool:
    gid, uid = interaction.guild_id, interaction.user.id
    if kind.value == "single" and (number is None or not 1 <= number <= 6):
        await reject(interaction, "押指定點數要填 1~6 的點數，笨蛋！")
        return False
    err = validate_bet(gid, uid, bet)
    if err:
        await reject(interaction, err)
        return False
    try_deduct(gid, uid, bet)

    dice = [random.randint(1, 6) for _ in range(3)]
    payout = sicbo_payout(kind.value, dice, bet, number)
    if payout:
        add_balance(gid, uid, payout)
    faces = " ".join(DICE_FACES[d - 1] for d in dice)
    pick = f"指定點數 {number}" if kind.value == "single" else SICBO_NAMES[kind.value]
    triple_note = "　**（圍骰！）**" if dice[0] == dice[1] == dice[2] else ""
    detail = f"🎲 {faces}　（{'+'.join(map(str, dice))} = **{sum(dice)}**）{triple_note}\n你押：{pick}"
    embed = settle_embed("🎲 骰寶", detail, bet, payout, gid, uid)
    runner = partial(play_sicbo, kind=kind, number=number)
    await respond(interaction, embed=embed, view=ReplayView(interaction, uid, bet, runner))
    return True


async def play_hilo(interaction: discord.Interaction, bet: int) -> bool:
    gid, uid = interaction.guild_id, interaction.user.id
    if (gid, uid) in ACTIVE_HILO:
        await reject(interaction, "你還有一局高低牌沒打完呢，笨蛋！")
        return False
    err = validate_bet(gid, uid, bet)
    if err:
        await reject(interaction, err)
        return False
    try_deduct(gid, uid, bet)
    ACTIVE_HILO.add((gid, uid))

    view = HiLoView(interaction, gid, uid, bet)
    await respond(interaction, embed=view.embed(), view=view)
    view.message = await interaction.original_response()
    return True


async def play_race(interaction: discord.Interaction, bet: int, horse: app_commands.Choice) -> bool:
    gid, uid = interaction.guild_id, interaction.user.id
    err = validate_bet(gid, uid, bet)
    if err:
        await reject(interaction, err)
        return False
    try_deduct(gid, uid, bet)

    pick = int(horse.value)
    winner = random.choices(range(len(HORSES)), weights=[p for _, p in HORSES])[0]
    payout = int(bet * horse_mult(pick)) if winner == pick else 0
    if payout:
        add_balance(gid, uid, payout)
    frames = simulate_race(winner)
    final = settle_embed(
        "🏇 賽馬",
        f"{race_text(frames[-1], pick)}\n\n冠軍：**{winner + 1} 號 {HORSES[winner][0]}**　你押：{pick + 1} 號 {HORSES[pick][0]}",
        bet, payout, gid, uid,
    )

    await respond(
        interaction,
        embed=discord.Embed(title="🏇 賽馬", description=race_text([0] * len(HORSES), pick), color=discord.Color.blurple()),
    )
    for pos in frames[:-1]:
        await pause(1.0)
        await safe_edit(
            interaction,
            embed=discord.Embed(title="🏇 賽馬", description=race_text(pos, pick), color=discord.Color.blurple()),
        )
    await pause(1.0)
    await safe_edit(interaction, embed=final, view=ReplayView(interaction, uid, bet, partial(play_race, horse=horse)))
    return True


async def play_scratch(interaction: discord.Interaction, bet: int) -> bool:
    gid, uid = interaction.guild_id, interaction.user.id
    err = validate_bet(gid, uid, bet)
    if err:
        await reject(interaction, err)
        return False
    try_deduct(gid, uid, bet)

    mult, sym = draw_scratch()
    payout = bet * mult
    if payout:
        add_balance(gid, uid, payout)
    net = payout - bet
    bal = get_balance(gid, uid)
    cells = build_grid(sym)
    result = f"🎉 三個 {sym}！×{mult}　**{net_text(net)} {CURRENCY}**" if sym else f"沒中……**-{bet:,} {CURRENCY}**"
    embed = discord.Embed(
        title="🎟️ 刮刮樂",
        description=(
            f"點開每一格刮刮看，湊出三個相同圖案就中獎！\n\n{grid_text(cells)}\n\n"
            f"結果（點開看）：||{result}｜餘額 {bal:,}||"
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"下注 {bet:,} {CURRENCY}")
    await respond(interaction, embed=embed, view=ReplayView(interaction, uid, bet, play_scratch))
    return True


def setup_casino(tree: app_commands.CommandTree) -> None:
    @tree.command(name="老虎機", description="拉霸！三個相同最高 ×100，兩個相同 ×1.2")
    @app_commands.guild_only()
    @app_commands.describe(bet="下注金額")
    @app_commands.rename(bet="賭注")
    async def slots_cmd(interaction: discord.Interaction, bet: int):
        await play_slots(interaction, bet)

    @tree.command(name="輪盤", description="歐式輪盤（0~36）。顏色單雙賠 ×2，一打賠 ×3，單一號碼賠 ×36")
    @app_commands.guild_only()
    @app_commands.describe(bet="下注金額", kind="要押什麼", number="只有選「單一號碼」才要填（0~36）")
    @app_commands.rename(bet="賭注", kind="押法", number="號碼")
    @app_commands.choices(kind=ROULETTE_CHOICES)
    async def roulette_cmd(
        interaction: discord.Interaction,
        bet: int,
        kind: app_commands.Choice[str],
        number: Optional[int] = None,
    ):
        await play_roulette(interaction, bet, kind, number)

    @tree.command(name="百家樂", description="押閒（×2）、莊（×1.95）或和（×9）")
    @app_commands.guild_only()
    @app_commands.describe(bet="下注金額", side="要押哪一邊")
    @app_commands.rename(bet="賭注", side="押哪邊")
    @app_commands.choices(
        side=[
            app_commands.Choice(name="閒（×2）", value="player"),
            app_commands.Choice(name="莊（×1.95，抽 5%）", value="banker"),
            app_commands.Choice(name="和（×9；押閒莊遇和局退回本金）", value="tie"),
        ]
    )
    async def baccarat_cmd(interaction: discord.Interaction, bet: int, side: app_commands.Choice[str]):
        await play_baccarat(interaction, bet, side)

    @tree.command(name="骰寶", description="三顆骰子：押大小單雙、圍骰或指定點數")
    @app_commands.guild_only()
    @app_commands.describe(bet="下注金額", kind="要押什麼", number="只有選「指定點數」才要填（1~6）")
    @app_commands.rename(bet="賭注", kind="押法", number="點數")
    @app_commands.choices(kind=SICBO_CHOICES)
    async def sicbo_cmd(
        interaction: discord.Interaction,
        bet: int,
        kind: app_commands.Choice[str],
        number: Optional[int] = None,
    ):
        await play_sicbo(interaction, bet, kind, number)

    @tree.command(name="高低牌", description="連續猜下一張牌比較高還是低，答對獎金一直滾，隨時可以收手")
    @app_commands.guild_only()
    @app_commands.describe(bet="下注金額")
    @app_commands.rename(bet="賭注")
    async def hilo_cmd(interaction: discord.Interaction, bet: int):
        await play_hilo(interaction, bet)

    @tree.command(name="賽馬", description="押一匹馬，冠軍賠率依勝率而定（×2.7 ~ ×11.9）")
    @app_commands.guild_only()
    @app_commands.describe(bet="下注金額", horse="要押哪一匹馬")
    @app_commands.rename(bet="賭注", horse="馬匹")
    @app_commands.choices(horse=HORSE_CHOICES)
    async def race_cmd(interaction: discord.Interaction, bet: int, horse: app_commands.Choice[str]):
        await play_race(interaction, bet, horse)

    @tree.command(name="刮刮樂", description="九格刮刮樂，湊出三個相同圖案就中獎，最高 ×200")
    @app_commands.guild_only()
    @app_commands.describe(bet="下注金額（就是這張刮刮樂的價錢）")
    @app_commands.rename(bet="賭注")
    async def scratch_cmd(interaction: discord.Interaction, bet: int):
        await play_scratch(interaction, bet)

    # ── 決鬥（玩家對玩家，需要對手同意，所以沒有「再來一把」）──
    @tree.command(name="決鬥", description="向其他玩家下戰帖，雙方各出同額賭注，擲 1~100 點數大的全拿")
    @app_commands.guild_only()
    @app_commands.describe(opponent="你要挑戰的人", bet="雙方各自要出的賭注")
    @app_commands.rename(opponent="對手", bet="賭注")
    async def duel_cmd(interaction: discord.Interaction, opponent: discord.Member, bet: int):
        gid, uid = interaction.guild_id, interaction.user.id
        if opponent.bot:
            return await reject(interaction, "機器人才不跟你決鬥呢，雜魚♡")
        if opponent.id == uid:
            return await reject(interaction, "自己跟自己決鬥是怎樣啦，笨蛋！")
        err = validate_bet(gid, uid, bet)
        if err:
            return await reject(interaction, err)
        if get_balance(gid, opponent.id) < bet:
            return await reject(interaction, f"{opponent.display_name} 的錢不夠 {bet:,} {CURRENCY}，欺負窮人很開心嗎？")
        try_deduct(gid, uid, bet)

        view = DuelView(gid, interaction.user, opponent, bet)
        embed = discord.Embed(
            title="⚔️ 決鬥邀請",
            description=(
                f"**{interaction.user.display_name}** 向 **{opponent.display_name}** 發起決鬥！\n"
                f"雙方各出 **{bet:,}** {CURRENCY}，擲 1~100 點，點數大的全拿（**{bet * 2:,}**）。\n"
                f"發起人的賭注已先扣下，60 秒內沒人應戰會退回。"
            ),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(
            content=f"{opponent.mention}，有人要跟你決鬥哦！", embed=embed, view=view
        )
        view.message = await interaction.original_response()

    # ── 賭場說明 ──
    @tree.command(name="賭場", description="查看所有遊戲、玩法與賠率")
    async def casino_help_cmd(interaction: discord.Interaction):
        e = discord.Embed(
            title="🎰 Sayaka 的賭場",
            description=(
                f"全部都是虛擬{CURRENCY}，沒有任何現實價值。沒錢了就用 `/每日`、`/每小時` 領。\n"
                "每局結束後可以直接按「再來一把」或「雙倍再來」，不用重打指令。"
            ),
            color=discord.Color.gold(),
        )
        e.add_field(name="🪙 `/猜硬幣`", value="猜正反面，×2（約 100%）", inline=False)
        e.add_field(name="🎲 `/擲骰`", value="猜點數 ×6、猜大小 ×2（約 100%）", inline=False)
        e.add_field(name="🃏 `/21點`", value="按鈕操作（要牌／停牌），Blackjack ×2.5，莊家 17 點停牌", inline=False)
        e.add_field(name="🎰 `/老虎機`", value="三個相同 ×4~×100，兩個相同 ×1.2（約 94%）", inline=False)
        e.add_field(name="🎡 `/輪盤`", value="紅黑單雙大小 ×2、一打 ×3、單號 ×36（約 97.3%）", inline=False)
        e.add_field(name="🎴 `/百家樂`", value="閒 ×2、莊 ×1.95、和 ×9（約 98.8%~85.6%）", inline=False)
        e.add_field(name="🎲 `/骰寶`", value="大小單雙 ×2（圍骰通殺）、圍骰 ×31、指定點數（約 86%~97%）", inline=False)
        e.add_field(name="🃏 `/高低牌`", value="連續猜高低，獎金滾雪球，隨時收手（約 95%）", inline=False)
        e.add_field(name="🏇 `/賽馬`", value="五匹馬，賠率 ×2.7 ~ ×11.9（約 95%）", inline=False)
        e.add_field(name="🎟️ `/刮刮樂`", value="九格湊三個相同，最高 ×200（約 93.5%）", inline=False)
        e.add_field(name="⚔️ `/決鬥`", value="玩家對玩家，雙方各出賭注，點數大的全拿", inline=False)
        e.set_footer(text=f"最低下注 10 {CURRENCY}｜括號內是長期平均每 100 塊會拿回多少")
        await interaction.response.send_message(embed=e)
