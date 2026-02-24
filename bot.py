import discord
from discord.ext import commands, tasks
from discord import app_commands
import random
import time
import pymongo
import os
from dotenv import load_dotenv
import certifi
from aiohttp import web

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
TOKEN = os.getenv("DISCORD_TOKEN")

client = pymongo.MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client['economy_bot_db']

users_col = db['users']
shop_col = db['shop']
gacha_col = db['gacha']
config_col = db['config']
items_col = db['items']       
inv_col = db['inventory']     

def get_user_data(user_id):
    user = users_col.find_one({"user_id": user_id})
    if not user:
        user = {"user_id": user_id, "coins": 5, "xp": 10, "level": 1}
        users_col.insert_one(user)
    return user

def update_coins(user_id, amount):
    user = get_user_data(user_id)
    new_amount = max(0, user.get("coins", 0) + amount)
    users_col.update_one({"user_id": user_id}, {"$set": {"coins": new_amount}})
    return new_amount

async def add_xp(user_id, amount, origin_channel, member):
    user = get_user_data(user_id)
    new_xp = user.get("xp", 0) + amount
    current_level = user.get("level", 1)
    
    next_level_xp = current_level * 250 
    
    leveled_up = False
    reward_coins = 0
    
    while new_xp >= next_level_xp:
        new_xp -= next_level_xp
        current_level += 1
        reward_coins += current_level * 5 
        next_level_xp = current_level * 250
        leveled_up = True
        
    users_col.update_one({"user_id": user_id}, {"$set": {"xp": new_xp, "level": current_level}})
    
    if leveled_up:
        update_coins(user_id, reward_coins)
        
        lvl_ch_id = get_config("lvl_channel")
        target_channel = origin_channel
        if lvl_ch_id:
            fetched_ch = member.guild.get_channel(int(lvl_ch_id))
            if fetched_ch: target_channel = fetched_ch
            
        embed = discord.Embed(title="🎉 Level Up!", description=f"เลเวลมึงอัพละ {member.mention} ตอนนี้มึงอัพเป็น **เลเวล {current_level}** แล้ว!\n\n🎁 ได้รับเหรียญรางวัล: **+{reward_coins}** 🪙", color=discord.Color.gold())
        embed.set_thumbnail(url=member.display_avatar.url)
        
        if target_channel:
            await target_channel.send(embed=embed)
        else:
            try: await member.send(embed=embed)
            except: pass

def set_config(key, value):
    config_col.update_one({"key": key}, {"$set": {"value": str(value)}}, upsert=True)

def get_config(key, default=None):
    doc = config_col.find_one({"key": key})
    if doc:
        val = doc["value"]
        return int(val) if val.isdigit() else val
    return default

def get_inventory(user_id):
    return list(inv_col.find({"user_id": user_id, "amount": {"$gt": 0}}))

def add_to_inventory(user_id, item_id, amount=1):
    inv_col.update_one({"user_id": user_id, "item_id": item_id}, {"$inc": {"amount": amount}}, upsert=True)

def remove_from_inventory(user_id, item_id, amount=1):
    inv_col.update_one({"user_id": user_id, "item_id": item_id}, {"$inc": {"amount": -amount}})
    inv_col.delete_many({"amount": {"$lte": 0}})

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

async def web_server():
    app = web.Application()
    app.router.add_get('/', lambda request: web.Response(text="Bot is running! (By Render)"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server started on port {port}")

class UseItemSelect(discord.ui.Select):
    def __init__(self, inv_items):
        options = []
        for inv in inv_items:
            item_data = items_col.find_one({"item_id": inv["item_id"]})
            if item_data:
                options.append(discord.SelectOption(label=item_data["name"], description=f"มีอยู่: {inv['amount']} ชิ้น", value=inv["item_id"]))
        if not options: options.append(discord.SelectOption(label="ไม่มีไอเทม", value="none"))
        super().__init__(placeholder="เลือกไอเทมที่ต้องการใช้", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        item_id = self.values[0]
        if item_id == "none": return await interaction.response.defer()
        await interaction.response.defer(ephemeral=False)
        user_id = interaction.user.id
        
        has_item = inv_col.find_one({"user_id": user_id, "item_id": item_id, "amount": {"$gt": 0}})
        if not has_item: return await interaction.followup.send("❌ มึงไม่มีไอเทมนี้", ephemeral=True)
        item_data = items_col.find_one({"item_id": item_id})
        if not item_data: return await interaction.followup.send("❌ ไอเทมนี้ถูกระบบลบไปแล้ว", ephemeral=True)

        remove_from_inventory(user_id, item_id, 1)
        effect = item_data.get("effect")
        value = item_data.get("value")
        
        if effect == "coins":
            update_coins(user_id, int(value))
            embed = discord.Embed(title="🎒 ใช้ไอเทมสำเร็จ", description=f"คุณใช้ **{item_data['name']}**\nได้รับเหรียญ **+{value}** 🪙", color=discord.Color.gold())
            await interaction.followup.send(embed=embed)
        elif effect == "xp":
            embed = discord.Embed(title="🎒 ใช้ไอเทมสำเร็จ", description=f"คุณใช้ **{item_data['name']}**\nได้รับ Exp **+{value}** EXP 🌟", color=discord.Color.blue())
            await interaction.followup.send(embed=embed)
            await add_xp(user_id, int(value), interaction.channel, interaction.user)
        elif effect == "role":
            role = interaction.guild.get_role(int(value))
            if role:
                try:
                    await interaction.user.add_roles(role)
                    embed = discord.Embed(title="🎒 ใช้ไอเทมสำเร็จ", description=f"มึงใช้ **{item_data['name']}**\nได้รับยศ {role.mention} 🎭", color=discord.Color.purple())
                    await interaction.followup.send(embed=embed)
                except: await interaction.followup.send("❌ บอทให้ยศไม่ได้ สิทธิ์ไม่พอ", ephemeral=True)
            else: await interaction.followup.send("❌ หาไอดียศของไอเทมนี้ไม่เจอ", ephemeral=True)

class UseItemView(discord.ui.View):
    def __init__(self, inv_items):
        super().__init__(timeout=120)
        self.add_item(UseItemSelect(inv_items))

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
            await interaction.response.defer(ephemeral=True)
            if get_user_data(interaction.user.id).get("coins", 0) < p: 
                return await interaction.followup.send("❌ เงินมึงไม่พอ ดูเงินมึงบ้างดิ", ephemeral=True)
            role = interaction.guild.get_role(r_id)
            if not role: return await interaction.followup.send("❌ หาไอดียศไม่เจอ", ephemeral=True)
            try:
                await interaction.user.add_roles(role)
                update_coins(interaction.user.id, -p)
                await interaction.followup.send(f"✅ ได้รับยศ {role.mention} แล้ว!", ephemeral=True)
                await send_audit_log(interaction.guild, "🛒 ซื้อยศ", f"{interaction.user.mention} ซื้อยศ {role.mention}", discord.Color.green())
            except: await interaction.followup.send("❌ บอทสิทธิ์ไม่พอให้ยศมึง", ephemeral=True)
        return callback

class GachaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        btn = discord.ui.Button(label=f"🎲 หมุนกาชา", style=discord.ButtonStyle.blurple, custom_id="gacha_roll_btn")
        btn.callback = self.gacha_callback
        self.add_item(btn)
        
    async def gacha_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        price = int(get_config("gacha_price", 10))
        if get_user_data(interaction.user.id).get("coins", 0) < price: 
            return await interaction.followup.send("❌ ก่อนมึงจะหมุน มึงไปเช็กเหรียญตัวเองก่อนว่าพอรึป่าว", ephemeral=True)
        pool = list(gacha_col.find())
        if not pool: return await interaction.followup.send("❌ ตู้กาชายังว่าง!", ephemeral=True)

        update_coins(interaction.user.id, -price)
        choices = [item["role_id"] for item in pool] + ["เกลือ"]
        weights = [item["percent"] for item in pool]
        weights.append(max(0.0, 100.0 - sum(weights)))

        won_id = random.choices(choices, weights=weights, k=1)[0]
        if won_id == "เกลือ": 
            await send_audit_log(interaction.guild, "🎲 เกลือ", f"{interaction.user.mention} หมุนได้เกลือ", discord.Color.light_grey())
            return await interaction.followup.send(embed=discord.Embed(title="🧂 เกลือ", description="แดกเกลือไปนะไอสัส", color=discord.Color.light_grey()), ephemeral=True)
        role = interaction.guild.get_role(won_id)
        try:
            await interaction.user.add_roles(role)
            await interaction.followup.send(embed=discord.Embed(title="🎉 ยินดีด้วย!", description=f"ได้รับยศ {role.mention}", color=discord.Color.gold()), ephemeral=True)
            await send_audit_log(interaction.guild, "🎲 กาชาแตก", f"{interaction.user.mention} ได้ยศ {role.mention}", discord.Color.gold())
        except: await interaction.followup.send("❌ บอทให้ยศไม่ได้ สิทธิ์ไม่พอ", ephemeral=True)

async def update_shop_ui(guild):
    try:
        msg = await guild.get_channel(int(get_config("shop_channel"))).fetch_message(int(get_config("shop_msg")))
        embed = discord.Embed(title="🛒 ร้านค้ายศ", description="กดปุ่มด้านล่างเพื่อสั่งซื้อยศที่ต้องการ", color=discord.Color.green())
        items = list(shop_col.find())
        if not items: embed.add_field(name="สินค้า", value="ยังไม่มีสินค้าในร้าน")
        else:
            for item in items: embed.add_field(name=f"ยศ <@&{item['role_id']}>", value=f"ราคา: **{item['price']}** 🪙", inline=False)
        await msg.edit(embed=embed, view=ShopView())
    except: pass

async def update_gacha_ui(guild):
    try:
        msg = await guild.get_channel(int(get_config("gacha_channel"))).fetch_message(int(get_config("gacha_msg")))
        embed = discord.Embed(title="🎲 ตู้กาชายศ", description="กดปุ่มเพื่อเสี่ยงดวงหมุนกาชาลุ้นรับยศ", color=discord.Color.purple())
        embed.add_field(name="🏷️ ราคาหมุนต่อรอบ", value=f"**{get_config('gacha_price', 10)}** 🪙", inline=False)
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
        self.loop.create_task(web_server())
        self.add_view(ShopView())
        self.add_view(GachaView())
        await self.tree.sync()
        self.update_leaderboard.start()
        self.change_status.start()

    @tasks.loop(minutes=5)
    async def update_leaderboard(self):
        try:
            msg = await self.get_channel(int(get_config("lb_channel"))).fetch_message(int(get_config("lb_msg")))
            users = list(users_col.find().sort([("level", -1), ("xp", -1)]).limit(10))
            embed = discord.Embed(title="🏆 ตารางอันดับ", description="ตารางอัปเดตทุกๆ 5 นาที", color=discord.Color.blue())
            if not users: embed.add_field(name="ยังไม่มีข้อมูล", value="ไม่มีใครติดอันดับ")
            else:
                for idx, u in enumerate(users, 1): 
                    embed.add_field(name=f"อันดับ {idx}", value=f"<@{u['user_id']}>\n🌟 เลเวล **{u.get('level', 1)}** | 🪙 **{u.get('coins', 0)}** เหรียญ", inline=False)
            await msg.edit(embed=embed)
        except: pass

    @tasks.loop(seconds=30)
    async def change_status(self):
        ping = round(self.latency * 1000)
        users_count = sum(guild.member_count for guild in self.guilds)
        
        statuses = [
            discord.Activity(type=discord.ActivityType.playing, name="/ช่วยเหลือ เพื่อดูคำสั่ง"),
        ]
        status = random.choice(statuses)
        await self.change_presence(activity=status)

bot = EconomyBot()

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild: return
    user_temp = get_temp(message.author.id)
    now = time.time()
    
    if now - user_temp["last_chat"] > 2:
        user_temp["last_chat"] = now
        gained_xp = random.randint(1, 5)
        await add_xp(message.author.id, gained_xp, message.channel, message.author)

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    user_temp = get_temp(member.id)
    if not before.channel and after.channel:
        user_temp["voice_join"] = time.time()
    elif before.channel and not after.channel:
        if user_temp["voice_join"]:
            stayed_minutes = int((time.time() - user_temp["voice_join"]) / 60)
            if stayed_minutes > 0:
                gained_xp = sum(random.randint(1, 20) for _ in range(stayed_minutes))
                try: await member.send(embed=discord.Embed(title="⭐ แจ้งเตือนจากบอท", description=f"คุยไป **{stayed_minutes} นาที**\nได้รับ EXP **+{gained_xp}** 🌟", color=discord.Color.purple()))
                except: pass
                await add_xp(member.id, gained_xp, None, member)
            user_temp["voice_join"] = None

@bot.tree.command(name="ช่วยเหลือ", description="ดูคำสั่งทั้งหมดของบอท")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 คู่มือการใช้คำสั่งบอท", description="รวมคำสั่งที่สามารถใช้ได้", color=discord.Color.blue())
    
    embed.add_field(name="🧑‍🤝‍🧑 คำสั่งทั่วไป", value=(
        "💳 `/กระเป๋า` - เช็คตังค์ เลเวล และดูไอเทมในกระเป๋าตัวเอง\n"
        "💸 `/โอนเงิน [คนรับ] [จำนวน]` - โอนเงินไปให้เพื่อน\n"
        "🏓 `/ping` - ดูความเร็วการตอบสนองของบอท\n"
        "📖 `/help` - เปิดหน้านี้แหละ"
    ), inline=False)
    
    if interaction.user.guild_permissions.administrator:
        embed.add_field(name="👑 คำสั่งแอดมิน (Admin Only)", value=(
            "⚙️ `/ตั้งค่าระบบ` - สร้างห้องบอร์ดอันดับ ร้านค้า กาชา แจ้งเลเวล\n"
            "💰 `/ให้เหรียญ [คน] [จำนวน]` - เสกเงินให้ลูกบ้าน\n"
            "🔥 `/ลบเหรียญ [คน] [จำนวน]` - ริบเงินจากลูกบ้าน\n"
            "📦 `/สร้างไอเทม` - สร้างไอเทมใหม่เข้าระบบ\n"
            "🎁 `/ให้ไอเทม [คน] [IDไอเทม] [จำนวน]` - เสกของให้คนอื่น\n"
            "🛒 `/เพิ่มยศลงร้านค้า` | ❌ `/ลบยศจากร้านค้า`\n"
            "🎲 `/เพิ่มยศลงตู้กาชา` | 🗑️ `/ลบยศจากตู้กาชา`\n"
            "🏷️ `/ตั้งราคากาชา [ราคา]` - ปรับราคาค่าหมุน"
        ), inline=False)

    embed.set_footer(text="PDR COMMUNITY")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="เช็คค่าปิง", description="เช็คค่าปิงของบอท")
async def cmd_ping(interaction: discord.Interaction):
    bot_latency = round(bot.latency * 1000)
    
    embed = discord.Embed(title="เช็คค่าปิง", description=f"ความหน่วงปัจจุบัน: **{bot_latency} ms**", color=discord.Color.green())
    if bot_latency < 100: embed.color = discord.Color.green()
    elif bot_latency < 300: embed.color = discord.Color.gold()
    else: embed.color = discord.Color.red()
        
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="กระเป๋า", description="🎒 เช็คกระเป๋า")
async def cmd_wallet(interaction: discord.Interaction):
    user = get_user_data(interaction.user.id)
    coins = user.get("coins", 0)
    level = user.get("level", 1)
    xp = user.get("xp", 0)
    next_xp = level * 250
    
    embed = discord.Embed(title=f"🎒 กระเป๋าของ {interaction.user.display_name}", color=discord.Color.gold())
    embed.add_field(name="🌟 เลเวล", value=f"**{level}** ({xp}/{next_xp} EXP)", inline=True)
    embed.add_field(name="💳 ยอดเงิน", value=f"**{coins}** 🪙", inline=True)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    
    inv_items = get_inventory(interaction.user.id)
    if inv_items:
        inv_text = ""
        for inv in inv_items:
            item_data = items_col.find_one({"item_id": inv["item_id"]})
            if item_data: inv_text += f"🔸 **{item_data['name']}** (x{inv['amount']})\n"
        embed.add_field(name="📦 ไอเทมที่มี", value=inv_text or "ไม่มี", inline=False)
        await interaction.response.send_message(embed=embed, view=UseItemView(inv_items))
    else:
        embed.add_field(name="📦 ไอเทมที่มี", value="กระเป๋าว่างเปล่า...", inline=False)
        await interaction.response.send_message(embed=embed)

@bot.tree.command(name="โอนเงิน", description="โอนเงินให้คนอื่น")
@app_commands.describe(receiver="คนที่จะโอนให้", amount="จำนวนเงินที่ต้องการโอน")
async def cmd_transfer(interaction: discord.Interaction, receiver: discord.Member, amount: int):
    if amount <= 0: return await interaction.response.send_message("❌ มึงต้องใส่ตัวเลขที่มากกว่า 0", ephemeral=True)
    if receiver.id == interaction.user.id or receiver.bot: return await interaction.response.send_message("❌ มึงจะโอนให้ตัวเองหรือบอททำไม", ephemeral=True)
    
    sender_id = interaction.user.id
    if get_user_data(sender_id).get("coins", 0) < amount: return await interaction.response.send_message("❌ เงินไม่พอโอน", ephemeral=True)
        
    update_coins(sender_id, -amount)
    update_coins(receiver.id, amount)
    
    embed = discord.Embed(title="✅ โอนเงินสำเร็จ!", description=f"โอนเงินให้ {receiver.mention} จำนวน **{amount}** 🪙 เรียบร้อย", color=discord.Color.green())
    await interaction.response.send_message(embed=embed) 
    await send_audit_log(interaction.guild, "💸 โอนเงิน", f"{interaction.user.mention} โอนให้ {receiver.mention} จำนวน {amount} 🪙", discord.Color.blue())

@bot.tree.command(name="ตั้งค่าระบบ", description="ตั้งค่าระบบต่างๆ")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_setup(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild
    category = await guild.create_category("╭・💎・𝗘𝗖𝗢𝗡𝗢𝗠𝗬 𝗦𝗬𝗦𝗧𝗘𝗠・╮")
    
    guide_ch = await guild.create_text_channel("📖︱คู่มือระบบเกม", category=category)
    embed_guide = discord.Embed(
        title="📖 ระบบต่างๆ", 
        description="อ่านให้จบไม่อ่านขอให้ไม่มีแฟน", 
        color=discord.Color.dark_theme()
    )
    embed_guide.add_field(name="🎁 ของขวัญต้อนรับ", value="ผู้เล่นใหม่รับทันที **ทุน 5 เหรียญ** และ **10 EXP** เริ่มต้น!", inline=False)
    embed_guide.add_field(name="🌟 ระบบเลเวล", value="• **พิมพ์แชท:** สุ่มรับ 1-5 EXP ต่อข้อความ \n• **สิงห้องเสียง:** สุ่มรับ 1-20 EXP ต่อทุกๆ 1 นาทีที่อยู่ในห้อง\n*หลอด EXP จะยาวขึ้นเรื่อยๆ เลเวลยิ่งสูงยิ่งอัพยาก*", inline=False)
    embed_guide.add_field(name="🪙 ระบบเงิน", value="จะได้เหรียญก็ต่อเมื่อ **เลเวลอัพ** เท่านั้น!\nเมื่อเวลอัพ จะได้เหรียญโบนัส", inline=False)
    embed_guide.add_field(name="🎒 การเช็คกระเป๋า & ใช้ไอเทม", value="พิมพ์คำสั่ง `/กระเป๋า` เพื่อดูกระเป๋าและไอเทมในตัว\nหากแอดมินใจดีแจกไอเทมมาให้ เช่น ยา EXP, ถุงเงิน สามารถกดใช้จากปุ่มใต้กระเป๋าเงินได้เลย", inline=False)
    embed_guide.add_field(name="🛒 ร้านค้า & 🎲 กาชา", value="เอาเหรียญที่หามาอย่างยากลำบาก ไปเลือกเปย์ยศเท่ๆ ในห้องร้านค้า หรือถ้ารู้สึกว่าตัวเองดวงดี ก็ไปเสี่ยงดวงหมุนตู้กาชาได้เลย!", inline=False)
    embed_guide.add_field(name="💸 คำสั่งโอนเงิน", value="พิมพ์ `/โอนเงิน` แล้วแท็กเพื่อน พร้อมระบุจำนวนเงินที่ต้องการให้", inline=False)
    embed_guide.set_footer(text="PDR COMMUNITY")
    await guide_ch.send(embed=embed_guide)

    audit_ch = await guild.create_text_channel("🕵️︱audit-log", category=category, overwrites={guild.default_role: discord.PermissionOverwrite(read_messages=False), guild.me: discord.PermissionOverwrite(read_messages=True)})
    set_config("audit_channel", audit_ch.id)

    lvl_ch = await guild.create_text_channel("⭐︱แจ้งเตือนเลเวลอัพ", category=category)
    set_config("lvl_channel", lvl_ch.id)
    await lvl_ch.send(embed=discord.Embed(title="⭐ กระดานข่าวสารเลเวลอัพ", description="ใครเลเวลอัพ บอทจะมาประกาศและแจกเหรียญรางวัลให้ที่นี่", color=discord.Color.gold()))
    
    lb_ch = await guild.create_text_channel("🏆︱บอร์ดเศรษฐี", category=category)
    set_config("lb_msg", (await lb_ch.send(embed=discord.Embed(title="🏆 ตารางอันดับ", description="กำลังโหลดข้อมูล...", color=discord.Color.blue()))).id)
    set_config("lb_channel", lb_ch.id)

    shop_ch = await guild.create_text_channel("🛒︱ร้านค้ายศ", category=category)
    set_config("shop_msg", (await shop_ch.send(embed=discord.Embed(title="🛒 ร้านค้ายศ", description="กำลังโหลดข้อมูลร้านค้า...", color=discord.Color.green()))).id)
    set_config("shop_channel", shop_ch.id)

    gacha_ch = await guild.create_text_channel("🎲︱กาชาสุ่มยศ", category=category)
    set_config("gacha_msg", (await gacha_ch.send(embed=discord.Embed(title="🎲 ตู้กาชายศ", description="กำลังโหลดข้อมูลกาชา...", color=discord.Color.purple()))).id)
    set_config("gacha_channel", gacha_ch.id)

    await update_shop_ui(guild)
    await update_gacha_ui(guild)
    await interaction.followup.send(embed=discord.Embed(title="✅ ตั้งค่าระบบสำเร็จ!", description="จัดห้อง สร้างคู่มือ และหมวดหมู่เสร็จเรียบร้อย!", color=discord.Color.green()))

@bot.tree.command(name="สร้างไอเทม", description="สร้างไอเทมใหม่ลงระบบ")
@app_commands.describe(item_id="ไอดีไอเทม", name="ชื่อไอเทม", effect="ความสามารถ", value="ตัวเลขจำนวน หรือ ไอดีของยศ")
@app_commands.choices(effect=[
    app_commands.Choice(name="💰 เพิ่มเหรียญ", value="coins"),
    app_commands.Choice(name="🌟 เพิ่ม EXP", value="xp"),
    app_commands.Choice(name="🎭 ให้ยศ", value="role")
])
@app_commands.checks.has_permissions(administrator=True)
async def cmd_add_item(interaction: discord.Interaction, item_id: str, name: str, effect: app_commands.Choice[str], value: str):
    if not value.isdigit(): return await interaction.response.send_message("❌ ช่อง value มึงต้องใส่เป็นตัวเลขเท่านั้น (จำนวนเงิน หรือ ไอดีของยศ)!", ephemeral=True)
    items_col.update_one({"item_id": item_id}, {"$set": {"name": name, "effect": effect.value, "value": value}}, upsert=True)
    await interaction.response.send_message(f"✅ สร้างไอเทม **{name}** (ID: `{item_id}`) สำเร็จ! ความสามารถ: {effect.name} ({value})", ephemeral=True)

@bot.tree.command(name="ให้ไอเทม", description="ให้ไอเทมให้Member")
@app_commands.describe(user="คนที่อยากให้", item_id="ไอดีของไอเทมที่สร้างไว้", amount="จำนวนที่ให้")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_give_item(interaction: discord.Interaction, user: discord.Member, item_id: str, amount: int = 1):
    if not items_col.find_one({"item_id": item_id}): return await interaction.response.send_message("❌ หาไอดีไอเทมนี้ไม่เจอในระบบ!", ephemeral=True)
    add_to_inventory(user.id, item_id, amount)
    await interaction.response.send_message(f"✅ ให้ไอเทม ID `{item_id}` ให้ {user.mention} จำนวน {amount} ชิ้น", ephemeral=True)

@bot.tree.command(name="ให้เหรียญ", description="ให้เหรียญเข้ากระเป๋าMember")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_add_coins(interaction: discord.Interaction, user: discord.Member, amount: int):
    new_balance = update_coins(user.id, amount)
    await interaction.response.send_message(f"✅ ให้เหรียญ {user.mention} จำนวน **{amount}** 🪙\n💳 ยอดปัจจุบัน: **{new_balance}** 🪙", ephemeral=True)
    await send_audit_log(interaction.guild, "💰 ให้เงิน", f"{interaction.user.mention} ให้เงิน {amount} 🪙 ให้ {user.mention}", discord.Color.green())

@bot.tree.command(name="ลบเหรียญ", description="ลบเหรียญจากกระเป๋าMember")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_remove_coins(interaction: discord.Interaction, user: discord.Member, amount: int):
    new_balance = update_coins(user.id, -amount)
    await interaction.response.send_message(f"✅ ลบเงินจาก {user.mention} จำนวน **{amount}** 🪙\n💳 ยอดปัจจุบัน: **{new_balance}** 🪙", ephemeral=True)
    await send_audit_log(interaction.guild, "🔥 ลบเงิน", f"{interaction.user.mention} ลบเงิน {amount} 🪙 จาก {user.mention}", discord.Color.red())

@bot.tree.command(name="เพิ่มยศลงร้านค้า", description="เพิ่มยศเข้าไปขายในร้านค้า")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_add_role(interaction: discord.Interaction, role: discord.Role, price: int):
    shop_col.update_one({"role_id": role.id}, {"$set": {"price": price}}, upsert=True)
    await update_shop_ui(interaction.guild)
    await interaction.response.send_message(f"✅ เพิ่มยศ {role.mention} ลงร้านค้า ราคา {price} 🪙", ephemeral=True)

@bot.tree.command(name="ลบยศจากร้านค้า", description="ถอดยศออกจากร้านค้า")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_remove_role(interaction: discord.Interaction, role: discord.Role):
    shop_col.delete_one({"role_id": role.id})
    await update_shop_ui(interaction.guild)
    await interaction.response.send_message(f"✅ ลบยศ {role.mention} ออกจากร้านค้าแล้ว", ephemeral=True)

@bot.tree.command(name="เพิ่มยศลงตู้กาชา", description="เอายศใส่ตู้กาชา")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_gacha_role(interaction: discord.Interaction, role: discord.Role, percent: float):
    gacha_col.update_one({"role_id": role.id}, {"$set": {"percent": percent}}, upsert=True)
    await update_gacha_ui(interaction.guild)
    await interaction.response.send_message(f"✅ เพิ่มยศ {role.mention} ลงตู้กาชา เรท {percent}%", ephemeral=True)

@bot.tree.command(name="ลบยศจากตู้กาชา", description="เอายศออกจากตู้กาชา")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_remove_gacha_role(interaction: discord.Interaction, role: discord.Role):
    gacha_col.delete_one({"role_id": role.id})
    await update_gacha_ui(interaction.guild)
    await interaction.response.send_message(f"✅ ลบยศ {role.mention} ออกจากตู้กาชาแล้ว", ephemeral=True)

@bot.tree.command(name="ตั้งราคากาชา", description="ตั้งราคาหมุนกาชา")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_set_gacha_price(interaction: discord.Interaction, price: int):
    set_config("gacha_price", price)
    await update_gacha_ui(interaction.guild)
    await interaction.response.send_message(f"✅ เปลี่ยนราคากาชาเป็น {price} 🪙 แล้ว", ephemeral=True)

if __name__ == "__main__":
    bot.run(TOKEN)
