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
        user = {"user_id": user_id, "coins": 5, "xp": 10, "level": 1, "xp_boost_end": 0}
        users_col.insert_one(user)
    return user

def update_coins(user_id, amount):
    user = get_user_data(user_id)
    new_amount = max(0, user.get("coins", 0) + amount)
    users_col.update_one({"user_id": user_id}, {"$set": {"coins": new_amount}})
    return new_amount

async def add_xp(user_id, amount, origin_channel, member):
    user = get_user_data(user_id)
    
    multiplier = 1
    if user.get("xp_boost_end", 0) > time.time():
        multiplier = 2
    
    gained_xp = int(amount * multiplier)
    new_xp = user.get("xp", 0) + gained_xp
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
            
        embed = discord.Embed(title="🎉 Level Up!", description=f"ยินดีด้วย! {member.mention} อัพเป็น **เลเวล {current_level}** แล้ว!\n\n🎁 ได้รับรางวัล: **+{reward_coins}** 🪙", color=discord.Color.gold())
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
        if not has_item: return await interaction.followup.send("❌ คุณไม่มีไอเทมนี้", ephemeral=True)
        item_data = items_col.find_one({"item_id": item_id})
        if not item_data: return await interaction.followup.send("❌ ไอเทมนี้ถูกระบบลบไปแล้ว", ephemeral=True)

        remove_from_inventory(user_id, item_id, 1)
        effect = item_data.get("effect")
        value = item_data.get("value")
        
        if effect == "coins":
            update_coins(user_id, int(value))
            embed = discord.Embed(title="ใช้ไอเทมสำเร็จ", description=f"คุณใช้ **{item_data['name']}**\nได้รับเหรียญ **+{value}** 🪙", color=discord.Color.gold())
            await interaction.followup.send(embed=embed)
        elif effect == "xp":
            embed = discord.Embed(title="ใช้ไอเทมสำเร็จ", description=f"คุณใช้ **{item_data['name']}**\nได้รับ Exp **+{value}** EXP 🌟", color=discord.Color.blue())
            await interaction.followup.send(embed=embed)
            await add_xp(user_id, int(value), interaction.channel, interaction.user)
        elif effect == "role":
            role = interaction.guild.get_role(int(value))
            if role:
                try:
                    await interaction.user.add_roles(role)
                    embed = discord.Embed(title="ใช้ไอเทมสำเร็จ", description=f"มึงใช้ **{item_data['name']}**\nได้รับยศ {role.mention} 🎭", color=discord.Color.purple())
                    await interaction.followup.send(embed=embed)
                except: await interaction.followup.send("❌ บอทให้ยศไม่ได้ สิทธิ์ไม่พอ", ephemeral=True)
            else: await interaction.followup.send("❌ หาไอดียศของไอเทมนี้ไม่เจอ", ephemeral=True)
        elif effect == "xp_boost": 
            duration_min = int(value)
            end_time = time.time() + (duration_min * 60)
            users_col.update_one({"user_id": user_id}, {"$set": {"xp_boost_end": end_time}})
            embed = discord.Embed(title="ใช้ไอเทมสำเร็จ", description=f"คุณใช้ **{item_data['name']}**\n⚡ **XP คูณ 2** เป็นเวลา **{duration_min}** นาที!", color=discord.Color.orange())
            await interaction.followup.send(embed=embed)

class UseItemView(discord.ui.View):
    def __init__(self, inv_items):
        super().__init__(timeout=120)
        self.add_item(UseItemSelect(inv_items))

class ShopConfirmView(discord.ui.View):
    def __init__(self, role_id, price):
        super().__init__(timeout=60)
        self.role_id = role_id
        self.price = price

    @discord.ui.button(label="ยืนยันการซื้อ", style=discord.ButtonStyle.green, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = get_user_data(interaction.user.id)
        if user.get("coins", 0) < self.price:
            return await interaction.response.edit_message(content="❌ เงินมึงไม่พอ ซื้อไม่ได้!", embed=None, view=None)
        
        role = interaction.guild.get_role(self.role_id)
        if not role:
            return await interaction.response.edit_message(content="❌ หาไอดียศไม่เจอ", embed=None, view=None)
        
        try:
            await interaction.user.add_roles(role)
            update_coins(interaction.user.id, -self.price)
            await interaction.response.edit_message(content=f"✅ ซื้อยศ {role.mention} สำเร็จ! หักเงินไป {self.price} 🪙", embed=None, view=None)
            await send_audit_log(interaction.guild, "🛒 ซื้อยศ", f"{interaction.user.mention} ซื้อยศ {role.mention}", discord.Color.green())
        except:
            await interaction.response.edit_message(content="❌ บอทสิทธิ์ไม่พอให้ยศมึง เอาบอทยศไว้บนสุดด้วย", embed=None, view=None)

    @discord.ui.button(label="ยกเลิก", style=discord.ButtonStyle.red, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ ยกเลิกการสั่งซื้อแล้ว", embed=None, view=None)

class ShopBuySelect(discord.ui.Select):
    def __init__(self, items, guild):
        options = []
        for item in items:
            role = guild.get_role(int(item['role_id']))
            r_name = role.name if role else "ไม่พบข้อมูลยศ"
            options.append(discord.SelectOption(label=f"ยศ {r_name}", description=f"ราคา {item['price']} 🪙", value=f"{item['role_id']}_{item['price']}"))
        super().__init__(placeholder="เลือกยศที่มึงต้องการซื้อ...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        role_id_str, price_str = self.values[0].split('_')
        role_id, price = int(role_id_str), int(price_str)
        role = interaction.guild.get_role(role_id)
        
        embed = discord.Embed(title="⚠️ ยืนยันการสั่งซื้อ", description=f"มึงแน่ใจนะว่าจะซื้อยศ <@&{role_id}>\nในราคา **{price} 🪙** ?", color=discord.Color.gold())
        await interaction.response.send_message(embed=embed, view=ShopConfirmView(role_id, price), ephemeral=True)

class ShopInfoSelect(discord.ui.Select):
    def __init__(self, items, guild):
        options = []
        for item in items:
            role = guild.get_role(int(item['role_id']))
            r_name = role.name if role else "ไม่พบข้อมูลยศ"
            options.append(discord.SelectOption(label=f"ยศ {r_name}", value=str(item['role_id'])))
        super().__init__(placeholder="เลือกยศเพื่อดูข้อมูลสิทธิ์...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(int(self.values[0]))
        if not role: return await interaction.response.send_message("❌ ไม่พบข้อมูลยศ", ephemeral=True)
        perms = [perm[0].replace('_', ' ').title() for perm in role.permissions if perm[1]]
        perms_text = ", ".join(perms[:15]) + ("..." if len(perms) > 15 else "") if perms else "ไม่มีสิทธิ์พิเศษอะไรเลย"
        embed = discord.Embed(title=f"ℹ️ ข้อมูลยศ {role.name}", color=role.color)
        embed.add_field(name="สิทธิ์ที่ทำได้ในดิสนี้:", value=f"```\n{perms_text}\n```")
        await interaction.response.send_message(embed=embed, ephemeral=True)

class ShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None) 
    
    @discord.ui.button(label="🛒 ซื้อยศ", style=discord.ButtonStyle.green, custom_id="shop_buy_btn")
    async def btn_buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = list(shop_col.find())
        if not items: return await interaction.response.send_message("❌ ร้านค้ายังว่างเปล่า", ephemeral=True)
        view = discord.ui.View(timeout=60)
        view.add_item(ShopBuySelect(items, interaction.guild))
        await interaction.response.send_message("👇 เลือกลงตะกร้าเลย:", view=view, ephemeral=True)

    @discord.ui.button(label="💳 เช็คเหรียญ", style=discord.ButtonStyle.blurple, custom_id="shop_bal_btn")
    async def btn_bal(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = get_user_data(interaction.user.id)
        await interaction.response.send_message(f"💳 ตอนนี้มีเหรียญอยู่: **{user.get('coins', 0)}** 🪙", ephemeral=True)

    @discord.ui.button(label="ℹ️ ข้อมูลยศ", style=discord.ButtonStyle.gray, custom_id="shop_info_btn")
    async def btn_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = list(shop_col.find())
        if not items: return await interaction.response.send_message("❌ ร้านค้ายังว่างเปล่า", ephemeral=True)
        view = discord.ui.View(timeout=60)
        view.add_item(ShopInfoSelect(items, interaction.guild))
        await interaction.response.send_message("🔍 เลือกยศที่อยากส่องข้อมูล:", view=view, ephemeral=True)
        
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
            return await interaction.followup.send("❌ เหรียญไม่เพียงพอต่อการสุ่ม", ephemeral=True)
        pool = list(gacha_col.find())
        if not pool: return await interaction.followup.send("❌ ไม่มีของในตู้กาชา", ephemeral=True)

        update_coins(interaction.user.id, -price)
        
        item_weights = [float(i["percent"]) for i in pool]
        sum_items = sum(item_weights)
        salt_rate = get_config("salt_rate")
        
        if salt_rate is not None:
            salt_chance = float(salt_rate)
            mult = 0.0 if salt_chance >= 100 else (100.0 - salt_chance) / sum_items if sum_items > 0 else 0
        else:
            salt_chance = max(0.0, 100.0 - sum_items)
            mult = 1.0 if sum_items <= 100.0 else (100.0 / sum_items)
            
        final_weights = [w * mult for w in item_weights]
        choices = [i["role_id"] for i in pool] + ["เกลือ"]
        final_weights.append(salt_chance if sum(final_weights) + salt_chance > 0 else 100.0)

        won_id = random.choices(choices, weights=final_weights, k=1)[0]
        if won_id == "เกลือ": 
            await send_audit_log(interaction.guild, "🎲 เกลือ", f"{interaction.user.mention} หมุนได้เกลือ", discord.Color.light_grey())
            return await interaction.followup.send(embed=discord.Embed(title="🧂 เกลือ", description="ไม่ได้ของรางวัล", color=discord.Color.light_grey()), ephemeral=True)
        role = interaction.guild.get_role(won_id)
        try:
            await interaction.user.add_roles(role)
            await interaction.followup.send(embed=discord.Embed(title="🎉 ยินดีด้วย!", description=f"ได้รับยศ {role.mention}", color=discord.Color.gold()), ephemeral=True)
            await send_audit_log(interaction.guild, "🎲 กาชาแตก", f"{interaction.user.mention} ได้ยศ {role.mention}", discord.Color.gold())
        except: await interaction.followup.send("❌ บอทให้ยศไม่ได้ สิทธิ์ไม่พอ", ephemeral=True)

async def update_gacha_ui(guild):
    try:
        msg = await guild.get_channel(int(get_config("gacha_channel"))).fetch_message(int(get_config("gacha_msg")))
        price = get_config('gacha_price', 10)
        desc = f"กดปุ่มเพื่อหมุนกาชา\n🏷️ ราคาหมุนต่อรอบ: **{price}** 🪙\n\n**🎁 ของรางวัล :**\n"
        
        pool = list(gacha_col.find())
        pool.sort(key=lambda x: float(x.get('percent', 0)))
        
        if not pool: desc += "- ตู้ว่างเปล่า"
        else:
            item_weights = [float(i["percent"]) for i in pool]
            sum_items = sum(item_weights)
            salt_rate = get_config("salt_rate")
            
            if salt_rate is not None:
                salt_chance = float(salt_rate)
                mult = 0.0 if salt_chance >= 100 else (100.0 - salt_chance) / sum_items if sum_items > 0 else 0
            else:
                salt_chance = max(0.0, 100.0 - sum_items)
                mult = 1.0 if sum_items <= 100.0 else (100.0 / sum_items)
                if sum_items > 100.0: salt_chance = 0.0
                
            for item in pool:
                real_percent = float(item['percent']) * mult
                desc += f"🔸 <@&{item['role_id']}> | โอกาสออก: **{real_percent:.2f}%**\n"
            desc += f"\n🧂 **เกลือ** | โอกาสออก: **{salt_chance:.2f}%**"
            
        await msg.edit(embed=discord.Embed(title="🎲 ตู้กาชายศ", description=desc, color=discord.Color.purple()), view=GachaView())
    except: pass
        
async def update_gacha_ui(guild):
    try:
        msg = await guild.get_channel(int(get_config("gacha_channel"))).fetch_message(int(get_config("gacha_msg")))
        
        price = get_config('gacha_price', 10)
        desc = f"กดปุ่มเพื่อหมุนกาชา\n🏷️ ราคาหมุนต่อรอบ: **{price}** 🪙\n\n**🎁 ของรางวัล :**\n"
        
        pool = list(gacha_col.find())
        pool.sort(key=lambda x: float(x.get('percent', 0)))
        
        if not pool: 
            desc += "- ตู้ว่างเปล่า"
        else:
            for item in pool:
                desc += f"🔸 <@&{item['role_id']}> | โอกาสออก: **{item['percent']}%**\n"
                
            salt_rate_config = get_config("salt_rate")
            if salt_rate_config is not None:
                salt_chance = float(salt_rate_config)
            else:
                salt_chance = max(0.0, 100.0 - sum(float(i['percent']) for i in pool))
                
            desc += f"\n🧂 **เกลือ** | โอกาสออก: **{salt_chance:.2f}%**"
            
        embed = discord.Embed(title="🎲 ตู้กาชายศ", description=desc, color=discord.Color.purple())
        await msg.edit(embed=embed, view=GachaView())
    except: pass

class EconomyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        
    async def setup_hook(self):
        self.loop.create_task(web_server())
        self.add_view(ShopView())
        self.add_view(GachaView())
        
        my_server = discord.Object(id=1141311336601628712) 
        
        self.tree.clear_commands(guild=my_server)
        await self.tree.sync(guild=my_server)
        await self.tree.sync()
        
        self.update_leaderboard.start()
        self.auto_update_ui.start() 

    @tasks.loop(minutes=5)
    async def update_leaderboard(self):
        try:
            msg = await self.get_channel(int(get_config("lb_channel"))).fetch_message(int(get_config("lb_msg")))
            users = list(users_col.find().sort([("level", -1), ("xp", -1)]).limit(10))
            embed = discord.Embed(title="🏆 ตารางอันดับ", description="ตารางอัปเดตทุกๆ 5 นาที", color=discord.Color.blue())
            
            rich_role_id = get_config("rich_role")
            if rich_role_id and users:
                top_user_id = users[0]["user_id"]
                guild = msg.guild
                rich_role = guild.get_role(int(rich_role_id))
                
                if rich_role:
                    for member in rich_role.members:
                        if member.id != top_user_id:
                            await member.remove_roles(rich_role)
                    
                    top_member = guild.get_member(top_user_id)
                    if top_member and rich_role not in top_member.roles:
                        await top_member.add_roles(rich_role)

            if not users: 
                embed.add_field(name="ยังไม่มีข้อมูล", value="ไม่มีใครติดอันดับ")
            else:
                for idx, u in enumerate(users, 1): 
                    embed.add_field(name=f"อันดับ {idx}", value=f"<@{u['user_id']}>\n🌟 เลเวล **{u.get('level', 1)}** | 🪙 **{u.get('coins', 0)}** เหรียญ", inline=False)
            await msg.edit(embed=embed)
        except: pass

    @tasks.loop(minutes=1)
    async def auto_update_ui(self):
        for guild in self.guilds:
            await update_shop_ui(guild)
            await update_gacha_ui(guild)

    @auto_update_ui.before_loop
    async def before_auto_update(self):
        await self.wait_until_ready()

    async def on_ready(self):
        print(f"Logged in as {self.user}!")
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name="/ช่วยเหลือ เพื่อดูคำสั่ง"))
        for guild in self.guilds:
            await update_shop_ui(guild)
            await update_gacha_ui(guild)

bot = EconomyBot()

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild: return
    user_temp = get_temp(message.author.id)
    now = time.time()
    
    if now - user_temp["last_chat"] > 60:
        user_temp["last_chat"] = now
        gained_xp = random.randint(1, 3)
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
                gained_xp = sum(random.randint(1, 3) for _ in range(stayed_minutes))
                try: await member.send(embed=discord.Embed(title="⭐ แจ้งเตือนจากบอท", description=f"คุยไป **{stayed_minutes} นาที**\nได้รับ EXP **+{gained_xp}** 🌟", color=discord.Color.purple()))
                except: pass
                await add_xp(member.id, gained_xp, None, member)
            user_temp["voice_join"] = None

@bot.tree.command(name="ช่วยเหลือ", description="ดูคำสั่งทั้งหมดของบอท")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 คู่มือการใช้คำสั่งบอท", description="รวมคำสั่งที่สามารถใช้ได้", color=discord.Color.blue())
    
    embed.add_field(name="🧑‍🤝‍🧑 คำสั่งทั่วไป", value=(
        "💳 `/กระเป๋า` - เช็คตังค์ เลเวล และดูไอเทมในกระเป๋าตัวเอง\n"
        "💸 `/โอนเงิน [คนรับ] [จำนวน]` - โอนเงินไปให้เพื่อน (มีภาษี 5%)\n"
        "🎁 `/โอนของ [คนรับ] [ไอดีไอเทม] [จำนวน]` - ส่งไอเทมให้เพื่อน\n"
        "🏓 `/เช็คค่าปิง` - ดูความเร็วการตอบสนองของบอท\n"
        "📖 `/ช่วยเหลือ` - เปิดหน้านี้แหละ"
    ), inline=False)
    
    if interaction.user.guild_permissions.administrator:
        embed.add_field(name="👑 คำสั่งแอดมิน (Admin Only)", value=(
            "⚙️ `/ตั้งค่าระบบ` - สร้างห้องบอร์ดอันดับ ร้านค้า กาชา แจ้งเลเวล\n"
            "🏆 `/ตั้งค่ายศอันดับ1 [ยศ]` - ตั้งยศให้เศรษฐีอันดับ 1 อัตโนมัติ\n"
            "💰 `/ให้เหรียญ` | 🔥 `/ลบเหรียญ`\n"
            "📦 `/สร้างไอเทม` - สร้างไอเทม (มีเอฟเฟกต์ xp_boost)\n"
            "🎁 `/ให้ไอเทม` - เสกของให้คนอื่น\n"
            "📋 `/ไอเทมทั้งหมด` - ดูไอเทมทั้งหมดที่สร้างไว้\n"
            "🛒 `/เพิ่มยศลงร้านค้า` | ❌ `/ลบยศจากร้านค้า`\n"
            "🎲 `/เพิ่มยศลงตู้กาชา` | 🗑️ `/ลบยศจากตู้กาชา`\n"
            "🏷️ `/ตั้งราคากาชา [ราคา]` - ปรับราคาค่าหมุน\n"
            "🧂 `/ตั้งค่าเรทเกลือ [เปอร์เซ็นต์]` - ตั้งเรทเกลือ (ใส่ -1 ยกเลิก)\n"
            "🗑️ `/ลบยศทั้งหมด` - ล้างข้อมูลตู้กาชาหรือร้านค้า"
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

@bot.tree.command(name="กระเป๋า", description="เช็คกระเป๋า")
async def cmd_wallet(interaction: discord.Interaction):
    user = get_user_data(interaction.user.id)
    coins = user.get("coins", 0)
    level = user.get("level", 1)
    xp = user.get("xp", 0)
    next_xp = level * 250
    
    embed = discord.Embed(title=f"🎒 กระเป๋าของ {interaction.user.display_name}", color=discord.Color.gold())
    embed.add_field(name="🌟 เลเวล", value=f"**{level}** ({xp}/{next_xp} EXP)", inline=True)
    embed.add_field(name="💳 ยอดเงิน", value=f"**{coins}** 🪙", inline=True)
    
    if user.get("xp_boost_end", 0) > time.time():
        remaining = int((user["xp_boost_end"] - time.time()) / 60)
        embed.add_field(name="⚡ สถานะบัฟ", value=f"XP คูณ 2 (เหลือ {remaining} นาที)", inline=False)
        
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    
    inv_items = get_inventory(interaction.user.id)
    if inv_items:
        inv_text = ""
        for inv in inv_items:
            item_data = items_col.find_one({"item_id": inv["item_id"]})
            if item_data: inv_text += f"🔸 **{item_data['name']}** (x{inv['amount']}) - ID: `{inv['item_id']}`\n"
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
        
    tax = int(amount * 0.05)
    amount_received = amount - tax
    
    update_coins(sender_id, -amount)
    update_coins(receiver.id, amount_received)
    
    embed = discord.Embed(title="✅ โอนเงินสำเร็จ!", description=f"โอนเงินให้ {receiver.mention} จำนวน **{amount}** 🪙\n(หักภาษี 5% = {tax} 🪙 | ผู้รับได้**{amount_received}** 🪙)", color=discord.Color.green())
    await interaction.response.send_message(embed=embed) 
    await send_audit_log(interaction.guild, "💸 โอนเงิน", f"{interaction.user.mention} โอนให้ {receiver.mention} {amount} 🪙 (ภาษี {tax})", discord.Color.blue())

@bot.tree.command(name="โอนของ", description="ส่งไอเทมให้เพื่อน")
@app_commands.describe(receiver="คนที่จะโอนให้", item_id="ไอดีไอเทม (ดูในกระเป๋า)", amount="จำนวน")
async def cmd_transfer_item(interaction: discord.Interaction, receiver: discord.Member, item_id: str, amount: int = 1):
    if amount <= 0: return await interaction.response.send_message("❌ จำนวนต้องมากกว่า 0", ephemeral=True)
    if receiver.id == interaction.user.id or receiver.bot: return await interaction.response.send_message("❌ ส่งให้ตัวเองไม่ได้", ephemeral=True)
    
    user_id = interaction.user.id
    has_item = inv_col.find_one({"user_id": user_id, "item_id": item_id, "amount": {"$gte": amount}})
    
    if not has_item: return await interaction.response.send_message("❌ มึงไม่มีไอเทมนี้ หรือมีไม่พอ!", ephemeral=True)
    
    item_data = items_col.find_one({"item_id": item_id})
    if not item_data: return await interaction.response.send_message("❌ ไอเทมนี้ไม่มีอยู่จริง", ephemeral=True)

    remove_from_inventory(user_id, item_id, amount)
    add_to_inventory(receiver.id, item_id, amount)
    
    await interaction.response.send_message(f"✅ ส่ง **{item_data['name']}** x{amount} ให้ {receiver.mention} เรียบร้อย!")
    await send_audit_log(interaction.guild, "🎁 โอนของ", f"{interaction.user.mention} ส่ง {item_data['name']} x{amount} ให้ {receiver.mention}", discord.Color.orange())

@bot.tree.command(name="ตั้งค่าระบบ", description="ตั้งค่าระบบต่างๆ")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_setup(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild
    category = await guild.create_category("╭・💎・𝗘𝗖𝗢𝗡𝗢𝗠𝗬 𝗦𝗬𝗦𝗧𝗘𝗠・╮")
    
    guide_ch = await guild.create_text_channel("📖︱คู่มือระบบเกม", category=category)
    embed_guide = discord.Embed(title="📖 ระบบต่างๆ", description="อ่านให้จบไม่อ่านขอให้ไม่มีแฟน", color=discord.Color.dark_theme())
    embed_guide.add_field(name="🎁 กล่องผู้เล่นใหม่", value="ผู้เล่นใหม่รับทันที ** 5 เหรียญ** และ **10 EXP**", inline=False)
    embed_guide.add_field(name="🌟 ระบบเลเวล", value="• **พิมพ์แชท:** สุ่มรับ 1-3 EXP\n• **สิงห้องเสียง:** รับ 1-3 EXP ต่อนาที\n", inline=False)
    embed_guide.add_field(name="🪙 ระบบเงิน", value="จะได้เหรียญก็ต่อเมื่อ **เลเวลอัพ** เท่านั้น!", inline=False)
    embed_guide.add_field(name="🎒 การเช็คกระเป๋า & ไอเทม", value="พิมพ์ `/กระเป๋า` ดูของและกดใช้ไอเทมได้เลย\n", inline=False)
    embed_guide.add_field(name="🤝 การโอนของ & เงิน", value="`/โอนเงิน` โดนหักภาษี 5% และ `/โอนของ` ส่งไอเทมให้เพื่อนได้", inline=False)
    embed_guide.set_footer(text="PDR COMMUNITY")
    await guide_ch.send(embed=embed_guide)

    audit_ch = await guild.create_text_channel("🕵️︱audit-log", category=category, overwrites={guild.default_role: discord.PermissionOverwrite(read_messages=False), guild.me: discord.PermissionOverwrite(read_messages=True)})
    set_config("audit_channel", audit_ch.id)

    lvl_ch = await guild.create_text_channel("⭐︱แจ้งเตือนเลเวลอัพ", category=category)
    set_config("lvl_channel", lvl_ch.id)
    await lvl_ch.send(embed=discord.Embed(title="⭐ กระดานข่าวสารเลเวลอัพ", description="ใครเลเวลอัพ บอทจะมาประกาศและแจกเหรียญรางวัลให้ที่นี่", color=discord.Color.gold()))
    
    lb_ch = await guild.create_text_channel("🏆︱บอร์ดอันดับ", category=category)
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

@bot.tree.command(name="ตั้งค่ายศอันดับ1", description="ตั้งยศอันดับ1")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_set_rich_role(interaction: discord.Interaction, role: discord.Role):
    set_config("rich_role", role.id)
    await interaction.response.send_message(f"✅ ตั้งค่าให้ยศ {role.mention} เป็นยศสำหรับอันดับ 1 (ระบบจะเช็คทุก 5 นาที)", ephemeral=True)

@bot.tree.command(name="สร้างไอเทม", description="สร้างไอเทมใหม่ลงระบบ")
@app_commands.describe(item_id="ไอดีไอเทม (อังกฤษล้วน)", name="ชื่อไอเทม", effect="ความสามารถ", value="ค่าพลัง")
@app_commands.choices(effect=[
    app_commands.Choice(name="💰 เพิ่มเหรียญ (ใส่จำนวนเงิน)", value="coins"),
    app_commands.Choice(name="🌟 เพิ่ม EXP (ใส่จำนวน EXP)", value="xp"),
    app_commands.Choice(name="⚡ บัฟ XP คูณ 2 (ใส่เวลาเป็นนาที)", value="xp_boost"),
    app_commands.Choice(name="🎭 ใส่ ID ยศ", value="role")
])
@app_commands.checks.has_permissions(administrator=True)
async def cmd_add_item(interaction: discord.Interaction, item_id: str, name: str, effect: app_commands.Choice[str], value: str):
    if not value.isdigit(): return await interaction.response.send_message("❌ ช่อง value ต้องเป็นตัวเลขเท่านั้น", ephemeral=True)
    items_col.update_one({"item_id": item_id}, {"$set": {"name": name, "effect": effect.value, "value": value}}, upsert=True)
    await interaction.response.send_message(f"✅ สร้างไอเทม **{name}** (ID: `{item_id}`) สำเร็จ!\nความสามารถ: {effect.name} ({value})", ephemeral=True)

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
    await interaction.response.send_message(f"✅ เพิ่มยศ {role.mention} ลงร้านค้า ราคา {price} 🪙", ephemeral=True)

@bot.tree.command(name="ลบยศจากร้านค้า", description="ถอดยศออกจากร้านค้า")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_remove_role(interaction: discord.Interaction, role: discord.Role):
    shop_col.delete_one({"role_id": role.id})
    await interaction.response.send_message(f"✅ ลบยศ {role.mention} ออกจากร้านค้าแล้ว", ephemeral=True)

@bot.tree.command(name="เพิ่มยศลงตู้กาชา", description="เอายศใส่ตู้กาชา")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_gacha_role(interaction: discord.Interaction, role: discord.Role, percent: float):
    gacha_col.update_one({"role_id": role.id}, {"$set": {"percent": percent}}, upsert=True)
    await interaction.response.send_message(f"✅ เพิ่มยศ {role.mention} ลงตู้กาชา เรท {percent}%", ephemeral=True)

@bot.tree.command(name="ลบยศจากตู้กาชา", description="เอายศออกจากตู้กาชา")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_remove_gacha_role(interaction: discord.Interaction, role: discord.Role):
    gacha_col.delete_one({"role_id": role.id})
    await interaction.response.send_message(f"✅ ลบยศ {role.mention} ออกจากตู้กาชาแล้ว", ephemeral=True)

@bot.tree.command(name="ตั้งราคากาชา", description="ตั้งราคาหมุนกาชา")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_set_gacha_price(interaction: discord.Interaction, price: int):
    set_config("gacha_price", price)
    await interaction.response.send_message(f"✅ เปลี่ยนราคากาชาเป็น {price} 🪙 แล้ว", ephemeral=True)

@bot.tree.command(name="ตั้งค่าเรทเกลือ", description="ตั้งเปอร์เซ็นต์ออกเกลือ (ใส่ -1 ยกเลิก)")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_set_salt_rate(interaction: discord.Interaction, percent: float):
    if percent < 0:
        config_col.delete_one({"key": "salt_rate"})
        await interaction.response.send_message("✅ ยกเลิกการล็อคเรทเกลือแล้ว กลับไปใช้ระบบหักลบอัตโนมัติ", ephemeral=True)
    else:
        set_config("salt_rate", str(percent))
        await interaction.response.send_message(f"✅ ล็อคเรทเกลือเป็น **{percent}%** แล้ว", ephemeral=True)

@bot.tree.command(name="ลบยศทั้งหมด", description="ล้างบางยศทั้งหมดออกจากร้านค้าหรือตู้กาชา")
@app_commands.describe(system_type="เลือกระบบที่ต้องการล้างบาง")
@app_commands.choices(system_type=[
    app_commands.Choice(name="🛒 ร้านค้ายศ", value="shop"),
    app_commands.Choice(name="🎲 กาชายศ", value="gacha")
])
@app_commands.checks.has_permissions(administrator=True)
async def cmd_clear_all_roles(interaction: discord.Interaction, system_type: app_commands.Choice[str]):
    if system_type.value == "shop":
        shop_col.delete_many({})
        await interaction.response.send_message("✅ ทำการลบยศทั้งหมดออกจาก **ร้านค้ายศ** เรียบร้อย", ephemeral=True)
    elif system_type.value == "gacha":
        gacha_col.delete_many({})
        await interaction.response.send_message("✅ ทำการลบยศทั้งหมดออกจาก **ตู้กาชา** เรียบร้อย", ephemeral=True)

@bot.tree.command(name="ไอเทมทั้งหมด", description="ดูรายการไอเทมทั้งหมด")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_all_items(interaction: discord.Interaction):
    items = list(items_col.find())
    embed = discord.Embed(title="📦 รายการไอเทมทั้งหมดในระบบ", color=discord.Color.blue())
    
    if not items:
        embed.description = "❌ ยังไม่มีการสร้างไอเทมในระบบ"
    else:
        desc = ""
        effect_map = {
            "coins": "💰 เพิ่มเหรียญ",
            "xp": "🌟 เพิ่ม EXP",
            "xp_boost": "⚡ บัฟ XP x2",
            "role": "🎭 ให้ยศ"
        }
        for item in items:
            eff_name = effect_map.get(item.get('effect'), item.get('effect'))
            desc += f"🔸 **{item.get('name')}** (ID: `{item.get('item_id')}`)\n   ╰ ความสามารถ: {eff_name} | พลัง: {item.get('value')}\n\n"
        
        if len(desc) > 4000: 
            desc = desc[:4000] + "\n... (ข้อความยาวเกินไป)"
            
        embed.description = desc
        
    await interaction.response.send_message(embed=embed, ephemeral=True)

class EditRateModal(discord.ui.Modal, title='แก้ไขเปอร์เซ็นต์การออก'):
    new_rate = discord.ui.TextInput(label='เปอร์เซ็นต์ใหม่ (เช่น 1.5, 10)', placeholder='ใส่แค่ตัวเลข...', required=True)
    def __init__(self, role_id):
        super().__init__()
        self.role_id = role_id
    async def on_submit(self, interaction: discord.Interaction):
        try:
            rate = float(self.new_rate.value)
            gacha_col.update_one({"role_id": self.role_id}, {"$set": {"percent": rate}})
            await update_gacha_ui(interaction.guild)
            role = interaction.guild.get_role(self.role_id)
            await interaction.response.send_message(f"✅ อัปเดตเรทยศ **{role.name if role else 'ไม่ทราบชื่อ'}** เป็น **{rate}%** เรียบร้อย!", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ มึงใส่ตัวเลขไม่ถูกต้อง!", ephemeral=True)

class EditRateSelect(discord.ui.Select):
    def __init__(self, items, guild):
        options = []
        for item in items:
            role = guild.get_role(int(item['role_id']))
            r_name = role.name if role else "ไม่พบข้อมูลยศ"
            options.append(discord.SelectOption(label=f"ยศ {r_name}", description=f"เรทปัจจุบัน: {item['percent']}%", value=str(item['role_id'])))
        super().__init__(placeholder="เลือกยศที่ต้องการเปลี่ยนเรท...", min_values=1, max_values=1, options=options)
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditRateModal(int(self.values[0])))

class EditRateView(discord.ui.View):
    def __init__(self, items, guild):
        super().__init__(timeout=60)
        self.add_item(EditRateSelect(items, guild))

@bot.tree.command(name="เปลี่ยนเรทการออก", description="แก้ไขเรทการออกของยศในตู้กาชา")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_edit_rate(interaction: discord.Interaction):
    items = list(gacha_col.find())
    if not items: return await interaction.response.send_message("❌ ตู้กาชายังว่างเปล่า", ephemeral=True)
    await interaction.response.send_message("👇 เลือกยศที่ต้องการเปลี่ยนเรท:", view=EditRateView(items, interaction.guild), ephemeral=True)
            
if __name__ == "__main__":
    bot.run(TOKEN)
