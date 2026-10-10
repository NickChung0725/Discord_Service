import discord
from discord import app_commands
from discord.ext import tasks
import random
import os
from datetime import datetime
from dotenv import load_dotenv
from bot import register_stock_command

# ================= 參數設定區 =================
GUILD_ID = 1557751571205333082       # 你的伺服器 ID
ANNOUNCE_CHANNEL_ID = 1557751571742330892  # 想定時推播的文字頻道 ID
gcp_vm_server_CHANNEL_ID = 1558122364062015570  # gcp-vm-server 文字頻道 ID

load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("DISCORD_TOKEN (or legacy BOT_TOKEN) is missing.")
# =============================================

# 1. 權限意圖設定：開啟讀取訊息內容權限
intents = discord.Intents.default()
intents.message_content = True

# 2. 定義按鈕互動介面（View）
class InteractiveButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)  # 60 秒後按鈕失效

    @discord.ui.button(label="點我按讚 👍", style=discord.ButtonStyle.primary)
    async def like_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ephemeral=True 代表僅點擊者本人看得到回應
        await interaction.response.send_message(f"感謝 {interaction.user.mention} 按讚！🎉", ephemeral=True)

    @discord.ui.button(label="危險按鈕 ⚠️", style=discord.ButtonStyle.danger)
    async def danger_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⚠️ 你觸發了警告操作！", ephemeral=True)

# 3. 機器人主客戶端定義
class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # 同步斜線指令到指定伺服器（立即生效）
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print("✅ 斜線指令同步完成！")

client = MyClient()
register_stock_command(client.tree)

# 4. 背景定時排程任務（每 6 小時觸發一次）
@tasks.loop(hours=6)
async def scheduled_task():
    channel = client.get_channel(gcp_vm_server_CHANNEL_ID)
    if channel:
        current_time = datetime.now().strftime("%H:%M:%S")
        # 示範在終端機記錄排程狀態，若需在頻道推播可取消下方註解：
        print(f"[排程記錄] 背景任務運作中，當前時間：{current_time}")
        await channel.send(f"⏰ 定時心跳通知：當前系統時間為 {current_time}")

@scheduled_task.before_loop
async def before_scheduled_task():
    # 等待機器人完全連線登入後再開始跑排程
    await client.wait_until_ready()

# 5. 上線事件
@client.event
async def on_ready():
    print(f"🚀 機器人已上線：{client.user}")
    # 啟動背景排程
    if not scheduled_task.is_running():
        scheduled_task.start()

# 6. 即時聊天訊息監聽事件
@client.event
async def on_message(message: discord.Message):
    # 忽略機器人自己的發言
    if message.author == client.user:
        return

    # 終端機即時印出訊息
    print(f"[{message.channel.name}] {message.author.name}: {message.content}")

    # 簡易關鍵字互動範例
    if message.content == "ping":
        await message.channel.send("pong! 🏓")

# ================= 斜線指令清單 =================

# 指令 1：/ping（延遲測試）
@client.tree.command(name="ping", description="測試機器人連線延遲")
async def ping(interaction: discord.Interaction):
    latency = round(client.latency * 1000)
    await interaction.response.send_message(f"Pong! 🏓 延遲：{latency}ms")

# 指令 2：/say（文字複誦）
@client.tree.command(name="say", description="讓機器人重複你說的話")
@app_commands.describe(message="想讓機器人說的內容")
async def say(interaction: discord.Interaction, message: str):
    await interaction.response.send_message(f"📢 {message}")

# 指令 3：/roll（隨機擲骰）
@client.tree.command(name="roll", description="隨機擲骰子")
@app_commands.describe(max_number="最大骰子點數（預設 100）")
async def roll(interaction: discord.Interaction, max_number: int = 100):
    result = random.randint(1, max_number)
    await interaction.response.send_message(f"🎲 擲出了 **{result}**（範圍 1 ~ {max_number}）")

# 指令 4：/buttons（測試互動按鈕）
@client.tree.command(name="buttons", description="發送帶有互動按鈕的訊息")
async def buttons(interaction: discord.Interaction):
    await interaction.response.send_message("請點擊下方按鈕測試互動：", view=InteractiveButtonView())

# 指令 5：/card（發送美化資訊卡片 Embed）
@client.tree.command(name="card", description="發送系統狀態卡片")
async def card(interaction: discord.Interaction):
    latency = round(client.latency * 1000)
    member_count = interaction.guild.member_count if interaction.guild else "未知"

    embed = discord.Embed(
        title="🤖 機器人核心狀態",
        description="各項服務正常運作中",
        color=0x00FF7F  # 翡翠綠
    )
    embed.add_field(name="機器人名稱", value=client.user.name, inline=True)
    embed.add_field(name="API 延遲", value=f"{latency} ms", inline=True)
    embed.add_field(name="伺服器成員數", value=str(member_count), inline=False)
    embed.set_footer(text=f"查詢時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    await interaction.response.send_message(embed=embed)

# =============================================

# 啟動機器人
client.run(BOT_TOKEN)