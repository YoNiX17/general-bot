import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import os
import json
import time
import random
import asyncio
from aiohttp import web 

# --- CONFIGURATION ---
TOKEN = os.getenv("GENERAL_BOT_TOKEN") 
DATA_FILE = "general_data.json"
CONFIG_FILE = "server_config.json"

logging.basicConfig(level=logging.INFO)

# --- GESTION DES DONNÉES ---
class DataManager:
    def __init__(self):
        self.data = self.load_json(DATA_FILE)
        self.config = self.load_json(CONFIG_FILE)

    def load_json(self, filename):
        if not os.path.exists(filename): return {}
        with open(filename, "r") as f: return json.load(f)

    def save_json(self, filename, data):
        with open(filename, "w") as f: json.dump(data, f, indent=4)

    def save_data(self): self.save_json(DATA_FILE, self.data)
    def save_config(self): self.save_json(CONFIG_FILE, self.config)

    def get_user(self, user_id):
        uid = str(user_id)
        if uid not in self.data:
            self.data[uid] = {"xp": 0, "level": 1, "messages": 0, "voice_time": 0, "last_xp": 0}
        # Migration: s'assurer que voice_time existe
        if "voice_time" not in self.data[uid]: self.data[uid]["voice_time"] = 0
        return self.data[uid]

    def add_xp(self, user_id, amount):
        user = self.get_user(user_id)
        user["xp"] += amount
        next_level_xp = 5 * (user["level"] ** 2) + 50 * user["level"] + 100
        if user["xp"] >= next_level_xp:
            user["level"] += 1
            self.save_data()
            return True, user["level"]
        self.save_data()
        return False, user["level"]

    def add_voice_time(self, user_id, seconds):
        user = self.get_user(user_id)
        user["voice_time"] += seconds
        xp_gain = int((seconds / 60) * 10) # 10 XP par minute
        if xp_gain > 0: return self.add_xp(user_id, xp_gain)
        self.save_data()
        return False, user["level"]

    def get_leaderboard(self):
        # On renvoie tout, le tri se fait ici ou coté client, ici top 50 pour l'API
        return sorted(self.data.items(), key=lambda x: x[1].get('xp', 0), reverse=True)[:50]

    def set_stats_channels(self, guild_id, category_id, member_id, online_id, voice_id):
        self.config[str(guild_id)] = {
            "category": category_id,
            "members": member_id,
            "online": online_id,
            "voice": voice_id
        }
        self.save_config()

db = DataManager()

# --- BOT & WEB SERVER ---
class GeneralBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.voice_sessions = {}

    async def setup_hook(self):
        # Lancement du serveur Web (API)
        self.web_app = web.Application()
        self.web_app.router.add_get('/', self.web_home)
        self.web_app.router.add_get('/api/leaderboard', self.web_leaderboard)
        self.web_app.router.add_get('/api/stats', self.web_stats)
        
        runner = web.AppRunner(self.web_app)
        await runner.setup()
        port = int(os.getenv("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"🌐 API Web lancée sur le port {port}")

    async def on_ready(self):
        print(f'🤖 Bot Général connecté : {self.user}')
        try:
            await self.tree.sync()
            print("🔄 Commandes synchronisées.")
        except Exception as e:
            print(f"Erreur synchro : {e}")
        self.update_stats_loop.start()

    # --- ROUTES API WEB ---
    async def web_home(self, request):
        return web.Response(text=f"🤖 {self.user.name} est en ligne ! L'API est prête.")

    async def web_leaderboard(self, request):
        raw_data = db.get_leaderboard()
        json_data = []
        for uid, data in raw_data:
            user = self.get_user(int(uid))
            name = user.display_name if user else "Utilisateur parti"
            avatar = user.display_avatar.url if user else "https://cdn.discordapp.com/embed/avatars/0.png"
            
            json_data.append({
                "id": uid,
                "name": name,
                "avatar": avatar,
                "level": data.get("level", 1),
                "xp": data.get("xp", 0),
                "messages": data.get("messages", 0),
                "voice_time": data.get("voice_time", 0)
            })
        return web.json_response(json_data, headers={"Access-Control-Allow-Origin": "*"})

    async def web_stats(self, request):
        total_members = sum(g.member_count for g in self.guilds)
        total_online = sum(1 for g in self.guilds for m in g.members if m.status != discord.Status.offline)
        total_voice = sum(len(vc.members) for g in self.guilds for vc in g.voice_channels)
        return web.json_response({
            "guilds": len(self.guilds),
            "members": total_members,
            "online": total_online,
            "voice_count": total_voice
        }, headers={"Access-Control-Allow-Origin": "*"})

    # --- EVENTS BOT ---
    async def on_message(self, message):
        if message.author.bot or not message.guild: return
        user = db.get_user(message.author.id)
        if time.time() - user.get("last_xp", 0) > 10:
            user["messages"] += 1
            user["last_xp"] = time.time()
            levelup, lvl = db.add_xp(message.author.id, random.randint(15, 25))
            if levelup:
                embed = discord.Embed(description=f"🆙 **Level Up!** {message.author.mention} passe niveau **{lvl}** 🎉", color=discord.Color.gold())
                await message.channel.send(embed=embed)

    async def on_voice_state_update(self, member, before, after):
        if member.bot: return
        
        # Connexion vocal
        if before.channel is None and after.channel is not None:
            self.voice_sessions[member.id] = time.time()
        
        # Déconnexion vocal
        elif before.channel is not None and after.channel is None:
            if member.id in self.voice_sessions:
                duration = time.time() - self.voice_sessions.pop(member.id)
                levelup, lvl = db.add_voice_time(member.id, duration)
                if levelup:
                    chan = member.guild.system_channel or member.guild.text_channels[0]
                    if chan:
                        await chan.send(f"🎙️ **Vocal Up!** {member.mention} passe niveau **{lvl}** !")
        
        # Mise à jour des stats channels
        await self.update_server_stats(member.guild)

    async def on_member_join(self, member): await self.update_server_stats(member.guild)
    async def on_member_remove(self, member): await self.update_server_stats(member.guild)

    async def update_server_stats(self, guild):
        gid = str(guild.id)
        if gid in db.config:
            cfg = db.config[gid]
            try:
                member_count = guild.member_count
                online_count = sum(1 for m in guild.members if m.status != discord.Status.offline)
                voice_count = sum(len(vc.members) for vc in guild.voice_channels)

                c_members = guild.get_channel(cfg["members"])
                c_online = guild.get_channel(cfg["online"])
                c_voice = guild.get_channel(cfg["voice"])

                if c_members and c_members.name != f"👥 Membres : {member_count}": await c_members.edit(name=f"👥 Membres : {member_count}")
                if c_online and c_online.name != f"🟢 En ligne : {online_count}": await c_online.edit(name=f"🟢 En ligne : {online_count}")
                if c_voice and c_voice.name != f"🔊 En vocal : {voice_count}": await c_voice.edit(name=f"🔊 En vocal : {voice_count}")
            except Exception as e: print(f"Erreur update stats {guild.name}: {e}")

    @tasks.loop(minutes=10)
    async def update_stats_loop(self):
        for guild in self.guilds: await self.update_server_stats(guild)

bot = GeneralBot()

# --- COMMANDES SLASH ---

@bot.tree.command(name="setup_stats", description="[Admin] Crée les salons de statistiques")
@app_commands.checks.has_permissions(administrator=True)
async def setup_stats(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild
    overwrites = {guild.default_role: discord.PermissionOverwrite(connect=False)}
    
    cat = await guild.create_category("📊 STATISTIQUES")
    c1 = await guild.create_voice_channel(f"👥 Membres : {guild.member_count}", category=cat, overwrites=overwrites)
    online = sum(1 for m in guild.members if m.status != discord.Status.offline)
    c2 = await guild.create_voice_channel(f"🟢 En ligne : {online}", category=cat, overwrites=overwrites)
    voice = sum(len(vc.members) for vc in guild.voice_channels)
    c3 = await guild.create_voice_channel(f"🔊 En vocal : {voice}", category=cat, overwrites=overwrites)
    
    db.set_stats_channels(guild.id, cat.id, c1.id, c2.id, c3.id)
    await interaction.followup.send("✅ **Système de stats installé !**")

@bot.tree.command(name="rank", description="Affiche ton niveau et XP")
async def rank(interaction: discord.Interaction, membre: discord.Member = None):
    target = membre or interaction.user
    data = db.get_user(target.id)
    all_users = sorted(db.data.items(), key=lambda x: x[1]['xp'], reverse=True)
    try: rank_pos = [uid for uid, _ in all_users].index(str(target.id)) + 1
    except: rank_pos = "N/A"
    
    embed = discord.Embed(color=target.color)
    embed.set_author(name=f"Progression de {target.display_name}", icon_url=target.display_avatar.url)
    embed.add_field(name="🏆 Rang", value=f"#{rank_pos}", inline=True)
    embed.add_field(name="⭐ Niveau", value=f"{data['level']}", inline=True)
    embed.add_field(name="✨ XP Total", value=f"{data['xp']}", inline=True)
    
    next_xp = 5 * (data["level"] ** 2) + 50 * data["level"] + 100
    percent = min(1.0, data["xp"] / next_xp)
    bars = int(percent * 10)
    progress = "🟩" * bars + "⬛" * (10 - bars)
    
    embed.add_field(name="Prochain niveau", value=f"{progress} {int(percent*100)}%", inline=False)
    
    voice_h = int(data['voice_time'] // 3600)
    voice_m = int((data['voice_time'] % 3600) // 60)
    embed.set_footer(text=f"✉️ Messages: {data['messages']} • 🎙️ Vocal: {voice_h}h {voice_m}m")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Affiche le TOP 10 du serveur")
async def leaderboard(interaction: discord.Interaction):
    top_users = db.get_leaderboard() # Récupère top 50, on en montre 10
    if not top_users: return await interaction.response.send_message("❌ Pas assez de données.", ephemeral=True)
    
    desc = ""
    for i, (uid, data) in enumerate(top_users[:10]):
        user = interaction.guild.get_member(int(uid))
        name = user.display_name if user else "Utilisateur parti"
        medals = ["🥇", "🥈", "🥉"]
        rank_emoji = medals[i] if i < 3 else f"`{i+1}.`"
        desc += f"{rank_emoji} **{name}** • Lvl {data['level']} (*{data['xp']} XP*)\n"
    
    embed = discord.Embed(title="🏆 Classement du Serveur", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="clear", description="Supprime des messages")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, nombre: int):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=nombre)
    await interaction.followup.send(f"🧹 **{len(deleted)}** messages nettoyés.", ephemeral=True)

@bot.tree.command(name="serverinfo", description="Infos du serveur")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    embed = discord.Embed(title=f"Infos {guild.name}", color=discord.Color.blue())
    if guild.icon: embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Membres", value=str(guild.member_count))
    embed.add_field(name="En ligne", value=str(sum(1 for m in guild.members if m.status != discord.Status.offline)))
    embed.add_field(name="Salons", value=str(len(guild.channels)))
    await interaction.response.send_message(embed=embed)

if not TOKEN: print("❌ Variable GENERAL_BOT_TOKEN manquante")
else: bot.run(TOKEN)
