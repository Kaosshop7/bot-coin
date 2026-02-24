import discord
from discord.ext import commands, tasks
from discord import app_commands
import random
import time
import pymongo
import os
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
TOKEN = os.getenv("DISCORD_TOKEN")

client = pymongo.MongoClient(MONGO_URI)
db = client['economy_bot_db']

users_col = db['users']
shop_col = db['shop']
gacha_col = db['gacha']
config_col = db['config']

def get_coins(user_id):
    user = users_col.find_one({"user_id": user_id})
    return user["coins"] if user else 0

def update_coins(user_id, amount):
    current = get_coins(user_id)
    new_amount = max(0, current + amount)
    users_col.update_one({"user_id": user_id}, {"$set": {"coins": new_amount}}, upsert=True)
    return new_amount

def set_config(key, value):
    config_col.update_one({"key": key}, {"$set": {"value": str(value)}}, upsert=True)

def get_config(key, default=None):
    doc = config_col.find_one({"key": key})
    if doc:
        val = doc["value"]
        return int(val) if val.isdigit() else val
    return default

temp_state = {}
def get_temp(user_id):
    if user_id not in temp_state: temp_state[user_id] = {"voice_join": None, "last_chat": 0}
    return temp_state[user_id]

async def send_audit_log(guild, title, description, color):
    channel_id = get_config("audit_channel")
    if not channel_id: return
    channel = guild.get_channel(int(channel_id))
    if channel:
        embed = discord.Embed(title=title, description=description, color=color)
        embed.timestamp = discord.utils.utcnow()
        await channel.send(embed=embed)

class WalletView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="💳 เช็คเงิน", style=discord.ButtonStyle.primary, custom_id="check_wallet_btn")
    async def wallet_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        coins = get_coins(interaction.user.id)
        embed = discord.Embed(title="💳 กระเป๋าเงินของคุณ", description=f"ยอดเงิน: **{coins}** 🪙", color=discord.Color.gold())
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class ConfirmTransferView(discord.ui.View):
    def __init__(self, sender, receiver, amount):
        super().__init__(timeout=60)
        self.sender = sender
        self.receiver = receiver
        self.amount = amount
    @discord.ui.button(label="✅ ยืนยัน", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if get_coins(self.sender.id) < self.amount: return await interaction.response.edit_message(content="❌ เงินไม่พอ", view=None, embed=None)
        update_coins(self.sender.id, -self.amount)
        update_coins(self.receiver.id, self.amount)
        await interaction.response.edit_message(embed=discord.Embed(title="✅ สำเร็จ", description=f"โอนให้ {self.receiver.mention} **{self.amount}** 🪙", color=discord.Color.green()), view=None)
        await send_audit_log(interaction.guild, "💸 โอนเงิน", f"{self.sender.mention} โอนให้ {self.receiver.mention} จำนวน {self.amount} 🪙", discord.Color.blue())

    @discord.ui.button(label="❌ ยกเลิก", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="🛑 ยกเลิกแล้ว", view=None, embed=None)

class TransferModal(discord.ui.Modal, title="ระบุจำนวนเงินที่โอน"):
    amount_input = discord.ui.TextInput(label="จำนวนเหรียญ 🪙", required=True)
    def __init__(self, target_user):
        super().__init__()
        self.target_user = target_user
    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value)
            if amount <= 0: raise ValueError
        except: return await interaction.response.send_message("❌ ใส่ตัวเลขเต็มบวก", ephemeral=True)
        if get_coins(interaction.user.id) < amount: return await interaction.response.send_message("❌ เงินไม่พอ", ephemeral=True)
        await interaction.response.send_message(embed=discord.Embed(title="⚠️ ยืนยันโอน", description=f"โอน **{amount}** 🪙 ให้ {self.target_user.mention}?", color=discord.Color.gold()), view=ConfirmTransferView(interaction.user, self.target_user, amount), ephemeral=True)

class TransferSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        select = discord.ui.UserSelect(placeholder="เลือกคนรับเงิน...", min_values=1, max_values=1)
        select.callback = self.select_callback
        self.add_item(select)
    async def select_callback(self, interaction: discord.Interaction):
        target = self.children[0].values[0]
        if target.id == interaction.user.id or target.bot: return await interaction.response.send_message("❌ โอนให้ตัวเอง/บอทไม่ได้", ephemeral=True)
        await interaction.response.send_modal(TransferModal(target))

class TransferMainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="💸 กดเพื่อโอนเงิน", style=discord.ButtonStyle.primary, custom_id="main_transfer_btn")
    async def transfer_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("เลือกคนรับเงิน", view=TransferSelectView(), ephemeral=True)

class ShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        items = list(shop_col.find())
        for item in items:
            r_id, p = item["role_id"], item["price"]
            btn = discord.ui.Button(label=f"ซื้อราคา {p} 🪙", style=discord.ButtonStyle.green, custom_id=f"buy_{r_id}")
            btn.callback = self.create_callback(r_id, p)
            self.add_item(btn)
            
    def create_callback(self, r_id, p):
        async def callback(interaction: discord.Interaction):
            if get_coins(interaction.user.id) < p: return await interaction.response.send_message("❌ เงินไม่พอ", ephemeral=True)
            role = interaction.guild.get_role(r_id)
            if not role: return await interaction.response.send_message("❌ หาไอดียศไม่เจอ", ephemeral=True)
            try:
                await interaction.user.add_roles(role)
                update_coins(interaction.user.id, -p)
                await interaction.response.send_message(f"✅ ได้รับยศ {role.mention} แล้ว", ephemeral=True)
                await send_audit_log(interaction.guild, "🛒 ซื้อยศ", f"{interaction.user.mention} ซื้อยศ {role.mention}", discord.Color.green())
            except: await interaction.response.send_message("❌ สิทธิ์ไม่พอ!", ephemeral=True)
        return callback

class GachaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        btn = discord.ui.Button(label=f"🎲 หมุนกาชา ({get_config('gacha_price', 10)} 🪙)", style=discord.ButtonStyle.blurple, custom_id="gacha_roll_btn")
        btn.callback = self.gacha_callback
        self.add_item(btn)
        
    async def gacha_callback(self, interaction: discord.Interaction):
        price = int(get_config("gacha_price", 10))
        if get_coins(interaction.user.id) < price: return await interaction.response.send_message("❌ เงินไม่พอ", ephemeral=True)
        
        pool = list(gacha_col.find())
        if not pool: return await interaction.response.send_message("❌ ตู้ว่าง", ephemeral=True)

        update_coins(interaction.user.id, -price)
        choices = [item["role_id"] for item in pool] + ["เกลือ"]
        weights = [item["percent"] for item in pool]
        weights.append(max(0.0, 100.0 - sum(weights)))

        won_id = random.choices(choices, weights=weights, k=1)[0]
        if won_id == "เกลือ": 
            await send_audit_log(interaction.guild, "🎲 เกลือ", f"{interaction.user.mention} หมุนกาชาได้เกลือ", discord.Color.light_grey())
            return await interaction.response.send_message(embed=discord.Embed(title="🧂 เกลือ", description="เกลือเค็มๆพี่น้อง", color=discord.Color.light_grey()), ephemeral=True)
        
        role = interaction.guild.get_role(won_id)
        try:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(embed=discord.Embed(title="🎉 ยินดีด้วย!", description=f"ได้รับยศ {role.mention}", color=discord.Color.gold()), ephemeral=True)
            await send_audit_log(interaction.guild, "🎲 ยินดีด้วย", f"{interaction.user.mention} ได้ยศ {role.mention}", discord.Color.gold())
        except: await interaction.response.send_message("❌ สิทธิ์ไม่พอ", ephemeral=True)

async def update_shop_ui(guild):
    try:
        msg = await guild.get_channel(int(get_config("shop_channel"))).fetch_message(int(get_config("shop_msg")))
        embed = discord.Embed(title="🛒 ร้านค้ายศ", description="กดปุ่มด้านล่างเพื่อซื้อยศ", color=discord.Color.green())
        items = list(shop_col.find())
        if not items: embed.add_field(name="สินค้า", value="ว่างเปล่า")
        else:
            for item in items: embed.add_field(name=f"ยศ <@&{item['role_id']}>", value=f"ราคา: **{item['price']}** 🪙", inline=False)
        await msg.edit(embed=embed, view=ShopView())
    except: pass

async def update_gacha_ui(guild):
    try:
        msg = await guild.get_channel(int(get_config("gacha_channel"))).fetch_message(int(get_config("gacha_msg")))
        embed = discord.Embed(title="🎲 ตู้กาชายศ", description=f"ราคาหมุน: **{get_config('gacha_price', 10)}** 🪙", color=discord.Color.purple())
        pool = list(gacha_col.find())
        if not pool: embed.add_field(name="ของรางวัล", value="ตู้ว่างเปล่า")
        else:
            for item in pool: embed.add_field(name=f"ยศ <@&{item['role_id']}>", value=f"โอกาสออก: **{item['percent']}%**", inline=False)
            embed.add_field(name="🧂 เกลือ", value=f"โอกาสออก: **{max(0.0, 100.0 - sum(i['percent'] for i in pool)):.2f}%**", inline=False)
        await msg.edit(embed=embed, view=GachaView())
    except: pass

class EconomyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
    async def setup_hook(self):
        self.add_view(WalletView())
        self.add_view(ShopView())
        self.add_view(GachaView())
        self.add_view(TransferMainView())
        await self.tree.sync()
        self.update_leaderboard.start()

    @tasks.loop(minutes=5)
    async def update_leaderboard(self):
        try:
            msg = await self.get_channel(int(get_config("lb_channel"))).fetch_message(int(get_config("lb_msg")))
            users = list(users_col.find().sort("coins", -1).limit(10))
            embed = discord.Embed(title="🏆 ตารางอันดับ", description="ตารางจะอัปเดตทุกๆ 5 นาที", color=discord.Color.blue())
            if not users: embed.add_field(name="ยังไม่มีข้อมูล", value="PDR COMMUNITY")
            else:
                for idx, u in enumerate(users, 1): embed.add_field(name=f"อันดับ {idx}", value=f"<@{u['user_id']}>: **{u['coins']}** 🪙", inline=False)
            await msg.edit(embed=embed)
        except: pass

bot = EconomyBot()

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild: return
    user_temp = get_temp(message.author.id)
    now = time.time()
    
    if now - user_temp["last_chat"] > 60:
        user_temp["last_chat"] = now
        
        if random.random() < 0.30:
            update_coins(message.author.id, 1)
            embed_desc = f"💬 {message.author.mention} แชทจนนิ้วล็อค ได้มา **+1** 🪙"
            noti_msg = await message.channel.send(embed=discord.Embed(description=embed_desc, color=discord.Color.brand_green()))
            await noti_msg.delete(delay=5)

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    user_temp = get_temp(member.id)
    if not before.channel and after.channel:
        user_temp["voice_join"] = time.time()
    elif before.channel and not after.channel:
        if user_temp["voice_join"]:
            stayed_minutes = int((time.time() - user_temp["voice_join"]) / 60)
            
            earned = stayed_minutes // 5 
            
            if earned > 0:
                update_coins(member.id, earned)
                embed_desc = f"คุยไป **{stayed_minutes} นาที**\nได้รับเงิน **+{earned}** 🪙"
                try: await member.send(embed=discord.Embed(title="🎙️ แจ้งเตือนรายได้", description=embed_desc, color=discord.Color.purple()))
                except: pass
            user_temp["voice_join"] = None

@bot.tree.command(name="setup", description="สร้างห้อง")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_setup(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild
    category = await guild.create_category("💎・𝗘𝗖𝗢𝗡𝗢𝗠𝗬 𝗦𝗬𝗦𝗧𝗘𝗠")
    
    audit_ch = await guild.create_text_channel("🕵️│audit-log", category=category, overwrites={guild.default_role: discord.PermissionOverwrite(read_messages=False), guild.me: discord.PermissionOverwrite(read_messages=True)})
    set_config("audit_channel", audit_ch.id)
    
    wallet_ch = await guild.create_text_channel("💳│กระเป๋าเงิน", category=category)
    await wallet_ch.send(embed=discord.Embed(title="💳 เช็คกระเป๋าเงิน", description="กดปุ่มเพื่อดูว่ามีเหรียญอยู่เท่าไหร่", color=discord.Color.dark_theme()), view=WalletView())

    transfer_ch = await guild.create_text_channel("💸│โอนเงิน", category=category)
    await transfer_ch.send(embed=discord.Embed(title="💸 ศูนย์กลางการโอนเงิน", description="โอนเงินให้กันได้ที่นี่", color=discord.Color.blurple()), view=TransferMainView())

    lb_ch = await guild.create_text_channel("🏆│ลีดเดอร์บอร์ด", category=category)
    set_config("lb_msg", (await lb_ch.send(embed=discord.Embed(title="🏆 ตารางอันดับคนที่รวยที่สุด", description="โหลด..."))).id)
    set_config("lb_channel", lb_ch.id)

    shop_ch = await guild.create_text_channel("🛒│ร้านค้ายศ", category=category)
    set_config("shop_msg", (await shop_ch.send("โหลด...")).id)
    set_config("shop_channel", shop_ch.id)

    gacha_ch = await guild.create_text_channel("🎲│กาชายศ", category=category)
    set_config("gacha_msg", (await gacha_ch.send("โหลด...")).id)
    set_config("gacha_channel", gacha_ch.id)

    await update_shop_ui(guild)
    await update_gacha_ui(guild)
    await interaction.followup.send(embed=discord.Embed(title="✅ Setup สำเร็จ", description="PDR COMMUNITY", color=discord.Color.green()))

@bot.tree.command(name="add_role", description="เพิ่มยศลงในร้านค้า")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_add_role(interaction: discord.Interaction, role: discord.Role, price: int):
    shop_col.update_one({"role_id": role.id}, {"$set": {"price": price}}, upsert=True)
    await update_shop_ui(interaction.guild)
    await interaction.response.send_message(f"✅ เพิ่มยศ {role.mention} ลงร้านค้า ราคา {price} 🪙", ephemeral=True)

@bot.tree.command(name="remove_role", description="ลบยศออกจากร้านค้า")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_remove_role(interaction: discord.Interaction, role: discord.Role):
    shop_col.delete_one({"role_id": role.id})
    await update_shop_ui(interaction.guild)
    await interaction.response.send_message(f"✅ ลบยศ {role.mention} ออกจากร้านค้าแล้ว", ephemeral=True)

@bot.tree.command(name="gacha_role", description="เพิ่มยศลงตู้กาชาพร้อมตั้งเรท")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_gacha_role(interaction: discord.Interaction, role: discord.Role, percent: float):
    gacha_col.update_one({"role_id": role.id}, {"$set": {"percent": percent}}, upsert=True)
    await update_gacha_ui(interaction.guild)
    await interaction.response.send_message(f"✅ เพิ่มยศ {role.mention} ลงตู้กาชา เรท {percent}%", ephemeral=True)

@bot.tree.command(name="remove_gacha_role", description="ลบยศออกจากตู้กาชา")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_remove_gacha_role(interaction: discord.Interaction, role: discord.Role):
    gacha_col.delete_one({"role_id": role.id})
    await update_gacha_ui(interaction.guild)
    await interaction.response.send_message(f"✅ ลบยศ {role.mention} ออกจากตู้กาชาแล้ว", ephemeral=True)

@bot.tree.command(name="set_gacha_price", description="ตั้งราคาหมุนกาชา")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_set_gacha_price(interaction: discord.Interaction, price: int):
    set_config("gacha_price", price)
    await update_gacha_ui(interaction.guild)
    await interaction.response.send_message(f"✅ เปลี่ยนราคากาชาเป็น {price} 🪙 แล้ว", ephemeral=True)

if __name__ == "__main__":
    bot.run(TOKEN)
